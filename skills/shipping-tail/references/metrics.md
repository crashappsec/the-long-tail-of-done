# Metric definitions

Precise enough to reimplement, and to argue with.

## t=0

The moment a feature first served a user. Sources, in descending order of
authority:

1. **Flag activation.** Best available. Separates "the code is deployed" from
   "a human is being served by it", which is the distinction a dark launch
   makes and a deploy event cannot.
2. **Deploy event** to a production environment.
3. **Tag or release** event.
4. **First merge to the default branch.** Weakest: it conflates merge with
   exposure, and in trunk-based repos with flags it can be weeks early.

Whichever you use, name it in the output. Do not fall back silently. The
reference implementation reports every cluster as `unattributed` when no t=0
source is supplied, rather than substituting the first commit.

## The measured population

`t=0` above defines *when* a feature served a user. This defines *which*
features are eligible to carry a number at all. Every metric below is computed
over the first outcome only.

| Outcome | Condition | Treatment |
|---|---|---|
| **Measured** | Deployed, and served a user | Output. All four metrics computed here. |
| **Liability** | Deployed, never served a user | Cost, not output. Report the count separately; it is inventory you are paying to run. |
| **Carried** | Never deployed, but changed a later release | Neither output nor waste. Requires an explicit link to the release it changed. |
| **Sunk** | Never deployed, changed nothing | Cost. Record it as cost. |

Two rules make this defensible rather than a way to relabel work as waste:

1. **Censoring.** A cluster that has not deployed *yet* is **unresolved**, not
   sunk. Classify at the edge of the observation window and report the
   unresolved share, exactly as `deploys-to-stability` reports its plateau.
   Never let unresolved default into `sunk`.
2. **Absence of evidence.** "No usage signal" and "no usage" are different
   claims. Without a usage source (flag evaluation, or a caller for a
   platform capability), a deployed cluster is `measured` with the usage gate
   recorded as unavailable — not demoted to `liability`.

For a platform or internal capability the user is the next service, so the
usage signal moves rather than disappearing: the capability is used when
something else calls it.

Reference implementation status, stated plainly: it distinguishes `measured`
from `unattributed`, and it does **not** yet distinguish "never deployed" from
"deploy source not supplied". Until it does, treat the undeployed quadrants as
a manual classification over the `unattributed` set.

## The efficient set

Comparing workflows is a different question from measuring one, and it has
more than one objective. Three axes, all "more is better", so the efficient
set is the outer surface:

- **delivery per spend** — capabilities that shipped and were used, per unit
  of spend. Fully loaded where possible: agent tokens, CI, review time and the
  tail. Per `$10k` with a real cost input; per 1k changed lines as a git-only
  proxy, labelled as a proxy.
- **delivery per time** — the same numerator over engineer-months, or over
  elapsed calendar months as the proxy. Two denominators rather than one,
  because a flow can be cheap and slow or fast and expensive and a single
  score hides which.
- **share that settles** — `1 - never_stabilises_share`. The only one of the
  three that is already a rate.

A flow is **dominated** when another flow is at least as good on all three and
strictly better on one. Report the non-dominated set and the dominated list;
do not report a weighted total. Any weighting is an unstated claim about how
much stability a month of delay is worth.

Two properties to state whenever this is reported:

- **It is relative.** A flow on the frontier is not good, only unbeaten by the
  comparison set you supplied. Adding a better flow moves the frontier.
- **It is subject to the sweep.** Recompute the frontier at every clustering
  threshold. A flow efficient at one threshold out of four is a modifiable-unit
  artefact. `--front` reports membership as `n/total` for exactly this reason.

Economics is a denominator here rather than a fourth axis, because cheaper is
not independent of the other three — it is what falls out when they go right.
Performance is the fourth axis and is read as a pairwise projection.

## Agent spend, and what it is not

Of the four cost components — agent spend, build and CI, review and correction,
and the tail — only agent spend is available locally without new
instrumentation, because the session archive is already on disk. `--agentsview`
plus `--prices` reads token counts by model and applies a blended per-model
rate.

Three rules, all of them about not overstating it:

1. **Report the priced share.** Tokens on a model with no price row are counted
   in the token total and excluded from the spend. Below roughly 90% priced,
   the total is not worth quoting.
