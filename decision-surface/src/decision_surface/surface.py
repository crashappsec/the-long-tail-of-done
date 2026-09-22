#!/usr/bin/env python3
"""decision-surface: store, analyse and draw build-level delivery numbers.

Standard library only -- same constraint and same reason as `shipping_tail.py`:
a team must be able to run this on a locked-down laptop without asking anyone.
Bootstrap, Kendall tau, non-dominated sorting, the BKST recurrence, the
permutation null and the restricted formula evaluator are a few dozen lines
each, and every one of them is here to be read rather than trusted.

    surface ingest  builds.csv          idempotent, on build_id
    surface define  --name debt ...     register a formula, get its version
    surface list    metrics|groups|windows|runs
    surface compute --objectives ...    stages 0-7 -> analysis.json
    surface render  analysis.json       -> one self-contained .html (via render.mjs)
    surface explain --cell g/window     why this cell is not on the front
    surface diff    run_a run_b         what moved, and whether definitions did

The contract is ../SCHEMA.md. The procedure is ../references/front.md, and the
stage numbering in this file is that document's. If the two disagree, the
document is the specification and this file has a bug.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from . import formula as F
from . import store as S
from .rendering import render_analysis

# ---------------------------------------------------------------------------
# Defaults, all declared in the output of every run
# ---------------------------------------------------------------------------

DEFAULT_L_DAYS = 30
DEFAULT_MIN_BUILDS = 10
DEFAULT_WINDOW = "month"
DEFAULT_WINDOWS_RETAINED = 6
DEFAULT_BOOTSTRAP = 2000
DEFAULT_PERMUTATIONS = 1000
DEFAULT_SEED = 20260910
DEFAULT_OBJECTIVES = ["cost", "lead_time", "debt_defect"]
# Just outside the worst observed cell on every axis, in min-max-normalised
# minimise-space. The ranking a hypervolume produces depends on this choice, so
# it is part of the result and not a hidden parameter (Ishibuchi et al. 2018).
HV_REFERENCE = 1.1
MAX_OBJECTIVES = 8
MIN_OBJECTIVES = 2
CUBE_GROUP_CAP = 6
CUBE_WINDOW_CAP = 4
ATTAINMENT_REPLICATES = 200
ATTAINMENT_GRID = 40
HV_EXACT_CAP = 16

IMPACT_CLASSES = list(F.IMPACT_CLASSES)
QUESTION_CLASSES = list(F.QUESTION_CLASSES)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def parse_ts(value) -> datetime | None:
    """ISO 8601, with a bare date accepted. Naive input is read as UTC."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"cannot parse timestamp: {value!r}")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def window_label(moment: datetime, spec: str) -> str:
    if spec == "week":
        iso = moment.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if spec == "quarter":
        return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"
    return f"{moment.year}-{moment.month:02d}"


def window_sort_key(label: str) -> tuple:
    """Chronological, for labels that do not sort lexicographically by month."""
    if "-W" in label:
        year, week = label.split("-W")
        return (int(year), int(week))
    if "-Q" in label:
        year, quarter = label.split("-Q")
        return (int(year), int(quarter))
    year, month = label.split("-")
    return (int(year), int(month))


def to_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value):
    number = to_float(value)
    return None if number is None else int(round(number))


# ---------------------------------------------------------------------------
# Reading builds
# ---------------------------------------------------------------------------


def read_builds_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, str) and value.strip() == "":
                row[key] = None
    return rows


# ---------------------------------------------------------------------------
# The impact gate (../SCHEMA.md section 2)
# ---------------------------------------------------------------------------


@dataclass
class GateParams:
    u: float | None
    u_source: str
    L_days: int
    as_of: datetime
    baseline: str | None = None

    def as_json(self) -> dict:
        return {
            "u": self.u,
            "u_source": self.u_source,
            "L_days": self.L_days,
            "as_of": self.as_of.isoformat(),
            "baseline_window": self.baseline,
        }


def derive_u(builds: list[dict], baseline: str | None) -> tuple[float | None, str]:
    """Median usage across served builds in the baseline window.

    Relative and self-calibrating on purpose: an absolute default is a fiction
    across teams, since a platform capability serving 40 internal callers and a
    consumer feature serving 400,000 sessions cannot share a threshold.
    """
    pool = [
        b
        for b in builds
        if b.get("served")
        and b.get("usage") is not None
        and (baseline is None or b.get("window") == baseline)
    ]
    if not pool:
        return None, "UNAVAILABLE: no served build carries a usage signal"
    value = F.median([float(b["usage"]) for b in pool])
    scope = baseline or "all retained windows"
    return value, f"median usage over {len(pool)} served builds in {scope}"


def classify(build: dict, u: float | None, L_days: int, as_of: datetime) -> str:
    """First matching rule wins. Order is the contract, not an implementation detail."""
    if build.get("age_days") is not None and build["age_days"] < L_days:
        return "unresolved"
    if not build.get("deployed"):
        return "carried" if build.get("carried_into") else "sunk"
    usage = build.get("usage")
    if usage is None or u is None:
        # "No usage signal" and "no usage" are different claims. The build stays
        # impactful and the gate is flagged; demoting it would manufacture waste
        # out of missing telemetry.
        return "impactful"
    if usage >= u:
        return "impactful"
    if usage > 0:
        return "low_impact"
    return "liability"


def prepare_builds(
    rows: list[dict],
    window_spec: str = DEFAULT_WINDOW,
    windows_retained: int = DEFAULT_WINDOWS_RETAINED,
    L_days: int = DEFAULT_L_DAYS,
    u: float | None = None,
    as_of: datetime | None = None,
    baseline: str | None = None,
) -> tuple[list[dict], GateParams, list[str], list[dict]]:
    """Coerce, derive, window and classify. Returns (builds, gate, windows, warnings)."""
    warnings: list[dict] = []
    prepared: list[dict] = []
    for index, row in enumerate(rows):
        try:
            ts = parse_ts(row.get("ts"))
            first = parse_ts(row.get("first_commit_ts"))
            deployed_at = parse_ts(row.get("deployed_at"))
            served_at = parse_ts(row.get("served_at"))
        except ValueError as exc:
            warnings.append({"code": "BAD_TIMESTAMP", "row": index, "message": str(exc)})
            continue
        if ts is None or first is None:
            warnings.append(
                {
                    "code": "MISSING_REQUIRED",
                    "row": row.get("build_id") or index,
                    "message": "ts and first_commit_ts are required",
                }
            )
            continue
        loc_added = to_float(row.get("loc_added"))
        loc_removed = to_float(row.get("loc_removed"))
        if loc_added is None or loc_removed is None:
            warnings.append(
                {
                    "code": "MISSING_REQUIRED",
                    "row": row.get("build_id") or index,
                    "message": "loc_added and loc_removed are required",
                }
            )
            continue
        build = {
            "build_id": row.get("build_id") or f"row-{index}",
            "group": (row.get("group") or "all"),
            "first_commit_ts": first.isoformat(),
            "ts": ts.isoformat(),
            "deployed_at": deployed_at.isoformat() if deployed_at else None,
            "served_at": served_at.isoformat() if served_at else None,
            "usage": to_float(row.get("usage")),
            "loc_added": loc_added,
            "loc_removed": loc_removed,
            "loc_total": loc_added + loc_removed,
            "bugs": to_int(row.get("bugs")),
            "cost_usd": to_float(row.get("cost_usd")),
            "question": row.get("question") or None,
            "carried_into": row.get("carried_into") or None,
            "deployed": deployed_at is not None,
            "served": served_at is not None,
            "_ts": ts,
        }
        if build["question"] and build["question"] not in QUESTION_CLASSES:
            warnings.append(
                {
                    "code": "UNKNOWN_QUESTION",
                    "row": build["build_id"],
                    "message": f"question '{build['question']}' is not one of "
                    f"{', '.join(QUESTION_CLASSES)}; counted as unclassified",
                }
            )
        # Lead time, honestly. Served beats deployed, and the source travels with
        # the number so a comparison can refuse to mix the two.
        #
        # The DEPLOYED fallback is for a build whose serving we could not
        # observe -- no usage telemetry -- and not for a build we know was never
        # served. A build with `usage == 0` was deployed and used by nobody, so
        # its first-commit-to-first-user interval does not exist; recording the
        # deploy interval instead would put a systematically smaller number into
        # the median and make the groups with the most waste look the fastest.
        # ../SCHEMA.md section 3 refines the rule for exactly this reason.
        if served_at is not None:
            build["lead_time_days"] = (served_at - first).total_seconds() / 86400.0
            build["lead_time_source"] = "SERVED"
        elif deployed_at is not None and build["usage"] in (None, ""):
            build["lead_time_days"] = (deployed_at - first).total_seconds() / 86400.0
            build["lead_time_source"] = "DEPLOYED"
        elif deployed_at is not None and (build["usage"] or 0) > 0:
            # Served -- the usage signal says so -- but the moment was not
            # recorded. Same fallback, same reason: this is unobserved, not absent.
            build["lead_time_days"] = (deployed_at - first).total_seconds() / 86400.0
            build["lead_time_source"] = "DEPLOYED"
        else:
            build["lead_time_days"] = None
            build["lead_time_source"] = None
        build["window"] = window_label(ts, window_spec)
        prepared.append(build)

    if not prepared:
        raise ValueError("no usable builds: every row was rejected, see warnings")

    edge = as_of or datetime.now(timezone.utc)
    for build in prepared:
        build["age_days"] = (edge - build["_ts"]).total_seconds() / 86400.0

    windows = sorted({b["window"] for b in prepared}, key=window_sort_key)
    retained = windows[-windows_retained:] if windows_retained else windows
    dropped = [w for w in windows if w not in retained]
    if dropped:
        warnings.append(
            {
                "code": "WINDOWS_DROPPED",
                "message": f"retaining the last {len(retained)} windows; dropped "
                f"{', '.join(dropped)}",
            }
        )
    prepared = [b for b in prepared if b["window"] in retained]

    if u is None:
        u_value, u_source = derive_u(prepared, baseline)
    else:
        u_value, u_source = float(u), "supplied with --u"
    gate = GateParams(u=u_value, u_source=u_source, L_days=L_days, as_of=edge,
                      baseline=baseline)

    for build in prepared:
        build["impact"] = classify(build, gate.u, L_days, edge)
        build["gate_unavailable"] = bool(
            build["deployed"] and (build["usage"] is None or gate.u is None)
        )
        build.pop("_ts", None)
    return prepared, gate, retained, warnings


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    group: str
    window: str
    builds: list[dict]
    objectives: dict[str, float | None] = field(default_factory=dict)
    descriptors: dict[str, float | None] = field(default_factory=dict)
    norm: dict[str, float | None] = field(default_factory=dict)
    rank: dict[str, float | None] = field(default_factory=dict)
    impact_mix: dict[str, float] = field(default_factory=dict)
    question_mix: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    aggregate_n: int | None = None
    aggregate_fallback: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.group, self.window)

    @property
    def label(self) -> str:
        return f"{self.group}/{self.window}"

    @property
    def n_builds(self) -> int:
        return len(self.builds) if self.aggregate_n is None else self.aggregate_n


def unclassified_share(
    builds: list[dict], field_name: str, classes: Sequence[str]
) -> float | None:
    """Share of builds whose value for `field_name` is outside the class list.

    The impact gate always assigns one of its six classes, so this is only ever
    non-zero for `question`, which a team supplies. It has to be reported: the
    shares are NOT renormalised to close the gap, so a cell with two off-enum
    questions in five builds has a question mix summing to 0.6, and a mix that
    does not sum to 1 with no number explaining why is exactly the silent
    renormalisation this tool exists to refuse.
    """
    if not builds:
        return None
    known = set(classes)
    return sum(1 for b in builds if b.get(field_name) not in known) / len(builds)


def composition(
    builds: list[dict], field_name: str, classes: Sequence[str]
) -> dict[str, float]:
    """Shares over a fixed class list. Never renormalised to hide an unclassified part."""
    total = len(builds)
    if total == 0:
        return {c: 0.0 for c in classes}
    counts = {c: 0 for c in classes}
    for build in builds:
        value = build.get(field_name)
        if value in counts:
            counts[value] += 1
    return {c: counts[c] / total for c in classes}


def build_cells(builds: list[dict]) -> list[Cell]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for build in builds:
        grouped.setdefault((build["group"], build["window"]), []).append(build)
    cells = [Cell(group=g, window=w, builds=rows) for (g, w), rows in grouped.items()]
    cells.sort(key=lambda c: (c.group, window_sort_key(c.window)))
    return cells


