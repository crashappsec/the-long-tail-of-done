"""Counterexamples from the independent review, through public entry points."""
from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from decision_surface import formula as F
from decision_surface import surface as SU
from decision_surface import store as S
from decision_surface import mcp_server as MCP
from test_render import render, node_available


def builds(windows=1):
    return [dict(build_id=f"{g}-{w}-{i}", group=f"g{g}",
                 first_commit_ts=f"2026-{w:02d}-01", ts=f"2026-{w:02d}-02",
                 deployed_at=f"2026-{w:02d}-{g+3:02d}",
                 served_at=f"2026-{w:02d}-{g+4:02d}", usage=100,
                 loc_added=100, loc_removed=0, bugs=g, cost_usd=100-g*10,
                 question="features")
            for g in range(4) for w in range(1, windows+1) for i in range(10)]


def params(**overrides):
    return SU.ComputeParams(**dict(dict(objectives=["cost", "lead_time"],
        as_of=SU.parse_ts("2026-06-01"), bootstrap=0, permutations=10,
        min_builds=1), **overrides))


def aggregates():
    return [dict(group=f"g{i}", window="2026-01", n_builds=10,
                 cost=100+i*10, lead_time=4-i, cost_se=3, lead_time_se=.2)
            for i in range(4)]


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class CalculationRegressions(unittest.TestCase):
    def test_null_cost_and_zero_cost_have_different_fronts(self):
        metrics = [m for m in F.default_metrics() if m.name == "cost"]
        metrics.append(F.Metric("bugs", "objective", "min", "mean(bugs)").compile())
        rows = builds()
        for r in rows:
            g = int(r["group"][1:])
            r["bugs"] = [2, 1, 0, 3][g]
            r["cost_usd"] = [100, 75, 150, 200][g]
            if g == 0 and int(r["build_id"].split("-")[-1]) % 2:
                r["cost_usd"] = None
        p = params(objectives=["cost", "bugs"])
        missing = SU.compute(rows, metrics, p)
        zero = SU.compute([{**r, "cost_usd": 0 if r["cost_usd"] is None else r["cost_usd"]} for r in rows], metrics, p)
        a, b = missing["cells"][0], zero["cells"][0]
        self.assertEqual(a["objectives"]["cost"], 100)
        self.assertEqual(b["objectives"]["cost"], 50)
        self.assertGreater(a["layer"], 1)
        self.assertEqual(b["layer"], 1)
        self.assertIn("cost: skipped 5 null value(s)", a["notes"])

    def test_refusals_suppress_only_the_affected_comparisons(self):
        rows = [{**r, "served_at": None if r["group"] == "g0" else r["served_at"]} for r in builds(4)]
        analysis = SU.compute(rows, F.default_metrics(), params())
        for comparison in analysis["comparisons"]:
            if comparison["kind"] == "across_groups":
                self.assertTrue(comparison["front_not_drawn"])
                self.assertNotIn("front", comparison)
                self.assertNotIn("layers", comparison)
                self.assertIn("DIVERGENT_LEAD_TIME_SOURCE", [r["code"] for r in comparison["refusals"]])
            else:
                self.assertIn("front", comparison)
        self.assertIsNone(analysis["stage7"]["persistence"]["g0"]["fraction"])
        self.assertEqual(analysis["stage7"]["persistence"]["g0"]["windows_considered"], 0)
        explanation = SU.explain_cell(analysis, "g0/2026-01")
        self.assertTrue(explanation["comparisons"][0]["front_not_drawn"])
        summary = MCP.Server.summarise(analysis)
        self.assertTrue(summary["comparisons"][0]["front_not_drawn"])
        allowed = SU.compute(rows, F.default_metrics(), params(allow_mixed_lead_time=True))
        self.assertIn("front", allowed["comparisons"][0])

    def test_replicates_keep_uncertainty_at_extreme_cells(self):
        cells = [SU.Cell(str(i), "2026-01", [{"cost_usd": v} for v in vals])
                 for i, vals in enumerate(([0, 10]*5, [100, 110]*5, [200, 210]*5))]
        for metric in (F.Metric("cost", "objective", "min", "mean(cost_usd)"),
                       F.Metric("cost", "objective", "min", "minmax(mean(cost_usd))")):
            metric.compile()
            replicas = SU.bootstrap_replicates(cells, [metric], 100, random.Random(7))
            for cell in (cells[0], cells[-1]):
                values = [r[cell.key][0] for r in replicas]
                self.assertGreater(max(values)-min(values), .005)
            self.assertLess(min(r[cells[0].key][0] for r in replicas), 0)
            self.assertGreater(max(r[cells[-1].key][0] for r in replicas), 1)
        for i, cell in enumerate(cells):
            cell.objectives["cost"] = 100*i + 5
        errors = {c.key: {"cost": 2} for c in cells}
        replicas = SU.parametric_replicates(cells, ["cost"], errors, 100, random.Random(7))
        self.assertGreater(len({r[cells[0].key][0] for r in replicas}), 1)

    def test_attainment_uses_all_replicates(self):
        key = ("a", "w")
        replicas = [{key: [.1, .2]}] + [{key: [.8, .2]} for _ in range(9)]
        bands = SU.attainment_bands([key], replicas, ["x", "y"], [(0, 1)])["bands"]["x|y"]
        self.assertEqual(bands[0], {"x": .1, "p10": .2, "p50": None, "p90": None})
        self.assertEqual(bands[-1]["p90"], .2)

    def test_aitchison_equals_clr_euclidean_distance(self):
        a, b = SU.clr(dict(a=.5, b=.5), 100), SU.clr(dict(a=.8, b=.2), 100)
        actual, _ = SU.aitchison_distance(a, b)
        self.assertAlmostEqual(actual, math.sqrt(sum((a[k]-b[k])**2 for k in a)))

    def test_observation_edge_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        prepared, gate, _, _ = SU.prepare_builds(builds())
        self.assertGreaterEqual(gate.as_of, before)
        self.assertLessEqual(gate.as_of, datetime.now(timezone.utc))
        self.assertTrue(all(b["impact"] != "unresolved" for b in prepared))

    def test_sparse_compositions_and_deep_graphs(self):
        rows = [r for r in builds(4) if r["build_id"].endswith("-0")]
        analysis = SU.compute(rows, F.default_metrics(), params())
        self.assertTrue(analysis["stage7"]["mix_movement"])
        for entry in analysis["stage7"]["mix_movement"]:
            self.assertTrue(math.isfinite(entry["aitchison_distance"]))
            self.assertLess(entry["zero_replacement"]["delta_from"] * 5, 1)
        chain = {i: [i+1] for i in range(1500)}
        self.assertEqual(SU._find_cycles(chain), [])
        chain[1500] = [0]
        self.assertEqual(SU._find_cycles(chain), [list(range(1501))])