2. **Say it is blended.** The export does not reliably separate input from
   output tokens, so the rate is blended and the output labels it that way. A
   split rate derived from a total would be a fabrication.
3. **Say which quarter of the problem it is.** Agent spend is the component
   everyone already tracks and usually the smallest. Reporting it alone
   reproduces exactly the mistake the tail metrics exist to correct.

If the archive's shape is unrecognised the tool reports `unavailable` and the
reason. It never emits a zero, because a zero here is indistinguishable from
"no agent activity" and would be read as the latter.

## Invisible effort

Every effort figure in this document is derived from commits. That is a blind
spot with a known direction and a measurable size, so measure it rather than
argue about it.

Feed `--sessions` one row per agent session with a parent pointer, and a run is
a maximal tree over `parent_session_id`. Then:

    invisible_effort_share(run) = effort in sessions with zero commits
                                  ------------------------------------
                                  effort in all sessions of that run

Reported as a **distribution** — median and P90 — never a mean. The published
finding this exists to check is that the effort is *concentrated*: of roughly
200,000 agent runs at Ramp, 17% contained more than one work item and those 17%
carried 58% of model spend, and in their worked example the root session held
about a fifth of the run's cost while both merged pull requests came from
child sessions. A mean would hide exactly that concentration.

Three companion numbers, all reported alongside it:

- **multi-session run share** and the effort share those runs carry, so any org
  can compute its own version of the 17% / 58% result.
- **no-artifact effort share** — runs where no session committed anything.
  Distinct from `sunk`: that work was abandoned, this may have succeeded and
  had nothing to commit. Investigations, research, triage and documentation
  live here.
- **unrated effort share** — sessions with no commit information at all.
  Excluded from the invisible-effort figure rather than counted as zero,
  because "committed nothing" and "not measured" are different claims.

What this means for tail mass, stated plainly wherever tail mass is reported:
it is an **undercount**, and it undercounts most in multi-objective runs, which
are the runs most likely to have a tail. The mitigation is that tail mass is a
ratio over a consistent denominator, so a proportional undercount moves the
level and not the shape — but concede the bias before offering that, never
instead of it.

## Never landed

Share of sessions the archive classes `abandoned` or `errored`, as a local
estimate of the **sunk** quadrant — work that was paid for and did not ship.

Two caveats travel with it everywhere, and they are not optional:

- The outcome labels are **triage heuristics** by the archive's own
  documentation, not ground truth. This is an estimate.
- The **unknown share is reported alongside it** and is not folded in. A
  session whose outcome could not be classified is not a session that
  succeeded.

This does not replace the four-way gate. It estimates one quadrant of it from
one data source, at session granularity rather than per feature cluster.

### Amortised spend

`--cost` also yields the rate a budget is actually denominated in: fully-loaded
spend per engineer-month, reported as `$/eng-mo`. Cost per capability is the
better number for a single feature; spend per engineer-month is the one that
compares two teams shipping different things, and one quarter to the next
through a model or harness change. Report both.

It is a rate for comparison, **not a target**. Dividing by headcount rewards
shrinking headcount, which is the same failure mode as every other metric here
and the reason all of them are descriptive.

### Why three axes and not all four

Dominance loses its discriminating power as the objective count rises relative
to the number of units: past three objectives, almost everything comes out
non-dominated and the method stops separating anything. Ishibuchi, Tsukamoto &
Nojima put it directly — "when the number of objectives increases, almost all
solutions in each population become non-dominated" (IEEE CEC 2008). So three
axes is a defended choice, not an omission.

`--front` acts on this rather than leaving it to the reader. If *every* flow
comes out non-dominated it says `DEGENERATE` and tells you to add flows or drop
to two axes; below roughly two flows per objective it says `CAUTION` and marks
the efficient set provisional. A full frontier is not good news.

The multi-input, multi-output version of this is Data Envelopment Analysis
(Charnes, Cooper & Rhodes 1978; Banker, Charnes & Cooper 1984), and it is
deliberately **not** what this tool implements. DEA has the same problem more
sharply: published rules of thumb want the number of units to be two to three
times the number of inputs plus outputs, so two inputs and four outputs needs
twelve to eighteen workflows before it discriminates at all (Charles, Aparicio
& Zhu, *EJOR* 279(3), 2019). Almost no organisation has eighteen
distinguishable workflows. Pure dominance over three axes degrades more
honestly.

