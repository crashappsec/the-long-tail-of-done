#!/usr/bin/env python3
"""Synthetic builds with a front nobody told the tool about.

Nothing in this tool is trusted until it recovers a front it was not given. So
the fixtures here are built backwards: pick where each cell should land in
objective space, then emit builds whose aggregates put it there, and write the
planted truth to a manifest the tests assert against.

Geometry: cells sit on the **DTLZ2 concave-sphere octant**, where the true
front is known in closed form -- `sum f_i^2 = 1`, `f_i >= 0`. A dominated cell
is generated as `r * f` for `r > 1` along the *same* direction as a front cell,
which makes it componentwise worse and therefore genuinely dominated. Picking a
random point at radius > 1 would not: on a sphere octant two points at
different radii need not be comparable at all.

Objective units are increasing functions of the DTLZ2 coordinates, and
dominance is invariant under any monotone per-objective transform, so the
planted front in `f` space is the planted front in dollars and days.

    f1 -> cost_usd per build      150 + 900 * f1
    f2 -> lead time in days       1.5 +  22 * f2
    f3 -> defect density per KLOC 0.15 +   5 * f3

Fixtures:

    ground-truth  5 groups x 6 windows on 3 sphere directions: one improving on
                  all three objectives, one worsening, one trading debt for lead
                  time, one trading further out, one parked. The front is known
                  per window and within each group.
    degeneracy    objectives drawn independently -> the null model should say
                  NOT INFORMATIVE.
    overlap       for running with debt_composite: liability and sunk shares
                  that actually move, so stage 2's tau confirms stage 0.
    censoring     a rising unresolved share against a flat impact mix.
    threshold     a cell whose mix flips between 0.5u and 2u.

Usage:

    python3 make-sample.py                       # ground-truth -> builds.csv
    python3 make-sample.py --fixture censoring --out /tmp/censoring.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260910
# The sample's builds all land at least L days before this, so a reader running
# the tool on the committed fixture gets resolved builds rather than a window
# that is entirely `unresolved` -- which is the correct but unhelpful answer for
# a month that ended yesterday (../SCHEMA.md section 8).
AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)
WINDOWS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04"]

COST_BASE, COST_SPAN = 150.0, 900.0
LEAD_BASE, LEAD_SPAN = 1.5, 22.0
DEFECT_BASE, DEFECT_SPAN = 0.15, 5.0

# Usage levels: two clusters with an empty band between them, and the fixture
# pins `u` inside that band rather than letting it self-calibrate.
#
# The default `u` is the median usage over served builds, which lands wherever
# the larger cluster is -- and if it lands in the gap instead, then
# 2u ~ low_max + high_min sits a hair above the far side of the gap, so whether
# the sensitivity strip fires depends on how many builds sit in that hair. That
# is unavoidable geometry for a bimodal usage distribution, not a tuning
# problem, so a fixture with an exactly-known impact mix has to declare its
# threshold. PINNED_U is in the manifest, tests pass `--u`, and the
# self-calibrating path is what the `threshold` fixture exercises.
USAGE_LOW_RANGE = (5.0, 40.0)
USAGE_HIGH_RANGE = (5_000.0, 200_000.0)
PINNED_U = 1000.0

HEADER = [
    "build_id",
    "group",
    "first_commit_ts",
    "ts",
    "deployed_at",
    "served_at",
    "usage",
    "loc_added",
    "loc_removed",
    "bugs",
    "cost_usd",
    "question",
    "carried_into",
]


def dtlz2(a: float, b: float) -> tuple[float, float, float]:
    """A point on the unit-sphere octant. a, b in [0, pi/2]."""
    return (
        math.cos(a) * math.cos(b),
        math.cos(a) * math.sin(b),
        math.sin(a),
    )


def to_units(f: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        COST_BASE + COST_SPAN * f[0],
        LEAD_BASE + LEAD_SPAN * f[1],
        DEFECT_BASE + DEFECT_SPAN * f[2],
    )


@dataclass
class CellSpec:
    group: str
    window: str
    f: tuple[float, float, float]
    radius: float
    n_builds: int
    mix: dict[str, float]
    on_planted_front: bool = False
    question_mix: dict[str, float] = field(default_factory=dict)

    @property
    def scaled(self) -> tuple[float, float, float]:
        return tuple(self.radius * v for v in self.f)  # type: ignore[return-value]

    @property
    def units(self) -> tuple[float, float, float]:
        return to_units(self.scaled)


def window_dates(window: str) -> tuple[datetime, datetime]:
    year, month = (int(x) for x in window.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
    return start, end - timedelta(days=1)


def allocate(n: int, mix: dict[str, float]) -> list[str]:
    """Largest-remainder allocation, so the emitted shares are the asked-for ones."""
    exact = {k: v * n for k, v in mix.items()}
    counts = {k: int(math.floor(v)) for k, v in exact.items()}
    short = n - sum(counts.values())
    order = sorted(mix, key=lambda k: -(exact[k] - counts[k]))
    for i in range(short):
        counts[order[i % len(order)]] += 1
    out = []
    for klass, count in counts.items():
        out.extend([klass] * count)
    return out


COST_NOISE = 0.04
LEAD_SPREAD = 0.5
STAMP = "minutes"


def emit_cell(
    spec: CellSpec, rng: random.Random, as_of: datetime, l_days: int = 30
) -> list[dict]:
    """Builds whose aggregates land on the spec's objective coordinates.

    Three different kinds of aggregate need three different tricks:

    * **cost** is a mean over every build, so symmetric noise leaves it in
      place. The noise is small (4%) because the fixture's radius steps have to
      stay distinguishable from it -- otherwise the recovered front is testing
      the sampler.
    * **lead time** is a median over the builds that *have* one, which after
      ../SCHEMA.md section 3 is neither all of them nor a fixed share: a build
      nobody used has no first-commit-to-first-user interval. So the ladder is
      laid over exactly that subset, spread uniformly from 0.5x to 1.5x the
      target, whose median is the target by construction.
    * **defect density** is a ratio over the whole cell, so total bugs are set
      from total KLOC once, at the end.

    Timestamps carry minutes, not just dates. Truncating to whole days
    quantises every lead time to an integer and the median lands wherever the
    rounding put it.
    """
    cost, lead, defect = spec.units
    start, end = window_dates(spec.window)
    classes = allocate(spec.n_builds, spec.mix)
    rng.shuffle(classes)
    questions = allocate(
        spec.n_builds,
        spec.question_mix
        or {"features": 0.5, "correctness": 0.25, "performance": 0.15, "economics": 0.10},
    )
    rng.shuffle(questions)

    # The ladder covers only the builds that will carry a lead time.
    timed = [i for i, klass in enumerate(classes) if klass in ("impactful", "low_impact")]
    lead_of: dict[int, float] = {}
    for rank, i in enumerate(timed):
        fraction = 0.5 if len(timed) == 1 else rank / (len(timed) - 1)
        lead_of[i] = max(0.25, lead * (1 - LEAD_SPREAD + 2 * LEAD_SPREAD * fraction))

    rows = []
    loc_totals = []
    span = (end - start).days or 1
    for i, klass in enumerate(classes):
        loc_added = max(20, int(rng.gauss(240, 70)))
        loc_removed = max(0, int(rng.gauss(60, 30)))
        loc_totals.append(loc_added + loc_removed)
        lead_days = lead_of.get(i, lead)
        produced = start + timedelta(
            days=min(span, i * span / max(1, spec.n_builds)),
            minutes=rng.randrange(0, 1440),
        )
        if klass == "unresolved":
            # Younger than L at the observation edge, by construction -- and
            # inside its own window, which is the constraint that makes the
            # censoring fixture coherent. Unresolvedness is a function of ts
            # against the observation edge, so a build can only be unresolved in
            # a window that overlaps the last L days. Asking for an unresolved
            # build in a window four months old is asking for a contradiction,
            # and the fixture would silently move it instead.
            lo = max(start, as_of - timedelta(days=l_days - 0.5))
            hi = min(end + timedelta(days=1), as_of) - timedelta(hours=1)
            if hi <= lo:
                raise ValueError(
                    f"{spec.group}/{spec.window}: an unresolved build needs the "
                    f"window to overlap the last {l_days} days before {as_of.date()}"
                )
            produced = lo + timedelta(
                seconds=rng.uniform(0, (hi - lo).total_seconds())
            )
        first_commit = produced - timedelta(days=lead_days * 0.30)
        deployed = produced + timedelta(days=lead_days * 0.35)
        served = first_commit + timedelta(days=lead_days)
        row = {
            "build_id": f"{spec.group}-{spec.window}-{i:02d}",
            "group": spec.group,
            "first_commit_ts": first_commit.isoformat(timespec=STAMP),
            "ts": produced.isoformat(timespec=STAMP),
            "deployed_at": "",
            "served_at": "",
            "usage": "",
            "loc_added": loc_added,
            "loc_removed": loc_removed,
            "bugs": 0,
            "cost_usd": round(max(20.0, rng.gauss(cost, cost * COST_NOISE)), 2),
            "question": questions[i],
            "carried_into": "",
        }
        if klass in ("impactful", "low_impact", "liability"):
            row["deployed_at"] = deployed.isoformat(timespec=STAMP)
        if klass == "impactful":
            row["served_at"] = served.isoformat(timespec=STAMP)
            row["usage"] = round(rng.uniform(*USAGE_HIGH_RANGE), 1)
        elif klass == "low_impact":
            row["served_at"] = served.isoformat(timespec=STAMP)
            row["usage"] = round(rng.uniform(*USAGE_LOW_RANGE), 1)
        elif klass == "liability":
            # Deployed, and used by nobody. `usage` 0 is a measurement, not a
            # missing value, so this build has no lead time at all -- there was
            # no first user to measure to.
            row["usage"] = 0
        elif klass == "carried":
            row["carried_into"] = f"{spec.group}-carry-{i:02d}"
        rows.append(row)

    kloc = sum(loc_totals) / 1000.0
    total_bugs = max(0, int(round(defect * kloc)))
    for _ in range(total_bugs):
        rows[rng.randrange(len(rows))]["bugs"] += 1
    return rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Three sphere directions, each shared by two groups where a dominance claim is
# wanted. Two points on the *same* direction are always comparable -- the
# smaller radius wins componentwise -- and two points on different directions
# at radius 1.0 are always mutually non-dominated. Every planted claim below is
# one of those two facts, which is what makes them exact rather than likely.
DIR_A = (0.35, 0.55)
DIR_B = (0.95, 0.35)
DIR_C = (0.62, 1.15)

# Five groups: m + 1 = 4 cells are needed before a 3-objective front may be
# drawn at all (../references/front.md, what the tool refuses), so a fixture
# with three groups would exercise nothing but the refusal.
#
# `rotates` matters for the dominance claims. Two cells are only comparable when
# they sit on the same direction, so `contractors` rotates in lockstep with
# `review-first` at a larger radius -- that is what makes "review-first
# dominates contractors in every window" an exact statement rather than a
# probable one.
TRAJECTORIES = {
    # group:          direction, r_first, r_last, builds, rotates
    "agent-heavy":   (DIR_A, 1.90, 1.00, 18, False),  # improving on all three
    "legacy-owner":  (DIR_A, 1.10, 1.90, 12, False),  # worsening on all three
    "review-first":  (DIR_B, 1.00, 1.00, 15, True),   # on the true front, trading
    "contractors":   (DIR_B, 1.35, 1.35, 13, True),   # same trades, further out
    "platform-core": (DIR_C, 1.00, 1.00, 16, False),  # on the true front, parked
}


def ground_truth(rng: random.Random) -> tuple[list[dict], dict]:
    """Five groups on three directions, with the front changing hands over time.

    * `agent-heavy` improves from 1.55 to 1.00 along direction A and
      `legacy-owner` worsens from 1.20 to 1.45 along the same direction, so the
      two swap places mid-run: in every window the smaller radius dominates the
      larger, and the membership timeline has something real to show.
    * `review-first` rotates along direction B at radius 1.00 -- on the true
      DTLZ2 front the whole time, trading debt against lead time, so its own
      windows are mutually non-dominated and its travel angle is large.
    * `contractors` improves along direction B but never inside radius 1.25, so
      `review-first` dominates it in every window.
    * `platform-core` sits at radius 1.00 on direction C and does not move.

    Planted front in the latest window: the three groups at radius 1.00.
    """
    specs: list[CellSpec] = []
    last = len(WINDOWS) - 1
    for group, (direction, r_first, r_last, n, rotates) in TRAJECTORIES.items():
        for i, window in enumerate(WINDOWS):
            radius = r_first + (r_last - r_first) * (i / last)
            if rotates:
                # Move along the front: debt up, lead time down, cost held.
                f = dtlz2(direction[0] - i * 0.10, direction[1] + i * 0.03)
            else:
                f = dtlz2(*direction)
            specs.append(
                CellSpec(
                    group=group,
                    window=window,
                    f=f,
                    radius=radius,
                    n_builds=n + (i % 3),
                    mix=_planted_mix(group, i),
                    on_planted_front=abs(radius - 1.0) < 1e-9,
                )
            )

    rows: list[dict] = []
    for spec in specs:
        rows.extend(emit_cell(spec, rng, AS_OF))

    latest = WINDOWS[-1]
    radius_at = {
        (spec.group, spec.window): spec.radius for spec in specs
    }
    # Same-direction pairs are exactly comparable, so the dominated set per
    # window falls out of the radii rather than out of a recomputed front.
    dominated_per_window = {}
    for window in WINDOWS:
        dominated = []
        for direction_groups in (
            ("agent-heavy", "legacy-owner"),
            ("review-first", "contractors"),
        ):
            a, b = direction_groups
            ra, rb = radius_at[(a, window)], radius_at[(b, window)]
            if ra < rb:
                dominated.append(f"{b}/{window}")
            elif rb < ra:
                dominated.append(f"{a}/{window}")
        dominated_per_window[window] = sorted(dominated)

    manifest = {
        "fixture": "ground-truth",
        "as_of": AS_OF.date().isoformat(),
        "run_with": ["--as-of", AS_OF.date().isoformat(), "--u", str(PINNED_U)],
        "pinned_u": PINNED_U,
        "u_note": "usage sits in two clusters, 5-40 and 5000-200000, with u pinned "
        "at 1000 between them; 0.5u and 2u stay inside the empty band, so the "
        "impact mix is exactly the planted one at all three levels",
        "geometry": "DTLZ2 concave sphere octant. Two cells on one direction are "
        "always comparable (smaller radius dominates); two cells at radius 1.0 on "
        "different directions are never comparable.",
        "objective_map": {
            "cost": f"{COST_BASE} + {COST_SPAN} * f1",
            "lead_time": f"{LEAD_BASE} + {LEAD_SPAN} * f2",
            "debt_defect": f"{DEFECT_BASE} + {DEFECT_SPAN} * f3 (bugs per KLOC)",
        },
        "planted": {
            "front_latest_window": sorted(
                f"{group}/{latest}"
                for group, (_, _, r_last, _, _) in TRAJECTORIES.items()
                if abs(r_last - 1.0) < 1e-9
            ),
            "dominated_latest_window": dominated_per_window[latest],
            "dominated_per_window": dominated_per_window,
            "within_group_front": {
                # Monotone radius along one direction is a total order, so the
                # front is one window. A constant radius on a rotating direction
                # is mutually non-dominated, so the front is every window.
                "agent-heavy": [f"agent-heavy/{latest}"],
                "legacy-owner": [f"legacy-owner/{WINDOWS[0]}"],
                "review-first": [f"review-first/{w}" for w in WINDOWS],
                "contractors": [f"contractors/{w}" for w in WINDOWS],
            },
            "travel": {
                "agent-heavy": "small angle: improving on all three together",
                "legacy-owner": "large angle: worsening on all three together",
                "review-first": "mid angle: moving along the front, trading debt "
                "against lead time",
                "platform-core": "near-zero magnitude: parked",
            },
        },
        "cells": [
            {
                "group": spec.group,
                "window": spec.window,
                "n_builds": spec.n_builds,
                "radius": spec.radius,
                "f_scaled": list(spec.scaled),
                "cost": spec.units[0],
                "lead_time": spec.units[1],
                "defect_density": spec.units[2],
                "impact_mix": spec.mix,
            }
            for spec in specs
        ],
    }
    return rows, manifest


def _planted_mix(group: str, i: int) -> dict[str, float]:
    """Impact mixes with roughly as many impactful as low-impact builds overall.

    That balance is deliberate: `u` defaults to the median usage over served
    builds, so with the two usage clusters far apart and their counts close, the
    median lands in the gap between them and the classification is stable at
    0.5u and 2u. A fixture whose default mix flipped under the sensitivity strip
    would be testing the fixture rather than the tool -- the `threshold` fixture
    below is the one that flips, on purpose.
    """
    table = {
        "agent-heavy":   (0.32, 0.28, 0.10, 0.15, 0.15),
        "legacy-owner":  (0.26, 0.26, 0.16, 0.14, 0.18),
        "review-first":  (0.34, 0.30, 0.08, 0.16, 0.12),
        "contractors":   (0.28, 0.26, 0.14, 0.16, 0.16),
        "platform-core": (0.30, 0.30, 0.10, 0.15, 0.15),
    }
    impactful, low, liability, carried, sunk = table[group]
    drift = 0.01 * i
    return _mix(
        impactful=impactful + drift,
        low=low,
        liability=max(0.02, liability - drift),
        carried=carried,
        sunk=sunk,
    )


def _mix(impactful: float, low: float, liability: float, carried: float,
         sunk: float, unresolved: float = 0.0) -> dict[str, float]:
    total = impactful + low + liability + carried + sunk + unresolved
    return {
        "impactful": impactful / total,
        "low_impact": low / total,
        "liability": liability / total,
        "carried": carried / total,
        "sunk": sunk / total,
        "unresolved": unresolved / total,
    }


def degeneracy(rng: random.Random) -> tuple[list[dict], dict]:
    """Objectives drawn independently: the front should be the size independent
    noise produces, and the null model should say NOT INFORMATIVE."""
    specs = []
    for group in ("alpha", "beta", "gamma", "delta"):
        for window in WINDOWS:
            f = (rng.random(), rng.random(), rng.random())
            specs.append(
                CellSpec(
                    group=group,
                    window=window,
                    f=f,  # type: ignore[arg-type]
                    radius=1.0,
                    n_builds=15,
                    mix=_mix(0.4, 0.2, 0.1, 0.15, 0.15),
                )
            )
    rows: list[dict] = []
    for spec in specs:
        rows.extend(emit_cell(spec, rng, AS_OF))
    return rows, {
        "fixture": "degeneracy",
        "as_of": AS_OF.date().isoformat(),
        "planted": {
            "expect_null_model_verdict": "NOT INFORMATIVE",
            "reason": "each objective is an independent uniform draw, so the front "
            "size is whatever the dimension count produces",
        },
    }


def overlap(rng: random.Random) -> tuple[list[dict], dict]:
    """Liability and sunk shares that move a lot, so `debt_composite`'s declared
    overlap (stage 0) shows up as a measured tau (stage 2) as well."""
    specs = []
    for i, window in enumerate(WINDOWS):
        waste = 0.05 + i * 0.10
        specs.append(
            CellSpec(
                group="one-team",
                window=window,
                f=dtlz2(0.6, 0.6),
                radius=1.0 + i * 0.08,
                n_builds=20,
                mix=_mix(
                    impactful=max(0.05, 0.70 - waste),
                    low=0.15,
                    liability=waste * 0.6,
                    carried=0.05,
                    sunk=waste * 0.4,
                ),
            )
        )
    rows: list[dict] = []
    for spec in specs:
        rows.extend(emit_cell(spec, rng, AS_OF))
    return rows, {
        "fixture": "overlap",
        "as_of": AS_OF.date().isoformat(),
        "planted": {
            "expect_structural_overlap": "debt_composite <- share(liability), share(sunk)",
            "expect_measured_tau_positive_against": ["share(liability)", "share(sunk)"],
            "run_with": "--objectives cost,lead_time,debt_composite",
        },
    }


# Mid-month, so the last L=30 days straddle a window boundary and two windows
# can carry an unresolved share rather than only the newest one.
CENSORING_AS_OF = datetime(2026, 5, 15, tzinfo=timezone.utc)
CENSORING_WINDOWS = ["2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]


def censoring(rng: random.Random) -> tuple[list[dict], dict]:
    """Rising unresolved share, flat impact mix underneath.

    The mix must never be renormalised to hide it: "we shipped no waste" and
    "we have not looked yet" are different claims, and this fixture is the
    difference. Nothing about the team changes across the six windows; only the
    observation edge moves closer.
    """
    specs = []
    # The resolved part of the mix is held *constant* in shape and squeezed by
    # the unresolved share, which is exactly the reporting artefact the fixture
    # exists to catch: nothing about the team changed.
    resolved = {"impactful": 0.50, "low_impact": 0.17, "liability": 0.11,
                "carried": 0.11, "sunk": 0.11}
    planted_unresolved = {}
    for i, window in enumerate(CENSORING_WINDOWS):
        unresolved = [0.0, 0.0, 0.0, 0.0, 0.25, 0.75][i]
        mix = {k: v * (1 - unresolved) for k, v in resolved.items()}
        mix["unresolved"] = unresolved
        planted_unresolved[window] = unresolved
        specs.append(
            CellSpec(
                group="one-team",
                window=window,
                f=dtlz2(0.7, 0.7),
                radius=1.0,
                n_builds=16,
                mix=mix,
            )
        )
    rows: list[dict] = []
    for spec in specs:
        rows.extend(emit_cell(spec, rng, CENSORING_AS_OF))
    return rows, {
        "fixture": "censoring",
        "as_of": CENSORING_AS_OF.date().isoformat(),
        "run_with": ["--as-of", CENSORING_AS_OF.date().isoformat(), "--u", "1000",
                     "--min-builds", "10"],
        "planted": {
            # Not the exact shares. Unresolvedness is derived from `ts` against
            # the observation edge, as it must be, so every build in the last L
            # days is unresolved whatever class the generator intended -- which
            # means the asked-for share is a floor, not an equality. What is
            # exactly plantable is the shape of what remains.
            "requested_unresolved_share": planted_unresolved,
            "resolved_shape": resolved,
            "expect_monotone_unresolved": True,
            "expect_constant_resolved_ratios": True,
            "expect": "the impact mix sums to 1 with unresolved as a part; the "
            "remaining parts are squeezed proportionally and never rescaled to fill "
            "the gap, so impactful:low_impact is identical in every window that has "
            "any resolved builds at all",
        },
    }


def threshold(rng: random.Random) -> tuple[list[dict], dict]:
    """A cell whose mix flips between 0.5u and 2u.

    Half its served builds sit just above the median usage and half just below,
    so halving or doubling `u` moves them across the gate and the sensitivity
    strip has to fire.
    """
    rows: list[dict] = []
    start, end = window_dates(WINDOWS[-1])
    for i in range(20):
        produced = start + timedelta(days=i)
        # Usage tightly clustered around 100: u lands inside the cluster, so
        # 0.5u and 2u fall on opposite sides of most of it.
        usage = 100.0 * (1.15 if i % 2 else 0.85)
        rows.append(
            {
                "build_id": f"knife-edge-{i:02d}",
                "group": "knife-edge",
                "first_commit_ts": (produced - timedelta(days=4)).date().isoformat(),
                "ts": produced.date().isoformat(),
                "deployed_at": (produced + timedelta(days=1)).date().isoformat(),
                "served_at": (produced + timedelta(days=2)).date().isoformat(),
                "usage": usage,
                "loc_added": 200,
                "loc_removed": 40,
                "bugs": i % 3,
                "cost_usd": 400 + 10 * i,
                "question": "features",
                "carried_into": "",
            }
        )
    for i in range(14):
        produced = start + timedelta(days=i)
        rows.append(
            {
                "build_id": f"steady-{i:02d}",
                "group": "steady",
                "first_commit_ts": (produced - timedelta(days=6)).date().isoformat(),
                "ts": produced.date().isoformat(),
                "deployed_at": (produced + timedelta(days=2)).date().isoformat(),
                "served_at": (produced + timedelta(days=3)).date().isoformat(),
                "usage": 9000.0 if i % 2 else 5.0,
                "loc_added": 260,
                "loc_removed": 30,
                "bugs": (i + 1) % 2,
                "cost_usd": 520 + 8 * i,
                "question": "correctness",
                "carried_into": "",
            }
        )
    return rows, {
        "fixture": "threshold",
        "as_of": AS_OF.date().isoformat(),
        "planted": {
            "flipping_cell": f"knife-edge/{WINDOWS[-1]}",
            "expect": "the sensitivity strip fires and the finding is marked "
            "PROVISIONAL, because usage clusters within a factor of two of u",
        },
    }


FIXTURES = {
    "ground-truth": ground_truth,
    "degeneracy": degeneracy,
    "overlap": overlap,
    "censoring": censoring,
    "threshold": threshold,
}


def write_metrics_csv(path: Path) -> None:
    """The shipped defaults, as a file a team can edit. Kept in step with
    formula.DEFAULT_METRICS by tests/test_formula.py."""
    from decision_surface import formula as F

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "role", "direction", "expression", "version", "notes"])
        for metric in F.DEFAULT_METRICS:
            writer.writerow(
                [
                    metric.name,
                    metric.role,
                    metric.direction,
                    metric.expression,
                    metric.version,
                    metric.notes,
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default="ground-truth", choices=sorted(FIXTURES))
    parser.add_argument("--out", default=None, help="builds csv path")
    parser.add_argument("--manifest", default=None, help="planted-truth json path")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--metrics", action="store_true", help="also write metrics.csv")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    out = Path(args.out) if args.out else here / "builds.csv"
    manifest_path = (
        Path(args.manifest)
        if args.manifest
        else out.with_name(out.stem + ".manifest.json")
    )

    rng = random.Random(args.seed)
    rows, manifest = FIXTURES[args.fixture](rng)
    manifest["seed"] = args.seed
    manifest["n_builds"] = len(rows)

    with open(out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.metrics:
        write_metrics_csv(here / "metrics.csv")
        print(f"wrote {here / 'metrics.csv'}")
    print(f"wrote {out} ({len(rows)} builds) and {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
