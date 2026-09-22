---
name: decision-surface
description: Store, analyse and draw multi-objective delivery numbers over builds. Computes a Pareto front over cost, lead time and debt per (group, window), says whether that front is meaningful against a permutation null model, and reports the impact mix as a composition with right-censoring intact. Use when asked "which team or workflow is on the front", "are we improving or trading one thing for another", "is this front meaningful", "what is our waste share", "why are we not on the front", or when someone proposes ranking teams by a weighted score.
---

# Decision surface

Use the separately installed `decision-surface` Python package to store,
analyse and draw build-level delivery numbers. This skill provides workflow
and interpretation guidance only; the package works without it.

Before starting, run `surface --version` and `surface --help`. If the command is
unavailable, ask for the installed executable or the trusted package checkout.
Do not assume the package is published on PyPI or install arbitrary code.
The project README documents installation from source. An absolute path to a
virtual environment's `surface` executable works when it is not on PATH.

`shipping-tail` derives numbers from git; this package accepts builds from any
source. Neither requires the other.

The measured object is a **build**. Two gates that CI already answers produce
everything downstream: *did it deploy*, and *was it used*.

## The four things this does that a spreadsheet does not

1. **Says whether the front means anything.** Front size grows with objective
   count for purely combinatorial reasons, so "three of five teams are on the
   front" is uninterpretable alone. Every front is reported against a
   permutation null model, and `NOT INFORMATIVE` is a headline rather than a
   footnote (Bentley, Kung, Schkolnick & Thompson, *JACM* 25(4), Oct 1978).
2. **Keeps unresolved separate from waste.** A build younger than `L` at the
   observation edge is `unresolved`, not `sunk`. That distinction is the whole
   difference between "we shipped no waste" and "we have not looked yet".
3. **Makes double counting visible.** Metrics are declared formulas, so a
   dependency graph is built before any data is read; an objective that
   consumes a part of a mix it is drawn beside is reported, and those spokes
   are greyed in every glyph.
4. **Reports `P(on front)` instead of a flag.** Objectives are sample
   statistics over `n_builds` and every cell has a different `n_builds`, so a
   front computed once is a guess.

## Read these before running anything

| File | What is in it |
|---|---|
| `SCHEMA.md` | The build record, the impact gate, `u` and `L`, the formula language, a worked example, and every refusal. |
| `references/front.md` | Stages 0–7: roles, normalisation, conflict screen, objective reduction, layers and the null model, the relaxation ladder, uncertainty, time. Every default and every citation. |
| `references/pictures.md` | What is drawn, in what order, and the rules the renderer follows. |
| `references/api.md` | The HTTP tier: designed, deliberately not built. |

## Procedure

1. **Get builds into the shape in `SCHEMA.md` §1.** One row per build.
   `build_id`, `first_commit_ts`, `ts`, `loc_added`, `loc_removed` are
   required; everything else is nullable, and **nullable means "we could not
   observe it", never zero**. If a column would have to be invented, leave it
   empty and let the tool report the gap.

2. **Decide what `group` means, once.** It is opaque — team, repo, workflow,
   harness, model. The tool never interprets it. With no `group` the front runs
   over the team's own windows, which is the default mode and a real answer for
   a team with nobody to benchmark against.

3. **Ingest.** `surface ingest builds.csv --source ci-export-april`.
   Idempotent on `build_id`, so re-importing an overlapping export does not
   double-count. Conflicts are reported, never overwritten.

4. **Look at the registry before choosing objectives.**
   `surface list metrics`. The shipped default objective set is
   `cost, lead_time, debt_defect`. `debt_composite` is the alternative that
   folds in wasted-build share, and it carries a declared overlap with the
   impact radar — the tool will say so on every run and grey the affected
   spokes. Both ship; the choice is the team's.

5. **Compute.** `surface compute --objectives cost,lead_time,debt_defect
   --as-of 2026-06-01`. Then **read, in this order**:
   - `diagnostics.refusals` — if this is non-empty, there is no answer yet.
   - `stage0.overlaps` — computed from the formulas, before any data.
   - each comparison's `null_model.verdict` — **before quoting a front.**
   - `sensitivity.verdict` — whether the impact mix survives `0.5u` and `2u`.
   - `stage7.direction_of_travel` — the cheapest interpretable output here:
     `‖Δ‖` and the angle to the ideal point per group per window.

6. **Answer "why are we not on the front" with `explain`, not with prose.**
   `surface explain --cell platform/2026-04` names the cells that dominate it,
   on which objectives, by how much, and which objective is binding.

7. **Render.** `surface render analysis.json --out surface.html`. One
   self-contained file: inline SVG, inline CSS, no fetches. The six 2D
   companions come first and are the ones that can be read; the trajectory cube
   and the Kiviat tube are below them and every value in both has a printed
   twin.

8. **Re-run next month and `diff`.** `surface diff r-20260601-001
   r-20260701-001`. If a metric definition changed in between, the verdict is
   `NOT COMPARABLE` and the movement is not evidence of anything.

## How to report a result

State these four things or the number is misleading:

- **the objectives and their formula versions** — `debt` is not a universal
  quantity, it is a declared expression with a version;
