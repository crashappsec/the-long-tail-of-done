#!/usr/bin/env python3
"""The front, against fronts it was not told about.

Every test here runs the real pipeline over a generated fixture and compares
the result to that fixture's manifest. The manifests are written by
`sample/make-sample.py` from geometry -- radii on a DTLZ2 sphere octant -- and
never from the tool's own output, so agreement means something.

Run: python3 tests/run.py
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from decision_surface import formula as F
from decision_surface import surface as SU

FIXTURE_DIR = Path(__file__).resolve().parent / ".fixtures"
# Small but not tiny: the shipped default is 2000, and these assertions are
# about structure rather than the third decimal of a probability.
TEST_BOOTSTRAP = 300
TEST_PERMUTATIONS = 300


def make_fixture(name: str) -> tuple[list[dict], dict]:
    """Generate a fixture once per session and cache it under tests/.fixtures."""
    FIXTURE_DIR.mkdir(exist_ok=True)
    csv_path = FIXTURE_DIR / f"{name}.csv"
    manifest_path = FIXTURE_DIR / f"{name}.manifest.json"
    if not csv_path.exists():
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "sample" / "make-sample.py"),
                "--fixture",
                name,
                "--out",
                str(csv_path),
                "--manifest",
                str(manifest_path),
            ],
            check=True,
            capture_output=True,
        )
    return (
        SU.read_builds_csv(csv_path),
        json.loads(manifest_path.read_text(encoding="utf-8")),
    )


def run(
    rows: list[dict],
    manifest: dict,
    objectives: list[str] | None = None,
    **overrides,
) -> dict:
    """The pipeline, with the fixture's own declared parameters."""
    args = _manifest_args(manifest)
    args.update(overrides)
    params = SU.ComputeParams(
        objectives=objectives or SU.DEFAULT_OBJECTIVES,
        bootstrap=args.pop("bootstrap", TEST_BOOTSTRAP),
        permutations=args.pop("permutations", TEST_PERMUTATIONS),
        **args,
    )
    return SU.compute(rows, F.default_metrics(), params, run_id=f"r-test")


def _manifest_args(manifest: dict) -> dict:
    """A fixture declares how it must be run; honour that rather than guessing."""
    args: dict = {"as_of": SU.parse_ts(manifest["as_of"])}
    declared = manifest.get("run_with") or []
    for flag, value in zip(declared, declared[1:]):
        if flag == "--u":
            args["u"] = float(value)
        elif flag == "--min-builds":
            args["min_builds"] = int(value)
    return args


def front_of(analysis: dict, kind: str, **match) -> list[str]:
    for comparison in analysis["comparisons"]:
        if comparison["kind"] != kind:
            continue
        if all(comparison.get(k) == v for k, v in match.items()):
            return sorted(comparison.get("front", []))
    raise AssertionError(f"no {kind} comparison matching {match}")