## Feature cluster

The unit of analysis. Never an engineer, never a repository, never a sprint.

Construction, in order:

1. **Anchors.** Human-declared boundaries: flag keys, ticket references, API
   schema files, ADRs, spec files. Free, and defensible without argument.
2. **Clustering** for the remainder, over commit subjects plus touched paths.
   Pin the embedding or similarity model and record its version alongside
   every result.
3. **Threshold sweep.** Recompute everything across a range of thresholds and
   report whether the shape holds.

Infrastructure-only commits (CI config, Terraform, charts) are real effort
but belong to no feature. Count them separately so they cannot inflate a
cluster's tail.

### On the sweep

This is the modifiable areal unit problem, borrowed from spatial statistics:
redraw the unit boundaries and you change the correlation. Feature clustering
has exactly that failure mode. A single threshold is therefore a choice you
cannot defend on stage or in a review; a shape that survives the sweep is a
finding. Ship the sweep as a default, not behind a flag.

## Deploys-to-stability

Number of deploys touching a cluster from `t=0` until `k` consecutive quiet
days (default `k = 14`).

Fit as a **survival curve with right-censoring**, not as a mean:

- The event is "went quiet".
- A cluster still receiving deploys at the end of the observation window is
  **censored**, not excluded and not counted as an event.
- `window_end` is the end of the observation window, not the cluster's own
  last commit. A cluster is trivially silent after its final commit, so using
  it censors everything.

Report the curve and its plateau. Two workflows can post an identical mean
while one leaves a third of its features on a permanent plateau and the other
leaves none. **The plateau is the answer to "are we more stable?"**

## Rework-deploy ratio

    post_t0_changed_lines / pre_t0_changed_lines

Above 1.0 means more code changed after the feature shipped than to ship it,
i.e. a draft shipped. Undefined (not zero) when the pre-`t=0` churn is zero;
report it as undefined.

## Tail mass

    post_t0_effort / (pre_t0_effort + post_t0_effort)

**This is an undercount.** Effort is commit-derived, so agent work that
produced no commit is invisible to it; see *Invisible effort* above for how to
measure the size of that blind spot, and report it next to any tail mass you
publish.

Effort is changed lines by default; substitute session time or token spend if
you have a trustworthy join. Deliberately a **ratio**, so it survives
comparison across teams of different sizes. Absolute counts do not.

## Tail attribution

Tail mass grouped by the workflow, harness and review path that produced the
cluster. This is the metric that says what to change, and the only one that
requires the full join.

## Unattributed share

Share of clusters whose `t=0` or originating session could not be resolved.
Always reported, never renormalised away.

A tail metric computed over a broken join is worse than no tail metric,
because it looks like knowledge. Every missing link must remain visibly
missing.

---

# Citations

Every entry carries a publication date. On a subject whose object of study
changes every quarter, the vintage of a claim is part of the claim.

## The distribution: why these metrics are defined as distributions

- **Ostrand, T. J., Weyuker, E. J., & Bell, R. M.** *Predicting the Location
  and Number of Faults in Large Software Systems.* IEEE Transactions on
  Software Engineering 31(4):340-355. **Apr 2005.**
  doi:10.1109/TSE.2005.49. The 20% of files with the highest predicted fault
  count contained between **71% and 92%** of the faults actually detected,
  average 83%. Two large industrial systems: 17 consecutive quarterly releases
  over four years, and nine releases over two.
- **Louridas, P., Spinellis, D., & Vlachos, V.** *Power Laws in Software.*
  ACM TOSEM 18(1), Article 2. **Sep 2008.** doi:10.1145/1391984.1391986.
  Distributions with long, fat tails in software are "much more pervasive than
  already established, appearing at various levels of abstraction, in diverse
  systems and languages."
- **Muzammil, S., Ur Rehman, M., Kotti, Z., & Spinellis, D.** *Source Code
  Hotspots: A Diagnostic Method for Quality Issues.* arXiv:2602.13170,
  **13 Feb 2026.** Complete version histories of 91 actively developed GitHub
  repositories. Hotspots are small portions of code that change far more often
  than the rest and concentrate maintenance activity; **74% of all hotspot
  edits come from automated accounts.**

