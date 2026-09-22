#!/usr/bin/env python3
"""Reference implementation of the shipping-tail metrics.

Reads git history (and optionally an AgentsView stats JSON export), groups
commits into feature clusters, and reports the four metrics defined in
../SKILL.md:

    shipped capability count
    deploys-to-stability   (survival curve, right-censored)
    rework-deploy ratio
    tail mass
    tail attribution       (tail mass grouped by workflow)

No third-party dependencies: git plus the standard library. Deliberately
readable rather than clever, so a sceptic can audit it in one sitting.

Design decisions worth knowing before you trust the output:

* t=0 is taken from the most authoritative source available and the choice is
  reported, never silently defaulted. Preference order: flag events, deploy
  events, tags, first merge to the default branch.
* Clusters that never go quiet are right-censored rather than dropped. They
  are the population the metric exists to find, so an average would delete
  the finding.
* The clustering threshold is swept by default. A single threshold is a
  modifiable-unit choice that cannot be defended; a shape that holds across
  thresholds can be.
* Anything whose t=0 or originating session cannot be resolved is counted in
  an explicit `unattributed` bucket. Nothing is renormalised away.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Commit-message anchors, in descending order of how much we trust them. A
# human-declared boundary always beats a similarity score.
ANCHOR_PATTERNS = [
    ("ticket", re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")),
    ("flag", re.compile(r"\bflag[:/ ]+([a-z0-9][a-z0-9._-]{2,})", re.I)),
    ("conventional-scope", re.compile(r"^\w+\(([^)]{2,40})\)!?:")),
    ("revert", re.compile(r"^Revert .*\"(.{4,60})\"")),
]

# Paths that are effort but not feature work. Counted separately so they do
# not inflate any cluster's tail.
INFRA_PATH = re.compile(
    r"(^|/)(\.github|\.gitlab|ci|infra|terraform|helm|charts|deploy)(/|$)"
)

STOPWORDS = {
    "add", "added", "adds", "fix", "fixed", "fixes", "update", "updated",
    "updates", "remove", "removed", "chore", "refactor", "test", "tests",
    "wip", "bump", "merge", "revert", "the", "a", "an", "of", "to", "for",
    "and", "in", "on", "with", "from", "into", "make", "use", "using",
}


@dataclass
class Commit:
    sha: str
    when: datetime
    author: str
    subject: str
    files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions

    @property
    def is_infra_only(self) -> bool:
        return bool(self.files) and all(INFRA_PATH.search(f) for f in self.files)

    def tokens(self) -> set[str]:
        words = re.findall(r"[a-z][a-z0-9_]{2,}", self.subject.lower())
        return {w for w in words if w not in STOPWORDS}

    def dirs(self) -> set[str]:
        out = set()
        for f in self.files:
            parts = f.split("/")
            out.add("/".join(parts[:2]) if len(parts) > 2 else parts[0])
        return out


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def read_commits(repo: Path, since: str | None, until: str | None) -> list[Commit]:
    """One `git log --numstat` pass. Merge commits are excluded: their churn is
    double-counted against the commits they bring in."""
    sep = "\x1e"
    fmt = f"{sep}%H%x1f%aI%x1f%aE%x1f%s"
    args = ["log", "--no-merges", f"--pretty=format:{fmt}", "--numstat"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")

    commits: list[Commit] = []
    for chunk in run_git(repo, *args).split(sep):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, *rest = chunk.split("\n")
        try:
            sha, iso, author, subject = head.split("\x1f", 3)
        except ValueError:
            continue
        c = Commit(
            sha=sha,
            when=datetime.fromisoformat(iso),
            author=author,
            subject=subject,
        )
        for line in rest:
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            ins, dele, path = cols
            # "-" means binary; count the file but not the churn.
            c.insertions += int(ins) if ins.isdigit() else 0
            c.deletions += int(dele) if dele.isdigit() else 0
            c.files.append(path)
        commits.append(c)
    commits.sort(key=lambda c: c.when)
    return commits


def anchor_of(commit: Commit) -> tuple[str, str] | None:
    """The strongest human-declared feature boundary in a commit, if any."""
    for kind, pattern in ANCHOR_PATTERNS:
        m = pattern.search(commit.subject)
        if m:
            return kind, m.group(1).strip().lower()
    return None


def cluster(commits: list[Commit], threshold: int) -> dict[str, list[Commit]]:
    """Anchors first; agglomerate the remainder by directory and subject-token
    overlap.

    `threshold` is the minimum combined overlap score for two commits to join
    the same cluster. It is the knob the sweep varies, and the reason the
    sweep exists: the cluster boundaries are a modelling choice, so the only
    honest claim is that the metric's shape survives moving them.
    """
    clusters: dict[str, list[Commit]] = defaultdict(list)
    loose: list[Commit] = []

    for c in commits:
        anchor = anchor_of(c)
        if anchor:
            clusters[f"{anchor[0]}:{anchor[1]}"].append(c)
        else:
            loose.append(c)

    # Greedy single-pass agglomeration. O(n * k); fine at repo scale, and the
    # ordering is deterministic because commits arrive in time order.
    groups: list[tuple[set[str], set[str], list[Commit]]] = []
    for c in loose:
        toks, dirs = c.tokens(), c.dirs()
        best, best_score = None, 0
        for g in groups:
            score = 2 * len(dirs & g[1]) + len(toks & g[0])
            if score > best_score:
                best, best_score = g, score
        if best is not None and best_score >= threshold:
            best[0].update(toks)
            best[1].update(dirs)
            best[2].append(c)
        else:
            groups.append((set(toks), set(dirs), [c]))

    for i, g in enumerate(groups):
        # Name the cluster after its most common directory so the output is
        # legible to someone who knows the repo.
        label = sorted(g[1])[0] if g[1] else f"group-{i}"
        clusters[f"cluster:{label}#{i}"] = g[2]

    return dict(clusters)


def load_t0(path: Path | None) -> tuple[str, list[tuple[datetime, str]]]:
    """Read explicit t=0 events. CSV, two columns: iso8601 timestamp, hint.

    The hint is matched against a cluster key or any of its commit subjects,
    which keeps the format usable for flag keys, ticket ids and service names
    alike.
    """
    if path is None:
        return "none supplied - every cluster reported unattributed", []
    rows: list[tuple[datetime, str]] = []
    with path.open(newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].startswith("#"):
                continue
            when = datetime.fromisoformat(row[0].strip())
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            rows.append((when, (row[1] if len(row) > 1 else "").strip().lower()))
    rows.sort()
    return f"supplied events ({path.name}, {len(rows)} rows)", rows


def resolve_t0(
    key: str, commits: list[Commit], events: list[tuple[datetime, str]]
) -> tuple[datetime | None, bool]:
    """Return (t0, resolved_from_events).

    Without events there is no defensible ship moment, so the caller is told
    the cluster is unattributed rather than being handed the first commit as
    if it were a deploy.
    """
    if not events:
        return None, False
    haystack = key.lower() + " " + " ".join(c.subject.lower() for c in commits)
    first = commits[0].when
    for when, hint in events:
        if when < first:
            continue
        if not hint or hint in haystack:
            return when, True
    return None, False


def quiet_split(
    commits: list[Commit], t0: datetime, quiet_days: int, window_end: datetime
) -> tuple[list[Commit], list[Commit], int, bool]:
    """Split a cluster at t=0 and find where the tail goes quiet.

    Returns (pre, post, deploys_to_stability, censored). `censored` is True
    when the cluster never accumulated `quiet_days` of silence before the end
    of the observation window, which is the case the survival curve exists to
    represent.

    `window_end` must be the end of the *observation window*, not the
    cluster's own last commit. Using the cluster's own last commit makes every
    cluster look censored, because a cluster is by definition silent after its
    final commit.
    """
    pre = [c for c in commits if c.when < t0]
    post = [c for c in commits if c.when >= t0]

    gap = timedelta(days=quiet_days)
    stability_index = None
    for i, c in enumerate(post):
        nxt = post[i + 1].when if i + 1 < len(post) else None
        if nxt is None or nxt - c.when >= gap:
            stability_index = i + 1
            break

    if not post:
        return pre, post, 0, False

    trailing_quiet = window_end - post[-1].when
    censored = trailing_quiet < gap and stability_index == len(post)
    return pre, post, (stability_index or len(post)), censored


@dataclass
class ClusterResult:
    key: str
    commits: int
    t0: datetime | None
    resolved: bool
    pre_churn: int = 0
    post_churn: int = 0
    deploys_to_stability: int = 0
    censored: bool = False
    workflow: str = "unknown"

    @property
    def rework_ratio(self) -> float | None:
        if not self.pre_churn:
            return None
        return self.post_churn / self.pre_churn

    @property
    def tail_mass(self) -> float | None:
        total = self.pre_churn + self.post_churn
        if not total:
            return None
        return self.post_churn / total


def classify_workflow(agentsview: dict | None) -> str:
    """Map an AgentsView stats window onto one of the four talk archetypes.

    This is a coarse heuristic and it is labelled as one everywhere it
    surfaces. Its only job is to give tail attribution a grouping key when no
    better workflow label exists in the org.
    """
    if not agentsview:
        return "unknown (no session archive supplied)"

    archetypes = (
        agentsview.get("session_archetypes")
        or agentsview.get("archetypes")
        or {}
    )
    total = sum(v for v in archetypes.values() if isinstance(v, (int, float))) or 1
    share = {k: (v / total) for k, v in archetypes.items() if isinstance(v, (int, float))}

    automation = share.get("automation", 0.0)
    marathon = share.get("marathon", 0.0) + share.get("deep", 0.0)
    quick = share.get("quick", 0.0)

    # The talk carries three archetypes. A fourth, for autonomous or parallel
    # agents, was cut because nothing published measures it per-feature, so
    # heavy automation is reported as automation-heavy rather than mapped onto
    # an archetype the evidence does not support.
    if automation > 0.5:
        return "automation-heavy (unmapped: no archetype claims this)"
    if marathon > 0.4:
        return "prompt-and-iterate (majority long sessions)"
    if quick > 0.5:
        return "spec-first (majority short sessions)"
    return "mixed"


def analyse(
    commits: list[Commit],
    events: list[tuple[datetime, str]],
    threshold: int,
    quiet_days: int,
    workflow: str,
) -> list[ClusterResult]:
    # One observation end for the whole window, shared by every cluster.
    window_end = max(c.when for c in commits)
    results: list[ClusterResult] = []
    for key, group in cluster(commits, threshold).items():
        feature_commits = [c for c in group if not c.is_infra_only]
        if not feature_commits:
            continue
        t0, resolved = resolve_t0(key, feature_commits, events)
        res = ClusterResult(
            key=key,
            commits=len(feature_commits),
            t0=t0,
            resolved=resolved,
            workflow=workflow,
        )
        if t0 is not None:
            pre, post, dts, censored = quiet_split(
                feature_commits, t0, quiet_days, window_end
            )
            res.pre_churn = sum(c.churn for c in pre)
            res.post_churn = sum(c.churn for c in post)
            res.deploys_to_stability = dts
            res.censored = censored
        results.append(res)
    return results


def survival(results: list[ClusterResult]) -> list[tuple[int, float]]:
    """Kaplan-Meier estimate of "still receiving deploys" against deploys
    since first ship. Right-censored clusters leave the risk set without
    counting as an event, which is what keeps the never-stabilises plateau
    visible instead of averaged away."""
    obs = [(r.deploys_to_stability, not r.censored) for r in results if r.resolved]
    if not obs:
        return []
    n = len(obs)
    curve = [(0, 1.0)]
    surv = 1.0
    at_risk = n
    for t in sorted({t for t, _ in obs}):
        events = sum(1 for tt, event in obs if tt == t and event)
        if at_risk > 0 and events:
            surv *= 1 - events / at_risk
            curve.append((t, surv))
        at_risk -= sum(1 for tt, _ in obs if tt == t)
    return curve


def summarise(results: list[ClusterResult]) -> dict:
    resolved = [r for r in results if r.resolved]
    tails = [r.tail_mass for r in resolved if r.tail_mass is not None]
    ratios = [r.rework_ratio for r in resolved if r.rework_ratio is not None]
    curve = survival(resolved)
    return {
        "clusters_total": len(results),
        "clusters_resolved": len(resolved),
        "churn_total": sum(r.pre_churn + r.post_churn for r in results),
        "unattributed": len(results) - len(resolved),
        "unattributed_share": (
            round((len(results) - len(resolved)) / len(results), 3) if results else None
        ),
        "shipped_capability_count": len(resolved),
        "tail_mass_median": round(sorted(tails)[len(tails) // 2], 3) if tails else None,
        "rework_ratio_median": (
            round(sorted(ratios)[len(ratios) // 2], 3) if ratios else None
        ),
        "never_stabilises_share": (
            round(sum(1 for r in resolved if r.censored) / len(resolved), 3)
            if resolved
            else None
        ),
        "survival_plateau": round(curve[-1][1], 3) if curve else None,
        "survival_curve": [[t, round(s, 4)] for t, s in curve],
    }


# --------------------------------------------------------------- the frontier
#
# The four metrics describe one group. Choosing between groups is a different
# question with more than one objective, so it gets a non-dominated set rather
# than a score. Deliberately not a weighted total: any weighting is a claim
# about how much stability a month of delay is worth, and nobody in the room
# agrees on that number.
#
# All three axes are "more is better", so the efficient set is the outer
# surface. A flow is dominated when some other flow is at least as good on all
# three and strictly better on one.

AXES = ("per_spend", "per_month", "settled")


@dataclass
class FlowPoint:
    label: str
    delivered: int
    per_spend: float
    per_month: float
    settled: float
    spend_unit: str
    # Fully-loaded spend per engineer-month. Only computable with --cost, and
    # reported because no budget is denominated in tasks or capabilities: this
    # is the rate that compares two teams shipping different things, and one
    # quarter to the next through a model change. A rate for comparison, never
    # a target - dividing by headcount rewards shrinking headcount.
    spend_per_month: float | None = None


def load_costs(path: Path | None) -> dict[str, tuple[float, float]]:
    """CSV: label,dollars,engineer_months.

    Without it the denominators are total churn and elapsed calendar time.
    Those are proxies, they are labelled as proxies in the output, and they are
    the best git alone can do. A proxy denominator is honest; an unlabelled one
    is not.
    """
    if not path:
        return {}
    costs: dict[str, tuple[float, float]] = {}
    with path.open(newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 3 or row[0].strip().startswith("#") or row[0].strip() == "label":
                continue
            costs[row[0].strip()] = (float(row[1]), float(row[2]))
    return costs


def elapsed_months(window: dict) -> float:
    first = datetime.fromisoformat(window["first"])
    last = datetime.fromisoformat(window["last"])
    return max((last - first).days, 1) / 30.44


def flow_point(
    label: str, run: dict, threshold: str, costs: dict[str, tuple[float, float]]
) -> FlowPoint | None:
    s = run["sweep"][threshold]
    delivered = s["shipped_capability_count"]
    never = s["never_stabilises_share"]
    if not delivered or never is None:
        return None
    if label in costs:
        dollars, eng_months = costs[label]
        if dollars <= 0 or eng_months <= 0:
            return None
        return FlowPoint(label, delivered, delivered / (dollars / 10_000),
                         delivered / eng_months, 1.0 - never, "per $10k",
                         spend_per_month=dollars / eng_months)
    churn = s.get("churn_total") or 0
    if not churn:
        return None
    return FlowPoint(label, delivered, delivered / (churn / 1000),
                     delivered / elapsed_months(run["window"]), 1.0 - never,
                     "per 1k changed lines (proxy)")


def dominates(a: FlowPoint, b: FlowPoint) -> bool:
    va = [getattr(a, k) for k in AXES]
    vb = [getattr(b, k) for k in AXES]
    return all(x >= y for x, y in zip(va, vb)) and any(x > y for x, y in zip(va, vb))


def pareto_front(points: list[FlowPoint]) -> list[str]:
    """Labels of the non-dominated points. O(n^2) on purpose: n is the number
    of workflows you can actually instrument, which is single digits."""
    return [p.label for p in points
            if not any(dominates(q, p) for q in points if q.label != p.label)]


def front_mode(paths: list[Path], costs_path: Path | None) -> int:
    costs = load_costs(costs_path)
    runs: dict[str, dict] = {}
    for path in paths:
        run = json.loads(path.read_text())
        # The workflow label only carries information when a session archive
        # was supplied. Otherwise every run claims the same "unknown" string
        # and the comparison becomes unreadable, so fall back to the filename
        # and let the caller name the flows by naming the files.
        label = run.get("workflow") or ""
        if not label or label.startswith("unknown"):
            label = path.stem
        if label in runs:
            label = f"{label} [{path.stem}]"
        runs[label] = run

    if len(runs) < 2:
        print("a frontier needs at least two runs to compare", file=sys.stderr)
        return 1

    shared = sorted(
        set.intersection(*(set(r["sweep"].keys()) for r in runs.values())),
        key=lambda t: int(t),
    )
    if not shared:
        print("runs share no clustering threshold; re-run with the same --sweep",
              file=sys.stderr)
        return 1

    # Same discipline as the rest of the tool: compute at every shared
    # threshold and report whether membership of the efficient set survives.
    # A flow efficient at one threshold out of four is a clustering artefact.
    wins: dict[str, int] = defaultdict(int)
    latest: dict[str, FlowPoint] = {}
    skipped: set[str] = set()
    for t in shared:
        pts = []
        for label, run in runs.items():
            fp = flow_point(label, run, t, costs)
            if fp is None:
                skipped.add(label)
                continue
            pts.append(fp)
            latest[label] = fp
        for label in pareto_front(pts):
            wins[label] += 1

    unit = next(iter(latest.values())).spend_unit if latest else "-"
    print(f"\nfrontier over {len(latest)} flows, {len(shared)} shared thresholds "
          f"({', '.join(shared)})")
    print(f"spend axis   used capability {unit}")
    print("axes         delivery per spend, delivery per month, share that settles")
    print("             more is better on all three\n")

    amortised = any(fp.spend_per_month is not None for fp in latest.values())
    cols = ("flow", "used", "per_spend", "per_month", "settles", "efficient")
    widths = [38, 6, 11, 11, 9, 11]
    if amortised:
        cols = cols[:5] + ("$/eng-mo", "efficient")
        widths = widths[:5] + [11, 11]

    def short(label: str) -> str:
        # Truncate from the front: what distinguishes two runs of the same
        # workflow is the suffix, so cutting the tail hides exactly the part
        # that matters.
        return label if len(label) <= widths[0] - 1 else "\u2026" + label[-(widths[0] - 2):]
    print("  " + "".join(c.ljust(w) if i == 0 else c.rjust(w)
                         for i, (c, w) in enumerate(zip(cols, widths))))
    for label, fp in sorted(latest.items(), key=lambda kv: -wins[kv[0]]):
        n = wins[label]
        row = [short(label), str(fp.delivered), f"{fp.per_spend:.2f}",
               f"{fp.per_month:.2f}", f"{fp.settled:.0%}"]
        if amortised:
            row.append(f"{fp.spend_per_month:,.0f}" if fp.spend_per_month else "-")
        row.append(f"{n}/{len(shared)}")
        print("  " + "".join(v.ljust(w) if i == 0 else v.rjust(w)
                             for i, (v, w) in enumerate(zip(row, widths))))

    always = [l for l, n in wins.items() if n == len(shared)]
    sometimes = [l for l, n in wins.items() if 0 < n < len(shared)]
    print()

    # Dominance stops discriminating as the objective count rises relative to
    # the number of units: with three objectives and a handful of flows,
    # "everything is efficient" is the expected degenerate outcome, not a
    # finding. Ishibuchi, Tsukamoto & Nojima, IEEE CEC 2008. Say so rather
    # than let the caller read a full frontier as good news.
    if len(always) == len(latest) and len(latest) > 1:
        print(
            f"DEGENERATE: all {len(latest)} flows are non-dominated, which is what"
            " happens when\nthere are too few flows for three objectives rather"
            " than a finding about them.\nAdd flows, or drop to two axes and say"
            " which two.\n"
        )
    elif len(latest) < 2 * len(AXES):
        print(
            f"CAUTION: {len(latest)} flows against {len(AXES)} objectives is thin."
            " Dominance discriminates\npoorly at this ratio, so treat the"
            " efficient set as provisional.\n"
        )
    if always:
        print("efficient at every threshold: " + ", ".join(sorted(always)))
    if sometimes:
        print("efficient at some thresholds only, so treat as unresolved: "
              + ", ".join(sorted(sometimes)))
    if not always and not sometimes:
        print("no flow was efficient at any threshold, which means the inputs "
              "are degenerate; check that each run has a t=0 source.")
    for label in sorted(skipped):
        print(f"skipped {label}: no delivered capability, or no stability share, "
              "at one or more thresholds")
    print(
        "\nthe frontier is relative to the flows you supplied. Adding a better "
        "flow moves it,\nand a flow on the frontier is not good, only "
        "unbeaten by this comparison set.\n"
    )
    return 0


# ------------------------------------------------- the cost leg, from the archive
#
# The four cost components on the deck's cost slide are agent spend, build/CI,
# review, and the tail. Only one of them is available locally without new
# instrumentation, and it is the one everybody already has on disk: the agent
# session archive. AgentsView exposes token mix by model, and the session
# outcome distribution, so agent spend and an estimate of never-landed work
# both fall out of a file the user already has.
#
# AgentsView's own docs say the stats JSON is experimental, additive, and
# should be parsed defensively, and that a zero counter is not the same as
# zero activity. Everything below honours that: unknown shapes yield None and
# say so, never a plausible-looking zero.

AV_TOKEN_KEYS = ("tokens", "total_tokens", "token_count", "tokens_total")


def _as_token_map(block: object) -> dict[str, int] | None:
    """Coax AgentsView's model_mix into {model: tokens}, or give up honestly.

    The documented shape is "token and session mix by model", which has been a
    dict and a list of records across versions, so both are accepted and
    anything else returns None rather than a guess.
    """
    out: dict[str, int] = {}
    if isinstance(block, dict):
        for model, v in block.items():
            if isinstance(v, (int, float)):
                out[str(model)] = int(v)
            elif isinstance(v, dict):
                for k in AV_TOKEN_KEYS:
                    if isinstance(v.get(k), (int, float)):
                        out[str(model)] = int(v[k])
                        break
    elif isinstance(block, list):
        for rec in block:
            if not isinstance(rec, dict):
                continue
            model = rec.get("model") or rec.get("name")
            for k in AV_TOKEN_KEYS:
                if model and isinstance(rec.get(k), (int, float)):
                    out[str(model)] = int(rec[k])
                    break
    return out or None


def load_prices(path: Path | None) -> dict[str, float]:
    """CSV: model,usd_per_million_tokens.

    Deliberately a blended rate per model rather than split input/output
    pricing: the stats export does not reliably separate the two, and a
    blended rate that is labelled blended is honest, where a split rate
    derived from a total is not.
    """
    if not path:
        return {}
    prices: dict[str, float] = {}
    with path.open(newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 2 or row[0].strip().startswith("#") or row[0].strip() == "model":
                continue
            prices[row[0].strip()] = float(row[1])
    return prices


def agent_spend(agentsview: dict | None, prices: dict[str, float]) -> dict:
    """Estimated agent spend for the window, plus how much of it is guesswork.

    priced_share is the load-bearing field. If half the tokens ran on a model
    with no price, the spend figure is half a number, and the caller has to be
    told rather than shown a total that looks complete.
    """
    if not agentsview:
        return {"status": "no session archive supplied"}
    tokens = _as_token_map(agentsview.get("model_mix"))
    if not tokens:
        return {"status": "model_mix absent or in an unrecognised shape"}
    total = sum(tokens.values())
    if not prices:
        return {
            "status": "tokens found, no price table supplied",
            "tokens_total": total,
            "tokens_by_model": tokens,
        }
    priced = {m: n for m, n in tokens.items() if m in prices}
    spend = sum(n / 1_000_000 * prices[m] for m, n in priced.items())
    unpriced = sorted(set(tokens) - set(priced))
    return {
        "status": "estimated",
        "tokens_total": total,
        "tokens_by_model": tokens,
        "usd_estimate": round(spend, 2),
        "priced_share": round(sum(priced.values()) / total, 3) if total else None,
        "unpriced_models": unpriced,
        "basis": "blended per-model rate over total tokens",
    }


def never_landed(agentsview: dict | None) -> dict:
    """Share of sessions whose work did not land, as a SUNK-quadrant estimate.

    AgentsView rolls its four raw outcomes into success / failure / unknown.
    Failure is abandoned-or-errored, which is the closest local signal for
    agent work that was paid for and never shipped. Its own documentation
    calls these labels triage heuristics rather than ground truth, so that
    caveat travels with the number everywhere it is reported.
    """
    if not agentsview:
        return {"status": "no session archive supplied"}
    block = agentsview.get("outcomes") or agentsview.get("outcome_stats") or {}
    if isinstance(block, dict) and isinstance(block.get("buckets"), dict):
        block = block["buckets"]
    if not isinstance(block, dict):
        return {"status": "outcomes absent or in an unrecognised shape"}
    counts = {k: v for k, v in block.items()
              if k in ("success", "failure", "unknown") and isinstance(v, (int, float))}
    total = sum(counts.values())
    if not total:
        return {"status": "outcomes absent or in an unrecognised shape"}
    return {
        "status": "estimated",
        "sessions": int(total),
        "failure_share": round(counts.get("failure", 0) / total, 3),
        "unknown_share": round(counts.get("unknown", 0) / total, 3),
        "caveat": "AgentsView outcome labels are triage heuristics by its own "
                  "documentation, not ground truth. The unknown share is not zero.",
    }


# ------------------------------------------------------------- session fan-out
#
# Every effort number above is derived from commits, and that is a real blind
# spot rather than a rounding error. An agent run fans out into child sessions
# that investigate, test, audit and explain, most of which commit nothing, and
# published measurement says the money is in the fan-out: of roughly 200,000
# runs at Ramp, 17% contained more than one work item and those 17% accounted
# for 58% of model spend, with the root session holding about a fifth of a
# run's cost in their worked example.
#
# So this measures the size of the blind spot instead of arguing about it.
# Feed it one row per agent session and it reports how much effort sits in
# sessions that produced nothing a commit-based metric can see.

COMMIT_FIELDS = ("produced_commits", "commits", "commit_count")
COST_FIELDS = ("cost_usd", "usd", "cost")
TOKEN_FIELDS = ("tokens", "total_tokens", "token_count")


@dataclass
class Session:
    sid: str
    parent: str | None
    effort: float
    commits: int | None
    repo: str | None = None


def _first(rec: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if isinstance(rec.get(k), (int, float)):
            return float(rec[k])
    return None


def load_sessions(path: Path) -> tuple[list[Session], str]:
    """JSONL, one object per agent session.

    Recognised: session_id, parent_session_id, cost_usd (or tokens), and a
    commit count under produced_commits / commits / commit_count. A truthy
    produced_pr or produced_branch counts as at least one commit. A session
    with no commit information at all is `None` rather than 0, because those
    two mean very different things here and conflating them would manufacture
    the finding.
    """
    out: list[Session] = []
    unit = "usd"
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rec = json.loads(line)
        sid = str(rec.get("session_id") or rec.get("id") or "")
        if not sid:
            continue
        cost = _first(rec, COST_FIELDS)
        if cost is None:
            cost = _first(rec, TOKEN_FIELDS)
            if cost is not None:
                unit = "tokens"
        commits = _first(rec, COMMIT_FIELDS)
        if commits is None and (rec.get("produced_pr") or rec.get("produced_branch")):
            commits = 1.0
        parent = rec.get("parent_session_id") or rec.get("parent") or None
        out.append(Session(sid, str(parent) if parent else None, cost or 0.0,
                           int(commits) if commits is not None else None,
                           rec.get("repo")))
    return out, unit


def group_runs(sessions: list[Session]) -> list[list[Session]]:
    """A run is a maximal tree over parent_session_id.

    Walks to the root per session with a seen-set, so a malformed parent chain
    that loops is treated as its own root instead of hanging the tool.
    """
    by_id = {s.sid: s for s in sessions}
    root_of: dict[str, str] = {}
    for s in sessions:
        seen, cur = set(), s
        while cur.parent and cur.parent in by_id and cur.sid not in seen:
            seen.add(cur.sid)
            cur = by_id[cur.parent]
        root_of[s.sid] = cur.sid
    runs: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        runs[root_of[s.sid]].append(s)
    return list(runs.values())


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    xs = sorted(vals)
    return xs[min(int(q * len(xs)), len(xs) - 1)]


def fanout(sessions: list[Session], unit: str) -> dict:
    """How much effort a commit-derived metric cannot see.

    Reported as a distribution, never a mean: the whole point of the published
    finding is that the effort is concentrated, and a mean would hide exactly
    the concentration that matters.
    """
    runs = group_runs(sessions)
    if not runs:
        return {"status": "no sessions"}

    total = sum(s.effort for r in runs for s in r)
    known = [s for r in runs for s in r if s.commits is not None]
    unknown_effort = total - sum(s.effort for s in known)

    invisible, multi_runs, multi_effort, noartifact_effort = [], 0, 0.0, 0.0
    for r in runs:
        eff = sum(s.effort for s in r)
        if len(r) > 1:
            multi_runs += 1
            multi_effort += eff
        rated = [s for s in r if s.commits is not None]
        if rated and eff:
            blind = sum(s.effort for s in rated if s.commits == 0)
            invisible.append(blind / eff)
            if all(s.commits == 0 for s in rated):
                noartifact_effort += eff

    return {
        "status": "measured",
        "unit": unit,
        "sessions": len(sessions),
        "runs": len(runs),
        "effort_total": round(total, 2),
        "multi_session_run_share": round(multi_runs / len(runs), 3),
        "multi_session_effort_share": round(multi_effort / total, 3) if total else None,
        "invisible_effort_share_median": (
            round(_pct(invisible, 0.5), 3) if invisible else None),
        "invisible_effort_share_p90": (
            round(_pct(invisible, 0.9), 3) if invisible else None),
        "no_artifact_effort_share": round(noartifact_effort / total, 3) if total else None,
        "unrated_effort_share": round(unknown_effort / total, 3) if total else None,
        "caveat": "Invisible effort is spend in sessions that produced no commit. "
                  "It is what a commit-derived tail metric cannot see, and it "
                  "biases tail effort downward, most in multi-objective runs.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compute the shipping-tail metrics over a git repository."
    )
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--since", default=None, help="git --since value")
    ap.add_argument("--until", default=None, help="git --until value")
    ap.add_argument(
        "--deploys",
        type=Path,
        default=None,
        help="CSV of t=0 events: iso8601,hint. Flag activations preferred over "
        "deploys; without this every cluster is reported unattributed.",
    )
    ap.add_argument(
        "--agentsview",
        type=Path,
        default=None,
        help="Output of `agentsview stats --json`, used only to label the workflow",
    )
    ap.add_argument(
        "--quiet-days",
        type=int,
        default=14,
        help="consecutive quiet days that count as stable (default 14)",
    )
    ap.add_argument(
        "--sweep",
        default="2,3,4,6",
        help="clustering thresholds to sweep (default 2,3,4,6). The sweep is "
        "the answer to the modifiable-unit objection; do not disable it.",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument(
        "--front",
        nargs="+",
        type=Path,
        default=None,
        metavar="RUN.JSON",
        help="compare two or more prior --json runs and print the non-dominated "
        "set of workflows. Choosing between flows is multi-objective, so this "
        "returns a frontier rather than a ranking.",
    )
    ap.add_argument(
        "--sessions",
        type=Path,
        default=None,
        metavar="SESSIONS.JSONL",
        help="One JSON object per agent session: session_id, "
        "parent_session_id, cost_usd (or tokens), and a commit count. Measures "
        "how much effort sits in sessions that produced no commit, which is "
        "what every commit-derived number here cannot see.",
    )
    ap.add_argument(
        "--prices",
        type=Path,
        default=None,
        help="CSV of model,usd_per_million_tokens. Turns the session archive's "
        "token mix into an agent-spend estimate, and reports what share of the "
        "tokens it could actually price.",
    )
    ap.add_argument(
        "--cost",
        type=Path,
        default=None,
        help="CSV of label,dollars,engineer_months for --front. Without it the "
        "denominators are churn and calendar time, and are labelled as proxies.",
    )
    args = ap.parse_args()

    if args.front:
        return front_mode(args.front, args.cost)

    commits = read_commits(args.repo, args.since, args.until)
    if not commits:
        print("no commits in window", file=sys.stderr)
        return 1

    t0_source, events = load_t0(args.deploys)
    agentsview = json.loads(args.agentsview.read_text()) if args.agentsview else None
    workflow = classify_workflow(agentsview)
    spend = agent_spend(agentsview, load_prices(args.prices))
    unlanded = never_landed(agentsview)
    if args.sessions:
        sess, sess_unit = load_sessions(args.sessions)
        blind = fanout(sess, sess_unit)
    else:
        blind = {"status": "no session export supplied"}

    thresholds = [int(x) for x in args.sweep.split(",") if x.strip()]
    sweep = {}
    for t in thresholds:
        results = analyse(commits, events, t, args.quiet_days, workflow)
        sweep[t] = summarise(results)

    payload = {
        "window": {
            "since": args.since,
            "until": args.until,
            "commits": len(commits),
            "first": commits[0].when.isoformat(),
            "last": commits[-1].when.isoformat(),
        },
        "t0_source": t0_source,
        "quiet_days": args.quiet_days,
        "workflow": workflow,
        "agent_spend": spend,
        "never_landed": unlanded,
        "session_fanout": blind,
        "sweep": sweep,
        "caveats": [
            "Tail mass is descriptive, not a target. Some tail is healthy iteration.",
            "The unit is a feature cluster, never a named engineer.",
            "Feature clustering is a modelling choice; the sweep shows its sensitivity.",
            "Workflow labels derived from session archetypes are heuristics.",
            "Agent spend is one of four cost components and usually the "
            "smallest. Build, review and the tail are not in this number.",
        ],
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    w = payload["window"]
    print(f"\nwindow      {w['first'][:10]} .. {w['last'][:10]}  ({w['commits']} commits)")
    print(f"t=0 source  {t0_source}")
    print(f"workflow    {workflow}")
    print(f"quiet days  {args.quiet_days}\n")

    cols = (
        "thresh", "clusters", "shipped", "unattrib", "tail_mass",
        "rework", "never_stab", "plateau",
    )
    print("  " + "".join(c.rjust(12) for c in cols))
    for t, s in sweep.items():
        row = [
            str(t),
            str(s["clusters_total"]),
            str(s["shipped_capability_count"]),
            f"{s['unattributed_share']:.0%}" if s["unattributed_share"] is not None else "-",
            f"{s['tail_mass_median']:.2f}" if s["tail_mass_median"] is not None else "-",
            f"{s['rework_ratio_median']:.2f}" if s["rework_ratio_median"] is not None else "-",
            f"{s['never_stabilises_share']:.0%}" if s["never_stabilises_share"] is not None else "-",
            f"{s['survival_plateau']:.2f}" if s["survival_plateau"] is not None else "-",
        ]
        print("  " + "".join(v.rjust(12) for v in row))

    if spend.get("status") == "estimated":
        pct = spend.get("priced_share")
        print(f"\nagent spend  ~${spend['usd_estimate']:,.2f} over "
              f"{spend['tokens_total']:,} tokens, {spend['basis']}")
        if pct is not None and pct < 1.0:
            print(f"             only {pct:.0%} of tokens had a price; "
                  f"unpriced: {', '.join(spend['unpriced_models'])}")
        print("             one of four cost components, and usually the smallest")
    elif spend.get("status") and spend["status"] != "no session archive supplied":
        print(f"\nagent spend  unavailable: {spend['status']}")

    if unlanded.get("status") == "estimated":
        print(f"\nnever landed {unlanded['failure_share']:.0%} of "
              f"{unlanded['sessions']:,} sessions abandoned or errored, "
              f"{unlanded['unknown_share']:.0%} unknown")
        print("             a SUNK-quadrant estimate, not a measurement: "
              "outcome labels are triage heuristics")

    if blind.get("status") == "measured":
        u = "$" if blind["unit"] == "usd" else ""
        print(f"\nfan-out     {blind['sessions']:,} sessions in {blind['runs']:,} runs, "
              f"{u}{blind['effort_total']:,.0f} {blind['unit']}")
        print(f"            {blind['multi_session_run_share']:.0%} of runs are "
              f"multi-session and carry {blind['multi_session_effort_share']:.0%} "
              "of the effort")
        if blind["invisible_effort_share_median"] is not None:
            print(f"            invisible to commits: median "
                  f"{blind['invisible_effort_share_median']:.0%} of a run, P90 "
                  f"{blind['invisible_effort_share_p90']:.0%}")
        if blind.get("no_artifact_effort_share"):
            print(f"            {blind['no_artifact_effort_share']:.0%} of effort in "
                  "runs that produced no commit at all")
        if blind.get("unrated_effort_share"):
            print(f"            {blind['unrated_effort_share']:.0%} of effort in "
                  "sessions with no commit information, excluded above")
        print("            this is the effort a commit-derived tail metric misses")
    elif blind.get("status") not in (None, "no session export supplied"):
        print(f"\nfan-out     unavailable: {blind['status']}")

    print("\nread the shape across thresholds, not any single row.")
    if not events:
        print(
            "no t=0 events supplied, so every cluster is unattributed by design.\n"
            "pass --deploys with flag activations or deploy events to get real numbers."
        )
    for c in payload["caveats"]:
        print(f"  - {c}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