class TestGroundTruth(unittest.TestCase):
    """Does it recover a planted front."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("ground-truth")
        cls.analysis = run(cls.rows, cls.manifest)
        cls.planted = cls.manifest["planted"]

    def test_latest_window_front_is_the_planted_one(self):
        latest = self.analysis["windows"][-1]
        self.assertEqual(
            front_of(self.analysis, "across_groups", window=latest),
            sorted(self.planted["front_latest_window"]),
        )

    def test_dominated_cells_are_off_the_front_in_every_window(self):
        for window, dominated in self.planted["dominated_per_window"].items():
            front = front_of(self.analysis, "across_groups", window=window)
            for label in dominated:
                self.assertNotIn(label, front, f"{label} should be dominated")

    def test_within_group_fronts_are_the_planted_ones(self):
        for group, expected in self.planted["within_group_front"].items():
            self.assertEqual(
                front_of(self.analysis, "within_group", group=group),
                sorted(expected),
                f"within-group front for {group}",
            )

    def test_layers_separate_rather_than_collapsing_to_a_flag(self):
        comparison = next(
            c for c in self.analysis["comparisons"]
            if c["kind"] == "within_group" and c.get("group") == "agent-heavy"
        )
        layers = sorted(v for v in comparison["layers"].values() if v)
        self.assertEqual(
            layers, list(range(1, 7)),
            "a monotone trajectory is a total order, so its six windows are six layers",
        )

    def test_direction_of_travel_matches_the_planted_trajectories(self):
        angles: dict[str, list[float]] = {}
        for step in self.analysis["stage7"]["direction_of_travel"]:
            if step.get("available"):
                angles.setdefault(step["group"], []).append(step["angle_to_ideal_deg"])
        improving = F.median(angles["agent-heavy"]) or 0.0
        worsening = F.median(angles["legacy-owner"]) or 0.0
        trading = F.median(angles["review-first"]) or 0.0
        self.assertLess(improving, 45, "improving on all three is a small angle")
        self.assertGreater(worsening, 135, "worsening on all three is a large angle")
        self.assertTrue(
            45 < trading < 135, f"a trade is a mid angle, got {trading}"
        )

    def test_p_on_front_is_ordered_by_how_planted_the_membership_is(self):
        latest = self.analysis["windows"][-1]
        comparison = next(
            c for c in self.analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        probabilities = comparison["p_on_front"]
        for label in self.planted["front_latest_window"]:
            for other in self.planted["dominated_latest_window"]:
                self.assertGreater(
                    probabilities[label], probabilities[other],
                    f"{label} is planted on the front and {other} is not",
                )

    def test_the_knee_is_a_cell_on_the_front(self):
        latest = self.analysis["windows"][-1]
        comparison = next(
            c for c in self.analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        knee = comparison["relaxation_ladder"]["knees"]["knee"]
        self.assertIn(knee, comparison["front"])

    def test_skyline_frequency_ranks_the_front_above_the_dominated(self):
        latest = self.analysis["windows"][-1]
        comparison = next(
            c for c in self.analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        scores = comparison["relaxation_ladder"]["skyline_frequency"]["scores"]
        for label in self.planted["front_latest_window"]:
            for other in self.planted["dominated_latest_window"]:
                self.assertGreater(scores[label], scores[other])

    def test_explain_names_the_cells_that_beat_a_dominated_one(self):
        dominated = self.planted["dominated_latest_window"][0]
        explanation = SU.explain_cell(self.analysis, dominated)
        across = next(
            c for c in explanation["comparisons"] if c["kind"] == "across_groups"
        )
        self.assertFalse(across["on_front"])
        self.assertTrue(across["dominated_by"])
        self.assertIn("binding_objective", across)

    def test_normalisation_does_not_move_front_membership(self):
        """Dominance is invariant under any monotone per-objective transform, and
        the tool prints that claim on every run. Here it is, checked."""
        latest = self.analysis["windows"][-1]
        cells = [
            c for c in self.analysis["cells"]
            if c["window"] == latest and not c["below_floor"]
        ]
        names = self.analysis["run"]["objectives"]
        normalised = [[c["objectives_norm"][n] for n in names] for c in cells]
        # A monotone transform per axis: cube the min-max values, then rescale.
        transformed = [[(v or 0.0) ** 3 for v in row] for row in normalised]
        self.assertEqual(
            SU.front_indices(normalised), SU.front_indices(transformed)
        )

    def test_hypervolume_reports_its_reference_point(self):
        latest = self.analysis["windows"][-1]
        comparison = next(
            c for c in self.analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        self.assertEqual(
            comparison["hypervolume"]["reference_point"], SU.HV_REFERENCE
        )
        self.assertGreater(comparison["hypervolume"]["value"], 0.0)


class TestDegeneracy(unittest.TestCase):
    """"Half the cells are on the front" has to be a diagnosis, not a shrug."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("degeneracy")
        cls.analysis = run(cls.rows, cls.manifest)

    def test_the_null_model_fires_on_independent_objectives(self):
        verdicts = [
            c["null_model"]["verdict"]
            for c in cls_comparisons(self.analysis)
            if c.get("null_model", {}).get("available")
        ]
        self.assertTrue(verdicts, "no comparison produced a null model")
        uninformative = sum(1 for v in verdicts if v == "NOT INFORMATIVE")
        self.assertGreaterEqual(
            uninformative, 2 * len(verdicts) // 3,
            f"objectives are independent draws, so most comparisons should read "
            f"NOT INFORMATIVE; got {verdicts}",
        )

    def test_the_closed_form_agrees_with_the_permutation_null(self):
        """BKST 1978 is the sanity check on the permutation code, and both are
        reported. If they disagree, one of them is wrong."""
        for comparison in cls_comparisons(self.analysis):
            null_model = comparison.get("null_model", {})
            if not null_model.get("available"):
                continue
            closed = null_model["closed_form_A_n_d"]
            observed_mean = null_model["permutation_mean"]
            self.assertAlmostEqual(
                closed, observed_mean, delta=0.6,
                msg=f"closed form {closed} vs permutation mean {observed_mean} "
                f"for n={null_model['n_cells']} d={null_model['n_objectives']}",
            )


