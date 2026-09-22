"""Invoke the bundled renderer without depending on a source checkout."""

from importlib.resources import as_file, files
from pathlib import Path
import subprocess


def render_analysis(analysis: Path, output: Path) -> subprocess.CompletedProcess:
    with as_file(files("decision_surface").joinpath("render.mjs")) as renderer:
        try:
            return subprocess.run(
                ["node", str(renderer), str(analysis), str(output)],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                "HTML rendering requires Node.js on PATH; analysis does not."
            ) from exc
