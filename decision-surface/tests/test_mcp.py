#!/usr/bin/env python3
"""The MCP server: the same numbers as the CLI, with the caveats still attached.

The risk this file exists for is an agent reading a front and reporting it as a
finding. So the assertions are about what travels *with* the result -- the
refusals, the null-model verdict, the structural overlap -- as much as about
the result itself. An agent that cannot see NOT INFORMATIVE will state a
conclusion the tool went to some trouble to deny.

Driven end to end over real stdio, one JSON-RPC message per line, because a
protocol implemented by hand deserves to be tested through the wire rather
than through its handlers.

Run: python3 tests/run.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from decision_surface import mcp_server as M

from test_front import make_fixture  # noqa: E402

SERVER = [sys.executable, "-m", "decision_surface.mcp_server"]


def rpc(*messages: dict, db: Path, extra_args: list[str] | None = None) -> list[dict]:
    """Drive the server over real stdio and return every reply, in order."""
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    result = subprocess.run(
        [*SERVER, "--db", str(db), *(extra_args or [])],
        input=payload,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise AssertionError(f"server exited {result.returncode}: {result.stderr}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def call(name: str, arguments: dict, request_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def payload_of(response: dict) -> dict:
    """The tool result, parsed back out of its text content block."""
    return json.loads(response["result"]["content"][0]["text"])


class TestProtocol(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_initialize_advertises_tools_and_says_how_to_read_the_output(self):
        [response] = rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            db=self.db,
        )
        result = response["result"]
        self.assertEqual(result["protocolVersion"], M.PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], "decision-surface")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("null-model", result["instructions"])

    def test_a_notification_gets_no_reply(self):
        responses = rpc(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            db=self.db,
        )
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 2)

    def test_all_nine_tools_are_listed_with_schemas(self):
        [response] = rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, db=self.db
        )
        tools = {t["name"]: t for t in response["result"]["tools"]}
        self.assertEqual(
            sorted(tools),
            [
                "compute",
                "define_metric",
                "diff",
                "explain",
                "list_groups",
                "list_metrics",
                "list_windows",
                "render",
                "submit_builds",
            ],
        )
        for name, tool in tools.items():
            self.assertIn("inputSchema", tool, name)
            self.assertGreater(len(tool["description"]), 40, name)

    def test_an_unknown_method_is_a_jsonrpc_error(self):
        [response] = rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "nope/nope"}, db=self.db
        )
        self.assertEqual(response["error"]["code"], -32601)

    def test_malformed_json_does_not_kill_the_server(self):
        payload = "{not json\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        ) + "\n"
        result = subprocess.run(
            [*SERVER, "--db", str(self.db)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0)
        lines = [json.loads(l) for l in result.stdout.splitlines() if l.strip()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["id"], 2)

    def test_a_tool_error_is_reported_as_data_not_as_a_transport_failure(self):
        [response] = rpc(call("explain", {"cell": "nope/2026-04"}), db=self.db)
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("no runs in the store", response["result"]["content"][0]["text"])


class TestEndToEnd(unittest.TestCase):
    """One session: submit, define, compute, explain, render, diff."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows, cls.manifest = make_fixture("ground-truth")
        cls.csv = HERE / ".fixtures" / "ground-truth.csv"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "db.sqlite"
        self.db.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def compute_args(self, **overrides) -> dict:
        args = {
            "as_of": self.manifest["as_of"],
            "u": self.manifest["pinned_u"],
            "bootstrap": 100,
        }
        args.update(overrides)
        return args

    def test_an_agent_ingests_computes_and_renders_with_no_shell(self):
        responses = rpc(
            call("submit_builds", {"rows": self.rows, "source": "agent"}, 1),
            call("compute", self.compute_args(), 2),
            call("render", {}, 3),
            db=self.db,
        )
        ingest = payload_of(responses[0])
        self.assertEqual(ingest["accepted"], len(self.rows))
        self.assertEqual(ingest["duplicate"], 0)

        summary = payload_of(responses[1])
        latest = summary["windows"][-1]
        across = [
            c
            for c in summary["comparisons"]
            if c["kind"] == "across_groups" and c["label"].endswith(latest)
        ]
        self.assertTrue(across)
        self.assertEqual(
            sorted(across[0]["front"]),
            sorted(self.manifest["planted"]["front_latest_window"]),
            "the MCP path must recover the same planted front the CLI does",
        )

        rendered = payload_of(responses[2])
        self.assertTrue(Path(rendered["path"]).exists())
        self.assertIn("<svg", Path(rendered["path"]).read_text(encoding="utf-8"))

    def test_submitting_the_same_builds_twice_does_not_double_count(self):
        responses = rpc(
            call("submit_builds", {"rows": self.rows}, 1),
            call("submit_builds", {"rows": self.rows}, 2),
            db=self.db,
        )
        first = payload_of(responses[0])
        second = payload_of(responses[1])
        self.assertEqual(second["accepted"], 0)
        self.assertEqual(second["duplicate"], len(self.rows))
        self.assertEqual(second["total_builds_in_store"], first["total_builds_in_store"])

    def test_the_summary_carries_the_refusals_and_the_null_model(self):
        """The caveats must reach the agent, or it reports a front as a finding."""
        responses = rpc(
            call("submit_builds", {"rows": self.rows}, 1),
            call("compute", self.compute_args(), 2),
            db=self.db,
        )
        summary = payload_of(responses[1])
        self.assertIn("diagnostics", summary)
        self.assertIn("refusals", summary["diagnostics"])
        self.assertIn("read_this_first", summary)
        self.assertIn("NOT INFORMATIVE", summary["read_this_first"])
        for comparison in summary["comparisons"]:
            if comparison.get("front_not_drawn"):
                self.assertTrue(comparison["refusals"])
            else:
                self.assertIn("null_model", comparison)
                self.assertIn(
                    comparison["null_model"]["verdict"],
                    ("NOT INFORMATIVE", "CONFLICTING", "AGREEING", None),
                )

    def test_the_summary_is_small_enough_to_be_useful_to_a_model(self):
        responses = rpc(
            call("submit_builds", {"rows": self.rows}, 1),
            call("compute", self.compute_args(bootstrap=0), 2),
            db=self.db,
        )
        text = responses[1]["result"]["content"][0]["text"]
        self.assertLess(
            len(text),
            200_000,
            "a tool result larger than this is unusable; the full analysis is on disk",
        )
        self.assertIn("analysis_path", payload_of(responses[1]))

    def test_define_metric_rejects_a_formula_that_is_not_data(self):
        responses = rpc(
            call(
                "define_metric",
                {
                    "name": "evil",
                    "role": "objective",
                    "direction": "min",
                    "expression": "__import__('os').system('echo pwned')",
                },
                1,
            ),
            call("list_metrics", {}, 2),
            db=self.db,
        )
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("FormulaError", responses[0]["result"]["content"][0]["text"])
        names = {m["name"] for m in payload_of(responses[1])["metrics"]}
        self.assertNotIn("evil", names, "a formula that will not run must not be stored")

    def test_define_metric_versions_an_edit_and_diff_notices(self):
        responses = rpc(
            call("submit_builds", {"rows": self.rows}, 1),
            call("compute", self.compute_args(bootstrap=0), 2),
            call(
                "define_metric",
                {
                    "name": "debt_defect",
                    "role": "objective",
                    "direction": "min",
                    "expression": "minmax(sum(bugs) / (sum(loc_total) / 500))",
                    "notes": "bugs per half-KLOC, to prove the diff notices",
                },
                3,
            ),
            call("compute", self.compute_args(bootstrap=0), 4),
            db=self.db,
        )
        defined = payload_of(responses[2])
        self.assertEqual(defined["version"], 2)
        run_a = payload_of(responses[1])["run_id"]
        run_b = payload_of(responses[3])["run_id"]
        [diffed] = rpc(call("diff", {"run_a": run_a, "run_b": run_b}, 5), db=self.db)
        result = payload_of(diffed)
        self.assertTrue(result["verdict"].startswith("NOT COMPARABLE"))
        self.assertEqual(result["definition_changes"][0]["metric"], "debt_defect")

    def test_defining_first_still_leaves_the_defaults_computable(self):
        """define -> submit -> compute is the natural first session, and it must
        work: defining one metric must not opt a team out of the other seven."""
        responses = rpc(
            call(
                "define_metric",
                {
                    "name": "debt_mine",
                    "role": "objective",
                    "direction": "min",
                    "expression": "minmax(sum(bugs) / (sum(loc_total) / 1000)) "
                    "* 0.7 + 0.3 * share(liability)",
                },
                1,
            ),
            call("submit_builds", {"rows": self.rows}, 2),
            call(
                "compute",
                self.compute_args(
                    objectives=["cost", "lead_time", "debt_mine"], bootstrap=0
                ),
                3,
            ),
            db=self.db,
        )
        self.assertFalse(
            responses[2]["result"].get("isError"),
            responses[2]["result"]["content"][0]["text"][:400],
        )
        summary = payload_of(responses[2])
        self.assertEqual(summary["objectives"], ["cost", "lead_time", "debt_mine"])
        self.assertTrue(
            any("debt_mine" in o for o in summary["structural_overlaps"]),
            "a team's own metric that consumes share(liability) must still be "
            "flagged as overlapping the impact radar",
        )

    def test_define_metric_reports_a_structural_overlap(self):
        [response] = rpc(
            call(
                "define_metric",
                {
                    "name": "my_debt",
                    "role": "objective",
                    "direction": "min",
                    "expression": "share(liability) + share(sunk)",
                },
                1,
            ),
            db=self.db,
        )
        payload = payload_of(response)
        self.assertTrue(payload["structural_overlap"])
        self.assertIn("share(liability)", payload["structural_overlap"][0])

    def test_explain_answers_why_a_cell_is_not_on_the_front(self):
        dominated = self.manifest["planted"]["dominated_latest_window"][0]
        responses = rpc(
            call("submit_builds", {"rows": self.rows}, 1),
            call("compute", self.compute_args(bootstrap=0), 2),
            call("explain", {"cell": dominated}, 3),
            db=self.db,
        )
        payload = payload_of(responses[2])
        self.assertEqual(payload["cell"], dominated)
        across = next(c for c in payload["comparisons"] if c["kind"] == "across_groups")
        self.assertFalse(across["on_front"])
        self.assertTrue(across["dominated_by"])
        self.assertIn("binding_objective", across)
        self.assertIn("closest_dominator", across)

    def test_list_groups_and_windows_describe_the_store(self):
        responses = rpc(
            call("submit_builds", {"rows": self.rows}, 1),
            call("list_groups", {}, 2),
            call("list_windows", {}, 3),
            db=self.db,
        )
        groups = payload_of(responses[1])
        self.assertEqual(
            sorted(groups["groups"]), sorted({r["group"] for r in self.rows})
        )
        self.assertIn("opaque", groups["note"])
        windows = payload_of(responses[2])
        self.assertTrue(windows["windows"])
        self.assertIn("gate", windows)

    def test_list_metrics_seeds_the_defaults_and_flags_the_overlap(self):
        [response] = rpc(call("list_metrics", {}, 1), db=self.db)
        payload = payload_of(response)
        names = {m["name"] for m in payload["metrics"]}
        self.assertIn("debt_composite", names)
        self.assertIn("debt_defect", names)
        self.assertEqual(
            payload["default_objectives"], ["cost", "lead_time", "debt_defect"]
        )
        self.assertTrue(any("debt_composite" in o for o in payload["structural_overlaps"]))

    def test_submit_builds_from_a_csv_path(self):
        [response] = rpc(
            call("submit_builds", {"csv": str(self.csv)}, 1),
            db=self.db,
            extra_args=["--allow-any-path"],
        )
        self.assertEqual(payload_of(response)["accepted"], len(self.rows))

    def test_a_path_outside_the_store_is_refused_by_default(self):
        """This process acts for a model that may be reading untrusted text, so
        an arbitrary path is not a free parameter."""
        [response] = rpc(call("submit_builds", {"csv": str(self.csv)}, 1), db=self.db)
        self.assertTrue(response["result"]["isError"])
        self.assertIn(
            "outside the store directory", response["result"]["content"][0]["text"]
        )

    def test_compute_on_an_empty_store_says_what_to_do(self):
        [response] = rpc(call("compute", {}, 1), db=self.db)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("submit_builds", response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