def cls_comparisons(analysis: dict) -> list[dict]:
    return analysis.get("comparisons", [])


class TestCensoring(unittest.TestCase):
    """Right-censoring is the difference between "we shipped no waste" and "we
    have not looked yet"."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("censoring")
        cls.analysis = run(cls.rows, cls.manifest, bootstrap=50)

    def test_the_unresolved_share_rises(self):
        shares = [
            c["impact_mix"]["unresolved"]
            for c in sorted(
                self.analysis["cells"], key=lambda c: SU.window_sort_key(c["window"])
            )
        ]
        self.assertEqual(shares, sorted(shares), f"not monotone: {shares}")
        self.assertGreater(shares[-1], 0.5)
        self.assertEqual(shares[0], 0.0)

    def test_the_mix_is_never_renormalised(self):
        for cell in self.analysis["cells"]:
            self.assertAlmostEqual(sum(cell["impact_mix"].values()), 1.0, places=9)

    def test_the_resolved_parts_keep_their_ratios(self):
        """Nothing about the team changed; only the observation edge moved. So
        the parts under the unresolved share must be squeezed proportionally,
        not rescaled to fill the gap."""
        ratios = []
        for cell in self.analysis["cells"]:
            mix = cell["impact_mix"]
            if mix["low_impact"] > 0 and mix["unresolved"] < 1.0:
                ratios.append(mix["impactful"] / mix["low_impact"])
        self.assertGreater(len(ratios), 3)
        for ratio in ratios[1:]:
            self.assertAlmostEqual(ratio, ratios[0], places=6)

    def test_unresolved_overrides_every_other_class(self):
        """A build younger than L is unresolved whatever else is true of it --
        the gate checks that first, and never lets unresolved default to sunk."""
        as_of = SU.parse_ts(self.manifest["as_of"])
        builds, gate, _, _ = SU.prepare_builds(self.rows, as_of=as_of, u=1000.0)
        young = [b for b in builds if b["age_days"] < gate.L_days]
        self.assertTrue(young)
        self.assertTrue(all(b["impact"] == "unresolved" for b in young))


class TestUnclassifiedQuestion(unittest.TestCase):
    """A mix that quietly adds up to 1 after dropping a part is the thing this
    tool refuses to do."""

    def rows(self) -> list[dict]:
        questions = ["features", "correctness", "refactor", "chore", "performance"]
        return [
            dict(
                build_id=f"b-{i}",
                group="one-team",
                first_commit_ts="2026-01-01",
                ts="2026-01-05",
                deployed_at="2026-01-06",
                served_at="2026-01-07",
                usage=5000,
                loc_added=100,
                loc_removed=10,
                bugs=0,
                cost_usd=100,
                question=q,
                carried_into="",
            )
            for i, q in enumerate(questions)
        ]

    def test_an_off_enum_question_is_reported_and_not_renormalised(self):
        params = SU.ComputeParams(
            objectives=["cost", "lead_time"],
            as_of=SU.parse_ts("2026-06-01"),
            u=1000.0,
            bootstrap=0,
            permutations=20,
            min_builds=1,
        )
        analysis = SU.compute(self.rows(), F.default_metrics(), params)
        cell = analysis["cells"][0]
        self.assertAlmostEqual(cell["question_unclassified_share"], 0.4)
        self.assertAlmostEqual(sum(cell["question_mix"].values()), 0.6, places=9)
        codes = [c["code"] for c in analysis["diagnostics"]["cautions"]]
        self.assertIn("QUESTION_UNCLASSIFIED", codes)
        warnings = [w["code"] for w in analysis["diagnostics"]["warnings"]]
        self.assertIn("UNKNOWN_QUESTION", warnings)

    def test_the_impact_mix_always_sums_to_one(self):
        """The gate assigns one of six classes to every build, so unlike the
        question mix there is no gap to report."""
        params = SU.ComputeParams(
            objectives=["cost", "lead_time"],
            as_of=SU.parse_ts("2026-06-01"),
            u=1000.0,
            bootstrap=0,
            permutations=20,
            min_builds=1,
        )
        analysis = SU.compute(self.rows(), F.default_metrics(), params)
        for cell in analysis["cells"]:
            self.assertAlmostEqual(sum(cell["impact_mix"].values()), 1.0, places=9)


class TestThresholdSensitivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("threshold")
        cls.analysis = run(cls.rows, cls.manifest, bootstrap=50)

    def test_u_is_self_calibrating_and_says_so(self):
        gate = self.analysis["gate"]
        self.assertIsNotNone(gate["u"])
        self.assertIn("median usage", gate["u_source"])

    def test_a_flipping_mix_is_marked_provisional(self):
        sensitivity = self.analysis["sensitivity"]
        self.assertTrue(sensitivity["available"])
        self.assertTrue(sensitivity["verdict"].startswith("PROVISIONAL"))
        self.assertTrue(sensitivity["flips"])

    def test_all_three_levels_are_computed(self):
        levels = self.analysis["sensitivity"]["levels"]
        self.assertEqual(sorted(levels), ["0.5u", "2u", "u"])
        self.assertAlmostEqual(levels["2u"]["u"], 4 * levels["0.5u"]["u"])


class TestOverlap(unittest.TestCase):
    """Two independent checks on double counting: one from the definitions, one
    from the data, and a run reports both."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("overlap")
        cls.analysis = run(
            cls.rows, cls.manifest,
            objectives=["cost", "lead_time", "debt_composite"],
            bootstrap=50,
        )

    def test_structural_overlap_is_reported(self):
        self.assertIn(
            "OVERLAP: debt_composite <- share(liability), share(sunk)",
            self.analysis["stage0"]["overlaps"],
        )

    def test_the_overlapping_spokes_are_marked_for_greying(self):
        greyed = self.analysis["stage0"]["greyed_spokes"]
        self.assertIn("liability", greyed)
        self.assertIn("sunk", greyed)

    def test_stage_two_measures_the_same_overlap_in_the_data(self):
        taus = self.analysis["stage2"]["objective_vs_descriptor"]["debt_composite"]
        self.assertGreater(
            taus["share(liability)"], 0.7,
            "the declared overlap must show up as a measured tau as well",
        )

    def test_the_overlap_report_precedes_the_numbers(self):
        """Stage 0 is computed from the formulas, so it must be present even when
        the data is refused outright."""
        params = SU.ComputeParams(objectives=["cost"] * 9)
        refused = SU.compute(self.rows, F.default_metrics(), params)
        self.assertTrue(refused["diagnostics"]["refusals"])
        self.assertIn("overlaps", refused["stage0"])


class TestRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("ground-truth")

    def test_too_many_objectives_is_refused(self):
        names = [m.name for m in F.default_metrics() if m.role == "objective"]
        analysis = run(
            self.rows, self.manifest, objectives=(names * 4)[:9], bootstrap=0
        )
        codes = [r["code"] for r in analysis["diagnostics"]["refusals"]]
        self.assertIn("TOO_MANY_OBJECTIVES", codes)

    def test_one_objective_is_a_ranking_not_a_front(self):
        analysis = run(self.rows, self.manifest, objectives=["cost"], bootstrap=0)
        codes = [r["code"] for r in analysis["diagnostics"]["refusals"]]
        self.assertIn("TOO_FEW_OBJECTIVES", codes)

    def test_cells_below_the_build_floor_are_reported_and_not_plotted(self):
        analysis = run(self.rows, self.manifest, min_builds=15, bootstrap=0)
        below = [c["label"] for c in analysis["cells"] if c["below_floor"]]
        self.assertTrue(below)
        codes = [c["code"] for c in analysis["diagnostics"]["cautions"]]
        self.assertIn("BELOW_BUILD_FLOOR", codes)
        for comparison in analysis["comparisons"]:
            for label in below:
                self.assertNotIn(label, comparison["cells"])

    def test_a_front_with_too_few_cells_is_refused(self):
        """m + 1 cells before a front may be drawn at all."""
        rows = [r for r in self.rows if r["group"] in ("agent-heavy", "review-first")]
        analysis = run(rows, self.manifest, bootstrap=0)
        across = [c for c in analysis["comparisons"] if c["kind"] == "across_groups"]
        self.assertTrue(across)
        for comparison in across:
            self.assertIn(
                "TOO_FEW_CELLS", [r["code"] for r in comparison["refusals"]]
            )
            self.assertNotIn("front", comparison)

    def test_explain_does_not_answer_a_question_the_tool_refused_to_ask(self):
        """With fewer than m + 1 cells no front is drawn, so a cell in that
        comparison is neither on it nor off it. The old wording said "sits on
        layer None, so it is dominated once earlier layers are peeled", which
        is both false and a number a reader would quote."""
        rows = [r for r in self.rows if r["group"] in ("agent-heavy", "review-first")]
        analysis = run(rows, self.manifest, bootstrap=0)
        explanation = SU.explain_cell(analysis, "review-first/2026-04")
        across = next(
            c for c in explanation["comparisons"] if c["kind"] == "across_groups"
        )
        self.assertTrue(across["front_not_drawn"])
        self.assertTrue(across["refusals"])
        self.assertIn("neither", across["reading"])
        self.assertNotIn("layer None", across["reading"])
        # The within-group comparison has enough cells and still answers.
        within = next(
            c for c in explanation["comparisons"] if c["kind"] == "within_group"
        )
        self.assertNotIn("front_not_drawn", within)
        self.assertIn("front", within["reading"])

    def test_divergent_lead_time_sources_are_refused(self):
        """One group measured to the first user, another measured to deploy,
        manufactures exactly the difference the front then reports."""
        rows = []
        for row in self.rows:
            row = dict(row)
            # Strip served_at from one group only: its lead times fall back to
            # DEPLOYED while everyone else's stay SERVED.
            if row["group"] == "legacy-owner" and row.get("served_at"):
                row["served_at"] = None
            rows.append(row)
        analysis = run(rows, self.manifest, bootstrap=0)
        codes = [r["code"] for r in analysis["diagnostics"]["refusals"]]
        self.assertIn("DIVERGENT_LEAD_TIME_SOURCE", codes)

    def test_the_refusal_can_be_overridden_and_then_it_is_stamped(self):
        rows = []
        for row in self.rows:
            row = dict(row)
            if row["group"] == "legacy-owner" and row.get("served_at"):
                row["served_at"] = None
            rows.append(row)
        analysis = run(
            rows, self.manifest, bootstrap=0, allow_mixed_lead_time=True
        )
        self.assertNotIn(
            "DIVERGENT_LEAD_TIME_SOURCE",
            [r["code"] for r in analysis["diagnostics"]["refusals"]],
        )
        self.assertIn(
            "DIVERGENT_LEAD_TIME_SOURCE",
            [c["code"] for c in analysis["diagnostics"]["cautions"]],
        )

    def test_a_liability_build_has_no_lead_time_at_all(self):
        """It was deployed and used by nobody, so there was no first user to
        measure to. Recording the deploy interval instead would make the groups
        with the most waste look the fastest."""
        builds, _, _, _ = SU.prepare_builds(
            self.rows, as_of=SU.parse_ts(self.manifest["as_of"]), u=1000.0
        )
        liabilities = [b for b in builds if b["impact"] == "liability"]
        self.assertTrue(liabilities)
        for build in liabilities:
            self.assertIsNone(build["lead_time_days"])
            self.assertIsNone(build["lead_time_source"])