Together these are why tail mass is reported as median and P90 rather than a
mean, and why deploys-to-stability is a survival curve rather than an average.

## Load-bearing

- **Demirer, M., Musolff, L., & Yang, L.** *Writing Code vs. Shipping Code:
  Productivity Effects Across Generations of AI Coding Tools.* **27 May 2026.**
  Matched event study over >100,000 GitHub developers joined to AI usage
  telemetry. Cumulative commit effects: autocomplete +40%, interactive agents
  +140%, autonomous agents +180%. Attenuation: +50% at project level, +30% at
  releases. Estimated elasticity of substitution between AI and human effort
  0.25, i.e. strong complementarities. Across four app marketplaces: moderate
  increase in new apps, no increase in total usage. Their term for the
  mechanism is the *weak-link hypothesis*.
- **Becker, J., Rush, N., Barnes, B., & Rein, D.** (METR) *Measuring the
  Impact of Early-2025 AI on Experienced Open-Source Developer Productivity.*
  arXiv:2507.09089, **Jul 2025** (v2 25 Jul 2025). RCT, 16 developers, 246
  tasks, mature repos averaging 23k stars, randomised per task. Forecast -24%,
  post-hoc self-report -20%, measured **+19%**. Economics experts -39%, ML
  experts -38%. Vintage: Feb-Jun 2025 frontier, primarily Cursor Pro with
  Claude 3.5/3.7 Sonnet.
- **Murphy-Hill, E., Butler, J., & Savelieva, A.** (Microsoft) *Adoption and
  Impact of Command-Line AI Coding Agents.* arXiv:2607.01418, **1 Jul 2026.**
  Tens of thousands of engineers; adopters merged roughly 24% more PRs over a
  four-month window. Source of the proxy caveat.
- **Rossi, C., Shibley, E., Su, S., Beck, K., Savor, T., & Stumm, M.**
  *Continuous deployment of mobile software at Facebook (showcase).* FSE 2016,
  **Nov 2016.** doi:10.1145/2950290.2994157. Release cycles 4 weeks -> 2 weeks
  (iOS) and 1 week (Android); crash rates, critical issues and post-branch
  fixes constant or decreasing. Deploy frequency was not the risk. Gatekeeper
  is the flag-gating system, which is also the t=0 argument.
- **Maddila, C., Upadrasta, S. S., Bansal, C., Nagappan, N., Gousios, G., &
  van Deursen, A.** *Nudge: Accelerating Overdue Pull Requests Towards
  Completion.* ACM TOSEM, **published 30 Mar 2023**; preprint arXiv:2011.12468,
  v5 17 Jun 2022. doi:10.1145/3544791. Randomised trial on **147** Microsoft
  repositories: resolution time cut by **60%** across **8,500** pull requests
  against un-notified overdue PRs; developers resolved **73%** of notifications
  as positive; deployment scaled to 8,000 repositories and 210,000
  notifications over a full year. The design lesson is that interventions must
  be activity-aware and actor-targeted.

  > Do not repeat "10 repositories, 22,875 pull requests" as the trial result.
  > That is the dataset used to build the completion-time model.

## Quality and tail evidence

- **Liu, Y., Widyasari, R., Zhao, Y., Irsan, I. C., Chen, J., & Lo, D.**
  *Debt Behind the AI Boom.* arXiv:2603.28592, **Mar 2026** (v2 26 Apr 2026).
  302.6k verified AI-authored commits across 6,299 repositories; 484,366
  distinct issues, 89.3% code smells; >15% of commits from every assistant
  introduce at least one issue, though rates vary by tool; **22.7%** of tracked
  issues survive to the latest revision.
- **Orlanski, G., et al.** *SlopCodeBench.* arXiv:2603.24755, **Mar 2026**
  (v2 7 May 2026). 36 problems, 196 checkpoints; best agent passes 14.8% of
  checkpoints; structural erosion rises in **77%** of trajectories and
  verbosity in **75.5%**; agent code 2.3x more verbose and 2.0x more eroded
  than 473 open-source Python repositories. Explicit quality guidance reduces
  initial verbosity and erosion by up to a third without changing degradation
  rates. A benchmark, not production: do not blur it with the figure above.
