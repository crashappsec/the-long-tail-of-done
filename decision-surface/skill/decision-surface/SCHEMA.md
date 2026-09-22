# The contract

Two files in, one analysis JSON out. Everything the tool knows about your
engineering comes from `builds.csv`; everything it computes from them is
declared in `metrics.csv`. Nothing else is read, and nothing is inferred from
column names.

Read this before running anything. If your CI export can be shaped into
`builds.csv`, you never need to run `shipping_tail.py` or any other producer —
that independence is the reason this tool exists as a sibling rather than a
subcommand.

---

## 1. `builds.csv` — one row per build

The measured object is a **build**: one attempt to change production. Not a
commit, not a PR, not a sprint. A build is the smallest unit that can be asked
the only two questions that matter downstream — *did it deploy* and *was it
used* — and CI already knows both.

| Column | Required | Type | Meaning |
|---|---|---|---|
| `build_id` | yes | string, unique | Stable identifier. Ingest is idempotent on this. |
| `group` | no | opaque string | Team, repo, workflow, harness, model — whatever you compare. Never interpreted. |
| `first_commit_ts` | yes | timestamp | Start of the clock for lead time. |
| `ts` | yes | timestamp | Build produced. Decides which window the build lands in. |
| `deployed_at` | no | timestamp | Reached production. Null means never (or not yet). |
| `served_at` | no | timestamp | First served a user. Flag activation preferred over first request. |
| `usage` | no | number | Usage signal over the window. Requests, sessions, distinct users — your choice, one definition. |
| `loc_added` | yes | integer | Lines added. |
| `loc_removed` | yes | integer | Lines removed. |
| `bugs` | no | integer | Defects attributed to this build. |
| `cost_usd` | no | number | Fully loaded where possible: engineer time, agent tokens, review, CI. |
| `question` | no | enum | `features` \| `correctness` \| `performance` \| `economics`. |
| `carried_into` | no | build_id | Explicit link. Required to classify a non-deployed build as `carried`. |

Timestamps are ISO 8601. Dates (`2026-04-01`) are accepted and read as
midnight UTC. A `Z` suffix or a numeric offset is accepted; a naive timestamp
is read as UTC.

### Nullable means "we could not observe it", never zero

This is load-bearing and the tool will not collapse the two. `cost_usd` empty
means the build's cost is unknown and it is excluded from the cost mean with
the exclusion reported. `cost_usd` of `0` means the build was free. `bugs`
empty means nobody looked; `bugs` of `0` means somebody looked and found none.
A tool that silently reads empty as zero manufactures a clean team out of an
unmeasured one.

### `group` is opaque and optional

The tool never parses it, never orders it, never infers a hierarchy from it.
Absent, every build lands in one group named `all` and the comparison runs over
*windows* instead — see §4.

---

## 2. The impact gate

Two signals, six classes, applied in this order. The first matching rule wins.

```
as_of - ts < L                           -> unresolved   (checked first, overrides all)
not deployed  &  carried_into set         -> carried
not deployed  &  carried_into null        -> sunk
deployed      &  usage null               -> impactful    + gate recorded UNAVAILABLE
deployed      &  usage >= u               -> impactful
deployed      &  0 < usage < u            -> low_impact
deployed      &  usage == 0               -> liability
```

`deployed` means `deployed_at` is non-null. Nothing else is consulted.

Six classes, so the impact mix is a composition with **five degrees of
freedom**. Treat it as five, not six — §5.

### `u`, the usage threshold

Default: **the median `usage` across served builds in the baseline window**,
which is the whole pooled dataset unless you pass `--baseline`. Relative and
self-calibrating, because an absolute default is a fiction across teams — a
platform capability serving 40 internal callers and a consumer feature serving
400,000 sessions cannot share a number.

Every run computes the mix at `0.5u`, `u` and `2u` and prints all three. **A
finding that flips between them is an artefact of the threshold, not a property
of the team**, and the tool marks it provisional. Override with `--u`.