def lead_time_sources(cell: Cell) -> set[str]:
    if cell.aggregate_n is not None:
        share = cell.aggregate_fallback
        if share is None:
            return set()
        return ({"SERVED"} if share < 1 else set()) | ({"DEPLOYED"} if share > 0 else set())
    return {
        b["lead_time_source"]
        for b in cell.builds
        if b.get("lead_time_source") is not None
    }


def fallback_share(cell: Cell) -> float | None:
    """Share of the cell's defined lead times measured to deploy, not to serve."""
    if cell.aggregate_n is not None:
        return cell.aggregate_fallback
    defined = [b for b in cell.builds if b.get("lead_time_source") is not None]
    if not defined:
        return None
    return sum(1 for b in defined if b["lead_time_source"] == "DEPLOYED") / len(defined)


FALLBACK_DIVERGENCE = 0.20


def lead_time_source_check(
    cells: list[Cell], params: ComputeParams
) -> tuple[list[dict], list[dict]]:
    """Refuse the comparison that manufactures a difference; report the rest.

    The declared policy catches divergent measurement mixes. Equal fallback
    shares do not prove equal bias, so every mixed cell still gets a caution:

    * every cell carries its fallback share, always, in the output;
    * a cell that mixes at all is a CAUTION naming the share;
    * cells whose fallback shares differ by more than FALLBACK_DIVERGENCE
      inside one comparison are a REFUSAL, which `--allow-mixed-lead-time`
      downgrades to a caution.

    This check applies only to the cells in the comparison being reported.
    """
    refusals: list[dict] = []
    cautions: list[dict] = []
    shares = {cell.label: fallback_share(cell) for cell in cells}
    mixed = [cell.label for cell in cells if len(lead_time_sources(cell)) > 1]
    if mixed:
        cautions.append(
            {
                "code": "MIXED_LEAD_TIME_SOURCE",
                "message": "these cells measure some builds to serve and some to "
                "deploy: "
                + ", ".join(f"{label} ({shares[label]:.0%} to deploy)" for label in mixed)
                + ". The median is computed over two different measurements.",
                "cells": mixed,
                "fallback_shares": {k: v for k, v in shares.items() if v is not None},
            }
        )
    present = {k: v for k, v in shares.items() if v is not None}
    if present and (max(present.values()) - min(present.values())) > FALLBACK_DIVERGENCE:
        worst = max(present, key=lambda k: present[k])
        best = min(present, key=lambda k: present[k])
        entry = {
            "code": "DIVERGENT_LEAD_TIME_SOURCE",
            "message": f"{worst} measures {present[worst]:.0%} of its lead times to "
            f"deploy and {best} measures {present[best]:.0%}. Merge-to-exposure lag "
            f"differs by team, so comparing these two manufactures exactly the "
            f"difference the front then reports. Supply served_at for both, compare "
            f"like with like, or pass --allow-mixed-lead-time to proceed with this "
            f"stamped on every chart.",
            "fallback_shares": present,
        }
        if params.allow_mixed_lead_time:
            cautions.append(entry)
        else:
            refusals.append(entry)
    return refusals, cautions


# ---------------------------------------------------------------------------
# Statistics on the standard library
# ---------------------------------------------------------------------------


