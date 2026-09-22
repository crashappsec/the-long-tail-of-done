#!/usr/bin/env python3
"""stdio MCP server over the same local store, so an agent can push data and
read results without a human in the loop.

Standard library only, and that is a deliberate constraint rather than an
aesthetic one: this process reads build-level engineering data, and a
dependency-free server is one a security review can finish in an afternoon.
The protocol is small enough to implement directly -- JSON-RPC 2.0 over
newline-delimited stdio, `initialize` / `tools/list` / `tools/call`.

    claude mcp add decision-surface -- /abs/path/.venv/bin/decision-surface-mcp

Tools:

    submit_builds   validate and ingest rows or a CSV path; returns
                    accepted/duplicate/conflict/rejected with reasons
    define_metric   register a formula; returns the version in force
    list_metrics    the registry, latest version of each
    list_groups     the opaque group values present
    list_windows    windows and their build counts
    compute         stages 0-7 for an objective set and window spec
    render          write the standalone HTML; returns the path
    explain         one cell: on the front or not, what dominates it, by how much
    diff            two runs: what moved, and whether definitions changed

Every tool returns the same shape a CLI run prints, so an agent and a human
are reading the same numbers with the same caveats attached. In particular the
refusals and cautions travel with the result rather than being dropped on the
way out -- an agent that cannot see `NOT INFORMATIVE` will report a front as a
finding.

Path safety: `submit_builds` with a `csv` path and `render` with an `out` path
both touch the filesystem on behalf of a caller who may be a model reading
untrusted text. Both are confined to the store's directory tree unless
`--allow-any-path` is passed, and the confinement is reported in the error.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import __version__
from . import formula as F
from . import store as S
from . import surface as SU
from .rendering import render_analysis

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "decision-surface", "version": __version__}

# Kept small on purpose: an analysis is a few hundred kilobytes of JSON and a
# tool result that large is useless to a model. `compute` returns the summary
# plus the path to the full file, and `explain` is the drill-down.
MAX_INLINE_CELLS = 40


def tool_schemas() -> list[dict]:
    return [
        {
            "name": "submit_builds",
            "description": (
                "Validate and ingest build rows, idempotently on build_id. Pass "
                "either `rows` (a list of objects matching SCHEMA.md section 1) or "
                "`csv` (a path). Re-submitting the same builds is safe: they come "
                "back as duplicates rather than double-counting. A row whose "
                "contents differ under an existing build_id is reported as a "
                "conflict and NOT overwritten, because that is a producer bug."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "build records; build_id, first_commit_ts, ts, "
                        "loc_added and loc_removed are required, everything else is "
                        "nullable and null means 'not observed', never zero",
                    },
                    "csv": {"type": "string", "description": "path to builds.csv"},
                    "source": {
                        "type": "string",
                        "description": "tag for this batch, e.g. the CI export name",
                    },
                },
            },
        },
        {
            "name": "define_metric",
            "description": (
                "Register a derived metric as a declared formula. The registry is "
                "append-only: an unchanged re-definition is a no-op and a changed "
                "expression gets a new version, so redefining `debt` next quarter "
                "cannot silently rewrite last quarter's history. Expressions are "
                "evaluated by a restricted AST walker -- names, numbers, + - * / **, "
                "and the function allowlist (sum median mean p90 p10 min max count "
                "share abs sqrt log minmax z clip). Anything else is rejected with "
                "the offending node named."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string", "enum": ["objective", "descriptor"]},
                    "direction": {"type": "string", "enum": ["min", "max"]},
                    "expression": {"type": "string"},
                    "notes": {
                        "type": "string",
                        "description": "printed on every chart that uses the metric; "
                        "say what the number claims",
                    },
                },
                "required": ["name", "expression"],
            },
        },
        {
            "name": "list_metrics",
            "description": "The metric registry, latest version of each, with each "
            "formula's dependencies and any structural overlap it carries.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_groups",
            "description": "The distinct `group` values in the store. The tool never "
            "interprets a group; it is an opaque comparison key.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_windows",
            "description": "Windows present in the store with their build counts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "window": {"type": "string", "enum": ["week", "month", "quarter"]}
                },
            },
        },
        {
            "name": "compute",
            "description": (
                "Run stages 0-7 and store the result. Returns the summary an agent "
                "needs -- the front per comparison, the null-model verdict, the "
                "structural overlap report, every refusal and caution, and the path "
                "to the full analysis JSON. Read the null-model verdict before "
                "reporting a front: NOT INFORMATIVE means the front is the size "
                "independent noise produces and says nothing beyond the dimension "
                "count."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "objectives": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "metric names; 2 to 8 of them. Default is "
                        "cost, lead_time, debt_defect",
                    },
                    "cells": {"type": "array", "items": {"type": "object"},
                              "description": "aggregate cell records instead of stored builds"},
                    "cells_csv": {"type": "string", "description": "path to aggregate cells.csv"},
                    "window": {"type": "string", "enum": ["week", "month", "quarter"]},
                    "windows_retained": {"type": "integer"},
                    "u": {
                        "type": "number",
                        "description": "usage threshold; omit to self-calibrate to the "
                        "median usage across served builds",
                    },
                    "lag_days": {
                        "type": "integer",
                        "description": "L, the resolution lag. A build younger than "
                        "this at the observation edge is unresolved, not sunk.",
                    },
                    "as_of": {
                        "type": "string",
                        "description": "observation edge, ISO 8601; default is the "
                        "current UTC time",
                    },
                    "min_builds": {"type": "integer"},
                    "bootstrap": {"type": "integer", "description": "replicates, default 2000"},
                    "seed": {"type": "integer"},
                    "weights": {"type": "array", "items": {"type": "number"}},
                    "allow_mixed_lead_time": {"type": "boolean"},
                    "out": {"type": "string", "description": "path for the analysis JSON"},
                },
            },
        },
        {
            "name": "render",
            "description": "Write the standalone HTML for a stored run: inline SVG, "
            "inline CSS, no fetches. Returns the path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "default: the latest run"},
                    "out": {"type": "string"},
                },
            },
        },
        {
            "name": "explain",
            "description": (
                "Why is this cell not on the front. Names the cells that dominate it, "
                "on which objectives and by how much, the binding objective, and the "
                "nearest dominator. A cell with an unobserved objective is reported "
                "as unplaceable rather than as last."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cell": {"type": "string", "description": "group/window, e.g. platform/2026-04"},
                    "run_id": {"type": "string"},
                },
                "required": ["cell"],
            },
        },
        {
            "name": "diff",
            "description": (
                "Two runs: what moved, who joined or left the front, which parameters "
                "changed, and -- first -- whether any metric definition changed "
                "between them. If it did, the verdict is NOT COMPARABLE and the "
                "movement is not evidence of anything."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_a": {"type": "string"},
                    "run_b": {"type": "string"},
                },
                "required": ["run_a", "run_b"],
            },
        },
    ]


class Server:
    def __init__(self, db: Path, allow_any_path: bool = False) -> None:
        self.db = db
        self.allow_any_path = allow_any_path
        self.root = db.parent.resolve()

    # -- path confinement --------------------------------------------------

    def store(self) -> S.Store:
        """Open the store with the shipped defaults present, always. Defining
        one metric must not silently opt a team out of the other seven."""
        store = S.Store(self.db)
        SU.seed_default_metrics(store)
        return store

    def resolve_path(self, raw: str, must_exist: bool) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve() if must_exist else Path(str(path))
        if not self.allow_any_path:
            try:
                path.resolve().relative_to(self.root)
            except ValueError:
                raise ValueError(
                    f"path is outside the store directory ({self.root}). Move the "
                    f"file there, or start the server with --allow-any-path."
                )
        if must_exist and not path.exists():
            raise ValueError(f"no such file: {path}")
        return path

    # -- tools -------------------------------------------------------------

    def submit_builds(self, args: dict) -> dict:
        rows = args.get("rows")
        if args.get("csv"):
            rows = SU.read_builds_csv(self.resolve_path(args["csv"], must_exist=True))
        if not rows:
            raise ValueError("pass either `rows` or `csv`")
        with self.store() as store:
            report = store.ingest_builds(rows, source=args.get("source"))
            payload = report.as_json()
            payload["total_builds_in_store"] = store.build_count()
        payload["note"] = (
            "Ingest is idempotent on build_id. Conflicts are reported, not "
            "overwritten. Nullable columns left empty mean 'not observed'; a zero "
            "is a different claim and the tool will not collapse them."
        )
        return payload

    def define_metric(self, args: dict) -> dict:
        role = args.get("role", "objective")
        direction = args.get("direction", "min")
        # Compile first: a formula that will not run must not reach the registry.
        metric = F.Metric(
            args["name"], role, direction, args["expression"], notes=args.get("notes", "")
        ).compile()
        assert metric.formula is not None
        with self.store() as store:
            version = store.define_metric(
                args["name"], role, direction, args["expression"], args.get("notes", "")
            )
        overlaps = (
            [o.line() for o in F.overlap_report([metric])] if role == "objective" else []
        )
        return {
            "name": args["name"],
            "version": version,
            "role": role,
            "direction": direction,
            "expression": args["expression"],
            "dependencies": metric.formula.deps.as_json(),
            "structural_overlap": overlaps,
            "note": "The registry is append-only. Runs pin the versions they used, "
            "so `diff` can tell a changed number from a changed definition.",
        }

    def list_metrics(self, _args: dict) -> dict:
        with self.store() as store:
            metrics = SU.load_metrics(store, None)
        objectives = [m for m in metrics if m.role == "objective"]
        return {
            "metrics": [m.as_json() for m in metrics],
            "structural_overlaps": [o.line() for o in F.overlap_report(objectives)],
            "default_objectives": SU.DEFAULT_OBJECTIVES,
        }

    def list_groups(self, _args: dict) -> dict:
        with self.store() as store:
            groups = store.groups()
            total = store.build_count()
        return {
            "groups": groups,
            "total_builds": total,
            "note": "`group` is opaque and optional. With no group the front is "
            "computed over the group's own windows, which is the default mode and a "
            "real answer for a team with nobody to benchmark against.",
        }

    def list_windows(self, args: dict) -> dict:
        with self.store() as store:
            rows = store.read_builds()
        if not rows:
            return {"windows": [], "note": "the store is empty; submit_builds first"}
        builds, gate, windows, warnings = SU.prepare_builds(
            rows, window_spec=args.get("window", SU.DEFAULT_WINDOW)
        )
        counts: dict[str, int] = {}
        for build in builds:
            counts[build["window"]] = counts.get(build["window"], 0) + 1
        return {
            "windows": [{"window": w, "n_builds": counts.get(w, 0)} for w in windows],
            "gate": gate.as_json(),
            "warnings": warnings,
        }

    def compute(self, args: dict) -> dict:
        aggregate_rows = args.get("cells")
        if args.get("cells_csv"):
            if aggregate_rows is not None:
                raise ValueError("pass cells or cells_csv, not both")
            aggregate_rows = SU.read_cells_csv(self.resolve_path(args["cells_csv"], must_exist=True))
        with self.store() as store:
            metrics = SU.load_metrics(store, None)
            rows = store.read_builds() if aggregate_rows is None else []
            if not rows and aggregate_rows is None:
                raise ValueError("the store is empty; call submit_builds first")
            params = SU.ComputeParams(
                objectives=args.get("objectives") or SU.DEFAULT_OBJECTIVES,
                window_spec=args.get("window", SU.DEFAULT_WINDOW),
                windows_retained=args.get("windows_retained", SU.DEFAULT_WINDOWS_RETAINED),
                L_days=args.get("lag_days", SU.DEFAULT_L_DAYS),
                u=args.get("u"),
                as_of=SU.parse_ts(args["as_of"]) if args.get("as_of") else None,
                min_builds=args.get("min_builds", SU.DEFAULT_MIN_BUILDS),
                bootstrap=args.get("bootstrap", SU.DEFAULT_BOOTSTRAP),
                seed=args.get("seed", SU.DEFAULT_SEED),
                weights=args.get("weights"),
                allow_mixed_lead_time=bool(args.get("allow_mixed_lead_time")),
            )
            run_id = store.next_run_id(S.now_iso())
            analysis = SU.compute(rows, metrics, params, run_id=run_id, aggregate_rows=aggregate_rows)
            if analysis["cells"]:
                store.save_run(analysis)
        out = self.resolve_path(
            args.get("out") or f"analysis-{run_id}.json", must_exist=False
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")
        return {**self.summarise(analysis), "analysis_path": str(out)}

    @staticmethod
    def summarise(analysis: dict) -> dict:
        """What an agent needs, without the 300kB of replicate detail."""
        comparisons = []
        for comparison in analysis.get("comparisons", []):
            entry = {
                "kind": comparison["kind"],
                "label": comparison["label"],
                "question": comparison.get("question"),
                "cells": comparison.get("cells"),
            }
            if comparison.get("front_not_drawn"):
                entry["front_not_drawn"] = True
                entry["refusals"] = comparison.get("refusals")
            else:
                entry["front"] = comparison.get("front")
                entry["layers"] = comparison.get("layers")
                entry["p_on_front"] = comparison.get("p_on_front")
                entry["degeneracy_layer1_fraction"] = comparison.get(
                    "degeneracy_layer1_fraction"
                )
                entry["null_model"] = {
                    k: comparison.get("null_model", {}).get(k)
                    for k in ("verdict", "reading", "observed_front_size",
                              "permutation_mean", "closed_form_A_n_d", "percentile")
                }
                ladder = comparison.get("relaxation_ladder", {})
                entry["knee"] = ladder.get("knees", {}).get("knee")
                entry["skyline_frequency"] = ladder.get("skyline_frequency", {}).get(
                    "scores"
                )
                entry["epsilon_dominance_front"] = ladder.get(
                    "epsilon_dominance", {}
                ).get("front")
                entry["cautions"] = comparison.get("cautions")
            comparisons.append(entry)
        return {
            "run_id": analysis["run"]["run_id"],
            "objectives": analysis["run"]["objectives"],
            "metric_versions": analysis["run"]["metric_versions"],
            "mode": analysis.get("mode"),
            "primary_comparison": analysis.get("primary_comparison"),
            "gate": analysis.get("gate"),
            "groups": analysis.get("groups"),
            "windows": analysis.get("windows"),
            "structural_overlaps": analysis["stage0"]["overlaps"],
            "greyed_spokes": analysis["stage0"].get("greyed_spokes"),
            "conflict_screen": analysis.get("stage2", {}).get("verdicts"),
            "axis_order": analysis.get("stage2", {}).get("axis_order"),
            "objective_reduction": analysis.get("stage3", {}).get("greedy_reduction"),
            "delta_moss": analysis.get("stage3", {}).get("delta_moss", {}).get("ladder"),
            "uncertainty": analysis.get("stage6"),
            "persistence": analysis.get("stage7", {}).get("persistence"),
            "direction_of_travel": analysis.get("stage7", {}).get("direction_of_travel"),
            "mix_movement": analysis.get("stage7", {}).get("mix_movement"),
            "threshold_sensitivity": {
                "verdict": analysis.get("sensitivity", {}).get("verdict"),
                "flips": analysis.get("sensitivity", {}).get("flips"),
            },
            "comparisons": comparisons,
            "cells": [
                {
                    "label": c["label"],
                    "n_builds": c["n_builds"],
                    "below_floor": c["below_floor"],
                    "objectives": c["objectives"],
                    "impact_mix": c["impact_mix"],
                    "gate_unavailable_share": c["gate_unavailable_share"],
                    "question_unclassified_share": c.get(
                        "question_unclassified_share"
                    ),
                    "layer": c["layer"],
                    "p_on_front": c["p_on_front"],
                }
                for c in analysis["cells"][:MAX_INLINE_CELLS]
            ],
            "cells_truncated": max(0, len(analysis["cells"]) - MAX_INLINE_CELLS),
            "diagnostics": analysis["diagnostics"],
            "read_this_first": (
                "Check `diagnostics.refusals` and each comparison's "
                "`null_model.verdict` before reporting a front. NOT INFORMATIVE "
                "means the front is the size independent noise produces at this "
                "cell count and dimension, and says nothing about the teams."
            ),
        }

    def render(self, args: dict) -> dict:
        with self.store() as store:
            run_id = args.get("run_id") or store.latest_run_id()
            if not run_id:
                raise ValueError("no runs in the store; call compute first")
            analysis = store.load_run(run_id)
            if analysis is None:
                raise ValueError(f"no such run: {run_id}")
        analysis_path = self.resolve_path(f"analysis-{run_id}.json", must_exist=False)
        analysis_path.write_text(json.dumps(analysis, default=str), encoding="utf-8")
        out = self.resolve_path(args.get("out") or f"surface-{run_id}.html", must_exist=False)
        result = render_analysis(analysis_path, out)
        if result.returncode != 0:
            raise ValueError(f"render.mjs failed: {result.stderr.strip() or 'node missing?'}")
        return {
            "run_id": run_id,
            "path": str(out),
            "stdout": result.stdout.strip(),
            "note": "One self-contained HTML file: inline SVG, inline CSS, no "
            "fetches. The 2D companions above the 3D pictures are the ones that "
            "can be read.",
        }

    def explain(self, args: dict) -> dict:
        with self.store() as store:
            run_id = args.get("run_id") or store.latest_run_id()
            if not run_id:
                raise ValueError("no runs in the store; call compute first")
            analysis = store.load_run(run_id)
        if analysis is None:
            raise ValueError(f"no such run: {run_id}")
        return {"run_id": run_id, **SU.explain_cell(analysis, args["cell"])}

    def diff(self, args: dict) -> dict:
        with self.store() as store:
            return S.diff_runs(store, args["run_a"], args["run_b"])

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        handlers = {
            "submit_builds": self.submit_builds,
            "define_metric": self.define_metric,
            "list_metrics": self.list_metrics,
            "list_groups": self.list_groups,
            "list_windows": self.list_windows,
            "compute": self.compute,
            "render": self.render,
            "explain": self.explain,
            "diff": self.diff,
        }
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        return handlers[name](args or {})


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 over newline-delimited stdio
# ---------------------------------------------------------------------------


def respond(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(server: Server, request: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": (
                    "Measures builds, not commits. Two gates decide the impact mix: "
                    "did it deploy, and was it used. Read SCHEMA.md for the build "
                    "record and references/front.md for the eight stages. Before "
                    "reporting any front, read the null-model verdict and the "
                    "refusals -- a full front is a diagnosis, not good news."
                ),
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None  # a notification has no id and takes no reply
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tool_schemas()},
        }
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name") or ""
        try:
            result = server.call(name, params.get("arguments") or {})
            text = json.dumps(result, indent=2, default=str)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except (Exception, SystemExit) as exc:  # input failures must not close stdio
            detail = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, (F.FormulaError, ValueError, KeyError)):
                message = detail
            else:
                message = detail + "\n" + traceback.format_exc(limit=3)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": message}],
                    "isError": True,
                },
            }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(server: Server, stdin=None) -> int:
    source = stdin or sys.stdin
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            respond(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"parse error: {exc}"},
                }
            )
            continue
        response = handle(server, request)
        if response is not None:
            respond(response)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="path to the sqlite store")
    parser.add_argument(
        "--global", dest="global_store", action="store_true",
        help="use ~/.decision-surface/db.sqlite",
    )
    parser.add_argument(
        "--allow-any-path", action="store_true",
        help="permit file paths outside the store directory (off by default: this "
        "process acts on behalf of a model that may be reading untrusted text)",
    )
    args = parser.parse_args(argv)
    db = Path(args.db) if args.db else S.default_db_path(use_global=args.global_store)
    server = Server(db, allow_any_path=args.allow_any_path)
    return serve(server)


if __name__ == "__main__":
    raise SystemExit(main())