- *How Coding Agents Fail Their Users.* arXiv:2605.29442, **28 May 2026.**
  20,574 real sessions; constraint violation 38.3%, misread intent 27.0%,
  inaccurate self-reporting 22.6%; **91.5%** of fixes required an explicit
  developer correction and agents self-corrected only **3%** of the time. CLI
  agents violate stated constraints more often than IDE agents, 49.5% vs
  32.3%.
- *When the Specification Emerges* (SLUMP). arXiv:2603.17104, **17 Mar 2026.**
  Quantifies spec drift in long-horizon coding agents; single-shot
  specification beat emergent specification in 16 of 20 comparisons.
- *Beyond the 'Diff': Addressing Agentic Entropy in Agentic Software
  Development.* arXiv:2604.16323, **Apr 2026** (v2 21 Apr 2026). Names the
  accumulating divergence between agent action and architectural intent, and
  argues diff-level review cannot see it.

## Cost

- **Gaba, V., Mathur, A., Singh, R., Wendell, P., & Zaharia, M.**
  *Benchmarking coding agents on Databricks' multi-million-line codebase.*
  Databricks engineering, **8 Jul 2026.** Tasks derived from real merged PRs
  across 10+ languages, prompts stripped of solution hints, git history sealed,
  verified against held-out tests rather than an LLM judge. A model 1.7x
  cheaper per token consumed 1.9x more tokens, cost more per completed task
  ($2.09 vs $1.94) and scored six points lower (81% vs 87%). Swapping only the
  harness around a fixed model changed cost by more than 2x at equal quality,
  with one harness keeping a 3x tighter working set. An open model tied the
  frontier on quality at $1.28 per task.
- *The Efficiency Frontier: A Unified Framework for Cost-Performance
  Optimization in LLM Context Management.* arXiv:2605.23071, **May 2026**
  (v2 27 Jun 2026). Cost-performance framed as a deployment-aware problem; the
  useful output is the regime boundaries, not a winner.
- *Tokenomics: Quantifying Where Tokens Are Used in Agentic Software
  Engineering.* arXiv:2601.14470, **20 Jan 2026.**

## Adoption baselines, for comparison rather than argument

- **Linear.** `linear.app/data`, **Jun 2026.** Coding-agent-connected teams
  went from 21 to 65 PRs/week (+210%); traditional teams 8 to 10 (+25%). AI
  authors close to half of all issues created. 127,000 paid users.
- **Kumar, A., et al.** (Tata 1mg) *Intuition to Evidence.* arXiv:2509.19708,
  **24 Sep 2025.** 300 engineers over a year; PR review cycle time -31.8%;
  adoption 4% month one -> 83% peak -> 60% steady state.
- *Agentic Delegation and the Language Frontier of Software Developers.*
  arXiv:2605.25438, **May 2026** (v2 7 Jul 2026). 5,346 developers; active
  languages +2.5 against a 0.9 baseline. The authors describe these as
  event-time associations rather than definitive causal effects; quote them
  that way.
- *Agentic Coding in the Wild.* arXiv:2608.00101, **30 Jul 2026.** 13M
  sessions, 95T tokens of production traces. KV-cache hit ~90% within a turn,
  ~55% across turns.
- **DORA.** *State of AI-assisted Software Development.* Google Cloud,
  **Sep 2025.** ~5,000 respondents, survey conducted 13 Jun - 21 Jul 2025,
  100+ hours of qualitative data. AI characterised as an amplifier: it
  magnifies the strengths of high-performing organisations and the
  dysfunctions of struggling ones. Treat the wording as a paraphrase until
  checked against the primary report.

## Standards

- OpenTelemetry **CI/CD semantic conventions** - release candidate.
  `cicd.pipeline.*`, `cicd.pipeline.task.*`.
- OpenTelemetry **GenAI semantic conventions** - moved to
  `open-telemetry/semantic-conventions-genai`; schema URL still unset. Query
  by `gen_ai.*` attribute rather than span name, and pin the version.
- **OpenFeature** telemetry hooks for flag evaluation events. Watch the
  migration from span events toward log-based events.