- **`u` and `L`**, and that `u` is self-calibrating by default;
- **the null-model verdict**, in the same breath as the front;
- **the gate-unavailable share**, whenever the impact mix is quoted. A mix
  computed over builds with no usage telemetry is a statement about the
  telemetry.

Two sentences that are almost always worth saying out loud:

> Dominance is invariant under any monotone per-objective transform, so front
> membership does not depend on the normalisation. Knees, hypervolume,
> distance-to-ideal and every glyph radius do.

> A full front is a diagnosis, not good news.

## What it refuses, and what to do about it

| Refusal | What it means | The move |
|---|---|---|
| `TOO_MANY_OBJECTIVES` | more than 8 | stage 3 reduction, not a bigger chart |
| `TOO_FEW_OBJECTIVES` | fewer than 2 | that is a ranking; say so |
| `TOO_FEW_CELLS` | fewer than `m + 1` cells | more windows, or fewer objectives |
| `DIVERGENT_LEAD_TIME_SOURCE` | one group measured to first user, another to deploy | supply `served_at` for both, or compare like with like |
| `PROVISIONAL_FRONT` (caution) | fewer than `2 × m` cells | quote it as provisional |
| `BELOW_BUILD_FLOOR` (caution) | `n_builds` under the floor | reported, not plotted; do not fill the gap |
| `NOT INFORMATIVE` (not a refusal) | the front is the size noise produces | reduce objectives and re-run; report the verdict either way |

DEA is deliberately not implemented: published rules of thumb want 2–3× as
many units as inputs plus outputs, and almost nobody has eighteen
distinguishable workflows.

## Driving it from an agent

`decision-surface-mcp` is a stdio MCP server over the same store, so an agent
can ingest, compute, explain and render with no shell:

```
claude mcp add decision-surface -- /absolute/path/to/.venv/bin/decision-surface-mcp \
  --db /absolute/path/to/data/db.sqlite
```

Nine tools: `submit_builds`, `define_metric`, `list_metrics`, `list_groups`,
`list_windows`, `compute`, `render`, `explain`, `diff`. `compute` returns the
summary plus a path to the full analysis, because a 300 kB tool result is
useless to a model. Every refusal, caution and null-model verdict travels with
the result.

File paths passed to `submit_builds` and `render` are confined to the store's
directory unless the server is started with `--allow-any-path`. The server acts
on behalf of a model that may be reading untrusted text, so an arbitrary path
is not a free parameter.

## Try it on the fixture first

```bash
cd /path/to/decision-surface-project
python3 sample/make-sample.py --metrics          # 5 groups, 6 windows, planted front
surface ingest sample/builds.csv
surface compute --as-of 2026-06-01 --u 1000 --out analysis.json
surface render analysis.json --out surface.html
python3 tests/run.py                             # use the package's Python environment
```

The sample and tests belong to the package source distribution, not this skill
folder. The fixture's planted truth is in `sample/builds.manifest.json`, written from
geometry rather than from the tool's output: cells sit on the DTLZ2
concave-sphere octant, where two cells on one direction are always comparable
and two at radius 1.0 on different directions never are. `agent-heavy`
improves on all three objectives, `legacy-owner` worsens on all three,
`review-first` trades debt against lead time, `contractors` trades further out
and never catches up, `platform-core` is parked on the true front. The tests
assert the tool recovers all of it.

For aggregate exports, use `compute --cells cells.csv` with registered objective
columns and optional `<objective>_se` columns. MCP `compute` accepts `cells` or
`cells_csv` for the same workflow. Missing errors disable uncertainty; missing
mixes and build-level usage sensitivity stay unavailable. See SCHEMA section 7.
Without `--as-of`, the observation edge is the current UTC time. Pin it when
reproducing a historical result.

Note what the sample also demonstrates: with five groups and three objectives
the per-window front is diagnosed `NOT INFORMATIVE`, because at that cell count
and dimension the front is the size independent noise produces. That is the
tool working. The reply is to reduce objectives — δ-MOSS prints the error that
costs — or to add cells, not to quote the front anyway.

## Where this fits

| Question | Skill |
|---|---|
| Did that actually ship, and what did it cost afterwards? | `shipping-tail` |
| Which group is on the front, is that meaningful, which way are we moving? | this one |

`shipping-tail` produces builds; this consumes them. Neither needs the other.

## References

Full citations, with what each one is load-bearing for, are in
`references/front.md` and `references/pictures.md`. The four that decide the
shape of this skill:

- **Bentley, Kung, Schkolnick & Thompson**, *JACM* 25(4):536–543, Oct 1978 —
  the expected number of maxima, which is why front size gets a null model.
- **Brockhoff & Zitzler**, PPSN IX 2006 / 2007 — δ-MOSS and k-EMOSS, which is
  why dropping an axis reports a number.
- **Munzner**, *Visualization Analysis and Design*, 2014 — no unjustified 3D,
  which is why the 2D companions are built first and are not optional.
- **Fuchs, Isenberg, Bezerianos & Keim**, *IEEE TVCG*, Jul 2017 — 64 glyph
  user studies, which is why every glyph here has a printed twin.
