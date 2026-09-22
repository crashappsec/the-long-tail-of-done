#!/usr/bin/env python3
"""The store: idempotent ingest, an append-only registry, and a diff that says
when two runs are not comparable.

The property under test throughout is that history stays trustworthy. A store
that double-counts a re-imported CI export, or that lets this quarter's
definition of `debt` silently rewrite last quarter's numbers, is worse than no
store at all.

Run: python3 tests/run.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from decision_surface import formula as F
from decision_surface import store as S
from decision_surface import surface as SU

from test_front import make_fixture  # noqa: E402


def a_build(build_id: str = "b-1", **overrides) -> dict:
    row = {
        "build_id": build_id,
        "group": "one-team",
        "first_commit_ts": "2026-04-01",
        "ts": "2026-04-03",
        "deployed_at": "2026-04-04",
        "served_at": "2026-04-05",
        "usage": 1840,
        "loc_added": 420,
        "loc_removed": 60,
        "bugs": 1,
        "cost_usd": 900,
        "question": "features",
        "carried_into": "",
    }
    row.update(overrides)
    return row


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = S.Store(Path(self.tmp.name) / "db.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()


class TestIngest(StoreCase):
    def test_a_fresh_store_is_empty_and_usable(self):
        self.assertEqual(self.store.build_count(), 0)

    def test_ingest_is_idempotent_on_build_id(self):
        rows = [a_build("b-1"), a_build("b-2")]
        first = self.store.ingest_builds(rows, source="export-1")
        self.assertEqual(len(first.accepted), 2)
        second = self.store.ingest_builds(rows, source="export-2")
        self.assertEqual(len(second.accepted), 0)
        self.assertEqual(len(second.duplicate), 2)
        self.assertEqual(self.store.build_count(), 2)

    def test_a_changed_row_under_an_existing_id_is_a_conflict_not_an_overwrite(self):
        """That is a producer bug, and hiding it makes the store untrustworthy."""
        self.store.ingest_builds([a_build("b-1", cost_usd=900)])
        report = self.store.ingest_builds([a_build("b-1", cost_usd=1200)])
        self.assertEqual(len(report.conflict), 1)
        self.assertEqual(report.conflict[0][0], "b-1")
        self.assertEqual(report.conflict[0][1], "cost_usd")
        stored = self.store.read_builds()[0]
        self.assertEqual(stored["cost_usd"], 900)

    def test_missing_required_fields_are_rejected_with_a_reason(self):
        report = self.store.ingest_builds(
            [a_build("b-1", loc_added=""), {"build_id": ""}, a_build("b-2")]
        )
        self.assertEqual(len(report.accepted), 1)
        self.assertEqual(len(report.rejected), 2)
        reasons = " ".join(reason for _, reason in report.rejected)
        self.assertIn("loc_added", reasons)
        self.assertIn("build_id is required", reasons)

    def test_a_duplicate_inside_one_batch_is_rejected(self):
        report = self.store.ingest_builds([a_build("b-1"), a_build("b-1")])
        self.assertEqual(len(report.accepted), 1)
        self.assertIn("within this batch", report.rejected[0][1])

    def test_nulls_survive_the_round_trip_as_nulls(self):
        """Nullable means "we could not observe it", never zero -- including
        after a trip through sqlite."""
        self.store.ingest_builds([a_build("b-1", cost_usd="", bugs="", usage="")])
        stored = self.store.read_builds()[0]
        self.assertIsNone(stored["cost_usd"])
        self.assertIsNone(stored["bugs"])
        self.assertIsNone(stored["usage"])

    def test_group_is_exposed_as_group_not_grp(self):
        """`group` is a reserved word in SQL, so the column is `grp` inside and
        `group` everywhere a human or an agent sees it."""
        self.store.ingest_builds([a_build("b-1", group="platform")])
        stored = self.store.read_builds()[0]
        self.assertEqual(stored["group"], "platform")
        self.assertNotIn("grp", stored)

    def test_ingests_are_logged(self):
        self.store.ingest_builds([a_build("b-1")], source="export-1")
        self.store.ingest_builds([a_build("b-1")], source="export-1")
        rows = list(self.store.conn.execute("SELECT * FROM ingests ORDER BY ingest_id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["accepted"], 1)
        self.assertEqual(rows[1]["duplicate"], 1)


class TestRegistry(StoreCase):
    def test_defining_a_metric_returns_version_one(self):
        version = self.store.define_metric(
            "debt", "objective", "min", "sum(bugs) / count()"
        )
        self.assertEqual(version, 1)

    def test_an_unchanged_redefinition_is_a_no_op(self):
        first = self.store.define_metric("debt", "objective", "min", "count()")
        second = self.store.define_metric("debt", "objective", "min", "count()")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.metric_history("debt")), 1)

    def test_a_changed_expression_appends_a_version(self):
        self.store.define_metric("debt", "objective", "min", "count()")
        version = self.store.define_metric(
            "debt", "objective", "min", "sum(bugs) / count()"
        )
        self.assertEqual(version, 2)
        history = self.store.metric_history("debt")
        self.assertEqual([row["version"] for row in history], [1, 2])
        self.assertEqual(history[0]["expression"], "count()")

    def test_an_old_version_is_still_readable(self):
        """Redefining `debt` next quarter must not rewrite last quarter's
        meaning, so a run can always resolve the version it pinned."""
        self.store.define_metric("debt", "objective", "min", "count()")
        self.store.define_metric("debt", "objective", "min", "sum(bugs)")
        old = self.store.metric_at("debt", 1)
        self.assertIsNotNone(old)
        assert old is not None
        self.assertEqual(old["expression"], "count()")

    def test_latest_metrics_returns_one_row_per_name(self):
        self.store.define_metric("a", "objective", "min", "count()")
        self.store.define_metric("a", "objective", "min", "sum(bugs)")
        self.store.define_metric("b", "descriptor", "min", "count()")
        latest = {row["name"]: row["version"] for row in self.store.latest_metrics()}
        self.assertEqual(latest, {"a": 2, "b": 1})

    def test_the_shipped_defaults_seed_a_fresh_store(self):
        metrics = SU.load_metrics(self.store, None)
        names = {m.name for m in metrics}
        self.assertIn("debt_defect", names)
        self.assertIn("debt_composite", names)
        # Seeded, not just returned: a second call reads them back out of sqlite.
        self.assertTrue(self.store.latest_metrics())

    def test_defining_one_metric_does_not_opt_out_of_the_defaults(self):
        """A team whose first command defines their own `debt` must still have
        `cost` and `lead_time`. Seeding only into a wholly empty registry left
        them with a registry of one and a next command that failed with
        "unknown metric: cost"."""
        self.store.define_metric(
            "debt_mine", "objective", "min",
            "minmax(sum(bugs) / (sum(loc_total) / 1000)) * 0.7 "
            "+ 0.3 * share(liability)",
        )
        names = {m.name for m in SU.load_metrics(self.store, None)}
        self.assertIn("debt_mine", names)
        for shipped in SU.DEFAULT_OBJECTIVES:
            self.assertIn(shipped, names)

    def test_seeding_never_overwrites_a_redefined_default(self):
        self.store.define_metric("cost", "objective", "min", "median(cost_usd)")
        added = SU.seed_default_metrics(self.store)
        self.assertNotIn("cost", added)
        cost = self.store.latest_metric("cost")
        assert cost is not None
        self.assertEqual(cost["expression"], "median(cost_usd)")
        self.assertEqual(cost["version"], 1)

    def test_seeding_is_idempotent(self):
        first = SU.seed_default_metrics(self.store)
        second = SU.seed_default_metrics(self.store)
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_an_explicit_csv_wins_over_the_store(self):
        path = ROOT / "sample" / "metrics.csv"
        if not path.exists():
            self.skipTest("sample/metrics.csv not generated yet")
        self.store.define_metric("only_in_store", "descriptor", "min", "count()")
        metrics = SU.load_metrics(self.store, path)
        self.assertNotIn("only_in_store", {m.name for m in metrics})


class TestRunsAndDiff(StoreCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("ground-truth")

    def compute(self, run_id: str, metrics=None) -> dict:
        params = SU.ComputeParams(
            objectives=SU.DEFAULT_OBJECTIVES,
            as_of=SU.parse_ts(self.manifest["as_of"]),
            u=self.manifest["pinned_u"],
            bootstrap=0,
            permutations=50,
        )
        analysis = SU.compute(
            self.rows, metrics or F.default_metrics(), params, run_id=run_id
        )
        self.store.save_run(analysis)
        return analysis

    def test_a_run_round_trips_through_sqlite(self):
        original = self.compute("r-1")
        loaded = self.store.load_run("r-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["run"]["objectives"], original["run"]["objectives"])
        self.assertEqual(len(loaded["cells"]), len(original["cells"]))

    def test_results_rows_land_one_per_cell(self):
        analysis = self.compute("r-1")
        count = self.store.conn.execute(
            "SELECT count(*) FROM results WHERE run_id = 'r-1'"
        ).fetchone()[0]
        self.assertEqual(count, len(analysis["cells"]))

    def test_re_saving_a_run_replaces_its_results_rather_than_duplicating(self):
        self.compute("r-1")
        self.compute("r-1")
        count = self.store.conn.execute(
            "SELECT count(*) FROM results WHERE run_id = 'r-1'"
        ).fetchone()[0]
        self.assertEqual(count, len(self.store.load_run("r-1")["cells"]))  # type: ignore[index]

    def test_run_ids_are_unique_and_sortable(self):
        stamp = "2026-09-10T12:00:00+00:00"
        first = self.store.next_run_id(stamp)
        self.compute(first)
        second = self.store.next_run_id(stamp)
        self.assertNotEqual(first, second)
        self.assertLess(first, second)

    def test_diff_of_identical_runs_is_comparable_and_empty(self):
        self.compute("r-1")
        self.compute("r-2")
        result = S.diff_runs(self.store, "r-1", "r-2")
        self.assertEqual(result["verdict"], "COMPARABLE")
        self.assertEqual(result["definition_changes"], [])
        self.assertEqual(result["front_joined"], [])
        self.assertEqual(result["front_left"], [])
        for moved in result["moved"]:
            for delta in moved["objectives"].values():
                self.assertAlmostEqual(delta["delta"] or 0.0, 0.0)

    def test_diff_refuses_to_call_two_runs_comparable_across_a_redefinition(self):
        """A cost that "improved 12%" between two runs whose `cost` was edited in
        between has not been shown to improve at all."""
        self.compute("r-1")
        metrics = F.default_metrics()
        for metric in metrics:
            if metric.name == "debt_defect":
                metric.version = 2
                metric.expression = "minmax(sum(bugs) / (sum(loc_total) / 500))"
                metric.compile()
        self.compute("r-2", metrics=metrics)
        result = S.diff_runs(self.store, "r-1", "r-2")
        self.assertTrue(result["verdict"].startswith("NOT COMPARABLE"))
        changed = {c["metric"]: (c["version_a"], c["version_b"])
                   for c in result["definition_changes"]}
        self.assertEqual(changed["debt_defect"], (1, 2))

    def test_diff_reports_parameter_changes(self):
        self.compute("r-1")
        params = SU.ComputeParams(
            objectives=SU.DEFAULT_OBJECTIVES,
            as_of=SU.parse_ts(self.manifest["as_of"]),
            u=self.manifest["pinned_u"] * 2,
            bootstrap=0,
            permutations=50,
            L_days=45,
        )
        self.store.save_run(
            SU.compute(self.rows, F.default_metrics(), params, run_id="r-2")
        )
        result = S.diff_runs(self.store, "r-1", "r-2")
        self.assertIn("L_days", result["param_changes"])
        self.assertEqual(result["param_changes"]["L_days"], {"a": 30, "b": 45})

    def test_diff_names_cells_that_joined_or_left_the_front(self):
        self.compute("r-1")
        # Drop the group that is planted on the front in the latest window, so
        # the front necessarily changes hands.
        rows = [r for r in self.rows if r["group"] != "platform-core"]
        params = SU.ComputeParams(
            objectives=SU.DEFAULT_OBJECTIVES,
            as_of=SU.parse_ts(self.manifest["as_of"]),
            u=self.manifest["pinned_u"],
            bootstrap=0,
            permutations=50,
        )
        self.store.save_run(
            SU.compute(rows, F.default_metrics(), params, run_id="r-2")
        )
        result = S.diff_runs(self.store, "r-1", "r-2")
        self.assertTrue(
            any(cell[0] == "platform-core" for cell in result["cells_only_in_a"])
        )

    def test_diff_of_a_missing_run_raises(self):
        self.compute("r-1")
        with self.assertRaises(KeyError):
            S.diff_runs(self.store, "r-1", "r-nope")


class TestEndToEndCli(unittest.TestCase):
    """The CLI is what a locked-down laptop actually runs."""

    def test_ingest_compute_explain_through_the_command_line(self):
        import subprocess

        rows, manifest = make_fixture("ground-truth")
        csv_path = Path(__file__).resolve().parent / ".fixtures" / "ground-truth.csv"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            def cli(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-m", "decision_surface", "--db", str(db), *args],
                    capture_output=True,
                    text=True,
                )

            ingest = cli("ingest", str(csv_path), "--source", "test")
            self.assertEqual(ingest.returncode, 0, ingest.stderr)
            self.assertIn("accepted", ingest.stdout)

            analysis_path = Path(tmp) / "analysis.json"
            compute = cli(
                "compute",
                "--as-of", manifest["as_of"],
                "--u", str(manifest["pinned_u"]),
                "--bootstrap", "0",
                "--permutations", "50",
                "--out", str(analysis_path),
            )
            self.assertEqual(compute.returncode, 0, compute.stderr)
            self.assertTrue(analysis_path.exists())
            analysis = json.loads(analysis_path.read_text())
            self.assertEqual(
                sorted(analysis["groups"]),
                sorted({r["group"] for r in rows}),
            )

            explain = cli(
                "explain",
                "--cell", manifest["planted"]["dominated_latest_window"][0],
                "--analysis", str(analysis_path),
            )
            self.assertEqual(explain.returncode, 0, explain.stderr)
            self.assertIn("dominated_by", explain.stdout)

            listing = cli("list", "runs")
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertIn("cost", listing.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
