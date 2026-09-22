#!/usr/bin/env python3
"""The evaluator: what it computes, and what it refuses to run.

A formula is data. Data from a teammate arriving over MCP, or out of a CSV in
someone's Downloads folder, must not be able to become code -- so the refusal
half of this file matters more than the arithmetic half.

Run: python3 tests/run.py   (or python3 -m unittest discover -s tests)
"""

from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from decision_surface import formula as F


def builds() -> list[dict]:
    """Four builds, one per impact class, with a null in every nullable column."""
    return [
        dict(build_id="b-1", impact="impactful", question="features", cost_usd=900.0,
             bugs=1, loc_added=420.0, loc_removed=60.0, loc_total=480.0,
             lead_time_days=3.0, usage=1840.0, deployed=True, served=True),
        dict(build_id="b-2", impact="liability", question="correctness",
             cost_usd=300.0, bugs=0, loc_added=150.0, loc_removed=20.0,
             loc_total=170.0, lead_time_days=None, usage=0.0, deployed=True,
             served=False),
        dict(build_id="b-3", impact="carried", question="features", cost_usd=None,
             bugs=None, loc_added=80.0, loc_removed=10.0, loc_total=90.0,
             lead_time_days=None, usage=None, deployed=False, served=False),
        dict(build_id="b-4", impact="low_impact", question="performance",
             cost_usd=200.0, bugs=0, loc_added=60.0, loc_removed=5.0,
             loc_total=65.0, lead_time_days=9.0, usage=12.0, deployed=True,
             served=True),
    ]


class TestArithmetic(unittest.TestCase):
    def evaluate(self, expression: str):
        return F.Formula("t", expression).evaluate(builds())

    def test_aggregate_over_column(self):
        self.assertAlmostEqual(self.evaluate("sum(cost_usd)").value, 1400.0)
        self.assertAlmostEqual(self.evaluate("count()").value, 4.0)

    def test_null_is_skipped_and_counted_not_zeroed(self):
        # b-3 has no cost. The mean is over the three that do, and the tool says
        # how many it skipped -- a null and a zero are different claims.
        result = next(m for m in F.default_metrics() if m.name == "cost").formula.evaluate(builds())
        self.assertAlmostEqual(result.value, 1400.0 / 3)
        self.assertEqual(self.evaluate("sum(cost_usd)").nulls_skipped, 1)

    def test_aggregate_of_all_nulls_is_none_not_zero(self):
        rows = [dict(build_id="x", cost_usd=None, impact="sunk", loc_total=1.0)]
        self.assertIsNone(F.Formula("t", "sum(cost_usd)").evaluate(rows).value)

    def test_elementwise_inside_an_aggregate(self):
        self.assertAlmostEqual(
            self.evaluate("sum(loc_added + loc_removed)").value, 805.0
        )

    def test_median_and_percentiles_over_defined_values_only(self):
        # Only b-1 (3d) and b-4 (9d) have a lead time.
        self.assertAlmostEqual(self.evaluate("median(lead_time_days)").value, 6.0)
        self.assertAlmostEqual(self.evaluate("p90(lead_time_days)").value, 8.4)

    def test_class_sugar_and_explicit_predicate_agree(self):
        self.assertAlmostEqual(self.evaluate("share(liability)").value, 0.25)
        self.assertAlmostEqual(
            self.evaluate("count(impact == liability)").value, 1.0
        )
        self.assertAlmostEqual(self.evaluate("share(features)").value, 0.5)

    def test_division_by_zero_is_null_with_a_note(self):
        result = self.evaluate("sum(cost_usd) / count(impact == sunk)")
        self.assertIsNone(result.value)
        self.assertTrue(any("division by zero" in n for n in result.notes))

    def test_minmax_needs_two_passes(self):
        metric = F.Metric(
            "debt", "objective", "min",
            "minmax(sum(bugs) / (sum(loc_total) / 1000))",
        ).compile()
        assert metric.formula is not None
        cell_a, cell_b = builds(), builds()[:2]
        pass1 = [metric.formula.evaluate(c) for c in (cell_a, cell_b)]
        params = F.norm_params_from_deferred(
            metric.formula, [r.deferred for r in pass1]
        )
        values = [metric.formula.evaluate(c, params).value for c in (cell_a, cell_b)]
        # Two cells, so min-max sends one to 0 and the other to 1.
        self.assertEqual(sorted(values), [0.0, 1.0])

    def test_minmax_over_a_constant_objective_is_a_half_not_a_crash(self):
        metric = F.Metric("d", "objective", "min", "minmax(sum(bugs))").compile()
        assert metric.formula is not None
        cells = [builds(), builds()]
        pass1 = [metric.formula.evaluate(c) for c in cells]
        params = F.norm_params_from_deferred(
            metric.formula, [r.deferred for r in pass1]
        )
        result = metric.formula.evaluate(cells[0], params)
        self.assertEqual(result.value, 0.5)
        self.assertTrue(any("constant objective" in n for n in result.notes))