### `L`, the resolution lag

Default **30 days**. A build younger than `L` at the **observation edge** is
`unresolved` — not `sunk`, not `liability`. This is right-censoring, and it is
the entire difference between "we shipped no waste" and "we have not looked
yet". Rising `unresolved` with a flat mix elsewhere is a reporting artefact and
the tool says so.
The observation edge is `as_of`: the run date by default, or `--as-of`. It is
not the end of each window — a build from January is not unresolved in
September just because January ended within 30 days of itself. Classification
happens at the edge of the *observation*, which is why the unresolved share
concentrates in the newest window and why re-running an old analysis with a
later `as_of` resolves builds that were unresolved before.

### `usage` null keeps the build `impactful`

"No usage signal" and "no usage" are different claims. Demoting an unmeasured
build to `liability` manufactures waste out of missing telemetry. The build
stays `impactful`, the gate is flagged `UNAVAILABLE`, and **the share of builds
with an unavailable gate is printed next to every impact mix**. If that share
is large, the mix is a statement about your telemetry.

---

## 3. Lead time, honestly

```
served_at present                             -> SERVED    (best)
served_at null, deployed, usage unobserved    -> DEPLOYED  (fallback)
served_at null, deployed, usage > 0           -> DEPLOYED  (fallback)
served_at null, deployed, usage == 0          -> undefined
not deployed                                  -> undefined
```

**The `DEPLOYED` fallback is for a build whose serving we could not observe, not
for a build we know was never served.** A build with `usage == 0` was deployed
and used by nobody, so its first-commit-to-first-user interval does not exist —
there was no first user. Recording the deploy interval instead would put a
systematically smaller number into the median and make the groups carrying the
most waste look the fastest. `usage > 0` with no `served_at` is the opposite
case: we know it was served, we just do not have the moment, so the fallback
applies.

Recorded per build, and every cell reports its **fallback share** — the
proportion of its defined lead times measured to deploy rather than to first
user.

Two things follow, and the second is a deliberate refinement of what the plan
for this tool said:

- A cell that mixes the two sources at all is a `MIXED_LEAD_TIME_SOURCE`
  **caution** naming its share. The median is then computed over two different
  measurements, which is worth knowing.
- Cells whose fallback shares differ by more than **20 points inside one
  comparison** are a `DIVERGENT_LEAD_TIME_SOURCE` **refusal**. This is the
  condition the rule exists for: merge-to-exposure lag differs by team by days
  to weeks, so comparing a cell measured 8% to deploy against one measured 40%
  to deploy can manufacture the difference the front reports. Equal fallback
  shares do not establish equal measurement bias; mixed cells retain their
  caution even when the comparison passes this policy threshold.
  `--allow-mixed-lead-time` downgrades the refusal to a caution stamped on
  every chart in the run.

The axis is the **median**. P90 is carried as a descriptor and drawn as a
whisker, because the tail is where the interesting failure lives and a median
alone hides it.

---

## 4. Cells, groups, windows

A **cell** is `(group, window)`. Windows are calendar months by default
(`--window month|week|quarter`), last 6 retained, the tube draws the latest 4.
Every cell carries `n_builds`. Cells below the floor (default 10,
`--min-builds`) are reported and **not plotted**.

### Self-comparison is the default, not a fallback

A front is relative to the comparison set you supplied, and most teams are one
team. With no `group` column the tool computes the front **over the group's own
windows**: which of our past months is not beaten by another month of ours, on
cost, lead time and debt. That is a real answer, and it means the tool works on
day one for a team with nobody to benchmark against.

With groups present, both fronts are computed — across groups within the latest
window, and within each group across its windows — and the output labels which
is which. They answer different questions and mixing them up is the easiest
mistake to make with this tool.

### Time is two variables and they must not merge

- **window** — calendar sequencing. This is what "over time" means. An index,
  never an objective, never on an objective axis.
- **lead_time** — an objective, minimised.

Every picture in `references/pictures.md` follows from that split.

