# The HTTP tier — designed, and deliberately not built

Tier 1 is the local SQLite store. Tier 2 is the stdio MCP server over the same
store. **Tier 3 is this document, and nothing else.** No code, no scaffold, no
"just the read endpoints to get started".

The reason is not effort. A hosted multi-tenant store is a different product
from a local analysis tool, and the difference is not the HTTP layer:

- **Build-level engineering data is sensitive.** `cost_usd` per build across a
  `group` is a compensation-adjacent signal. `bugs` attributed per build, over
  a group that maps to a person, is a performance record. The local path sends
  nothing anywhere and that is most of its security story; a hosted path
  replaces that story with a set of promises.
- **Whose data lands where is a decision, not an implementation detail.** It
  needs an owner, a retention answer, a deletion answer and a legal review
  before it needs a route table.
- **The metric registry is the tenancy problem in miniature.** Formulas are
  data supplied by users. A shared registry means one tenant's `debt` can be
  read by another, and a shared *evaluator* means the restricted AST walker in
  `decision_surface.formula` is now a security boundary between tenants rather
  than a guard against a careless colleague.

So: build nothing until someone asks to host it. When they do, this is the
design to argue with.

---

## 1. Same verbs as MCP, resource-per-run

The MCP tool surface is already the API surface. Keeping them identical means
one mental model and one place where the meaning of a number is defined.

```
POST   /v1/builds                      submit_builds
GET    /v1/builds?group=&from=&to=     (paged, for verification only)
POST   /v1/metrics                     define_metric
GET    /v1/metrics                     list_metrics
GET    /v1/metrics/{name}/versions     the append-only history
GET    /v1/groups                      list_groups
GET    /v1/windows                     list_windows
POST   /v1/runs                        compute      -> 202 + run resource
GET    /v1/runs                        list runs
GET    /v1/runs/{run_id}               the analysis JSON
GET    /v1/runs/{run_id}/render        the standalone HTML
GET    /v1/runs/{run_id}/cells/{cell}  explain
GET    /v1/runs/{run_id}/diff/{other}  diff
```

- **A run is a resource, not a query.** It pins its objective set, its metric
  versions, its seed, `u`, `L`, the window spec and the normalisation. Two
  clients asking the same question get the same `run_id` back or two distinct
  runs — never one mutable "current answer".
- **`POST /v1/runs` is 202 with a poll target.** A 2000-replicate bootstrap
  over a large store is not a request-scoped computation. `GET
  /v1/runs/{run_id}` returns `{"status": "computing"}` until it is not.
- **Runs are immutable once complete.** A re-run under new definitions is a new
  run, and `diff` is how the two are compared. This is the one property that
  makes stored history worth keeping.

## 2. Tenancy is asserted in the query, not in the UI

Every table gets a `tenant_id`. Every statement filters on it. The filter lives
in the data-access layer, not in a handler, not in a view, and not in a
dashboard's default parameter.

```sql
-- every read, without exception
SELECT ... FROM builds WHERE tenant_id = :tenant AND ...
```

Row-level security in the database as well, so a missing `WHERE` is a query
that returns nothing rather than a query that returns everything. The mistake
this defends against is not malice, it is a developer adding an endpoint in a
hurry — and the local tier has no such failure mode at all, which is worth
remembering when weighing whether to build this.

`tenant_id` comes from the authenticated principal and is never accepted from
the request body or a query parameter, including for admin routes. An admin
acting across tenants uses a route that names the tenant in its path and
records the access.

## 3. Auth

- **Service-to-service:** OAuth 2.0 client credentials, or mutual TLS where the
  CI platform supports it. Scopes: `builds:write`, `metrics:write`,
  `runs:read`, `runs:write`. A CI job needs exactly `builds:write`, and giving
  it more is the most likely real-world misconfiguration.
- **Humans:** the org's SSO. No local accounts, no password reset flow, no
  session store to leak.
- **`builds:write` is append-only by design.** There is no `DELETE
  /v1/builds/{id}`. Correcting a bad export is a new `source` batch plus a
  documented conflict report, because a store that can be quietly edited
  cannot support `diff`.

## 4. What crosses the wire, and what does not

| Sent | Never sent |
|---|---|
| Aggregates per `(group, window)` | Commit messages, diffs, file paths |
| Counts and shares | Author names, emails, identifiers |
| `cost_usd` totals per cell | Per-person cost, per-person defect counts |
| Metric expressions and versions | Anything not in `../SCHEMA.md` §1 |

The build record is the whole contract. If a field is not in `SCHEMA.md` §1 it
is not accepted, and a request carrying extra fields is a 400 with the
offending keys named rather than a silently truncated insert. That refusal is
the mechanism that keeps scope creep out of a sensitive dataset.

**A `group` that maps one-to-one to a person turns this into a performance
management tool.** The schema cannot prevent that and neither can the API. What
the hosted tier can do is refuse to compute a comparison whose cells fall below
the build floor — which is where per-person groups land — and say why. That
refusal already exists in Tier 1 (`BELOW_BUILD_FLOOR`) and it is load-bearing
here for a different reason.

## 5. Retention and deletion

- Builds: default 24 months, per-tenant configurable.
- Runs: kept as long as their builds, because a run whose builds are gone can
  no longer be explained or re-derived.
- Deletion is a tenant-scoped hard delete of builds, runs and results, with a
  receipt. Not a soft-delete flag: the point of the deletion request is that
  the data is gone.
- **Deleting builds invalidates the runs computed from them**, and the API says
  so rather than leaving orphaned analyses that no longer reproduce.

## 6. Rate limits and cost

`POST /v1/runs` is the expensive verb, and it is expensive in a way clients
cannot see: cost is `bootstrap × cells × objectives`. So:

- the response echoes the replicate count actually used and the wall time;
- a per-tenant concurrent-run limit, because the honest failure mode here is
  one tenant's exploratory sweep starving another's scheduled report;
- `bootstrap` above the default is a scoped permission, not a free parameter.

## 7. What is unchanged from Tier 1

Deliberately, because the analysis is the product and it must not fork:

- `surface.py` computes. There is no second implementation, and no port to
  another language. The API is a transport in front of the same module, and if
  that becomes untrue the two will drift and the numbers will stop agreeing.
- `render.mjs` renders. The HTML stays a static, self-contained artefact that
  can be mailed to a sceptic.
- The metric registry is append-only, per tenant.
- **Off-the-shelf metric defaults ship in Tier 1**, seeded into a fresh store,
  so the "sensible defaults" benefit never waited on this document.
- Every refusal in `../SCHEMA.md` §9 is a 422 with the same code and the same
  message. An HTTP client must not be able to obtain a front the CLI would
  refuse to draw.

## 8. The test that decides whether to build this

Not "would an API be nice". This:

> Is there a team that has a builds export, wants the analysis, and **cannot**
> run a Python script on a machine they control?

If the answer is no — and for most teams it is no, which is why `surface.py` is
standard library only — then the hosted tier adds auth, tenancy, retention and
a compliance surface in exchange for nothing. Build it when the answer is yes,
and build it for that team's actual constraint rather than for the general case.
