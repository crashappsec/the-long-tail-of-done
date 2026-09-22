"""Exercise the installed distribution, not imports from a scripts directory."""

from __future__ import annotations

import json
import os
from importlib import metadata, resources
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

from decision_surface import __version__, mcp_server, rendering, surface


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(sysconfig.get_path("scripts"))


def executable(name: str) -> str:
    return str(SCRIPTS / (name + ".exe" if os.name == "nt" else name))


class TestInstalledPackage(unittest.TestCase):
    def run_command(self, args, cwd, **kwargs):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
            **kwargs,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_distribution_metadata_and_mcp_share_version(self):
        self.assertEqual(__version__, metadata.version("decision-surface"))
        self.assertEqual(mcp_server.SERVER_INFO["version"], __version__)
        entries = {
            ep.name: ep.value
            for ep in metadata.distribution("decision-surface").entry_points
            if ep.group == "console_scripts"
        }
        self.assertEqual(entries["surface"], "decision_surface.surface:main")
        self.assertEqual(entries["decision-surface-mcp"], "decision_surface.mcp_server:main")

    def test_renderer_is_a_package_resource(self):
        renderer = resources.files("decision_surface").joinpath("render.mjs")
        self.assertTrue(renderer.is_file())
        self.assertIn("node:fs", renderer.read_text(encoding="utf-8"))

    def test_console_and_module_commands_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            for command in (
                [executable("surface")],
                [sys.executable, "-m", "decision_surface"],
            ):
                version = self.run_command([*command, "--version"], tmp)
                self.assertEqual(version.stdout.strip(), f"surface {__version__}")
                help_result = self.run_command([*command, "--help"], tmp)
                self.assertIn("compute", help_result.stdout)

    def test_mcp_entry_point_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
            }) + "\n"
            result = self.run_command(
                [executable("decision-surface-mcp"), "--db", str(Path(tmp) / "db.sqlite")],
                tmp, input=request,
            )
            response = json.loads(result.stdout)
            self.assertEqual(response["result"]["serverInfo"]["version"], __version__)

    def test_missing_node_is_an_actionable_error(self):
        with patch.object(rendering.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError, "requires Node.js on PATH"):
                rendering.render_analysis(Path("analysis.json"), Path("surface.html"))

    def test_missing_node_cli_returns_error_without_traceback(self):
        with patch.object(rendering.subprocess, "run", side_effect=FileNotFoundError):
            with patch("sys.stderr") as stderr:
                result = surface.main(["render", "analysis.json"])
            self.assertEqual(result, 2)
            self.assertIn("requires Node.js", "".join(c.args[0] for c in stderr.write.call_args_list))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for HTML rendering")
    def test_compute_and_both_render_entry_points_outside_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cli = [executable("surface"), "--db", str(folder / "db.sqlite")]
            self.run_command([
                *cli, "compute", "--builds", str(ROOT / "sample" / "builds.csv"),
                "--as-of", "2026-06-01", "--u", "1000",
                "--bootstrap", "0", "--permutations", "20",
                "--run-id", "package-smoke", "--out", "analysis.json",
            ], tmp)
            analysis = json.loads((folder / "analysis.json").read_text())
            self.assertEqual(len(analysis["cells"]), 30)
            self.run_command([*cli, "render", "analysis.json", "--out", "cli.html"], tmp)
            self.assertIn("<svg", (folder / "cli.html").read_text())
            request = json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "render", "arguments": {
                    "run_id": "package-smoke", "out": "mcp.html",
                }},
            }) + "\n"
            result = self.run_command(
                [executable("decision-surface-mcp"), "--db", str(folder / "db.sqlite")],
                tmp, input=request,
            )
            response = json.loads(result.stdout)
            self.assertFalse(response["result"].get("isError"), response)
            self.assertIn("<svg", (folder / "mcp.html").read_text())


if __name__ == "__main__":
    unittest.main()