---

## 5. Mixes are compositions

The impact mix (6 parts) sums to 1 by construction, because the gate assigns
one of its six classes to every build. **The question mix does not**: `question`
is supplied, so a value outside the four classes leaves the mix summing to less
than 1. That shortfall is reported as `question_unclassified_share` per cell,
raised as a `QUESTION_UNCLASSIFIED` caution, and printed on the page. It is
never renormalised away — a mix that quietly adds up to 1 after dropping a part
is precisely the move this tool exists to refuse.

Both mixes sum to 1 over their declared parts plus that reported shortfall. That constraint has consequences the tool enforces rather than
documents:

- **No Pearson correlation between shares.** The sum constraint manufactures
  negative correlation; Pearson pointed this out in 1897.
- Distance, clustering or regression over a mix runs on **CLR** coordinates.
  Back-transform for display only.
- The interpretable quantities are **log-ratios**. `liability : impactful`
  means something. "Liability rose 4 points" also means three other things
  moved, and does not say which.
- Movement between windows gets **one honest scalar**: the Aitchison distance
  between consecutive windows, plus the log-ratio contributing most of it.

---

## 6. `metrics.csv` — the formula registry

Every derived number is a declared formula. Nothing is hard-coded to a column
name, including `debt`.

```csv
name,role,direction,expression,version,notes
cost,objective,min,mean(cost_usd),2,per observed build
lead_time,objective,min,median(lead_time_days),1,P90 carried as descriptor
debt,objective,min,minmax(sum(bugs) / (sum(loc_added + loc_removed) / 1000)),1,defect density
```

| Field | Meaning |
|---|---|
| `name` | Referenced on the command line and in every output. |
| `role` | `objective` or `descriptor`. Only objectives enter dominance. |
| `direction` | `min` or `max`. Required for objectives, ignored for descriptors. |
| `expression` | Arithmetic over build columns and cell aggregates. See below. |
| `version` | Integer. Bump it when you change the expression. The registry is append-only. |
| `notes` | Printed on any chart that uses the metric. Say what the number claims. |

### The expression language

Deliberately small. Names, numbers, `+ - * / ** ( )`, unary minus, comparison
inside `count(...)` and `share(...)`, and a fixed function allowlist:

```
sum(x)          median(x)        mean(x)        p90(x)      p10(x)
count()         count(pred)      share(pred)    share(class)
min(x) max(x)   abs(x)           sqrt(x)        log(x)
minmax(v)       z(v)             clip(v, lo, hi)
```

- `x` is a build column or a per-build derived name (`lead_time_days`,
  `loc_total`, `impact`, `question`).
- `count(impact == impactful)` and `share(liability)` are both accepted;
  a bare class name is sugar for `impact == <class>`.
- `minmax(v)` and `z(v)` are **cell-level** normalisations: they need every
  cell's value, so they are applied after all cells are aggregated. They may
  only wrap a scalar aggregate expression. They cannot appear inside another
  aggregate or normaliser. `clip` bounds must be scalar numeric expressions.

Expressions are limited to 4096 characters, 512 AST nodes and 64 nesting
levels. Arithmetic uses finite floating-point numbers; powers outside the
finite real domain return null with a note.

Evaluated by a **restricted AST walker** — no `eval`, no imports, no attribute
access, no subscripts, no comprehensions, no names outside the schema. A
formula is data, and data from a teammate must not become code. `formula.py`
rejects anything else with the offending node type named.

### Shipped defaults

Seeded into a fresh store, version 1 except `cost` (version 2):

| Name | Role | Expression |
|---|---|---|
| `cost` | objective min | `mean(cost_usd)` |
| `cost_kloc` | descriptor | `sum(cost_usd) / (sum(loc_total) / 1000)` |
| `cost_impact` | descriptor | `sum(cost_usd) / count(impact == impactful)` |
| `lead_time` | objective min | `median(lead_time_days)` |
| `lead_time_p90` | descriptor | `p90(lead_time_days)` |
| `debt_defect` | objective min | `minmax(sum(bugs) / (sum(loc_total) / 1000))` |
| `debt_composite` | objective min | `0.5 * minmax(sum(bugs) / (sum(loc_total) / 1000)) + 0.5 * (share(liability) + share(sunk))` |
| `throughput` | descriptor | `count()` |

