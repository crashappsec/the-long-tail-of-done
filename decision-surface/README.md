# Decision Surface

An installable Python package for multi-objective delivery analysis over build
records. It computes Pareto fronts, uncertainty, and impact compositions, stores
versioned runs in SQLite, and renders self-contained HTML reports.

The package works without an agent. The optional skill teaches an agent when to
invoke it and how to interpret its output; it contains no implementation code.

## Install

From this directory (the `decision-surface/` directory in the talk repository):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/surface --version
.venv/bin/surface --help
```

Python 3.9 or newer is required. There are no third-party Python runtime
dependencies. HTML rendering additionally requires Node.js on PATH; the
JavaScript renderer is included in the Python wheel and needs no npm install.
The deck's Slidev dependencies are not used.

This is a source-installable project, not a claim that a release exists on PyPI.
Install from this checkout or a wheel built from it.

## Run

```bash
.venv/bin/surface compute --builds sample/builds.csv \
  --as-of 2026-06-01 --u 1000 --no-store --out analysis.json
.venv/bin/surface render analysis.json --out surface.html
```

The sample is synthetic with planted ground truth in
`sample/builds.manifest.json`. Pin `--as-of` for reproducible historical runs;
the default observation edge is the current UTC time.

For a persistent workflow:

```bash
.venv/bin/surface ingest sample/builds.csv --source sample
.venv/bin/surface list metrics
.venv/bin/surface compute --as-of 2026-06-01 --u 1000 --out analysis.json
.venv/bin/surface explain --cell agent-heavy/2026-04
```

The default store is `./.decision-surface/db.sqlite`, relative to the caller's
working directory, not the installed package. Use `surface --db PATH ...` to
choose another store. Existing databases and JSON/CSV formats are unchanged.
`python -m decision_surface` is equivalent to the `surface` command when using
the same Python environment.

Python callers can import the existing analysis modules:

```python
from decision_surface import formula, store, surface

metrics = formula.DEFAULT_METRICS
parser = surface.build_parser()
```

## Agent Integration

The optional skill is [skill/decision-surface/SKILL.md](skill/decision-surface/SKILL.md).
Install that folder in your agent's skill directory separately from the Python
package. The package installer does not modify any agent configuration.
Ensure the agent can resolve `surface` on PATH, or give it the absolute path to
the virtual environment's executable.

For Claude Code, an optional stdio MCP server exposes the same engine:

```bash
claude mcp add decision-surface -- /absolute/path/to/.venv/bin/decision-surface-mcp \
  --db /absolute/path/to/data/db.sqlite
```

The server can also run with `python -m decision_surface.mcp_server`.
Input and output file paths are confined to the store directory by default.
Do not enable `--allow-any-path` for untrusted agent workloads.

The input contract and interpretation guide live alongside the skill:

- [Schema and refusals](skill/decision-surface/SCHEMA.md)
- [Analysis procedure](skill/decision-surface/references/front.md)
- [Visualization contract](skill/decision-surface/references/pictures.md)
- [Future HTTP API design, not implemented](skill/decision-surface/references/api.md)

## Develop and Test

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python tests/run.py -q
.venv/bin/python -m unittest discover -s tests
```

Tests import the installed package, without adding the source directory to
`sys.path`. Install first. Tests that invoke Node.js skip if it is unavailable.
Regenerate the synthetic fixture with:

```bash
.venv/bin/python sample/make-sample.py --metrics
```

Build a source distribution and wheel:

```bash
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

Packaging uses [setuptools' pyproject configuration](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html).
The wheel contains only the runtime package and renderer; the source
distribution also contains the sample, tests, and skill documentation.

## Repository Boundary

Everything needed to build, test, and use this project is under this directory.
It can move into its own repository without changing imports or requiring the
talk repository. No new remote repository or PyPI publication is required for
local use.

The former `skill/decision-surface/scripts/*.py` entry points have been
replaced by installed commands. Update existing agent configurations to use
`surface` and `decision-surface-mcp`; do not point them at source files.
Distribution name: `decision-surface`. Import name: `decision_surface`.