def kendall_tau_b(xs: list[float | None], ys: list[float | None]) -> float | None:
    """tau-b, not tau-a: cells tie on cost and lead time constantly, and tau-a
    would read a tie as disagreement."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    concordant = discordant = tie_x = tie_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = pairs[i][0] - pairs[j][0]
            dy = pairs[i][1] - pairs[j][1]
            product = dx * dy
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                tie_x += 1
            elif dy == 0:
                tie_y += 1
            elif product > 0:
                concordant += 1
            else:
                discordant += 1
    denominator = math.sqrt(
        (concordant + discordant + tie_x) * (concordant + discordant + tie_y)
    )
    if denominator == 0:
        return None
    return (concordant - discordant) / denominator


def average_ranks(values: list[float | None]) -> list[float | None]:
    """Average ranks over the non-null values, scaled to [0, 1]. Nulls stay null."""
    indexed = [(v, i) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    if not indexed:
        return out
    indexed.sort()
    i = 0
    ranks: dict[int, float] = {}
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][0] == indexed[i][0]:
            j += 1
        mean_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][1]] = mean_rank
        i = j + 1
    span = max(1.0, float(len(indexed) - 1))
    for index, rank in ranks.items():
        out[index] = rank / span
    return out


def minmax_scale(values: list[float | None]) -> tuple[list[float | None], float | None, float | None]:
    present = [v for v in values if v is not None]
    if not present:
        return [None] * len(values), None, None
    lo, hi = min(present), max(present)
    if hi == lo:
        # Degenerate: every cell identical. 0.5 rather than a division by zero;
        # it cannot affect dominance either way and stage 2 flags it.
        return [None if v is None else 0.5 for v in values], lo, hi
    return [None if v is None else (v - lo) / (hi - lo) for v in values], lo, hi


# ---------------------------------------------------------------------------
# Dominance
# ---------------------------------------------------------------------------


def dominates(a, b) -> bool:
    """Minimisation, strict Pareto dominance. Callers pass complete vectors --
    `usable_rows` is how they guarantee it, since a null is not a zero and an
    unobserved objective cannot be placed in an order at all."""
    better = False
    for av, bv in zip(a, b):
        if av > bv:
            return False
        if av < bv:
            better = True
    return better


def usable_rows(matrix: list[list[float | None]]) -> list[int]:
    """Indices whose objective vector is complete. A null is not a zero, so a
    cell with an unobserved objective cannot be placed in a dominance order."""
    return [i for i, row in enumerate(matrix) if all(v is not None for v in row)]


def nondominated_layers(matrix: list[list[float | None]]) -> list[int | None]:
    """Peel layer by layer. n is tens of cells and m <= 8, so naive O(n^2 m) is
    correct and instantaneous; Kung's algorithm and ENS exist and are not worth
    importing for 24 rows."""
    layers: list[int | None] = [None] * len(matrix)
    live = usable_rows(matrix)
    current = 1
    while live:
        front = []
        for i in live:
            if not any(
                dominates(matrix[j], matrix[i]) for j in live if j != i
            ):
                front.append(i)
        if not front:  # cannot happen for a strict order, but do not loop forever
            break
        for i in front:
            layers[i] = current
        live = [i for i in live if i not in set(front)]
        current += 1
    return layers


def front_indices(matrix: list[list[float | None]]) -> list[int]:
    live = usable_rows(matrix)
    return [
        i
        for i in live
        if not any(dominates(matrix[j], matrix[i]) for j in live if j != i)
    ]


def epsilon_dominance_front(
    matrix: list[list[float | None]], epsilon: list[float]
) -> list[int]:
    """Laumanns/Thiele/Deb/Zitzler 2002: box the space, dominate on boxes.

    Epsilon is a resolution heuristic. Crossing a box boundary is not a
    significance test and need not imply a difference greater than epsilon.
    """
    live = usable_rows(matrix)
    boxed: dict[int, list[float]] = {}
    for i in live:
        row = [float(v) for v in matrix[i]]  # type: ignore[arg-type]
        boxed[i] = [
            math.floor(row[j] / epsilon[j]) if epsilon[j] > 0 else row[j]
            for j in range(len(epsilon))
        ]
    keep = []
    for i in live:
        if not any(dominates(boxed[j], boxed[i]) for j in live if j != i):
            keep.append(i)
    return keep


def k_dominance_front(matrix: list[list[float | None]], k: int) -> tuple[list[int], list[list[int]]]:
    """Chan et al. SIGMOD 2006. Returns (skyline, cycles).

    k-dominance is *not transitive*, so the result is a set and not an order.
    Cycles are found and reported rather than silently broken.
    """
    live = usable_rows(matrix)
    d = len(matrix[live[0]]) if live else 0

    def k_dominates(a, b) -> bool:
        no_worse = sum(1 for av, bv in zip(a, b) if av <= bv)
        strictly = any(av < bv for av, bv in zip(a, b))
        return no_worse >= k and strictly

    edges = {
        i: [j for j in live if j != i and k_dominates(matrix[i], matrix[j])]
        for i in live
    }
    skyline = [i for i in live if not any(i in edges[j] for j in live if j != i)]
    cycles = _find_cycles(edges)
    return skyline, cycles


def _find_cycles(edges: dict[int, list[int]]) -> list[list[int]]:
    """Iterative strongly connected components, without a recursion-depth limit."""
    reverse = {node: [] for node in edges}
    for node, targets in edges.items():
        for target in targets:
            reverse.setdefault(target, []).append(node)
    seen, order = set(), []
    for node in reverse:
        if node in seen:
            continue
        seen.add(node)
        stack = [(node, iter(edges.get(node, [])))]
        while stack:
            current, successors = stack[-1]
            successor = next(successors, None)
            if successor is None:
                order.append(current)
                stack.pop()
            elif successor not in seen:
                seen.add(successor)
                stack.append((successor, iter(edges.get(successor, []))))
    seen, result = set(), []
    for node in reversed(order):
        if node in seen:
            continue
        pending, component = [node], []
        seen.add(node)
        while pending:
            current = pending.pop()
            component.append(current)
            for successor in reverse[current]:
                if successor not in seen:
                    seen.add(successor)
                    pending.append(successor)
        if len(component) > 1:
            result.append(sorted(component))
    return result


def skyline_frequency(matrix: list[list[float | None]]) -> tuple[dict[int, int], int]:
    """Chan et al. EDBT 2006: in how many of the 2^d - 1 subspaces is each row
    non-dominated. With d <= 6 that is at most 63 subspaces over tens of rows:
    exhaustive, instant, no approximation. It replaces a binary with a
    continuous interestingness score."""
    live = usable_rows(matrix)
    if not live:
        return {}, 0
    d = len(matrix[live[0]])
    counts = {i: 0 for i in live}
    subspaces = 0
    for mask in range(1, 1 << d):
        axes = [j for j in range(d) if mask & (1 << j)]
        subspaces += 1
        projected = {i: [matrix[i][j] for j in axes] for i in live}
        for i in live:
            if not any(
                dominates(projected[j], projected[i]) for j in live if j != i
            ):
                counts[i] += 1
    return counts, subspaces


# -- the null model ---------------------------------------------------------


def bkst_expected_maxima(n: int, d: int) -> float:
    """Bentley, Kung, Schkolnick & Thompson, JACM 25(4):536-543, Oct 1978.

        A(n, d) = A(n-1, d) + A(n, d-1) / n     for n, d >= 2
        A(1, d) = 1                             for d >= 1
        A(n, 1) = 1                             for n >= 1

    so A(n,2) = H_n ~ ln n, and O((ln n)^(d-1)) for fixed d. Three lines, no
    dependencies, and it is the sanity check on the permutation null below.
    """
    if n < 1 or d < 1:
        return 0.0
    table = [[0.0] * (d + 1) for _ in range(n + 1)]
    for j in range(1, d + 1):
        table[1][j] = 1.0
    for i in range(1, n + 1):
        table[i][1] = 1.0
    for i in range(2, n + 1):
        for j in range(2, d + 1):
            table[i][j] = table[i - 1][j] + table[i][j - 1] / i
    return table[n][d]


def permutation_null(
    matrix: list[list[float | None]], replicates: int, rng: random.Random
) -> dict:
    """Shuffle each objective column independently, recompute front size.

    The default output, because it needs no independence assumption and it
    handles ties -- neither of which the closed form does.
    """
    live = usable_rows(matrix)
    if len(live) < 3:
        return {"available": False, "reason": "fewer than 3 complete cells"}
    columns = list(zip(*[matrix[i] for i in live]))
    d = len(columns)
    observed = len(front_indices([list(matrix[i]) for i in live]))
    sizes = []
    for _ in range(replicates):
        shuffled = []
        for column in columns:
            values = list(column)
            rng.shuffle(values)
            shuffled.append(values)
        rows = [list(row) for row in zip(*shuffled)]
        sizes.append(len(front_indices(rows)))
    sizes.sort()
    below = sum(1 for s in sizes if s < observed)
    equal = sum(1 for s in sizes if s == observed)
    percentile = (below + 0.5 * equal) / len(sizes)
    closed_form = bkst_expected_maxima(len(live), d)
    if 0.05 <= percentile <= 0.95:
        verdict = "NOT INFORMATIVE"
        reading = (
            "The front is the size independent noise produces. These objectives "
            "are not trading off in this data, and the front says nothing beyond "
            "the dimension count."
        )
    elif percentile > 0.95:
        verdict = "CONFLICTING"
        reading = (
            "The front is larger than chance: the objectives conflict, the "
            "trade-off is real, and the front is the finding."
        )
    else:
        verdict = "AGREEING"
        reading = (
            "The front is smaller than chance: the objectives agree, one cell is "
            "genuinely better, and there are probably redundant objectives -- "
            "check stage 2."
        )
    return {
        "available": True,
        "observed_front_size": observed,
        "n_cells": len(live),
        "n_objectives": d,
        "permutation_mean": sum(sizes) / len(sizes),
        "permutation_p05": sizes[int(0.05 * (len(sizes) - 1))],
        "permutation_p95": sizes[int(0.95 * (len(sizes) - 1))],
        "percentile": percentile,
        "closed_form_A_n_d": closed_form,
        "replicates": replicates,
        "verdict": verdict,
        "reading": reading,
    }


# -- knees, level diagrams, hypervolume -------------------------------------


def solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. Returns None if singular."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for row in range(col + 1, n):
            factor = m[row][col] / m[col][col]
            for k in range(col, n + 1):
                m[row][k] -= factor * m[col][k]
    x = [0.0] * n
    for row in reversed(range(n)):
        total = m[row][n] - sum(m[row][k] * x[k] for k in range(row + 1, n))
        x[row] = total / m[row][row]
    return x


def knees(
    matrix: list[list[float | None]], front: list[int], rng: random.Random
) -> dict:
    """Branke, Deb, Dierolf & Osswald, PPSN VIII 2004.

    Max bend angle in 2D; for m > 2, max perpendicular distance from the
    hyperplane through the extreme points, plus a marginal-utility ranking over
    a sampled weight simplex. The natural answer to "which cell should we copy".
    """
    if len(front) < 3:
        return {"available": False, "reason": "fewer than 3 cells on the front"}
    d = len(matrix[front[0]])
    points = {i: [float(v) for v in matrix[i]] for i in front}  # type: ignore[arg-type]

    result: dict = {"method": None, "scores": {}, "knee": None}
    if d == 2:
        ordered = sorted(front, key=lambda i: points[i][0])
        result["method"] = "bend angle (2D)"
        for pos, i in enumerate(ordered):
            if pos == 0 or pos == len(ordered) - 1:
                result["scores"][i] = 0.0
                continue
            prev, nxt = points[ordered[pos - 1]], points[ordered[pos + 1]]
            here = points[i]
            v1 = (prev[0] - here[0], prev[1] - here[1])
            v2 = (nxt[0] - here[0], nxt[1] - here[1])
            n1 = math.hypot(*v1)
            n2 = math.hypot(*v2)
            if n1 == 0 or n2 == 0:
                result["scores"][i] = 0.0
                continue
            cosine = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            result["scores"][i] = math.pi - math.acos(cosine)
    else:
        extremes = []
        for j in range(d):
            extremes.append(min(front, key=lambda i: points[i][j]))
        extremes = list(dict.fromkeys(extremes))
        plane = None
        if len(extremes) == d:
            plane = solve_linear([points[i][:] for i in extremes], [1.0] * d)
        if plane is None:
            result["method"] = "distance to ideal (hyperplane through extremes was singular)"
            for i in front:
                result["scores"][i] = -math.sqrt(sum(v * v for v in points[i]))
        else:
            norm = math.sqrt(sum(w * w for w in plane))
            result["method"] = "perpendicular distance from the hyperplane through the extremes"
            for i in front:
                value = sum(w * v for w, v in zip(plane, points[i]))
                result["scores"][i] = abs(value - 1.0) / norm

    # Marginal utility: how often is this cell the best under a sampled weight
    # vector. Reported alongside the geometric knee because they disagree, and
    # the disagreement is informative.
    wins = {i: 0 for i in front}
    samples = 500
    for _ in range(samples):
        weights = [rng.expovariate(1.0) for _ in range(d)]
        total = sum(weights) or 1.0
        weights = [w / total for w in weights]
        best = min(front, key=lambda i: sum(w * v for w, v in zip(weights, points[i])))
        wins[best] += 1
    result["marginal_utility"] = {i: wins[i] / samples for i in front}
    result["weight_distribution"] = "uniform simplex (Dirichlet(1,...,1))"
    if result["scores"]:
        result["knee"] = max(result["scores"], key=lambda i: result["scores"][i])
    result["available"] = True
    return result


def distance_to_ideal(matrix: list[list[float | None]], indices: list[int]) -> dict[int, dict]:
    """For level diagrams: each objective against distance to the ideal point."""
    live = [i for i in indices if all(v is not None for v in matrix[i])]
    if not live:
        return {}
    d = len(matrix[live[0]])
    ideal = [min(float(matrix[i][j]) for i in live) for j in range(d)]  # type: ignore[arg-type]
    out = {}
    for i in live:
        deltas = [float(matrix[i][j]) - ideal[j] for j in range(d)]  # type: ignore[arg-type]
        out[i] = {
            "l1": sum(abs(x) for x in deltas),
            "l2": math.sqrt(sum(x * x for x in deltas)),
            "linf": max(abs(x) for x in deltas),
        }
    return out


def hypervolume(
    points: list[list[float]], reference: float = HV_REFERENCE, rng: random.Random | None = None
) -> dict:
    """Dominated hypervolume against a fixed reference point.

    Inclusion-exclusion over the front's boxes: exact, ~15 lines, and fine for
    the front sizes this tool produces. Above HV_EXACT_CAP points it falls back
    to a seeded Monte Carlo estimate and says so, because 2^n terms stops being
    instant somewhere around there.
    """
    boxes = [p for p in points if all(v < reference for v in p)]
    if not boxes:
        return {"value": 0.0, "method": "exact", "reference_point": reference,
                "points_used": 0}
    if len(boxes) <= HV_EXACT_CAP:
        total = 0.0
        n = len(boxes)
        for mask in range(1, 1 << n):
            corner = None
            bits = 0
            for i in range(n):
                if mask & (1 << i):
                    bits += 1
                    corner = (
                        boxes[i]
                        if corner is None
                        else [max(c, v) for c, v in zip(corner, boxes[i])]
                    )
            assert corner is not None
            volume = 1.0
            for value in corner:
                volume *= max(0.0, reference - value)
            total += volume if bits % 2 == 1 else -volume
        return {
            "value": total,
            "method": "exact (inclusion-exclusion)",
            "reference_point": reference,
            "points_used": len(boxes),
        }
    rng = rng or random.Random(DEFAULT_SEED)
    d = len(boxes[0])
    lower = [min(p[j] for p in boxes) for j in range(d)]
    samples = 200_000
    hits = 0
    for _ in range(samples):
        point = [rng.uniform(lower[j], reference) for j in range(d)]
        if any(all(p[j] <= point[j] for j in range(d)) for p in boxes):
            hits += 1
    box_volume = 1.0
    for j in range(d):
        box_volume *= reference - lower[j]
    return {
        "value": box_volume * hits / samples,
        "method": f"monte carlo, {samples} samples (front above {HV_EXACT_CAP} points)",
        "reference_point": reference,
        "points_used": len(boxes),
    }


# -- compositions -----------------------------------------------------------


def zero_replacement_delta(mix: dict[str, float], n_builds: int) -> float:
    zeros = sum(v <= 0 for v in mix.values())
    return min(0.5 / max(1, n_builds), 0.5 / max(1, zeros))


def clr(mix: dict[str, float], n_builds: int) -> dict[str, float] | None:
    """Centred log-ratio, with a reported multiplicative replacement for zeros.

    A composition with a structural zero is not the same object as one without,
    so the replacement is part of the output rather than a silent fix.
    """
    parts = list(mix)
    values = [mix[p] for p in parts]
    if all(v == 0 for v in values):
        return None
    delta = zero_replacement_delta(mix, n_builds)
    zeros = [i for i, v in enumerate(values) if v <= 0]
    if zeros:
        remaining = 1.0 - delta * len(zeros)
        nonzero_total = sum(v for v in values if v > 0) or 1.0
        values = [
            delta if v <= 0 else v * remaining / nonzero_total for v in values
        ]
    log_values = [math.log(v) for v in values]
    mean_log = sum(log_values) / len(log_values)
    return {p: lv - mean_log for p, lv in zip(parts, log_values)}


def aitchison_distance(a: dict[str, float], b: dict[str, float]) -> tuple[float | None, dict | None]:
    """Aitchison distance plus the log-ratio contributing most of it.

    Immune to the sum constraint, which is why it is the one honest scalar for
    "the mix moved". "Liability rose 4 points" also means three other things
    moved and does not say which.
    """
    parts = [p for p in a if p in b]
    if len(parts) < 2:
        return None, None
    total = 0.0
    contributions = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            term = (a[parts[i]] - a[parts[j]]) - (b[parts[i]] - b[parts[j]])
            total += term * term
            contributions.append(
                {"ratio": f"{parts[i]}:{parts[j]}", "contribution": term * term,
                 "direction": term}
            )
    distance = math.sqrt(total / len(parts))
    contributions.sort(key=lambda c: -c["contribution"])
    return distance, contributions[0] if contributions else None


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass
class ComputeParams:
    objectives: list[str]
    window_spec: str = DEFAULT_WINDOW
    windows_retained: int = DEFAULT_WINDOWS_RETAINED
    L_days: int = DEFAULT_L_DAYS
    u: float | None = None
    as_of: datetime | None = None
    baseline: str | None = None
    min_builds: int = DEFAULT_MIN_BUILDS
    bootstrap: int = DEFAULT_BOOTSTRAP
    permutations: int = DEFAULT_PERMUTATIONS
    seed: int = DEFAULT_SEED
    epsilon_source: str = "bootstrap"
    epsilon_manual: list[float] | None = None
    weights: list[float] | None = None
    allow_mixed_lead_time: bool = False

    def as_json(self) -> dict:
        return {
            "objectives": self.objectives,
            "window_spec": self.window_spec,
            "windows_retained": self.windows_retained,
            "L_days": self.L_days,
            "u": self.u,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "baseline": self.baseline,
            "min_builds": self.min_builds,
            "bootstrap": self.bootstrap,
            "permutations": self.permutations,
            "seed": self.seed,
            "epsilon_source": self.epsilon_source,
            "epsilon_manual": self.epsilon_manual,
            "weights": self.weights,
            "allow_mixed_lead_time": self.allow_mixed_lead_time,
            "normalisation": "min-max and rank, both emitted",
            "hv_reference_point": HV_REFERENCE,
        }


def evaluate_cells(
    cells: list[Cell], metrics: list[F.Metric]
) -> list[dict]:
    """Two passes, because minmax/z are cell-level and need every cell first."""
    notes: list[dict] = []
    for metric in metrics:
        assert metric.formula is not None
        pass1 = [metric.formula.evaluate(cell.builds) for cell in cells]
        params = F.norm_params_from_deferred(
            metric.formula, [r.deferred for r in pass1]
        )
        results = (
            [metric.formula.evaluate(cell.builds, params) for cell in cells]
            if metric.formula.norm_node_count
            else pass1
        )
        for cell, result in zip(cells, results):
            target = cell.objectives if metric.role == "objective" else cell.descriptors
            target[metric.name] = result.value
            for note in result.notes:
                cell.notes.append(note)
            if result.nulls_skipped:
                cell.notes.append(
                    f"{metric.name}: skipped {result.nulls_skipped} null value(s)"
                )
        if metric.formula.norm_node_count:
            notes.append(
                {
                    "metric": metric.name,
                    "normalisation_params": {str(k): v for k, v in params.items()},
                }
            )
    return notes


def flip_to_minimise(cells: list[Cell], objectives: list[F.Metric]) -> None:
    """Internally everything minimises. Flip once, here, so nothing downstream
    carries a direction flag."""
    for metric in objectives:
        if metric.sign < 0:
            for cell in cells:
                value = cell.objectives.get(metric.name)
                if value is not None:
                    cell.objectives[metric.name] = -value


def objective_matrix(cells: list[Cell], names: list[str], space: str = "norm") -> list[list[float | None]]:
    source = {"norm": "norm", "rank": "rank", "raw": "objectives"}[space]
    return [[getattr(cell, source).get(n) for n in names] for cell in cells]


def conflict_screen(
    cells: list[Cell], objective_names: list[str], descriptor_names: list[str]
) -> dict:
    """Stage 2. Kendall tau-b on ranks over pooled cells, plus the axis order
    every picture in the run will use."""
    ranks = {n: [cell.rank.get(n) for cell in cells] for n in objective_names}
    matrix = []
    verdicts = []
    for i, a in enumerate(objective_names):
        row = []
        for j, b in enumerate(objective_names):
            if i == j:
                row.append(1.0)
                continue
            tau = kendall_tau_b(ranks[a], ranks[b])
            row.append(tau)
            if i < j and tau is not None:
                if abs(tau) >= 0.8:
                    verdict = "redundant"
                elif tau <= -0.4:
                    verdict = "conflicting"
                else:
                    verdict = "independent"
                verdicts.append({"a": a, "b": b, "tau": tau, "verdict": verdict})
        matrix.append(row)

    # Descriptors go through it too, specifically each objective against every
    # mix part. This is the measured half of the overlap check; stage 0 is the
    # declared half.
    mix_series = {
        f"share({c})": [cell.impact_mix.get(c) for cell in cells] for c in IMPACT_CLASSES
    }
    mix_series.update(
        {
            f"share({c})": [cell.question_mix.get(c) for cell in cells]
            for c in QUESTION_CLASSES
        }
    )
    for name in descriptor_names:
        mix_series[name] = [cell.descriptors.get(name) for cell in cells]
    against_mix = {}
    for objective in objective_names:
        against_mix[objective] = {
            part: kendall_tau_b(ranks[objective], average_ranks(series))
            for part, series in mix_series.items()
        }

    axis_order = _axis_order(objective_names, verdicts)
    return {
        "objectives": objective_names,
        "tau_matrix": matrix,
        "verdicts": verdicts,
        "objective_vs_descriptor": against_mix,
        "axis_order": axis_order,
        "axis_order_rule": "most-conflicting pairs adjacent; fixed once here and "
        "reused by every picture in the run",
    }


def _axis_order(names: list[str], verdicts: list[dict]) -> list[str]:
    """Greedy chain: start from the most conflicting pair, extend by whichever
    remaining axis conflicts most with the current end."""
    if len(names) < 3:
        return list(names)
    scored = sorted(
        [v for v in verdicts if v["tau"] is not None], key=lambda v: v["tau"]
    )
    if not scored:
        return list(names)
    order = [scored[0]["a"], scored[0]["b"]]
    remaining = [n for n in names if n not in order]
    tau_of = {}
    for v in verdicts:
        tau_of[(v["a"], v["b"])] = v["tau"]
        tau_of[(v["b"], v["a"])] = v["tau"]
    def conflict_with(end: str, candidate: str) -> float:
        tau = tau_of.get((end, candidate))
        return 1.0 if tau is None else tau  # unknown pairs sort last

    while remaining:
        end = order[-1]
        nxt = min(remaining, key=lambda n: conflict_with(end, n))
        order.append(nxt)
        remaining.remove(nxt)
    return order


def greedy_reduction(objective_names: list[str], verdicts: list[dict]) -> dict:
    """Stage 3.1 -- cluster by conflict (single-linkage on |tau| >= 0.8), keep one
    representative per cluster. Jaimes & Coello's feature-selection framing."""
    parent = {n: n for n in objective_names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for v in verdicts:
        if v["tau"] is not None and abs(v["tau"]) >= 0.8:
            ra, rb = find(v["a"]), find(v["b"])
            if ra != rb:
                parent[rb] = ra
    clusters: dict[str, list[str]] = {}
    for name in objective_names:
        clusters.setdefault(find(name), []).append(name)
    kept = [sorted(members)[0] for members in clusters.values()]
    return {
        "clusters": [sorted(m) for m in clusters.values()],
        "kept": sorted(kept),
        "dropped": sorted(set(objective_names) - set(kept)),
        "rule": "single-linkage on |tau| >= 0.8; representative is the "
        "alphabetically first member, which is arbitrary and reported",
    }


def delta_moss(matrix: list[list[float | None]], names: list[str]) -> dict:
    """Stage 3.2 -- Brockhoff & Zitzler. Greedy delta-MOSS with delta reported.

    The error a dropped objective causes runs in one direction only, and it is
    worth being precise about which. Dropping an axis never *loses* a dominance
    relation: if `a` beats `b` on every objective it beats it on every subset of
    them. What dropping an axis does is *invent* relations -- `a` looks better
    than `b` once the axis on which `b` wins is not being looked at.

    So delta follows Brockhoff & Zitzler's definition: a subset S is
    delta-nonconflicting with the full set F when every pair that S orders is
    also ordered by F to within delta. For each pair (x, y) where x dominates y
    on S, delta must cover the largest amount by which x is *worse* than y on
    any objective in F:

        delta(S) = max over such pairs of  max over j in F of  f_j(x) - f_j(y)

    clipped at zero. delta = 0 means the subset invents nothing and the axis was
    genuinely redundant. A large delta is the price of the smaller chart, in
    min-max-normalised objective units, and printing it is what turns "we
    dropped an axis" into a sentence with a number in it.
    """
    live = usable_rows(matrix)
    if len(live) < 2:
        return {"available": False, "reason": "fewer than 2 complete cells"}
    rows = {i: [float(v) for v in matrix[i]] for i in live}  # type: ignore[arg-type]
    d = len(names)
    full = list(range(d))
    full_pairs = [
        (a, b) for a in live for b in live if a != b and dominates(rows[a], rows[b])
    ]

    def delta_of(subset: list[int]) -> float:
        if not subset:
            # An empty subset orders every pair, so it must cover every gap.
            return max(
                (
                    rows[a][j] - rows[b][j]
                    for a in live
                    for b in live
                    if a != b
                    for j in full
                ),
                default=0.0,
            )
        worst = 0.0
        for a in live:
            for b in live:
                if a == b:
                    continue
                sub_a = [rows[a][j] for j in subset]
                sub_b = [rows[b][j] for j in subset]
                if not dominates(sub_a, sub_b):
                    continue
                # a is ordered above b by the subset. What does the full set say?
                need = max(rows[a][j] - rows[b][j] for j in full)
                worst = max(worst, max(0.0, need))
        return worst

    ladder = []
    chosen: list[int] = []
    remaining = list(full)
    while remaining:
        best = min(remaining, key=lambda j: delta_of(chosen + [j]))
        chosen.append(best)
        remaining.remove(best)
        ladder.append(
            {
                "size": len(chosen),
                "subset": [names[j] for j in chosen],
                "delta": delta_of(chosen),
            }
        )
    smallest_exact = next(
        (rung["subset"] for rung in ladder if rung["delta"] <= 1e-9), None
    )
    return {
        "available": True,
        "dominating_pairs_full_set": len(full_pairs),
        "ladder": ladder,
        "smallest_exact_subset": smallest_exact,
        "note": "delta is in min-max-normalised objective units. It is the largest "
        "amount by which a cell the subset ranks above another is actually worse on "
        "some objective the subset dropped. delta 0 means the subset invents no "
        "ordering the full set does not already have.",
    }


# -- stage 6: uncertainty ---------------------------------------------------


def scale_to_reference(values: list[float | None], reference: list[float | None]) -> list[float | None]:
    present = [v for v in reference if v is not None]
    if not present:
        return [None for _ in values]
    lo, hi = min(present), max(present)
    span = hi - lo if hi != lo else max(1.0, abs(lo))
    offset = 0.0 if hi != lo else 0.5
    return [None if v is None else offset + (v - lo) / span for v in values]


def bootstrap_replicates(
    cells: list[Cell],
    objectives: list[F.Metric],
    replicates: int,
    rng: random.Random,
) -> list[dict[tuple[str, str], list[float | None]]]:
    """Resample builds with formula and chart scales fixed to the observed run."""
    names = [m.name for m in objectives]
    fixed = {}
    references = {}
    for metric in objectives:
        formula = metric.formula
        assert formula is not None
        first = [formula.evaluate(c.builds) for c in cells]
        fixed[metric.name] = F.norm_params_from_deferred(formula, [r.deferred for r in first])
        original = [formula.evaluate(c.builds, fixed[metric.name]) for c in cells]
        references[metric.name] = [None if r.value is None else metric.sign * r.value for r in original]
    out: list[dict[tuple[str, str], list[float | None]]] = []
    for _ in range(replicates):
        resampled = []
        for cell in cells:
            if cell.n_builds == 0:
                resampled.append([])
                continue
            picks = [
                cell.builds[rng.randrange(cell.n_builds)] for _ in range(cell.n_builds)
            ]
            resampled.append(picks)
        values: dict[str, list[float | None]] = {}
        for metric in objectives:
            assert metric.formula is not None
            results = [metric.formula.evaluate(builds, fixed[metric.name]) for builds in resampled]
            raw = [r.value for r in results]
            if metric.sign < 0:
                raw = [None if v is None else -v for v in raw]
            values[metric.name] = scale_to_reference(raw, references[metric.name])
        out.append(
            {
                cell.key: [values[n][i] for n in names]
                for i, cell in enumerate(cells)
            }
        )
    return out


def parametric_replicates(
    cells: list[Cell],
    objective_names: list[str],
    standard_errors: dict[tuple[str, str], dict[str, float]],
    replicates: int,
    rng: random.Random,
) -> list[dict[tuple[str, str], list[float | None]]]:
    """Normal draws, bounded by the observed range extended by three SEs."""
    support = {}
    for name in objective_names:
        present = [c.objectives.get(name) for c in cells]
        present = [v for v in present if v is not None]
        support[name] = (min(present), max(present)) if present else (None, None)
    out = []
    for _ in range(replicates):
        raw: dict[str, list[float | None]] = {n: [] for n in objective_names}
        for cell in cells:
            for name in objective_names:
                point = cell.objectives.get(name)
                se = (standard_errors.get(cell.key) or {}).get(name)
                if point is None:
                    raw[name].append(None)
                elif se is None:
                    raw[name].append(None)
                elif se == 0:
                    raw[name].append(point)
                else:
                    draw = rng.gauss(point, se)
                    lo, hi = support[name]
                    if lo is not None:
                        draw = max(lo - 3 * se, min(hi + 3 * se, draw))
                    raw[name].append(draw)
        scaled = {n: scale_to_reference(raw[n], [c.objectives.get(n) for c in cells]) for n in objective_names}
        out.append(
            {
                cell.key: [scaled[n][i] for n in objective_names]
                for i, cell in enumerate(cells)
            }
        )
    return out


def front_probabilities(
    keys: list[tuple[str, str]],
    replicates: list[dict[tuple[str, str], list[float | None]]],
) -> tuple[dict[tuple[str, str], float | None], dict[str, dict[str, float]], list[float] | None]:
    """P(on front), the probabilistic dominance matrix, and epsilon from the CIs.

    One bootstrap, three uses -- the epsilon for stage 5 rung 2 is the CI
    half-width per objective, used as a box resolution rather than a
    significance threshold.
    """
    if not replicates:
        return {k: None for k in keys}, {}, None
    counts = {k: 0 for k in keys}
    usable = {k: 0 for k in keys}
    dominance = {k: {j: 0 for j in keys if j != k} for k in keys}
    comparable = {k: {j: 0 for j in keys if j != k} for k in keys}
    # Per cell, per objective, the cell's own replicate values. epsilon comes
    # from these: the noise on one cell's measurement of one objective, not the
    # noise on the median across cells, which is a different and much smaller
    # quantity and would make epsilon-dominance a no-op.
    per_cell: dict[tuple[str, str], list[list[float]]] = {k: [] for k in keys}
    for replicate in replicates:
        matrix = [replicate.get(k) or [] for k in keys]
        complete = [
            i for i, row in enumerate(matrix) if row and all(v is not None for v in row)
        ]
        rows = {i: [float(v) for v in matrix[i]] for i in complete}  # type: ignore[arg-type]
        for i in complete:
            usable[keys[i]] += 1
        front = [
            i
            for i in complete
            if not any(dominates(rows[j], rows[i]) for j in complete if j != i)
        ]
        for i in front:
            counts[keys[i]] += 1
        for i in complete:
            per_cell[keys[i]].append(rows[i])
            for j in complete:
                if i == j:
                    continue
                comparable[keys[i]][keys[j]] += 1
                if dominates(rows[i], rows[j]):
                    dominance[keys[i]][keys[j]] += 1
    p_on_front = {
        k: (counts[k] / usable[k] if usable[k] else None) for k in keys
    }
    prob_dominance = {
        f"{a[0]}/{a[1]}": {
            f"{b[0]}/{b[1]}": (
                dominance[a][b] / comparable[a][b] if comparable[a][b] else None
            )
            for b in keys
            if b != a
        }
        for a in keys
    }
    epsilon = None
    widths: dict[int, list[float]] = {}
    for draws in per_cell.values():
        if len(draws) < 10:  # too few replicates for a 95% interval to mean much
            continue
        for j in range(len(draws[0])):
            column = sorted(row[j] for row in draws)
            lo = column[int(0.025 * (len(column) - 1))]
            hi = column[int(0.975 * (len(column) - 1))]
            widths.setdefault(j, []).append((hi - lo) / 2)
    if widths:
        # Median over cells of each cell's own half-width: one number per
        # objective, robust to the one cell with three builds in it.
        epsilon = [
            max(1e-9, F.median(widths[j]) or 0.0) for j in sorted(widths)
        ]
    return p_on_front, prob_dominance, epsilon


# -- stage 7: time ----------------------------------------------------------


def direction_of_travel(cells: list[Cell], names: list[str]) -> list[dict]:
    """Displacement between consecutive windows in min-max-normalised space, with
    the angle to the ideal point. Improving on every objective gives a small
    angle; trading debt for speed gives a large one, and the number says which
    trade. Cheapest interpretable output in the tool."""
    out = []
    by_group: dict[str, list[Cell]] = {}
    for cell in cells:
        by_group.setdefault(cell.group, []).append(cell)
    for group, group_cells in by_group.items():
        ordered = sorted(group_cells, key=lambda c: window_sort_key(c.window))
        for prev, curr in zip(ordered, ordered[1:]):
            a = [prev.norm.get(n) for n in names]
            b = [curr.norm.get(n) for n in names]
            if any(v is None for v in a + b):
                out.append(
                    {
                        "group": group,
                        "from": prev.window,
                        "to": curr.window,
                        "available": False,
                        "reason": "an objective is unobserved in one of the windows",
                    }
                )
                continue
            delta = [float(bv) - float(av) for av, bv in zip(a, b)]  # type: ignore[arg-type]
            magnitude = math.sqrt(sum(x * x for x in delta))
            # The ideal point is the origin in min-max minimise-space, so the
            # direction of improvement from the earlier cell is -a.
            toward = [-float(av) for av in a]  # type: ignore[arg-type]
            toward_norm = math.sqrt(sum(x * x for x in toward))
            if magnitude == 0 or toward_norm == 0:
                angle = None
            else:
                cosine = sum(x * y for x, y in zip(delta, toward)) / (
                    magnitude * toward_norm
                )
                angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
            out.append(
                {
                    "group": group,
                    "from": prev.window,
                    "to": curr.window,
                    "available": True,
                    "delta": {n: d for n, d in zip(names, delta)},
                    "magnitude": magnitude,
                    "angle_to_ideal_deg": angle,
                    "reading": _travel_reading(angle),
                }
            )
    return out


def _travel_reading(angle: float | None) -> str:
    if angle is None:
        return "no movement"
    if angle < 45:
        return "improving on the objectives together"
    if angle < 90:
        return "improving on balance, with a trade"
    if angle < 135:
        return "trading one objective against another, net away from ideal"
    return "worse on the objectives together"


def mix_movement(cells: list[Cell]) -> list[dict]:
    """Aitchison distance between consecutive windows, plus the top log-ratio."""
    out = []
    by_group: dict[str, list[Cell]] = {}
    for cell in cells:
        by_group.setdefault(cell.group, []).append(cell)
    for group, group_cells in by_group.items():
        ordered = sorted(group_cells, key=lambda c: window_sort_key(c.window))
        for prev, curr in zip(ordered, ordered[1:]):
            a = clr(prev.impact_mix, prev.n_builds)
            b = clr(curr.impact_mix, curr.n_builds)
            if a is None or b is None:
                out.append(
                    {
                        "group": group,
                        "from": prev.window,
                        "to": curr.window,
                        "available": False,
                        "reason": "an empty composition",
                    }
                )
                continue
            distance, top = aitchison_distance(a, b)
            zeros_prev = [k for k, v in prev.impact_mix.items() if v == 0]
            zeros_curr = [k for k, v in curr.impact_mix.items() if v == 0]
            out.append(
                {
                    "group": group,
                    "from": prev.window,
                    "to": curr.window,
                    "available": True,
                    "aitchison_distance": distance,
                    "top_log_ratio": top,
                    "zero_replacement": {
                        "delta_from": zero_replacement_delta(prev.impact_mix, prev.n_builds),
                        "delta_to": zero_replacement_delta(curr.impact_mix, curr.n_builds),
                        "parts_replaced_from": zeros_prev,
                        "parts_replaced_to": zeros_curr,
                    },
                }
            )
    return out


def attainment_bands(
    keys: list[tuple[str, str]],
    replicates: list[dict[tuple[str, str], list[float | None]]],
    names: list[str],
    pairs: list[tuple[int, int]],
) -> dict:
    """Empirical attainment function over the bootstrap replicates, per 2D
    projection (Fonseca, Guerreiro, Lopez-Ibanez & Paquete, EMO 2011).

    With bootstrap on, 50% and 90% bands replace the single staircase. It is the
    honest version of "the front moved".
    """
    if not replicates:
        return {"available": False, "reason": "no replicates"}
    sample = replicates[:ATTAINMENT_REPLICATES]
    out = {}
    for (xi, yi) in pairs:
        curves = []
        for replicate in sample:
            points: list[tuple[float, float]] = []
            for row in (replicate.get(k) for k in keys):
                if row and row[xi] is not None and row[yi] is not None:
                    points.append((float(row[xi]), float(row[yi])))
            curves.append(sorted(points))
        if not curves:
            continue
        xs = sorted({x for curve in curves for x, _ in curve})
        if len(xs) > ATTAINMENT_GRID:
            step = len(xs) / ATTAINMENT_GRID
            xs = [xs[min(len(xs) - 1, int(i * step))] for i in range(ATTAINMENT_GRID)]
        band = []
        for x in xs:
            attained = []
            for curve in curves:
                ys = [y for px, y in curve if px <= x]
                if ys:
                    attained.append(min(ys))
                else:
                    attained.append(math.inf)
            if not attained:
                continue
            attained.sort()
            def boundary(q):
                value = attained[max(0, math.ceil(q * len(attained)) - 1)]
                return value if math.isfinite(value) else None
            band.append(
                {
                    "x": x,
                    "p50": boundary(0.5),
                    "p90": boundary(0.9),
                    "p10": boundary(0.1),
                }
            )
        out[f"{names[xi]}|{names[yi]}"] = band
    return {"available": True, "replicates_used": len(sample), "bands": out}


# ---------------------------------------------------------------------------
# Comparisons: which set of cells is a front computed over
# ---------------------------------------------------------------------------


def front_block(
    cells: list[Cell],
    indices: list[int],
    names: list[str],
    params: ComputeParams,
    rng: random.Random,
    replicates: list[dict[tuple[str, str], list[float | None]]],
    epsilon: list[float] | None,
    check_lead_time: bool = False,
) -> dict:
    """Stages 4-6 over one comparison set."""
    sub_cells = [cells[i] for i in indices]
    matrix = objective_matrix(sub_cells, names, "norm")
    keys = [c.key for c in sub_cells]
    m = len(names)
    n = len(usable_rows(matrix))

    refusals: list[dict] = []
    cautions: list[dict] = []
    if check_lead_time:
        refusals, cautions = lead_time_source_check(sub_cells, params)
    if n < m + 1:
        refusals.append(
            {
                "code": "TOO_FEW_CELLS",
                "message": f"{n} complete cells for {m} objectives; a front needs "
                f"at least m + 1 = {m + 1}. Refusing to draw one.",
            }
        )
    elif n < 2 * m:
        cautions.append(
            {
                "code": "PROVISIONAL_FRONT",
                "message": f"{n} complete cells for {m} objectives; below 2m = {2 * m} "
                f"the front is provisional.",
            }
        )

    block: dict = {
        "cells": [c.label for c in sub_cells],
        "objectives": names,
        "refusals": refusals,
        "cautions": cautions,
    }
    if refusals:
        # Refusing to draw a front means not emitting one. A front computed and
        # labelled "refused" would be read off the page by someone, and the
        # renderer would have to be trusted not to draw it.
        block["front_not_drawn"] = True
        return block

    layers = nondominated_layers(matrix)
    front = [i for i, layer in enumerate(layers) if layer == 1]
    block["layers"] = {c.label: layers[i] for i, c in enumerate(sub_cells)}
    block["front"] = [sub_cells[i].label for i in front]
    block["degeneracy_layer1_fraction"] = (len(front) / n) if n else None

    block["null_model"] = permutation_null(matrix, params.permutations, rng)

    sub_replicates = [
        {k: r[k] for k in keys if k in r} for r in replicates
    ] if replicates else []
    p_on_front, prob_dominance, boot_epsilon = front_probabilities(keys, sub_replicates)
    block["p_on_front"] = {c.label: p_on_front.get(c.key) for c in sub_cells}
    block["probabilistic_dominance"] = prob_dominance

    eps = epsilon
    if params.epsilon_source == "manual" and params.epsilon_manual:
        eps = params.epsilon_manual
    elif params.epsilon_source == "bootstrap":
        eps = boot_epsilon or epsilon
    elif params.epsilon_source == "none":
        eps = None
    ladder: dict = {}
    if eps:
        eps_front = epsilon_dominance_front(matrix, eps)
        ladder["epsilon_dominance"] = {
            "epsilon": {n_: e for n_, e in zip(names, eps)},
            "epsilon_source": params.epsilon_source,
            "front": [sub_cells[i].label for i in eps_front],
            "size": len(eps_front),
            "note": "epsilon is the bootstrap CI half-width per objective; dominance "
            "on epsilon boxes is a resolution heuristic, not a significance test",
        }
    k_sweep = []
    for k in range(m, 1, -1):
        skyline, cycles = k_dominance_front(matrix, k)
        k_sweep.append(
            {
                "k": k,
                "size": len(skyline),
                "front": [sub_cells[i].label for i in skyline],
                "cycles": [[sub_cells[i].label for i in cycle] for cycle in cycles],
            }
        )
    ladder["k_dominance"] = {
        "sweep": k_sweep,
        "note": "k-dominance is not transitive, so the result is a set and not an "
        "order; cycles are reported rather than silently broken",
    }
    counts, subspaces = skyline_frequency(matrix)
    ladder["skyline_frequency"] = {
        "subspaces": subspaces,
        "scores": {sub_cells[i].label: counts[i] for i in counts},
        "note": f"non-dominated in how many of the {subspaces} non-empty objective "
        f"subspaces; a continuous interestingness score, not a binary",
    }
    ladder["knees"] = _relabel_knees(knees(matrix, front, rng), sub_cells)
    weights = params.weights or [1.0 / m] * m
    weighted = {}
    for i, cell in enumerate(sub_cells):
        row = matrix[i]
        if any(v is None for v in row):
            weighted[cell.label] = None
        else:
            weighted[cell.label] = sum(w * float(v) for w, v in zip(weights, row))  # type: ignore[arg-type]
    rank_matrix = objective_matrix(sub_cells, names, "rank")
    average_rank = {}
    for i, cell in enumerate(sub_cells):
        row = [v for v in rank_matrix[i] if v is not None]
        average_rank[cell.label] = sum(row) / len(row) if row else None
    ladder["weighted_sum"] = {
        "weights": {n_: w for n_, w in zip(names, weights)},
        "scores": weighted,
        "average_rank": average_rank,
        "warning": "A weighted sum is a claim about how many dollars a month of "
        "lead time is worth. Equal weights are also such a claim. The weights are "
        "printed on every chart that uses them.",
    }
    block["relaxation_ladder"] = ladder

    block["level_diagram"] = {
        sub_cells[i].label: v
        for i, v in distance_to_ideal(matrix, list(range(len(sub_cells)))).items()
    }
    front_points = [
        [float(v) for v in matrix[i] if v is not None]
        for i in front
        if all(x is not None for x in matrix[i])
    ]
    block["hypervolume"] = hypervolume(front_points, HV_REFERENCE, rng)
    block["attainment"] = attainment_bands(
        keys,
        sub_replicates,
        names,
        [(a, b) for a in range(m) for b in range(a + 1, m)],
    )
    return block


def _relabel_knees(knee_block: dict, sub_cells: list[Cell]) -> dict:
    if not knee_block.get("available"):
        return knee_block
    out = dict(knee_block)
    out["scores"] = {sub_cells[i].label: v for i, v in knee_block["scores"].items()}
    out["marginal_utility"] = {
        sub_cells[i].label: v for i, v in knee_block["marginal_utility"].items()
    }
    if knee_block.get("knee") is not None:
        out["knee"] = sub_cells[knee_block["knee"]].label
    return out


# ---------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------


def roles_hash(metrics: list[F.Metric]) -> str:
    payload = json.dumps(
        [[m.name, m.role, m.direction, m.version, m.expression] for m in sorted(metrics, key=lambda x: x.name)],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def compute(
    rows: list[dict],
    metrics: list[F.Metric],
    params: ComputeParams,
    run_id: str = "r-adhoc",
    standard_errors: dict[tuple[str, str], dict[str, float]] | None = None,
    aggregate_rows: list[dict] | None = None,
) -> dict:
    """Stages 0-7. One analysis dict, which is the only thing render.mjs reads."""
    rng = random.Random(params.seed)
    by_name = {m.name: m for m in metrics}
    missing = [n for n in params.objectives if n not in by_name]
    if missing:
        raise ValueError(
            f"unknown metric(s): {', '.join(missing)}. Known: "
            f"{', '.join(sorted(by_name))}"
        )

    # -- stage 0 ------------------------------------------------------------
    objectives = [by_name[n] for n in params.objectives]
    for metric in objectives:
        if metric.role != "objective":
            raise ValueError(
                f"{metric.name} has role '{metric.role}'; only objectives enter dominance"
            )
    descriptors = [m for m in metrics if m.role == "descriptor"]
    refusals: list[dict] = []
    if len(objectives) > MAX_OBJECTIVES:
        refusals.append(
            {
                "code": "TOO_MANY_OBJECTIVES",
                "message": f"{len(objectives)} objectives; the cap is {MAX_OBJECTIVES}. "
                f"Use stage 3 objective reduction rather than a bigger chart.",
            }
        )
    if len(objectives) < MIN_OBJECTIVES:
        refusals.append(
            {
                "code": "TOO_FEW_OBJECTIVES",
                "message": f"{len(objectives)} objective(s); that is a ranking, not a front.",
            }
        )
    overlaps = F.overlap_report(objectives)
    stage0 = {
        "objectives": [m.as_json() for m in objectives],
        "descriptors": [m.as_json() for m in descriptors],
        "overlaps": [o.line() for o in overlaps],
        "overlap_detail": [
            {"metric": o.metric, "kind": o.kind, "shared": o.shared} for o in overlaps
        ],
        "greyed_spokes": F.greyed_spokes(overlaps),
        "note": "computed from the declared formulas, before any data was read",
    }
    if refusals:
        return {
            "run": {
                "run_id": run_id,
                "ts": S.now_iso(),
                "objectives": params.objectives,
                "roles_hash": roles_hash(metrics),
                "metric_versions": {m.name: m.version for m in metrics},
                "metric_definitions": [m.as_json() for m in metrics],
                "params": params.as_json(),
            },
            "stage0": stage0,
            "cells": [],
            "diagnostics": {"refusals": refusals, "cautions": [], "warnings": []},
        }

    # -- builds, gate, cells -----------------------------------------------
    if aggregate_rows is None:
        builds, gate, windows, warnings = prepare_builds(
            rows, window_spec=params.window_spec, windows_retained=params.windows_retained,
            L_days=params.L_days, u=params.u, as_of=params.as_of, baseline=params.baseline,
        )
        cells = build_cells(builds)
        for cell in cells:
            cell.impact_mix = composition(cell.builds, "impact", IMPACT_CLASSES)
            cell.question_mix = composition(cell.builds, "question", QUESTION_CLASSES)
    else:
        cells, standard_errors, windows, warnings = prepare_aggregate_cells(aggregate_rows, metrics, params)
        gate = GateParams(None, "UNAVAILABLE: aggregate input has no build usage gate",
                          params.L_days, params.as_of or datetime.now(timezone.utc))

    cautions: list[dict] = []
    lead_time_used = any(
        "lead_time_days" in (m.formula.deps.columns if m.formula else set())
        for m in objectives
    )
    # -- stage 1: evaluate, then normalise ---------------------------------
    normalisation_notes = evaluate_cells(cells, list(by_name.values())) if aggregate_rows is None else []
    flip_to_minimise(cells, objectives)
    names = [m.name for m in objectives]
    normalisation = {}
    for name in names:
        raw = [cell.objectives.get(name) for cell in cells]
        scaled, lo, hi = minmax_scale(raw)
        ranked = average_ranks(raw)
        for cell, s, r in zip(cells, scaled, ranked):
            cell.norm[name] = s
            cell.rank[name] = r
        normalisation[name] = {
            "lo": lo,
            "hi": hi,
            "degenerate": lo == hi and lo is not None,
            "missing_cells": sum(1 for v in raw if v is None),
        }
    for name in [m.name for m in descriptors]:
        raw = [cell.descriptors.get(name) for cell in cells]
        scaled, lo, hi = minmax_scale(raw)
        for cell, s in zip(cells, scaled):
            cell.norm[name] = s

    stage1 = {
        "normalisation": normalisation,
        "formula_normalisation": normalisation_notes,
        "invariance_note": "Dominance is invariant under any monotone per-objective "
        "transform, so front membership does not depend on this choice. Knees, "
        "hypervolume, distance-to-ideal and every glyph radius do.",
    }

    # -- stage 2 -----------------------------------------------------------
    stage2 = conflict_screen(cells, names, [m.name for m in descriptors])

    # -- stage 3 -----------------------------------------------------------
    pooled = objective_matrix(cells, names, "norm")
    stage3 = {
        "greedy_reduction": greedy_reduction(names, stage2["verdicts"]),
        "delta_moss": delta_moss(pooled, names),
        "pca_note": "PCA-based reduction (Deb & Saxena 2006) is documented and not "
        "implemented: it produces axes with no physical meaning, which destroys "
        "the point of axes called cost, lead time and debt.",
    }

    # -- stage 6 replicates, computed once and shared ----------------------
    plotted = [c for c in cells if c.n_builds >= params.min_builds]
    below_floor = [c.label for c in cells if c.n_builds < params.min_builds]
    if below_floor:
        cautions.append(
            {
                "code": "BELOW_BUILD_FLOOR",
                "message": f"{len(below_floor)} cell(s) below the floor of "
                f"{params.min_builds} builds: reported, not plotted.",
                "cells": below_floor,
            }
        )
    unclassified = {
        cell.label: (unclassified_share(cell.builds, "question", QUESTION_CLASSES)
                     if cell.aggregate_n is None else
                     max(0.0, 1 - sum(cell.question_mix.values())) if cell.question_mix else None)
        for cell in cells
    }
    unclassified = {k: v for k, v in unclassified.items() if v}
    if unclassified:
        cautions.append(
            {
                "code": "QUESTION_UNCLASSIFIED",
                "message": "these cells carry builds whose `question` is outside "
                + ", ".join(QUESTION_CLASSES)
                + ": "
                + ", ".join(f"{k} ({v:.0%})" for k, v in sorted(unclassified.items()))
                + ". Their question mix sums to less than 1 by exactly that "
                "share, and is not renormalised to close the gap.",
                "cells": sorted(unclassified),
            }
        )

    uncertainty_mode = "bootstrap over builds"
    replicates: list[dict[tuple[str, str], list[float | None]]] = []
    if params.bootstrap > 0:
        if aggregate_rows is not None or standard_errors is not None:
            uncertainty_mode = "parametric bootstrap over supplied standard errors"
            missing_se = [c.label for c in cells if any(
                (standard_errors or {}).get(c.key, {}).get(n) is None for n in names
            )]
            if missing_se:
                uncertainty_mode = "UNAVAILABLE"
                cautions.append({"code": "UNCERTAINTY_UNAVAILABLE", "message":
                    "Standard errors are missing for one or more objectives in: " + ", ".join(missing_se)
                    + ". Comparison probabilities and attainment bands are unavailable."})
            else:
                replicates = parametric_replicates(cells, names, standard_errors, params.bootstrap, rng)
        elif all(c.n_builds >= 2 for c in cells):
            replicates = bootstrap_replicates(cells, objectives, params.bootstrap, rng)
        else:
            singles = [c.label for c in cells if c.n_builds < 2]
            uncertainty_mode = "UNAVAILABLE"
            cautions.append(
                {
                    "code": "UNCERTAINTY_UNAVAILABLE",
                    "message": "cells with fewer than 2 builds cannot be resampled: "
                    + ", ".join(singles)
                    + ". P(on front) is null for every cell, not zero.",
                }
            )
    else:
        uncertainty_mode = "disabled (--bootstrap 0)"

    _, _, epsilon = front_probabilities([c.key for c in cells], replicates)

    # -- comparisons -------------------------------------------------------
    groups = sorted({c.group for c in cells})
    comparisons = []
    plotted_keys = {c.key for c in plotted}
    index_of = {c.key: i for i, c in enumerate(cells)}

    if len(groups) > 1:
        # Across groups, per window: this is what the membership timeline and
        # persistence are computed from.
        for window in windows:
            indices = [
                index_of[c.key]
                for c in cells
                if c.window == window and c.key in plotted_keys
            ]
            if not indices:
                continue
            comparisons.append(
                {
                    "kind": "across_groups",
                    "label": f"groups within {window}",
                    "window": window,
                    "question": "which team is not beaten by another team on every "
                    "objective at once, in this window",
                    **front_block(cells, indices, names, params, rng, replicates, epsilon, lead_time_used),
                }
            )
    for group in groups:
        indices = [
            index_of[c.key] for c in cells if c.group == group and c.key in plotted_keys
        ]
        if not indices:
            continue
        comparisons.append(
            {
                "kind": "within_group",
                "label": f"{group} across its windows",
                "group": group,
                "question": "which of our past windows is not beaten by another "
                "window of ours on every objective at once",
                **front_block(cells, indices, names, params, rng, replicates, epsilon, lead_time_used),
            }
        )
    primary_kind = "across_groups" if len(groups) > 1 else "within_group"
    for comparison in comparisons:
        for source, target in (("refusals", refusals), ("cautions", cautions)):
            for entry in comparison[source]:
                if entry["code"] in ("DIVERGENT_LEAD_TIME_SOURCE", "MIXED_LEAD_TIME_SOURCE"):
                    target.append({**entry, "comparison": comparison["label"]})

    # Canonical layer and P(on front) per cell come from the primary comparison,
    # so a cell carries the answer to the question its mode is actually asking.
    for comparison in comparisons:
        if comparison["kind"] != primary_kind:
            continue
        for cell in cells:
            if cell.label in comparison.get("layers", {}):
                cell_layer = comparison["layers"][cell.label]
                setattr(cell, "_layer", cell_layer)
                setattr(cell, "_p_on_front", comparison.get("p_on_front", {}).get(cell.label))

    # -- stage 7 -----------------------------------------------------------
    persistence = {}
    if primary_kind == "across_groups":
        for group in groups:
            windows_seen = [
                c for c in comparisons if c["kind"] == "across_groups" and not c.get("front_not_drawn")
            ]
            on = sum(
                1
                for c in windows_seen
                if any(cell.group == group and cell.label in c.get("front", []) for cell in cells)
            )
            considered = sum(
                1
                for c in windows_seen
                if any(cell.group == group and c.get("layers", {}).get(cell.label) is not None for cell in cells)
            )
            persistence[group] = {
                "windows_on_front": on,
                "windows_considered": considered,
                "fraction": (on / considered) if considered else None,
            }
    else:
        persistence = {
            "note": "self-comparison mode: persistence across groups is not "
            "applicable with a single group. The within-group front over windows "
            "is the answer instead."
        }

    stage7 = {
        "persistence": persistence,
        "direction_of_travel": direction_of_travel(plotted, names),
        "mix_movement": mix_movement(plotted),
        "hypervolume_note": f"reference point {HV_REFERENCE} per axis in min-max "
        "normalised minimise-space; the ranking a hypervolume produces depends on "
        "this choice, so it is part of the result",
    }

    # -- threshold sensitivity ---------------------------------------------
    sensitivity = (threshold_sensitivity(rows, params, gate) if aggregate_rows is None else
                   {"available": False, "reason": "aggregate input has no build-level usage signal"})

    # -- assemble ----------------------------------------------------------
    cube_ok = len(groups) <= CUBE_GROUP_CAP and len(windows) <= CUBE_WINDOW_CAP + 2
    if not cube_ok:
        cautions.append(
            {
                "code": "CUBE_CLUTTER_CAP",
                "message": f"{len(groups)} groups x {len(windows)} windows exceeds the "
                f"cube cap of {CUBE_GROUP_CAP} x {CUBE_WINDOW_CAP}; split out the "
                f"largest group or narrow the window range. The 2D companions still "
                f"draw everything.",
            }
        )

    analysis = {
        "run": {
            "run_id": run_id,
            "ts": S.now_iso(),
            "objectives": names,
            "roles_hash": roles_hash(metrics),
            "metric_versions": {m.name: m.version for m in metrics},
            "metric_definitions": [m.as_json() for m in metrics],
            "params": {**params.as_json(), **gate.as_json()},
            "tool": "decision-surface",
            "input_mode": "builds" if aggregate_rows is None else "aggregate",
        },
        "schema": {
            "impact_classes": IMPACT_CLASSES,
            "question_classes": QUESTION_CLASSES,
        },
        "gate": gate.as_json(),
        "windows": windows,
        "groups": groups,
        "mode": "self-comparison over windows" if len(groups) == 1 else "groups and windows",
        "primary_comparison": primary_kind,
        "stage0": stage0,
        "stage1": stage1,
        "stage2": stage2,
        "stage3": stage3,
        "stage6": {
            "mode": uncertainty_mode,
            "replicates": len(replicates),
            "epsilon": {n: e for n, e in zip(names, epsilon)} if epsilon else None,
            "note": "P(on front) is reported instead of a flag; a cell that cannot "
            "be resampled is null, not zero.",
        },
        "stage7": stage7,
        "sensitivity": sensitivity,
        "comparisons": comparisons,
        "cells": [
            {
                "group": cell.group,
                "window": cell.window,
                "label": cell.label,
                "n_builds": cell.n_builds,
                "below_floor": cell.n_builds < params.min_builds,
                "objectives": cell.objectives,
                "objectives_norm": {n: cell.norm.get(n) for n in names},
                "objectives_rank": {n: cell.rank.get(n) for n in names},
                "descriptors": cell.descriptors,
                "descriptors_norm": {
                    m.name: cell.norm.get(m.name) for m in descriptors
                },
                "impact_mix": cell.impact_mix,
                "question_mix": cell.question_mix,
                "question_unclassified_share": unclassified_share(
                    cell.builds, "question", QUESTION_CLASSES
                ) if cell.aggregate_n is None else (
                    max(0.0, 1 - sum(cell.question_mix.values())) if cell.question_mix else None),
                "gate_unavailable_share": (
                    sum(1 for b in cell.builds if b.get("gate_unavailable"))
                    / cell.n_builds
                    if cell.n_builds and cell.aggregate_n is None
                    else None
                ),
                "lead_time_sources": sorted(lead_time_sources(cell)),
                "lead_time_fallback_share": fallback_share(cell),
                "layer": getattr(cell, "_layer", None),
                "p_on_front": getattr(cell, "_p_on_front", None),
                "notes": sorted(set(cell.notes)),
            }
            for cell in cells
        ],
        "diagnostics": {
            "refusals": refusals,
            "cautions": cautions,
            "warnings": warnings,
            "cube_drawable": cube_ok,
        },
    }
    return analysis


def threshold_sensitivity(rows: list[dict], params: ComputeParams, gate: GateParams) -> dict:
    """Impact mix at 0.5u, u, 2u. A mix that flips between them is an artefact of
    the threshold, not a property of the team -- so the finding is provisional and
    the strip is drawn."""
    if gate.u is None:
        return {"available": False, "reason": gate.u_source}
    out = {}
    for label, factor in (("0.5u", 0.5), ("u", 1.0), ("2u", 2.0)):
        builds, _, _, _ = prepare_builds(
            rows,
            window_spec=params.window_spec,
            windows_retained=params.windows_retained,
            L_days=params.L_days,
            u=gate.u * factor,
            as_of=params.as_of or gate.as_of,
            baseline=params.baseline,
        )
        cells = build_cells(builds)
        out[label] = {
            "u": gate.u * factor,
            "overall": composition(builds, "impact", IMPACT_CLASSES),
            "per_cell": {
                cell.label: composition(cell.builds, "impact", IMPACT_CLASSES)
                for cell in cells
            },
        }
    flipped = []
    for cell_label in out["u"]["per_cell"]:
        series = [out[k]["per_cell"].get(cell_label, {}) for k in ("0.5u", "u", "2u")]
        for part in IMPACT_CLASSES:
            values = [s.get(part, 0.0) for s in series]
            if max(values) - min(values) >= 0.10:
                flipped.append({"cell": cell_label, "part": part, "values": values})
    return {
        "available": True,
        "u_source": gate.u_source,
        "levels": out,
        "flips": flipped,
        "verdict": (
            "PROVISIONAL: the impact mix moves by 10 points or more between 0.5u "
            "and 2u, so it is a statement about the threshold as much as the team"
            if flipped
            else "STABLE: the impact mix holds across 0.5u, u and 2u"
        ),
    }


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def explain_cell(analysis: dict, label: str) -> dict:
    """Why is this cell not on the front. The question a team actually asks, and
    a few lines given the dominance relation."""
    cells = {c["label"]: c for c in analysis["cells"]}
    if label not in cells:
        raise KeyError(
            f"no such cell: {label}. Known: {', '.join(sorted(cells))}"
        )
    target = cells[label]
    names = analysis["run"]["objectives"]
    out: dict = {
        "cell": label,
        "n_builds": target["n_builds"],
        "objectives": target["objectives"],
        "layer": target["layer"],
        "p_on_front": target["p_on_front"],
        "impact_mix": target["impact_mix"],
        "gate_unavailable_share": target["gate_unavailable_share"],
        "comparisons": [],
    }
    for comparison in analysis.get("comparisons", []):
        if label not in comparison.get("cells", []):
            continue
        mine = [target["objectives_norm"].get(n) for n in names]
        entry: dict = {
            "kind": comparison["kind"],
            "label": comparison["label"],
            "on_front": label in comparison.get("front", []),
            "layer": comparison.get("layers", {}).get(label),
            "p_on_front": comparison.get("p_on_front", {}).get(label),
            "dominated_by": [],
        }
        if comparison.get("front_not_drawn"):
            # There is no front in this comparison, so there is nothing to be
            # on or off. Saying "not on the front" here would answer a question
            # the tool refused to ask.
            entry["front_not_drawn"] = True
            entry["refusals"] = comparison.get("refusals", [])
            entry["reading"] = (
                f"No front was drawn for this comparison, so {label} is neither "
                f"on it nor off it: "
                + "; ".join(r["message"] for r in comparison.get("refusals", []))
            )
            out["comparisons"].append(entry)
            continue
        if any(v is None for v in mine):
            entry["note"] = (
                "an objective is unobserved for this cell, so it cannot be placed "
                "in a dominance order -- a null is not a zero"
            )
            out["comparisons"].append(entry)
            continue
        for other_label in comparison.get("cells", []):
            if other_label == label:
                continue
            other = cells[other_label]
            theirs = [other["objectives_norm"].get(n) for n in names]
            if any(v is None for v in theirs):
                continue
            if dominates([float(v) for v in theirs], [float(v) for v in mine]):  # type: ignore[arg-type]
                entry["dominated_by"].append(
                    {
                        "cell": other_label,
                        "margins": {
                            n: {
                                "theirs": other["objectives"].get(n),
                                "ours": target["objectives"].get(n),
                                "normalised_gap": (
                                    float(mine[i]) - float(theirs[i])  # type: ignore[arg-type]
                                ),
                            }
                            for i, n in enumerate(names)
                        },
                    }
                )
        if entry["dominated_by"]:
            binding = {}
            for n in names:
                gaps = [
                    d["margins"][n]["normalised_gap"] for d in entry["dominated_by"]
                ]
                binding[n] = max(gaps)
            worst = max(binding, key=lambda n: binding[n])
            nearest = min(
                entry["dominated_by"],
                key=lambda d: max(d["margins"][n]["normalised_gap"] for n in names),
            )
            entry["closest_dominator"] = {
                "cell": nearest["cell"],
                "largest_gap": max(
                    nearest["margins"][n]["normalised_gap"] for n in names
                ),
            }
            entry["binding_objective"] = worst
            entry["reading"] = (
                f"{len(entry['dominated_by'])} cell(s) beat {label} on every "
                f"objective at once; the largest normalised gap is on {worst}."
            )
        elif entry["on_front"]:
            entry["reading"] = f"{label} is on the front in this comparison."
        else:
            entry["reading"] = (
                f"{label} is not dominated by any single cell but sits on layer "
                f"{entry['layer']}, so it is dominated once earlier layers are peeled."
            )
        out["comparisons"].append(entry)
    return out


# ---------------------------------------------------------------------------
# metric loading
# ---------------------------------------------------------------------------


def seed_default_metrics(store: S.Store) -> list[str]:
    """Insert any shipped default the store does not already have, by name.

    Seeded per missing name rather than only into a wholly empty registry. A
    team whose first command is `surface define --name debt_mine ...` otherwise
    ends up with a registry of exactly one metric, and the next `compute` fails
    with "unknown metric: cost" -- which is a confusing way to discover that
    defining one metric silently opted you out of the other seven.

    Custom definitions stay in force. The unchanged shipped cost v1 is
    migrated by appending v2; the old definition remains available to old runs.
    """
    added = []
    for metric in F.DEFAULT_METRICS:
        current = store.latest_metric(metric.name)
        shipped_cost_v1 = current and metric.name == "cost" and (
            current["version"] == 1 and current["expression"] == "sum(cost_usd) / count()"
            and current["role"] == "objective" and current["direction"] == "min"
            and current["notes"] == metric.notes
        )
        if current is None or shipped_cost_v1:
            store.define_metric(
                metric.name,
                metric.role,
                metric.direction,
                metric.expression,
                metric.notes,
                version=metric.version,
            )
            added.append(metric.name)
    return added


def load_metrics(
    store: S.Store | None, metrics_csv: Path | None, seed_defaults: bool = True
) -> list[F.Metric]:
    """Registry, in precedence order: an explicit CSV, then the store, then the
    shipped defaults (which are seeded into a fresh store so a new team gets
    off-the-shelf definitions without waiting for anything)."""
    if metrics_csv:
        return F.read_metrics_csv(metrics_csv)
    if store is not None:
        if seed_defaults:
            seed_default_metrics(store)
        rows = store.latest_metrics()
        if rows:
            return [
                F.Metric(
                    r["name"],
                    r["role"],
                    r["direction"],
                    r["expression"],
                    int(r["version"]),
                    r["notes"] or "",
                ).compile()
                for r in rows
            ]
    return F.default_metrics()


def read_cells_csv(path: Path) -> list[dict]:
    """Read aggregate observations without inventing build records."""
    with open(path, newline="", encoding="utf-8") as handle:
        return [dict(r) for r in csv.DictReader(handle)]


def prepare_aggregate_cells(rows: list[dict], metrics: list[F.Metric], params: ComputeParams):
    cells, errors, warnings = [], {}, []
    seen = set()
    def number(row, name, required=False):
        value = row.get(name)
        if value is None or value == "":
            if required:
                raise ValueError(f"aggregate cell requires {name}")
            return None
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return value

    for row in rows:
        window = row.get("window")
        try:
            year, period = window_sort_key(window)
            if "-W" in window:
                datetime.fromisocalendar(year, period, 1)
                spec = "week"
            elif "-Q" in window:
                datetime(year, (period - 1) * 3 + 1, 1)
                spec = "quarter"
            else:
                datetime(year, period, 1)
                spec = "month"
            if spec != params.window_spec:
                raise ValueError("window type differs from --window")
        except (TypeError, AttributeError, ValueError) as exc:
            raise ValueError(f"invalid aggregate window {window!r}: {exc}") from exc
        count = number(row, "n_builds", required=True)
        if count <= 0 or not count.is_integer():
            raise ValueError("n_builds must be a positive integer")
        cell = Cell(str(row.get("group") or "all"), window, [], aggregate_n=int(count))
        if cell.key in seen:
            raise ValueError(f"duplicate aggregate cell: {cell.label}")
        seen.add(cell.key)
        errors[cell.key] = {}
        for metric in metrics:
            target = cell.objectives if metric.role == "objective" else cell.descriptors
            target[metric.name] = number(row, metric.name)
            se = number(row, metric.name + "_se")
            if se is not None:
                if se < 0:
                    raise ValueError(f"{metric.name}_se must be nonnegative")
                errors[cell.key][metric.name] = se
        for name in params.objectives:
            if name not in row:
                raise ValueError(f"aggregate input is missing objective column {name}")
        cell.aggregate_fallback = number(row, "lead_time_fallback_share")
        if cell.aggregate_fallback is not None and not 0 <= cell.aggregate_fallback <= 1:
            raise ValueError("lead_time_fallback_share must be between 0 and 1")
        for field_name, classes in (("impact_mix", IMPACT_CLASSES), ("question_mix", QUESTION_CLASSES)):
            mix = {part: number(row, "share_" + part) for part in classes}
            if all(v is None for v in mix.values()):
                continue
            if any(v is None or not 0 <= v <= 1 for v in mix.values()):
                raise ValueError(f"{cell.label}: supply every {field_name} share in [0,1], or omit the mix")
            total = sum(mix.values())
            if total > 1 + 1e-9 or (field_name == "impact_mix" and not math.isclose(total, 1, abs_tol=1e-9)):
                raise ValueError(f"{cell.label}: invalid {field_name} total {total}")
            setattr(cell, field_name, mix)
        cells.append(cell)
    if not cells:
        raise ValueError("no aggregate cells supplied")
    windows = sorted({c.window for c in cells}, key=window_sort_key)
    retained = windows[-params.windows_retained:] if params.windows_retained else windows
    if retained != windows:
        warnings.append({"code": "WINDOWS_DROPPED", "message": "dropped " + ", ".join(w for w in windows if w not in retained)})
    cells = sorted((c for c in cells if c.window in retained), key=lambda c: (c.group, window_sort_key(c.window)))
    if any(not c.impact_mix or not c.question_mix for c in cells):
        warnings.append({"code": "MIX_UNAVAILABLE", "message": "Some aggregate cells omit compositions; those mixes are not drawn."})
    if any(c.aggregate_fallback is None for c in cells) and any(
        m.name in params.objectives and "lead_time_days" in m.formula.deps.columns for m in metrics
    ):
        warnings.append({"code": "LEAD_TIME_SOURCE_UNAVAILABLE", "message":
            "Aggregate lead-time sources are unknown for some cells; supply lead_time_fallback_share to check source comparability."})
    return cells, errors, retained, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open_store(args) -> S.Store:
    path = Path(args.db) if getattr(args, "db", None) else S.default_db_path(
        use_global=getattr(args, "global_store", False)
    )
    store = S.Store(path)
    # Every command that touches the store gets the shipped defaults present,
    # so the registry is never a partial one.
    seed_default_metrics(store)
    return store


def cmd_ingest(args) -> int:
    rows = read_builds_csv(Path(args.csv))
    with _open_store(args) as store:
        report = store.ingest_builds(rows, source=args.source or str(args.csv))
        print(f"{args.csv}: {report.summary()}")
        for build_id, field_name in report.conflict:
            print(f"  CONFLICT {build_id}: '{field_name}' differs from the stored row")
        for row, reason in report.rejected:
            print(f"  REJECTED {row}: {reason}")
        print(f"store now holds {store.build_count()} builds at {store.path}")
    return 0


def cmd_define(args) -> int:
    metric = F.Metric(
        args.name, args.role, args.direction, args.expression, notes=args.notes or ""
    )
    metric.compile()  # rejects anything the evaluator will not run
    with _open_store(args) as store:
        version = store.define_metric(
            args.name, args.role, args.direction, args.expression, args.notes or ""
        )
        print(f"{args.name} v{version}  {args.role}/{args.direction}  {args.expression}")
        assert metric.formula is not None
        print(f"  deps {json.dumps(metric.formula.deps.as_json())}")
    return 0


def cmd_list(args) -> int:
    with _open_store(args) as store:
        if args.what == "metrics":
            # Through load_metrics, so a fresh store is seeded with the shipped
            # defaults here rather than looking empty until the first compute.
            for metric in load_metrics(store, None):
                print(
                    f"{metric.name:16} v{metric.version}  {metric.role:10} "
                    f"{metric.direction:3}  {metric.expression}"
                )
                if metric.notes:
                    print(f"{'':16} {metric.notes}")
        elif args.what == "groups":
            for group in store.groups():
                print(group)
        elif args.what == "windows":
            builds, _, windows, _ = prepare_builds(store.read_builds())
            for window in windows:
                n = sum(1 for b in builds if b["window"] == window)
                print(f"{window}  {n} builds")
        elif args.what == "runs":
            for run in store.list_runs():
                print(f"{run['run_id']}  {run['ts']}  {', '.join(run['objectives'])}")
    return 0


def cmd_compute(args) -> int:
    store = None if args.no_store else _open_store(args)
    try:
        metrics = load_metrics(
            store, Path(args.metrics) if args.metrics else None
        )
        aggregate_rows = read_cells_csv(Path(args.cells)) if args.cells else None
        if aggregate_rows is not None:
            rows = []
        elif args.builds:
            rows = read_builds_csv(Path(args.builds))
        elif store is not None:
            rows = store.read_builds()
        else:
            raise SystemExit("nothing to read: pass --builds or use the store")
        if not rows and aggregate_rows is None:
            raise SystemExit("no builds: run `surface ingest builds.csv` first")
        params = ComputeParams(
            objectives=args.objectives.split(",") if args.objectives else DEFAULT_OBJECTIVES,
            window_spec=args.window,
            windows_retained=args.windows,
            L_days=args.lag,
            u=args.u,
            as_of=parse_ts(args.as_of) if args.as_of else None,
            baseline=args.baseline,
            min_builds=args.min_builds,
            bootstrap=args.bootstrap,
            permutations=args.permutations,
            seed=args.seed,
            epsilon_source=args.epsilon_source,
            weights=[float(w) for w in args.weights.split(",")] if args.weights else None,
            allow_mixed_lead_time=args.allow_mixed_lead_time,
        )
        run_id = args.run_id or (
            store.next_run_id(S.now_iso()) if store else "r-adhoc"
        )
        analysis = compute(rows, metrics, params, run_id=run_id, aggregate_rows=aggregate_rows)
        out = Path(args.out)
        out.write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")
        if store is not None and analysis["cells"]:
            store.save_run(analysis)
        print_summary(analysis)
        print(f"\nwrote {out}")
        return 1 if analysis["diagnostics"]["refusals"] else 0
    finally:
        if store is not None:
            store.close()


def print_summary(analysis: dict) -> None:
    run = analysis["run"]
    print(f"run {run['run_id']}  objectives: {', '.join(run['objectives'])}")
    print(f"mode: {analysis.get('mode')}")
    gate = analysis.get("gate", {})
    print(
        f"gate: u={gate.get('u')} ({gate.get('u_source')})  L={gate.get('L_days')}d  "
        f"as_of={gate.get('as_of')}"
    )
    for line in analysis["stage0"]["overlaps"]:
        print(f"  {line}")
    for refusal in analysis["diagnostics"]["refusals"]:
        print(f"  REFUSED {refusal['code']}: {refusal['message']}")
    for caution in analysis["diagnostics"]["cautions"]:
        print(f"  CAUTION {caution['code']}: {caution['message']}")
    sensitivity = analysis.get("sensitivity", {})
    if sensitivity.get("available"):
        print(f"  threshold: {sensitivity['verdict']}")
    for comparison in analysis.get("comparisons", []):
        null_model = comparison.get("null_model", {})
        verdict = null_model.get("verdict", "n/a")
        print(
            f"  [{comparison['kind']}] {comparison['label']}: front "
            f"{len(comparison.get('front', []))}/{len(comparison.get('cells', []))}  "
            f"null model: {verdict}"
        )
        if comparison.get("front"):
            print(f"      {', '.join(comparison['front'])}")


def cmd_render(args) -> int:
    analysis_path = Path(args.analysis)
    out = Path(args.out)
    result = render_analysis(analysis_path, out)
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    print(f"wrote {out}")
    return 0


def cmd_explain(args) -> int:
    if args.analysis:
        analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    else:
        with _open_store(args) as store:
            run_id = args.run_id or store.latest_run_id()
            if not run_id:
                raise SystemExit("no runs in the store; run `surface compute` first")
            loaded = store.load_run(run_id)
            if loaded is None:
                raise SystemExit(f"no such run: {run_id}")
            analysis = loaded
    result = explain_cell(analysis, args.cell)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_diff(args) -> int:
    with _open_store(args) as store:
        result = S.diff_runs(store, args.run_a, args.run_b)
    print(json.dumps(result, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="surface",
        description="Store, analyse and draw build-level delivery numbers. "
        "See the project README and skill references for the input contract and procedure.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", help="path to the sqlite store")
    parser.add_argument(
        "--global", dest="global_store", action="store_true",
        help="use ~/.decision-surface/db.sqlite instead of ./.decision-surface/",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="validate and ingest builds.csv, idempotently")
    p_ingest.add_argument("csv")
    p_ingest.add_argument("--source", help="tag for this batch, e.g. the export name")
    p_ingest.set_defaults(func=cmd_ingest)

    p_define = sub.add_parser("define", help="register a metric formula")
    p_define.add_argument("--name", required=True)
    p_define.add_argument("--role", default="objective", choices=["objective", "descriptor"])
    p_define.add_argument("--direction", default="min", choices=["min", "max"])
    p_define.add_argument("--expression", required=True)
    p_define.add_argument("--notes", default="")
    p_define.set_defaults(func=cmd_define)

    p_list = sub.add_parser("list", help="inspect the store")
    p_list.add_argument("what", choices=["metrics", "groups", "windows", "runs"])
    p_list.set_defaults(func=cmd_list)

    p_compute = sub.add_parser("compute", help="run stages 0-7")
    inputs = p_compute.add_mutually_exclusive_group()
    inputs.add_argument("--builds", help="read builds.csv directly instead of the store")
    inputs.add_argument("--cells", help="read aggregate cells.csv with optional standard errors")
    p_compute.add_argument("--metrics", help="read metrics.csv instead of the registry")
    p_compute.add_argument("--objectives", help="comma-separated metric names")
    p_compute.add_argument("--out", default="analysis.json")
    p_compute.add_argument("--window", default=DEFAULT_WINDOW, choices=["week", "month", "quarter"])
    p_compute.add_argument("--windows", type=int, default=DEFAULT_WINDOWS_RETAINED)
    p_compute.add_argument("--lag", type=int, default=DEFAULT_L_DAYS, help="L, resolution lag in days")
    p_compute.add_argument("--u", type=float, help="usage threshold; default is self-calibrating")
    p_compute.add_argument("--as-of", help="observation edge; default is the current UTC time")
    p_compute.add_argument("--baseline", help="window to calibrate u from")
    p_compute.add_argument("--min-builds", type=int, default=DEFAULT_MIN_BUILDS)
    p_compute.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP)
    p_compute.add_argument("--permutations", type=int, default=DEFAULT_PERMUTATIONS)
    p_compute.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p_compute.add_argument("--epsilon-source", default="bootstrap", choices=["bootstrap", "manual", "none"])
    p_compute.add_argument("--weights", help="comma-separated weights for the weighted sum")
    p_compute.add_argument("--allow-mixed-lead-time", action="store_true")
    p_compute.add_argument("--no-store", action="store_true", help="do not touch the store")
    p_compute.add_argument("--run-id")
    p_compute.set_defaults(func=cmd_compute)

    p_render = sub.add_parser("render", help="analysis.json -> one standalone .html")
    p_render.add_argument("analysis")
    p_render.add_argument("--out", default="surface.html")
    p_render.set_defaults(func=cmd_render)

    p_explain = sub.add_parser("explain", help="why is this cell not on the front")
    p_explain.add_argument("--cell", required=True, help="group/window")
    p_explain.add_argument("--analysis", help="read an analysis.json instead of the store")
    p_explain.add_argument("--run-id")
    p_explain.set_defaults(func=cmd_explain)

    p_diff = sub.add_parser("diff", help="two runs: what moved, and whether definitions did")
    p_diff.add_argument("run_a")
    p_diff.add_argument("run_b")
    p_diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
