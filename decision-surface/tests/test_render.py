#!/usr/bin/env python3
"""The pictures: what they must not do.

The load-bearing test here is the **axis-swap test**. A radar is a
parallel-coordinates plot in polar coordinates, and both inherit a dependence
on the order of their axes: reorder the spokes and the silhouette changes
shape, so a reader can be talked into a conclusion that is an artefact of the
ordering. The defence is to make the *findings* independent of it and then
check that mechanically -- swap two adjacent axes, re-render, and require the
emitted findings block to be byte-identical.

The rest of the file is the other three ways a picture can lie: drawing a null
as a zero, plotting a cell the analysis refused to plot, and drawing a front
the analysis refused to draw.

Requires node on PATH. Skips, loudly, if it is missing.

Run: python3 tests/run.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from importlib.resources import files

ROOT = Path(__file__).resolve().parent.parent

from decision_surface import formula as F
from decision_surface import surface as SU

from test_front import make_fixture  # noqa: E402

RENDERER = files("decision_surface").joinpath("render.mjs")
FINDINGS = re.compile(
    r'<script type="application/json" id="findings">(.*?)</script>', re.S
)


def node_available() -> bool:
    return shutil.which("node") is not None


def render(analysis: dict, tmp: Path, name: str = "a") -> str:
    analysis_path = tmp / f"{name}.json"
    html_path = tmp / f"{name}.html"
    analysis_path.write_text(json.dumps(analysis, default=str), encoding="utf-8")
    result = subprocess.run(
        ["node", str(RENDERER), str(analysis_path), str(html_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"render.mjs failed: {result.stderr}")
    return html_path.read_text(encoding="utf-8")


def findings_of(html: str) -> dict:
    match = FINDINGS.search(html)
    assert match, "the rendered page carries no findings block"
    return json.loads(match.group(1).replace("\\u003c", "<"))


@unittest.skipUnless(node_available(), "node is not on PATH")
class RenderCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        rows, cls.manifest = make_fixture("ground-truth")
        params = SU.ComputeParams(
            objectives=SU.DEFAULT_OBJECTIVES,
            as_of=SU.parse_ts(cls.manifest["as_of"]),
            u=cls.manifest["pinned_u"],
            bootstrap=100,
            permutations=100,
        )
        cls.analysis = SU.compute(rows, F.default_metrics(), params, run_id="r-render")
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls.tmpdir.name)
        cls.html = render(cls.analysis, cls.tmp, "base")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()


class TestAxisSwap(RenderCase):
    """Swap two adjacent axes, redraw, and require the findings not to move."""

    def test_swapping_two_adjacent_spokes_changes_no_finding(self):
        swapped = json.loads(json.dumps(self.analysis, default=str))
        order = swapped["stage2"]["axis_order"]
        self.assertGreaterEqual(len(order), 2)
        order[0], order[1] = order[1], order[0]
        swapped["stage2"]["axis_order"] = order
        html = render(swapped, self.tmp, "swapped")
        self.assertEqual(
            json.dumps(findings_of(self.html), sort_keys=True),
            json.dumps(findings_of(html), sort_keys=True),
            "a conclusion moved when the axis order did, so it was an "
            "axis-order artefact rather than a finding",
        )

    def test_the_swap_really_does_change_the_drawing(self):
        """Guard against the test passing because nothing reads the axis order."""
        swapped = json.loads(json.dumps(self.analysis, default=str))
        order = swapped["stage2"]["axis_order"]
        order[0], order[1] = order[1], order[0]
        swapped["stage2"]["axis_order"] = order
        html = render(swapped, self.tmp, "swapped2")
        self.assertNotEqual(
            html, self.html, "the renderer ignored the axis order entirely"
        )

    def test_reversing_the_group_order_changes_no_finding(self):
        reversed_groups = json.loads(json.dumps(self.analysis, default=str))
        reversed_groups["groups"] = list(reversed(reversed_groups["groups"]))
        reversed_groups["cells"] = list(reversed(reversed_groups["cells"]))
        html = render(reversed_groups, self.tmp, "regrouped")
        self.assertEqual(
            json.dumps(findings_of(self.html), sort_keys=True),
            json.dumps(findings_of(html), sort_keys=True),
        )


class TestSelfContained(RenderCase):
    def test_nothing_is_fetched_from_a_network(self):
        for pattern in ("http://", "https://", "@import", "<iframe", "fetch("):
            self.assertNotIn(
                pattern, self.html, f"the page must be self-contained; found {pattern}"
            )

    def test_it_is_one_file_with_inline_svg_and_inline_css(self):
        self.assertIn("<style>", self.html)
        self.assertIn("<svg", self.html)
        self.assertNotIn("<link", self.html)
        self.assertNotIn("<script src", self.html)

    def test_both_themes_are_styled(self):
        self.assertIn("prefers-color-scheme: dark", self.html)
        self.assertIn('data-theme="dark"', self.html)
        self.assertIn('data-theme="light"', self.html)

    def test_no_blank_line_inside_an_svg_element(self):
        """A blank line ends a raw-HTML block in Markdown and silently drops the
        rest of it, so slide-bound output must never contain one."""
        for block in re.findall(r"<svg.*?</svg>", self.html, re.S):
            self.assertNotIn(
                "\n\n", block, "a blank line inside an SVG breaks Markdown embedding"
            )


class TestHonesty(RenderCase):
    def test_every_declared_refusal_appears_on_the_page(self):
        analysis = json.loads(json.dumps(self.analysis, default=str))
        analysis["diagnostics"]["refusals"].append(
            {"code": "SYNTHETIC_REFUSAL", "message": "planted by the test"}
        )
        html = render(analysis, self.tmp, "refused")
        self.assertIn("SYNTHETIC_REFUSAL", html)
        self.assertIn("planted by the test", html)
        self.assertIn("SYNTHETIC_REFUSAL", findings_of(html)["refusals"])

    def test_a_null_objective_is_a_gap_and_never_a_point_at_the_origin(self):
        analysis = json.loads(json.dumps(self.analysis, default=str))
        # A cell the pairwise panels actually draw -- they draw the latest
        # window, so nulling an older cell would prove nothing.
        latest = analysis["windows"][-1]
        target = next(c for c in analysis["cells"] if c["window"] == latest)
        name = analysis["run"]["objectives"][0]
        target["objectives"][name] = None
        target["objectives_norm"][name] = None
        html = render(analysis, self.tmp, "nulled")
        # The marker for that cell must be gone from the pairwise panels: its
        # tooltip is how a marker identifies itself, so no tooltip means no mark.
        base_titles = self.html.count(f"<title>{target['label']}")
        nulled_titles = html.count(f"<title>{target['label']}")
        self.assertLess(
            nulled_titles, base_titles,
            "a cell with an unobserved objective must lose marks, not gain a "
            "mark at zero",
        )
        self.assertIn("—", html)

    def test_a_cell_below_the_floor_is_named_and_not_plotted(self):
        rows, manifest = make_fixture("ground-truth")
        params = SU.ComputeParams(
            objectives=SU.DEFAULT_OBJECTIVES,
            as_of=SU.parse_ts(manifest["as_of"]),
            u=manifest["pinned_u"],
            bootstrap=0,
            permutations=50,
            min_builds=15,
        )
        analysis = SU.compute(rows, F.default_metrics(), params, run_id="r-floor")
        below = [c["label"] for c in analysis["cells"] if c["below_floor"]]
        self.assertTrue(below)
        html = render(analysis, self.tmp, "floored")
        for label in below:
            self.assertIn(label, html, "a floored cell must still be reported")
        # Reported in the header and in the timeline's "< floor" marker, but
        # never as a data mark: a faint mark reads as "less", and the claim is
        # "not enough evidence".
        self.assertIn("&lt; floor", html)
        for label in below:
            self.assertNotIn(f"<title>{label} (front)", html)

    def test_a_refused_front_is_not_drawn(self):
        rows, manifest = make_fixture("ground-truth")
        rows = [r for r in rows if r["group"] in ("agent-heavy", "review-first")]
        params = SU.ComputeParams(
            objectives=SU.DEFAULT_OBJECTIVES,
            as_of=SU.parse_ts(manifest["as_of"]),
            u=manifest["pinned_u"],
            bootstrap=0,
            permutations=50,
        )
        analysis = SU.compute(rows, F.default_metrics(), params, run_id="r-refused")
        across = [c for c in analysis["comparisons"] if c["kind"] == "across_groups"]
        self.assertTrue(all(c.get("front_not_drawn") for c in across))
        html = render(analysis, self.tmp, "nofront")
        self.assertIn("TOO_FEW_CELLS", html)
        for comparison in findings_of(html)["comparisons"]:
            if comparison["kind"] == "across_groups":
                self.assertTrue(comparison["front_not_drawn"])
                self.assertEqual(comparison["front"], [])

    def test_the_overlap_report_and_greyed_spokes_reach_the_page(self):
        rows, manifest = make_fixture("overlap")
        params = SU.ComputeParams(
            objectives=["cost", "lead_time", "debt_composite"],
            as_of=SU.parse_ts(manifest["as_of"]),
            u=1000.0,
            bootstrap=0,
            permutations=50,
        )
        analysis = SU.compute(rows, F.default_metrics(), params, run_id="r-overlap")
        html = render(analysis, self.tmp, "overlap")
        self.assertIn("OVERLAP: debt_composite", html)
        self.assertIn("spoke--grey", html, "overlapping spokes must be greyed")
        self.assertIn(
            "OVERLAP: debt_composite &lt;- share(liability), share(sunk)",
            html,
        )

    def test_every_glyph_has_a_printed_twin(self):
        """Star and radar glyphs are silhouette-comparison instruments, not
        value-reading instruments (Fuchs et al., IEEE TVCG, Jul 2017)."""
        self.assertIn('class="glyph"', self.html)
        self.assertIn('class="twin"', self.html)
        for klass in SU.IMPACT_CLASSES:
            self.assertIn(f"<td>{klass}", self.html)

    def test_the_threshold_verdict_is_stated(self):
        self.assertIn(self.analysis["sensitivity"]["verdict"], self.html)
        self.assertEqual(
            findings_of(self.html)["threshold_verdict"],
            self.analysis["sensitivity"]["verdict"],
        )

    def test_the_null_model_verdict_is_stated_for_every_comparison(self):
        for comparison in self.analysis["comparisons"]:
            if comparison.get("front_not_drawn"):
                continue
            self.assertIn(comparison["null_model"]["verdict"], self.html)


class TestCli(RenderCase):
    def test_surface_render_shells_out_and_writes_the_file(self):
        analysis_path = self.tmp / "cli.json"
        analysis_path.write_text(json.dumps(self.analysis, default=str), encoding="utf-8")
        out = self.tmp / "cli.html"
        result = subprocess.run(
            [
                sys.executable,
                "-m", "decision_surface",
                "render",
                str(analysis_path),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.exists())
        self.assertIn("rendered", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