class TestSelfComparison(unittest.TestCase):
    """Most teams are one team, and the tool has to work on day one for them."""

    @classmethod
    def setUpClass(cls) -> None:
        rows, cls.manifest = make_fixture("ground-truth")
        cls.rows = [dict(r, group=None) for r in rows]

    def test_no_group_column_still_produces_a_front(self):
        analysis = run(self.rows, self.manifest, bootstrap=50)
        self.assertEqual(analysis["groups"], ["all"])
        self.assertEqual(analysis["primary_comparison"], "within_group")
        self.assertIn("self-comparison", analysis["mode"])
        within = [c for c in analysis["comparisons"] if c["kind"] == "within_group"]
        self.assertEqual(len(within), 1)
        self.assertTrue(within[0]["front"])

    def test_persistence_says_why_it_is_not_applicable(self):
        analysis = run(self.rows, self.manifest, bootstrap=0)
        self.assertIn("note", analysis["stage7"]["persistence"])


class TestMathematics(unittest.TestCase):
    """The pieces that are a few dozen lines each and have to be right."""

    def test_bkst_recurrence_base_cases(self):
        self.assertEqual(SU.bkst_expected_maxima(1, 5), 1.0)
        self.assertEqual(SU.bkst_expected_maxima(7, 1), 1.0)

    def test_bkst_two_dimensions_is_the_harmonic_number(self):
        for n in (2, 5, 10, 40):
            harmonic = sum(1.0 / k for k in range(1, n + 1))
            self.assertAlmostEqual(SU.bkst_expected_maxima(n, 2), harmonic, places=9)

    def test_kendall_tau_b_endpoints(self):
        ascending = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(SU.kendall_tau_b(ascending, ascending), 1.0)
        self.assertAlmostEqual(
            SU.kendall_tau_b(ascending, list(reversed(ascending))), -1.0
        )

    def test_kendall_tau_b_handles_ties(self):
        """tau-a would read a tie as disagreement, and cells tie constantly."""
        tied = [1.0, 1.0, 1.0, 2.0, 3.0]
        other = [1.0, 1.0, 1.0, 2.0, 3.0]
        self.assertAlmostEqual(SU.kendall_tau_b(tied, other), 1.0)

    def test_dominance_is_strict(self):
        self.assertTrue(SU.dominates([1.0, 1.0], [2.0, 2.0]))
        self.assertTrue(SU.dominates([1.0, 2.0], [1.0, 3.0]))
        self.assertFalse(SU.dominates([1.0, 2.0], [1.0, 2.0]))
        self.assertFalse(SU.dominates([1.0, 3.0], [2.0, 2.0]))

    def test_a_null_objective_keeps_a_cell_out_of_the_order(self):
        matrix = [[1.0, 1.0], [0.5, None], [2.0, 2.0]]
        self.assertEqual(SU.usable_rows(matrix), [0, 2])
        self.assertEqual(SU.nondominated_layers(matrix), [1, None, 2])

    def test_hypervolume_of_one_point_is_the_box(self):
        result = SU.hypervolume([[0.1, 0.1]], reference=1.1)
        self.assertAlmostEqual(result["value"], 1.0)
        self.assertEqual(result["method"], "exact (inclusion-exclusion)")

    def test_hypervolume_inclusion_exclusion_does_not_double_count(self):
        # Two nested boxes: the smaller is inside the larger, so the union is
        # the larger one alone.
        result = SU.hypervolume([[0.1, 0.1], [0.5, 0.5]], reference=1.1)
        self.assertAlmostEqual(result["value"], 1.0)

    def test_epsilon_dominance_boxes_the_space(self):
        matrix = [[0.10, 0.10], [0.11, 0.11], [0.90, 0.90]]
        strict = SU.front_indices(matrix)
        boxed = SU.epsilon_dominance_front(matrix, [0.2, 0.2])
        self.assertEqual(strict, [0])
        # Same box, so neither beats the other by more than we can measure.
        self.assertEqual(sorted(boxed), [0, 1])

    def test_k_dominance_cycles_are_reported_not_broken(self):
        """k-dominance is not transitive, so the result is a set and not an
        order. A cycle must surface rather than being silently resolved."""
        matrix = [[0.0, 1.0, 1.0], [1.0, 0.0, 1.0], [1.0, 1.0, 0.0]]
        _, cycles = SU.k_dominance_front(matrix, 2)
        self.assertTrue(cycles, "three-way rock-paper-scissors must be reported")

    def test_skyline_frequency_counts_every_non_empty_subspace(self):
        matrix = [[0.0, 0.0], [1.0, 1.0]]
        counts, subspaces = SU.skyline_frequency(matrix)
        self.assertEqual(subspaces, 3)  # 2^2 - 1
        self.assertEqual(counts[0], 3)
        self.assertEqual(counts[1], 0)

    def test_clr_replaces_zeros_and_reports_the_replacement(self):
        mix = {"a": 0.5, "b": 0.5, "c": 0.0}
        coordinates = SU.clr(mix, n_builds=20)
        self.assertIsNotNone(coordinates)
        assert coordinates is not None
        self.assertAlmostEqual(sum(coordinates.values()), 0.0, places=9)
        self.assertLess(coordinates["c"], coordinates["a"])

    def test_aitchison_distance_is_zero_for_an_unchanged_mix(self):
        mix = SU.clr({"a": 0.4, "b": 0.4, "c": 0.2}, 20)
        assert mix is not None
        distance, _ = SU.aitchison_distance(mix, mix)
        self.assertIsNotNone(distance)  # not `distance or ...`: 0.0 is the answer
        self.assertAlmostEqual(distance, 0.0, places=12)  # type: ignore[arg-type]

    def test_aitchison_distance_names_the_moving_log_ratio(self):
        before = SU.clr({"a": 0.4, "b": 0.4, "c": 0.2}, 20)
        after = SU.clr({"a": 0.4, "b": 0.2, "c": 0.4}, 20)
        assert before is not None and after is not None
        distance, top = SU.aitchison_distance(before, after)
        self.assertGreater(distance or 0.0, 0.0)
        assert top is not None
        self.assertEqual(top["ratio"], "b:c")

    def test_delta_moss_charges_for_a_dropped_axis(self):
        """Dropping an axis never loses a dominance relation -- it invents ones
        the full set does not have, and delta is the size of the invention."""
        matrix = [[0.0, 1.0], [1.0, 0.0]]
        result = SU.delta_moss(matrix, ["a", "b"])
        ladder = {rung["size"]: rung["delta"] for rung in result["ladder"]}
        self.assertAlmostEqual(ladder[1], 1.0)
        self.assertAlmostEqual(ladder[2], 0.0)
        self.assertEqual(result["smallest_exact_subset"], ["a", "b"])

    def test_delta_moss_is_zero_for_a_genuinely_redundant_axis(self):
        # b is a copy of a, so either one alone reproduces the full relation.
        matrix = [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]]
        result = SU.delta_moss(matrix, ["a", "b"])
        self.assertAlmostEqual(result["ladder"][0]["delta"], 0.0)
        self.assertEqual(len(result["smallest_exact_subset"] or []), 1)

    def test_average_ranks_are_scaled_and_keep_nulls(self):
        ranks = SU.average_ranks([10.0, None, 30.0, 20.0])
        self.assertIsNone(ranks[1])
        self.assertAlmostEqual(ranks[0], 0.0)
        self.assertAlmostEqual(ranks[2], 1.0)
        self.assertAlmostEqual(ranks[3], 0.5)

    def test_gaussian_solver_matches_a_known_system(self):
        solution = SU.solve_linear([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
        assert solution is not None
        self.assertAlmostEqual(solution[0], 1.0)
        self.assertAlmostEqual(solution[1], 3.0)

    def test_gaussian_solver_returns_none_when_singular(self):
        self.assertIsNone(SU.solve_linear([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]))

    def test_window_labels_sort_chronologically(self):
        labels = ["2026-01", "2025-12", "2026-10", "2026-02"]
        self.assertEqual(
            sorted(labels, key=SU.window_sort_key),
            ["2025-12", "2026-01", "2026-02", "2026-10"],
        )
        weeks = ["2026-W02", "2025-W52", "2026-W10"]
        self.assertEqual(
            sorted(weeks, key=SU.window_sort_key),
            ["2025-W52", "2026-W02", "2026-W10"],
        )


class TestUncertainty(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("ground-truth")

    def test_bootstrap_coverage_on_the_planted_front(self):
        """P(on front) has to be high for the cells genuinely on it and low for
        the ones planted underneath. Anything else and the interval is decoration."""
        analysis = run(self.rows, self.manifest, bootstrap=500)
        latest = analysis["windows"][-1]
        comparison = next(
            c for c in analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        planted = analysis and self.manifest["planted"]
        for label in planted["front_latest_window"]:
            self.assertGreater(comparison["p_on_front"][label], 0.5, label)
        for label in planted["dominated_latest_window"]:
            self.assertLess(comparison["p_on_front"][label], 0.5, label)

    def test_epsilon_comes_from_the_bootstrap_and_is_reported(self):
        analysis = run(self.rows, self.manifest, bootstrap=200)
        epsilon = analysis["stage6"]["epsilon"]
        self.assertIsNotNone(epsilon)
        for name, value in epsilon.items():
            self.assertGreater(value, 0.0, name)
        latest = analysis["windows"][-1]
        comparison = next(
            c for c in analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        ladder = comparison["relaxation_ladder"]["epsilon_dominance"]
        self.assertEqual(ladder["epsilon_source"], "bootstrap")
        self.assertGreaterEqual(ladder["size"], 1)

    def test_probabilistic_dominance_is_a_probability(self):
        analysis = run(self.rows, self.manifest, bootstrap=200)
        latest = analysis["windows"][-1]
        comparison = next(
            c for c in analysis["comparisons"]
            if c["kind"] == "across_groups" and c["window"] == latest
        )
        for row in comparison["probabilistic_dominance"].values():
            for value in row.values():
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_p_on_front_is_null_not_zero_when_it_cannot_be_computed(self):
        """A cell of one build cannot be resampled. Reporting 0.0 would say the
        cell is certainly off the front, which is a different claim."""
        rows = [dict(r) for r in self.rows]
        single = dict(rows[0])
        single["build_id"] = "lonely-1"
        single["group"] = "lonely"
        rows.append(single)
        analysis = run(rows, self.manifest, bootstrap=100, min_builds=1)
        codes = [c["code"] for c in analysis["diagnostics"]["cautions"]]
        self.assertIn("UNCERTAINTY_UNAVAILABLE", codes)
        lonely = next(c for c in analysis["cells"] if c["group"] == "lonely")
        self.assertIsNone(lonely["p_on_front"])

    def test_the_parametric_fallback_exists_for_aggregate_only_teams(self):
        """Half the audience has cell-level numbers and standard errors, and
        nothing else. Without this path they are locked out."""
        as_of = SU.parse_ts(self.manifest["as_of"])
        builds, _, _, _ = SU.prepare_builds(self.rows, as_of=as_of, u=1000.0)
        cells = SU.build_cells(builds)
        metrics = [m for m in F.default_metrics() if m.name in SU.DEFAULT_OBJECTIVES]
        SU.evaluate_cells(cells, metrics)
        names = [m.name for m in metrics]
        for name in names:
            raw = [c.objectives.get(name) for c in cells]
            scaled, _, _ = SU.minmax_scale(raw)
            for cell, value in zip(cells, scaled):
                cell.norm[name] = value
        errors = {
            c.key: {n: 0.05 for n in names} for c in cells
        }
        replicates = SU.parametric_replicates(
            cells, names, errors, 100, random.Random(1)
        )
        self.assertEqual(len(replicates), 100)
        probabilities, _, epsilon = SU.front_probabilities(
            [c.key for c in cells], replicates
        )
        self.assertTrue(any(v is not None for v in probabilities.values()))
        self.assertIsNotNone(epsilon)


class TestDeterminism(unittest.TestCase):
    def test_the_same_seed_gives_the_same_answer(self):
        rows, manifest = make_fixture("ground-truth")
        first = run(rows, manifest, bootstrap=100, seed=7)
        second = run(rows, manifest, bootstrap=100, seed=7)
        self.assertEqual(
            json.dumps(first["comparisons"], sort_keys=True, default=str),
            json.dumps(second["comparisons"], sort_keys=True, default=str),
        )

    def test_a_different_seed_moves_only_the_stochastic_parts(self):
        rows, manifest = make_fixture("ground-truth")
        first = run(rows, manifest, bootstrap=100, seed=7)
        second = run(rows, manifest, bootstrap=100, seed=8)
        for a, b in zip(first["cells"], second["cells"]):
            self.assertEqual(a["objectives"], b["objectives"])
            self.assertEqual(a["impact_mix"], b["impact_mix"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