class BoundaryRegressions(unittest.TestCase):
    def test_bad_formulas_never_reach_mcp_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = MCP.Server(Path(tmp)/"db.sqlite")
            for expression in ("abs()", "sqrt(1,2)", "log()", "median(minmax(cost_usd))",
                               "minmax(minmax(sum(cost_usd)))", "sum(sum(cost_usd))", "abs('x')", "1e999"):
                result = MCP.handle(server, {"id": 1, "method": "tools/call", "params": {
                    "name": "define_metric", "arguments": {"name": "bad", "expression": expression}}})
                self.assertTrue(result.get("result", {}).get("isError"), expression)
            self.assertNotIn("bad", [m["name"] for m in server.list_metrics({})["metrics"]])
        result = F.Formula("bounded", "2 ** (2 ** 100)").evaluate([])
        self.assertIsNone(result.value)

    def test_mcp_remains_alive_after_bad_compute(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = MCP.Server(Path(tmp)/"db.sqlite")
            server.submit_builds({"rows": builds()})
            requests = [dict(id=1, method="tools/call", params=dict(name="compute", arguments=dict(objectives=["unknown", "cost"]))),
                        dict(id=2, method="ping")]
            output = io.StringIO()
            with redirect_stdout(output):
                MCP.serve(server, io.StringIO("\n".join(json.dumps(r) for r in requests)))
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertTrue(replies[0]["result"]["isError"])
            self.assertEqual(replies[1]["id"], 2)

    def test_csv_definitions_are_pinned_and_diffed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            write_csv(tmp/"builds.csv", builds())
            for run_id, multiplier in (("a", 1), ("b", 2)):
                metrics = F.default_metrics()
                next(m for m in metrics if m.name == "cost").expression = f"mean(cost_usd)*{multiplier}"
                write_csv(tmp/"metrics.csv", [{k: m.as_json()[k] for k in ("name", "role", "direction", "expression", "version", "notes")} for m in metrics])
                result = subprocess.run([sys.executable, "-m", "decision_surface", "--db", str(tmp/"db.sqlite"),
                    "compute", "--builds", str(tmp/"builds.csv"), "--metrics", str(tmp/"metrics.csv"),
                    "--objectives", "cost,lead_time", "--bootstrap", "0", "--permutations", "10",
                    "--as-of", "2026-06-01", "--run-id", run_id, "--out", str(tmp/f"{run_id}.json")], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            with S.Store(tmp/"db.sqlite") as store:
                diff = S.diff_runs(store, "a", "b")
                self.assertTrue(diff["verdict"].startswith("NOT COMPARABLE"))
                change = next(c for c in diff["definition_changes"] if c["metric"] == "cost")
                self.assertEqual(change["version_a"], change["version_b"])
                self.assertEqual(change["expression_a"], "mean(cost_usd)*1")
                self.assertEqual(change["expression_b"], "mean(cost_usd)*2")
                self.assertIsNone(diff["moved"][0]["objectives"]["cost"]["delta"])

    def test_cost_migration_preserves_history_and_custom_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            with S.Store(Path(tmp)/"db.sqlite") as store:
                store.define_metric("cost", "objective", "min", "sum(cost_usd) / count()", "mean fully-loaded cost per build", version=1)
                SU.seed_default_metrics(store)
                self.assertEqual(store.latest_metric("cost")["version"], 2)
                self.assertEqual(store.metric_at("cost", 1)["expression"], "sum(cost_usd) / count()")
                store.define_metric("cost", "objective", "min", "median(cost_usd)")
                SU.seed_default_metrics(store)
                self.assertEqual(store.latest_metric("cost")["expression"], "median(cost_usd)")

    def test_aggregate_cli_and_mcp_without_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rows = aggregates()
            write_csv(tmp/"cells.csv", rows)
            output = tmp/"analysis.json"
            result = subprocess.run([sys.executable, "-m", "decision_surface", "compute", "--cells", str(tmp/"cells.csv"),
                "--objectives", "cost,lead_time", "--bootstrap", "30", "--permutations", "10", "--no-store", "--out", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            analysis = json.loads(output.read_text())
            self.assertEqual(analysis["run"]["input_mode"], "aggregate")
            self.assertEqual(analysis["cells"][0]["objectives"]["cost"], 100)
            self.assertEqual(analysis["cells"][0]["n_builds"], 10)
            self.assertEqual(analysis["cells"][0]["impact_mix"], {})
            self.assertIsNone(analysis["cells"][0]["gate_unavailable_share"])
            self.assertIsNotNone(analysis["cells"][0]["p_on_front"])
            server = MCP.Server(tmp/"db.sqlite")
            reply = server.compute({"cells_csv": str(tmp/"cells.csv"), "objectives": ["cost", "lead_time"], "bootstrap": 20})
            self.assertEqual(reply["cells"][0]["n_builds"], 10)
            for row in rows:
                del row["cost_se"]
            reply = server.compute({"cells": rows, "objectives": ["cost", "lead_time"], "bootstrap": 20})
            self.assertEqual(reply["uncertainty"]["mode"], "UNAVAILABLE")
            self.assertTrue(all(c["p_on_front"] is None for c in reply["cells"]))

    def test_aggregate_mix_validation_and_zero_errors(self):
        rows = aggregates()
        for row in rows:
            row.update({"share_"+c: 1 if c == "impactful" else 0 for c in SU.IMPACT_CLASSES})
            row["cost_se"] = row["lead_time_se"] = 0
        analysis = SU.compute([], F.default_metrics(), params(bootstrap=20), aggregate_rows=rows)
        self.assertEqual(analysis["cells"][0]["impact_mix"]["impactful"], 1)
        self.assertTrue(all(c["p_on_front"] is not None for c in analysis["cells"]))
        rows[0]["share_sunk"] = .5
        with self.assertRaisesRegex(ValueError, "total"):
            SU.compute([], F.default_metrics(), params(), aggregate_rows=rows)


@unittest.skipUnless(node_available(), "node is not on PATH")
class RenderRegressions(unittest.TestCase):
    def test_tooltip_parentage_and_p90_axis_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = builds()
            for r in rows:
                g = int(r["group"][1:])
                day = g+2 if int(r["build_id"].split("-")[-1]) < 6 else g*4+10
                r["served_at"] = f"2026-01-{day:02d}"
            for order in (["cost", "lead_time", "debt_defect"], ["lead_time", "cost", "debt_defect"]):
                analysis = SU.compute(rows, F.default_metrics(), params(objectives=order))
                html = render(analysis, Path(tmp))
                whiskers = []
                for block in re.findall(r"<svg.*?</svg>", html, re.S):
                    root = ET.fromstring(block)
                    self.assertFalse(any(t.tag == "title" and "/" in (t.text or "") for t in root))
                    whiskers.extend(root.findall(".//line[@class='whisker']"))
                self.assertTrue(whiskers)
                for mark in whiskers:
                    self.assertGreater(float(mark.get("data-p90")), float(mark.get("data-median")))
                    self.assertNotEqual((mark.get("x1"), mark.get("y1")), (mark.get("x2"), mark.get("y2")))
            analysis["stage0"]["objectives"][0]["expression"] = "mean(cost_usd)"
            self.assertNotIn('class="whisker"', render(analysis, Path(tmp)))

    def test_aggregate_and_unattained_bands_render_without_false_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = SU.compute([], F.default_metrics(), params(bootstrap=20), aggregate_rows=aggregates())
            comparison = analysis["comparisons"][0]
            comparison["attainment"]["bands"]["cost|lead_time"] = [
                dict(x=0, p10=.2, p50=None, p90=None), dict(x=1, p10=.2, p50=.4, p90=.8)]
            html = render(analysis, Path(tmp))
            self.assertNotIn("NaN", html)
            self.assertNotIn('class="glyph"', html)
            self.assertIn("MIX_UNAVAILABLE", html)


if __name__ == "__main__":
    unittest.main()