Default objective set is `cost, lead_time, debt_defect`.

An unchanged shipped cost v1 is upgraded by appending v2. Existing history is
preserved; custom definitions are not replaced. Each run pins the full metric
definitions, including CSV definitions, so a changed expression with an
unchanged version still makes `diff` report `NOT COMPARABLE`.

Two notes on `debt_composite`, both load-bearing:

1. **Term one is defect density — bugs per KLOC.** The heuristic it implements
   was written as "total LOC / bugs", which is inverted for a minimise axis:
   more LOC per bug is *better*, so it would have to be flipped before it could
   sit next to cost and lead time. Bugs per KLOC is the same claim pointing the
   right way. If you meant the other one, it is one row in `metrics.csv`:
   `surface define --name debt_loc --role objective --direction max
   --expression 'sum(loc_total) / sum(bugs)'`.
2. **Both terms are normalised to [0,1] before the weighted sum**, or raw
   defect density swamps a share that already lives in [0,1]. The weights are
   a claim about how many defects a point of liability is worth, they are
   declared, and they are printed on every chart that uses the metric.

### Structural overlap

`debt_composite` consumes `share(liability)` and `share(sunk)`, which are also
spokes on the impact radar. That is real double counting: an objective axis and
two glyph arms are the same measurement.

The tool does not ban it — the formula is your judgement — it makes it
impossible to miss. Because formulas are declared, a dependency graph from
metric to raw column is built and overlap is computed **syntactically, before
any data is read**:

```
OVERLAP: debt_composite <- share(liability), share(sunk)
```

Overlapping spokes are greyed in every glyph in the run, with a legend note.
Stage 2 (`references/front.md`) then measures how big the overlap actually is
as Kendall τ. Two independent checks, one from the definitions and one from the
data, and a run reports both.

`cost_impact` overlaps `share(impactful)` the same way, which is why the
shipped default for `cost` is per-build.

---

## 7. Aggregate-only input

Teams without build-level data supply cells directly:

`cells.csv` — `group,window,n_builds,cost,lead_time,debt_defect` plus optional
`cost_se,lead_time_se,debt_defect_se` and optional mix shares
`share_impactful,share_low_impact,share_liability,share_carried,share_sunk,share_unresolved`.

Use `compute --cells cells.csv`, or MCP `compute` with `cells` (records) or
`cells_csv` (a path confined to the store directory). Objective names must be
registered metrics; `--objectives` selects the columns. Supplied values are
already evaluated metrics, before the direction flip and chart normalization.
No synthetic builds are created and formulas are not evaluated over aggregates.

The non-parametric bootstrap is replaced by normal draws over supplied standard
errors (`references/front.md` section 6). Missing any required standard error
makes uncertainty unavailable for the run; zero means a known zero error.
Observed fronts remain available. The draws use the same fixed normalization
as the observed values. Include `lead_time_p90` to draw the tail and
`lead_time_fallback_share` to check lead-time source comparability; absent
source shares produce an explicit warning.

Provide every share of an optional mix or omit that mix. Impact shares must
sum to 1, including unresolved. Question shares may total less than 1 and
report the shortfall. Missing mixes are not drawn. Build-level threshold
sensitivity and usage-gate availability cannot be reconstructed from aggregate
input and are marked unavailable.

---

## 8. Worked example

Four builds from a CI export, one team, no groups. The window is April 2026 and
the run happens on 2026-05-01, so `window_end` is `2026-05-01` and `L` is 30
days.

