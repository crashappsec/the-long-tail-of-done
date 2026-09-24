# the-long-tail-of-done

Skills and tools from **“The Long Tail of ‘Done’”** — tracking what agents
actually ship, and where it gets deployed. AI Engineer Paris, 24 September 2026.

## Downloads

- **Crayon** (Crash Override Endpoint) — session tracking, process-tree and
  network visibility, insights and guardrails on the developer machine.
  **[Download](https://crashoverride.com/download/endpoint?utm_source=presentation&utm_medium=slides&utm_campaign=q3_events_2026)**
- **Chalk** — open-source artifact marking, build attestation and runtime
  heartbeats; joins workload → artifact → build → commit → session.
  **[Releases](https://github.com/crashappsec/chalk/releases)** ·
  [github.com/crashappsec/chalk](https://github.com/crashappsec/chalk) ·
  [docs](https://crashoverride.com/docs/chalk/quick-start)

The tools in this repo close the session-to-commit edge and, given deploy or
flag events, the deploy-to-`t=0` edge. The commit → artifact → deploy edges
need provenance recorded in the artifact itself — that's Crayon and Chalk.

## What's inside

Commit counts, merged PRs and token spend measure work at the point it leaves
a developer. Nothing here takes those at face value: these tools measure what
happened to the work *after* it shipped.

| Path | What it is |
|---|---|
| [`skills/shipping-tail/`](skills/shipping-tail/) | A Claude skill + dependency-free Python reference implementation. Computes deploys-to-stability, rework-deploy ratio, tail mass and tail attribution over a repository's git history, optionally joined to a local agent session archive. |
| [`decision-surface/`](decision-surface/) | An installable Python package for multi-objective delivery analysis over build records: Pareto fronts, uncertainty, impact compositions, versioned SQLite runs, self-contained HTML reports. Ships with an optional agent skill and a stdio MCP server. |

## Quick start

**shipping-tail** (git history only, no dependencies):

```bash
python3 skills/shipping-tail/scripts/shipping_tail.py --repo /path/to/repo --since 2026-06-01
```

To use it as a Claude skill, copy `skills/shipping-tail/` into your skills
directory (`~/.claude/skills/` or `.claude/skills/` in a project).

**decision-surface**:

```bash
cd decision-surface
python3 -m venv .venv && .venv/bin/python -m pip install .
.venv/bin/surface compute --builds sample/builds.csv --as-of 2026-06-01 --u 1000 --no-store --out analysis.json
.venv/bin/surface render analysis.json --out surface.html
```

See [`decision-surface/README.md`](decision-surface/README.md) for the MCP
server and agent-skill setup.
