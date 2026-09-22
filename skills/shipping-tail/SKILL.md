---
name: shipping-tail
description: Measure what actually shipped and what it cost after it shipped. Computes deploys-to-stability, rework-deploy ratio, tail mass and tail attribution over a repository's git history, optionally joined to a local AI agent session archive. Use when asked "did that actually ship", "where is our tail", "is the agent making us faster", "what did that feature really cost", or when someone proposes commits / PRs / lines / tokens as a productivity metric.
---

# Shipping tail

Commit counts, merged PRs and token spend all measure work at the point it
leaves a developer. None of them measure what happened to it afterwards. This
skill computes the part everyone stops looking at.

The published effect sizes are stark: across >100,000 GitHub developers,
autonomous coding agents lift commits by ~180%, but the same lift is ~50% at
the project level and ~30% at the level of actual releases (Demirer, Musolff &
Yang, *Writing Code vs. Shipping Code*, 27 May 2026). The gap is not a rounding error,
it is the thing to measure.

## The four numbers

`t=0` for a feature is **flag activation** where flag events exist, otherwise
**first production deploy**. Flags are strictly better because they separate
"the code is out there" from "a human is being served by it". State which one
you used in every result.

Every metric below is computed over one population: clusters that **reached
production and served a user**. Deployed-but-never-served is cost, not output;
never-deployed-and-changed-nothing is sunk cost; never-deployed-but-changed-a-
later-release is neither, and has to produce the link. A cluster that has not
deployed *yet* is unresolved, not sunk. See `references/metrics.md`.

| Metric | Definition | Reads on |
|---|---|---|
| **Shipped capability count** | Feature clusters reaching `t=0` in the window. | Did we build something |
| **Deploys-to-stability** | Deploys touching a cluster from `t=0` until `k` consecutive quiet days. Fit as a **survival curve with right-censoring**, never as a mean. | Are we more stable |
| **Rework-deploy ratio** | Post-`t=0` changed lines in the cluster over pre-`t=0` changed lines. Above 1.0 means a draft shipped. | Are we more stable |
| **Tail mass** | Share of total cluster effort spent *after* `t=0`. A ratio, so it survives comparison across teams of different sizes. | Are we faster |
| **Tail attribution** | Tail mass grouped by the workflow, harness and review path that produced the cluster. | Are we cheaper, and what do we change |
| **Efficient set** | Across workflows: the non-dominated set over delivery per unit spend, delivery per unit time, and share that settles. A frontier, never a ranking. | Which flow to copy |
| **Agent spend** | Tokens by model from the session archive, priced with a blended per-model rate. Reported with the share of tokens that could be priced. One of four cost components, and usually the smallest. | Are we cheaper |
| **Never landed** | Share of sessions the archive classes abandoned or errored. A local estimate of the SUNK quadrant, never a measurement. | What did we pay for and not ship |
| **Invisible effort** | Share of a run's effort in sessions that produced no commit, as a distribution. The size of the blind spot in every commit-derived number above. | How much of the tail can we not see |

### Why survival analysis and not an average

Clusters that never stabilise are the population you care about, and a mean
deletes them. Two workflows can post an identical mean deploys-to-stability
while one leaves a third of its features on a permanent plateau and the other
leaves none. The plateau is the finding. Report the curve.

## Procedure

1. **Establish the window and `t=0` source.** Ask which is available, in this
   order: flag evaluation events, deploy events, tag/release events, merge to
   the default branch. Record the choice in the output; do not silently fall
   back.

2. **Identify features. Anchors first.**
   - Use human-declared boundaries wherever they exist: flag keys, ticket
     references in commit messages, API schema files, ADRs, spec files.
     These are free and they are defensible.
   - Cluster only the remainder, over commit messages plus touched paths.
     **Pin the embedding model and record its version with every result** —
     you will be asked about drift.
   - If the repository's contracts are good, use them and skip clustering
     entirely. That is the best case, not a shortcut.

3. **Sweep the threshold.** Recompute every metric across a range of
   clustering thresholds and report whether the *shape* holds. This is the
   whole answer to the modifiable-unit objection: redraw the boundaries and
   you change the correlation, so a single threshold is a choice you cannot
   defend. Ship the sweep as a default, never behind a flag.