```csv
build_id,first_commit_ts,ts,deployed_at,served_at,usage,loc_added,loc_removed,bugs,cost_usd,question,carried_into
b-1,2026-04-01,2026-04-03,2026-04-03,2026-04-04,1840,420,60,1,900,features,
b-2,2026-04-02,2026-04-06,2026-04-07,,0,150,20,0,300,correctness,
b-3,2026-04-05,2026-04-09,,,,80,10,,120,features,b-11
b-4,2026-04-20,2026-04-28,2026-04-29,2026-04-29,12,60,5,0,200,performance,
```

Median `usage` over served builds (`b-1`, `b-4`) is `(1840 + 12) / 2 = 926`,
so `u = 926`.

| Build | Age at window end | Class | Why |
|---|---|---|---|
| `b-1` | 28d | `unresolved` | younger than `L = 30`, checked first |
| `b-2` | 25d | `unresolved` | same |
| `b-3` | 22d | `unresolved` | same |
| `b-4` |  3d | `unresolved` | same |

Every build is unresolved, and that is the correct answer for a window that
ended yesterday: **you cannot classify April's waste on the first of May.** The
mix is `unresolved: 1.0` and the tool refuses to draw a front from one cell.
This is the example most teams need to see first, because the instinct is to
run the tool on the month that just ended.

Re-run the same file on 2026-06-15 (`window_end = 2026-06-15`, all four builds
older than `L`):

| Build | Class | Why |
|---|---|---|
| `b-1` | `impactful` | deployed, `usage 1840 >= u` |
| `b-2` | `liability` | deployed, `usage == 0` |
| `b-3` | `carried` | not deployed, `carried_into = b-11` |
| `b-4` | `low_impact` | deployed, `0 < 12 < u` |

```
impact mix   impactful 0.25  low_impact 0.25  liability 0.25
             carried   0.25  sunk       0.00  unresolved 0.00
gate unavailable share  0.00
question mix features 0.50  correctness 0.25  performance 0.25  economics 0.00

lead_time  b-1 3d (SERVED)  b-2 undefined  b-3 undefined  b-4 9d (SERVED)
           median 6.0 days, fallback share 0.00, no source mixing
cost       (900 + 300 + 120 + 200) / 4 = 380 per build
debt       1 bug / 0.805 KLOC = 1.242 per KLOC, before minmax
```

`b-2` is deployed with `usage == 0`, so it has **no** lead time: nobody was
served, so there is no interval to first user (§3). `b-3` never deployed. Only
the two served builds carry a lead time, and the median is over those two —
which is also why the fallback share is zero and no source-mixing caution
fires.

`debt` counts one bug, not two: `b-1` has one and `b-3`'s `bugs` is **empty**,
which means nobody looked. An empty `bugs` read as zero would make this cell
look half as buggy as it is.

At `0.5u = 463`, `b-4` stays `low_impact`. At `2u = 1852`, `b-1` drops to
`low_impact` and the impactful share goes to zero — so **the sensitivity strip
fires and any claim about impactful share here is provisional.** Four builds is
below the floor of 10 and the cell is reported, not plotted. Both facts are in
the output before any number is.

---

## 9. What the tool refuses

| Condition | Behaviour |
|---|---|
| More than 8 objectives | Refuse. Point at stage 3 objective reduction. |
| Fewer than 2 objectives | Refuse. This is a ranking, not a front. |
| Fewer than `m + 1` cells | Refuse to draw a front. |
| Fewer than `2 × m` cells | `CAUTION`, front marked provisional. |
| `n_builds` below floor | Cell reported, not plotted. |
| Cells whose lead-time fallback shares differ by >20 points in one comparison | Refuse, unless `--allow-mixed-lead-time`. |
| Any cell mixing lead-time sources | Caution, with the share named. |
| More than 6 groups × 4 windows in the cube | Refuse to draw, and name what to split out. |
| Duplicate `build_id` in one ingest | Rejected with a reason, not overwritten. |

DEA is deliberately not implemented: published rules of thumb want
2–3× as many units as inputs plus outputs, and almost nobody has eighteen
distinguishable workflows.