class TestRefusals(unittest.TestCase):
    """Every one of these is a way a formula could stop being data."""

    REJECTED = [
        ("import", "__import__('os').system('ls')"),
        ("attribute access", "cost_usd.real"),
        ("subscript", "sum(cost_usd)[0]"),
        ("call outside the allowlist", "open('/etc/passwd')"),
        ("unknown name", "sum(salary)"),
        ("comprehension", "[c for c in cost_usd]"),
        ("conditional", "sum(cost_usd) if 1 else 0"),
        ("lambda", "lambda: 1"),
        ("walrus", "(x := 1)"),
        ("f-string", "f'{cost_usd}'"),
        ("dict literal", "{'a': 1}"),
        ("chained comparison", "share(3 < 4 < 5)"),
        ("bad class name", "count(bogus_class)"),
        ("bare column in scalar position", "cost_usd"),
        ("wrong arity", "median(loc_added, 2)"),
        ("keyword argument", "sum(x=cost_usd)"),
        ("nested function call as a name", "sum(unknown_fn(cost_usd))"),
    ]

    def test_every_unsafe_or_unknown_form_is_refused(self):
        for label, expression in self.REJECTED:
            with self.subTest(label):
                with self.assertRaises((F.FormulaError, SyntaxError)):
                    F.Formula("t", expression)

    def test_the_message_names_the_problem(self):
        with self.assertRaises(F.FormulaError) as caught:
            F.Formula("t", "cost_usd.real")
        self.assertIn("Attribute", str(caught.exception))

    def test_an_objective_needs_a_direction(self):
        with self.assertRaises(F.FormulaError):
            F.Metric("x", "objective", "", "count()").compile()

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(F.FormulaError):
            F.Metric("x", "whatever", "min", "count()").compile()


class TestDependenciesAndOverlap(unittest.TestCase):
    def test_dependency_graph_is_static(self):
        metric = F.Metric(
            "debt_composite", "objective", "min",
            "0.5 * minmax(sum(bugs) / (sum(loc_total) / 1000)) "
            "+ 0.5 * (share(liability) + share(sunk))",
        ).compile()
        assert metric.formula is not None
        deps = metric.formula.deps
        self.assertEqual(deps.mix_parts, {"liability", "sunk"})
        self.assertIn("bugs", deps.columns)
        self.assertIn("minmax", deps.functions)

    def test_structural_overlap_is_found_before_any_data_is_read(self):
        objectives = [m for m in F.default_metrics() if m.role == "objective"]
        overlaps = F.overlap_report(objectives)
        lines = [o.line() for o in overlaps]
        self.assertIn(
            "OVERLAP: debt_composite <- share(liability), share(sunk)", lines
        )
        greyed = F.greyed_spokes(overlaps)
        self.assertEqual(greyed["liability"], ["debt_composite"])
        self.assertEqual(greyed["sunk"], ["debt_composite"])

    def test_the_overlap_free_alternative_is_clean(self):
        debt = [m for m in F.default_metrics() if m.name == "debt_defect"]
        self.assertEqual(
            [o.line() for o in F.overlap_report(debt)], [],
            "debt_defect is the overlap-free alternative and must report nothing",
        )

    def test_a_timing_objective_is_a_shared_input_not_an_overlap(self):
        # lead_time reads served_at/deployed_at through lead_time_days, which the
        # gate also reads. That is worth printing and not worth greying a spoke
        # over, or every timing metric would be flagged and the real finding
        # would drown.
        metric = F.Metric(
            "lt", "objective", "min", "median(lead_time_days)"
        ).compile()
        overlaps = F.overlap_report([metric])
        self.assertTrue(all(o.kind != "mix_part" for o in overlaps))
        self.assertEqual(F.greyed_spokes(overlaps), {})


class TestShippedDefaults(unittest.TestCase):
    def test_every_default_compiles(self):
        for metric in F.default_metrics():
            self.assertIsNotNone(metric.formula, metric.name)

    def test_sample_metrics_csv_matches_the_shipped_defaults(self):
        """The committed CSV is what a team edits, so it must not drift."""
        path = ROOT / "sample" / "metrics.csv"
        if not path.exists():
            self.skipTest("sample/metrics.csv not generated yet")
        with open(path, newline="", encoding="utf-8") as handle:
            rows = {r["name"]: r for r in csv.DictReader(handle)}
        for metric in F.DEFAULT_METRICS:
            self.assertIn(metric.name, rows)
            self.assertEqual(rows[metric.name]["expression"], metric.expression)
            self.assertEqual(rows[metric.name]["role"], metric.role)
            self.assertEqual(int(rows[metric.name]["version"]), metric.version)

    def test_debt_defect_is_defect_density_not_its_inverse(self):
        """`debt` term one reads "total LOC / bugs", which is inverted for a
        minimise axis: more LOC per bug is better. Shipped as bugs per KLOC."""
        metric = next(m for m in F.default_metrics() if m.name == "debt_defect")
        self.assertIn("sum(bugs)", metric.expression)
        expression_after_divide = metric.expression.split("/", 1)[1]
        self.assertIn("loc_total", expression_after_divide)


if __name__ == "__main__":
    unittest.main(verbosity=2)