4. **Compute the four metrics per cluster**, then aggregate by workflow.

   Then, to choose between workflows, compute the **efficient set** rather
   than a score. Weighting delivery against stability requires knowing what a
   month of delay is worth in units of stability, and nobody agrees on that
   number, so the honest output is a frontier and the list of flows that are
   beaten on every axis at once.

5. **Report the unattributed share explicitly.** Any cluster whose `t=0` or
   whose originating session could not be resolved is counted and shown, not
   renormalised away. A tail metric computed over a broken join is worse than
   no tail metric, because it looks like knowledge.

## Running it

`scripts/shipping_tail.py` is a dependency-free reference implementation over
`git log`. It is deliberately readable rather than clever, so it can be
audited and forked.

```bash
# Minimum: git history only, ticket references and paths as anchors.
python3 scripts/shipping_tail.py --repo . --since 2026-06-01

# Join a local agent session archive so tails attribute to a workflow.
agentsview stats --json > window.json
python3 scripts/shipping_tail.py --repo . --since 2026-06-01 \
    --agentsview window.json

# Explicit t=0 source, one row per deploy event: iso8601,cluster_hint
python3 scripts/shipping_tail.py --repo . --deploys deploys.csv

# The cost leg, out of the session archive you already have. --prices turns
# the archive's token mix into an agent-spend estimate and reports what share
# of the tokens it could price; the session outcome distribution gives a
# SUNK-quadrant estimate. See sample/prices.example.csv — it ships with no
# numbers, deliberately.
agentsview stats --json > window.json
python3 scripts/shipping_tail.py --repo . --deploys deploys.csv \
    --agentsview window.json --prices prices.csv

# The sweep is on by default; widen it when a reviewer pushes back.
python3 scripts/shipping_tail.py --repo . --sweep 1,2,3,5,8

# Measure the blind spot. Every number above is commit-derived; this reports
# how much effort sits in sessions that committed nothing. See
# sample/sessions.example.jsonl for the schema.
python3 scripts/shipping_tail.py --repo . --sessions sessions.jsonl

# Choosing between workflows: run once per flow, then compute the frontier.
# The efficient set is recomputed at every shared threshold, so a flow that is
# efficient at one threshold out of four is a clustering artefact, not a
# finding.
for flow in spec-first prompt-and-iterate spec-first-test-gated; do
    python3 scripts/shipping_tail.py --repo "repos/$flow" \
        --deploys "$flow-deploys.csv" --json > "$flow.json"
done
python3 scripts/shipping_tail.py --front spec-first.json \
    prompt-and-iterate.json spec-first-test-gated.json

# With real inputs instead of proxies: label,dollars,engineer_months
python3 scripts/shipping_tail.py --front *.json --cost costs.csv
```

Without `--cost` the two denominators are total churn and elapsed calendar
time. Those are proxies, the output says so on the spend axis, and they are
the best git alone can do.

Output is a table plus `--json` for downstream use.

## Honesty requirements

These are part of the skill, not optional polish. Reproduce them in any
report you generate.

- **Tail mass is descriptive, not a target.** Some tail is healthy iteration
  on real user feedback. A team driving tail mass to zero has stopped
  listening. Read the shape, not the level.
- **The unit is a feature cluster, never a named engineer.** Attribute a tail
  to a person and the metric will be defeated within a sprint.
- **Association, not causation.** Adoption in the field studies is voluntary.
  Say so.
- **Feature clustering is a modelling choice.** Anchors and a sweep make it
  defensible; they do not make it objective.
- **Session health signals are heuristics.** If you consume AgentsView's
  outcome or grade fields, carry its own caveat forward: they are for triage
  and pattern-finding, not ground truth.

## What this skill cannot do

It closes the session-to-commit edge and, given deploy or flag events, the
deploy-to-`t=0` edge. It does **not** close:

- **commit → artifact.** The build is a black box unless something records
  inside it.
- **artifact → deploy.** Nobody knows where the image ended up unless the
  artifact itself reports back.

Those two need provenance embedded in the artifact — [Chalk](https://github.com/crashappsec/chalk)
is one way; anything that survives the artifact leaving your CI will do. When
they are missing, this skill reports the affected clusters as unattributed
rather than guessing.

## References

- `references/metrics.md` — precise definitions, censoring rules, worked
  example, and the full citation list.
