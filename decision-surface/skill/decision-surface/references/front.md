# The front is a procedure, not a call to `pareto()`

Teams pour every column they have into a multi-objective comparison and get a
front containing everything. The value of this tool is the part that says *that
happened*, with a number attached.

Eight stages. Every one of them prints, every parameter is declared in the
output, nothing is silently defaulted. `surface compute` runs them in order and
writes one `analysis.json`; `surface explain` re-reads it for a single cell.

---

## Stage 0 — roles

From the metric registry (`../SCHEMA.md` §6). Metrics with `role=objective` and
a `direction` enter dominance. Everything else is a descriptor: computed,
reported, plotted, and never allowed to decide who is on the front.

- Refuse below 2 objectives — that is a ranking.
- Refuse above 8 — the front is combinatorially guaranteed to swell (stage 4's
  null model shows why) and the answer is stage 3, not a bigger chart.
- **The structural overlap report prints here, before any data is read.**
  Formulas are declared, so the dependency graph from metric to raw column is
  static. Any objective sharing a raw input with a mix part is reported:

  ```
  OVERLAP: debt_composite <- share(liability), share(sunk)
  ```

  Consequences, all automatic: the overlapping spokes are greyed in every
  glyph in the run, the legend says why, and stage 2 measures the overlap in
  the data as well as in the definitions.

Internally every objective is flipped to minimise-form (`max` metrics are
negated) exactly once, at the end of this stage, so nothing downstream carries
a direction flag.

---

## Stage 1 — normalisation, and what it does and does not affect

Both are emitted for every cell:

- **min–max** over pooled cells — feeds hypervolume, knees, distance to the
  ideal point, level diagrams, glyph radii.
- **rank / quantile** — feeds the conflict screen and anything a single
  outlier could dominate.

Printed on every run, because it pre-empts the most common objection to a
normalised comparison:

> Dominance is invariant under any monotone per-objective transform, so **front
> membership does not depend on this choice**. Knees, hypervolume,
> distance-to-ideal and every glyph radius do.

A degenerate objective — every cell identical — is reported and normalised to
0.5 rather than dividing by zero. It cannot affect dominance either way, and
stage 2 will flag it as redundant with everything.

---

## Stage 2 — conflict screen

**Kendall τ-b** on ranks, every objective pair, over pooled cells. τ-b, not
τ-a, because cells tie on cost and lead time constantly and τ-a would read a
tie as disagreement.

| Condition | Verdict | Action |
|---|---|---|
| \|τ\| ≥ 0.8 | **redundant** | Collapse: keep the cheaper-to-measure one, log the τ and which was dropped. |
| τ ≤ −0.4 | **conflicting** | Keep both. This is where the trade-off lives. |
| otherwise | independent | Keep both. |

**Descriptors go through it too** — specifically, every objective against every
mix part. This is the measured half of the overlap check; stage 0 is the
declared half. A `debt_composite` run should show a large positive τ against
`share(liability)`, and if it does not, one of the two is not measuring what
its name says.

The full matrix is emitted. With `m ≤ 8` objectives and a handful of
descriptors it is at most a few hundred pairs over tens of cells: exhaustive,
instant.

**Axis order is fixed here, once, and reused for every picture in the run** —
most-conflicting pairs adjacent. A radar is a parallel-coordinates plot in
polar coordinates and inherits the same axis-order dependence, so ordering once
and reusing it is what keeps the pictures consistent with each other. The order
lands in `analysis.json` as `axis_order` and `render.mjs` never re-derives it.

---

## Stage 3 — objective subset, with a reported error

1. **Greedy correlation-based reduction.** Cluster objectives by conflict
   (single-linkage on |τ| ≥ 0.8), keep one representative per cluster. Jaimes &
   Coello's feature-selection framing. Always runs.
2. **δ-MOSS / k-EMOSS.** Brockhoff & Zitzler formalised exactly this problem:
   the smallest objective subset whose dominance structure differs from the
   full set by at most δ (δ-MOSS), and the best subset of fixed size k
   (k-EMOSS). Both NP-hard, both with published greedy algorithms. Greedy is
   implemented, and **δ is reported** — which is what turns "we dropped an
   axis" into a sentence with a number in it.

   The error runs in one direction and it is worth being precise about which.
   Dropping an axis never *loses* a dominance relation: if `a` beats `b` on
   every objective it beats it on every subset of them. What dropping an axis
   does is *invent* relations — `a` looks better than `b` once the axis on
   which `b` wins is not being looked at. So δ is the relaxation the **full**
   set needs in order to agree with the subset: over all pairs `(a, b)` where
   `a` dominates `b` on the subset, the largest amount by which `a` is worse
   than `b` on any objective in the full set, clipped at zero.

   δ = 0 means the subset invents nothing and the axis was genuinely
   redundant. A large δ is the price of the smaller chart, in min-max
   normalised objective units.
3. **PCA-based reduction** (Deb & Saxena) is documented and not implemented. It
   produces axes with no physical meaning, which destroys the point of axes
   called cost, lead time and debt.

---

## Stage 4 — layers, and a null model for degeneracy

**Non-dominated sorting into layers**, not a binary flag. Peel layer 1, sort
the remainder, repeat. When layer 1 swells the layers still separate, which is
the whole reason not to return a boolean.

n is tens of cells and m ≤ 8, so naive O(n²m) peeling is correct and
instantaneous. Kung's algorithm and ENS exist and are not worth importing for
24 rows; this sentence is the justification, and it is repeated in the code
where the loop is.

**Degeneracy diagnostic, printed with every front:** the fraction of cells on
layer 1.

### The null model — why "half the cells are on the front" is not a finding

Front size grows with objective count for purely combinatorial reasons, so the
raw fraction is uninterpretable alone. Bentley, Kung, Schkolnick & Thompson
(*JACM* 25(4):536–543, Oct 1978) give the expected number of maxima of n
vectors in d dimensions with independent components:

```
A(n, d) = A(n-1, d) + A(n, d-1) / n     for n, d >= 2
A(1, d) = 1                             for d >= 1
A(n, 1) = 1                             for n >= 1
```

so `A(n,2) = H_n ≈ ln n`, and `O((ln n)^(d-1))` for fixed d. Three lines, no
dependencies. Observed front size is reported against it:

| Observed vs `A(n,d)` | Reading |
|---|---|
| ≈ | **NOT INFORMATIVE.** The front is what independent noise produces. The objectives are not trading off in this data and the front says nothing beyond the dimension count. |
| ≫ | Objectives conflict more than chance. The trade-off is real and the front is the finding. |
| ≪ | Objectives agree. One cell is genuinely better — and there are probably redundant objectives, so check stage 2. |

The default output is the **permutation version**: shuffle each objective
column independently (default 1000 replicates, seeded), recompute front size,
report the observed value's percentile. It needs no independence assumption and
it handles ties, both of which the closed form does not. The closed form is
computed as well, as a sanity check on the permutation code, and both appear in
the output.

`NOT INFORMATIVE` fires when the observed front size sits between the 5th and
95th percentile of the permutation null. It is a headline, not a footnote.

---

## Stage 5 — the relaxation ladder

Applied in order when the front is degenerate. Every rung is a named, citable
relaxation with its parameter reported — never an ad-hoc filter.

1. **Reduce objectives** (stage 3). The only rung that does not weaken the
   meaning of "dominated". Always first.
2. **ε-dominance** — Laumanns, Thiele, Deb & Zitzler, *Evol. Comput.* 10(3),
   2002. Box the space at width ε per axis, apply dominance to boxes.
   **ε comes from measurement noise, not a tuning sweep**: `ε_j` is the
   bootstrap CI half-width of objective j from stage 6. Box boundaries can
   separate points less than epsilon apart, so this is a resolution heuristic,
   not a significance test or proof that differences exceed measurement noise.
   `--epsilon-source bootstrap|manual|none` chooses the source.
3. **k-dominance** — Chan, Jagadish, Tan, Tung & Zhang, SIGMOD 2006, 503–514.
   `o` k-dominates `p` if it is no worse on k of the d objectives and strictly
   better on at least one of those k. Swept from k = d down to k = 2, set size
   reported at each. **Known wrinkle, handled rather than hidden:**
   k-dominance is not transitive, so the result is a set and not an order, and
   cycles are detected and reported instead of being silently broken.
4. **Skyline frequency** — Chan et al., EDBT 2006. In how many of the `2^d − 1`
   non-empty objective subspaces is each cell non-dominated. With d ≤ 6 that is
   at most 63 subspaces over tens of rows: exhaustive, instant, no
   approximation. The best rung at this scale, because it replaces a binary
   with a **continuous interestingness score** — on the front in 40 of 63
   subspaces is a different claim from 3 of 63.
5. **Knees** — Branke, Deb, Dierolf & Osswald, PPSN VIII, 2004. Maximum
   reflex/bend angle in 2D; for m > 2, maximum perpendicular distance from the
   hyperplane through the extreme points, plus a marginal-utility ranking. The
   natural answer to "which cell should we copy".
6. **Average rank and weighted sum** — computed, and **always labelled as a
   weighting, i.e. a claim about how many dollars a month of lead time is
   worth**. It is in the tool because teams will do it anyway, and it is better
   with the weights printed on the chart than buried in a spreadsheet. Default
   weights are equal, and "equal weights" is itself a claim the label states.

---

## Stage 6 — uncertainty

Objectives are sample statistics over `n_builds`, and every cell has a
different `n_builds`. A front computed once is a guess.

- **Bootstrap over builds within the cell.** B = 2000 (`--bootstrap`), seeded
  (`--seed`). Resample the cell's builds with replacement, recompute every
  objective from the formulas, recompute the front, repeat. Yields
  **`P(on front)`**, which is reported instead of a flag. The build atom is
  what makes this available, and it is the reason the atom is a build.
- **Fixed normalization.** Formula-internal parameters and the chart transform
  are fitted once to the observed cells and held fixed across replicates.
  Replicates may extend outside [0,1]. A constant observed axis uses a scale
  of `max(1, abs(value))` around 0.5, and a constant z-normalizer uses that
  scale around zero, so sample variation is not erased at constant axes.
- **Aggregate fallback.** Cells supplied with `cost_se`, `lead_time_se`,
  `debt_defect_se` get a parametric bootstrap (normal draws clamped to the
  observed range extended by three standard errors). Missing required errors
  disable uncertainty with a caution; zero errors remain valid observations.
- **Probabilistic dominance** — `P(A dominates B)` from the replicates, as a
  matrix. Established technique; efficient computation in *ACM TELO*,
  doi:10.1145/3469801.
- **ε for stage 5 rung 2 comes from here.** One number, two uses: the CI
  half-width per objective.

Cells whose objectives cannot be recomputed under resampling (a cell of one
build, a cell where every `cost_usd` is null) are reported with
`p_on_front: null` rather than 0.0. A null is not a zero — the same rule as the
input schema.

---

## Stage 7 — the front over time

1. **Persistence.** Fraction of windows each group sits on the front, plus
   `P(on front)` per window. Feeds the membership timeline, which is the
   picture most teams end up using. Refused comparisons and cells with missing
   objectives do not enter the denominator. No usable comparisons means null.
2. **Movement of the surface.** Per-window fronts overlaid on the 2D
   projections, one staircase per window, oldest palest. The attainment-surface
   idea applied to windows instead of algorithm runs (empirical attainment
   function: Fonseca, Guerreiro, López-Ibáñez & Paquete, EMO 2011). With
   bootstrap on, 50% and 90% attainment boundaries use all replicates, including
   those that do not attain the point. A boundary stays null until enough
   replicates attain it; null portions are gaps in the drawing.
3. **Direction of travel.** Displacement between consecutive windows in
   min–max-normalised objective space: report `‖Δ‖` and the angle between `Δ`
   and the direction of the ideal point. Improving on every objective gives a
   small angle; trading debt for speed gives a large one, and the number says
   which trade. Cheapest interpretable output in the tool, and it is what the
   cube's arrows draw.
4. **Mix movement.** Aitchison distance between consecutive windows, plus the
   top contributing log-ratio (`../SCHEMA.md` §5). Zeros are handled by a
   multiplicative replacement at `δ = min(0.5 / n_builds, 0.5 / number_of_zeros)` with the remaining parts
   scaled down to preserve the sum — and the replacement is reported, because a
   composition with a structural zero is not the same object as one without.

Comparing two windows' fronts **as sets** uses hypervolume with a fixed,
published reference point. The ranking a hypervolume produces depends on that
choice, so the reference point is part of the result and not a hidden parameter
(Ishibuchi et al., *Evol. Comput.* 26(3), 2018). Default: `1.1` per axis in
min–max-normalised minimise-space, i.e. just outside the worst observed cell on
every axis, printed with the number.

---

## What the tool refuses

Repeated from `../SCHEMA.md` §9 because this is the file people read when the
tool says no:

- More than 8 objectives, or fewer than 2 → refuse.
- Fewer than `m + 1` cells → refuse to draw a front. **A refused comparison
  emits no front and no layers**, only the refusal: a front computed and
  labelled "refused" gets read off the page by somebody, and then the renderer
  is the only thing standing between that number and a slide.
- Fewer than `2 × m` cells → `CAUTION`, provisional.
- Cells whose lead-time fallback shares diverge by more than 20 points inside
  one comparison → refuse (`../SCHEMA.md` §3, which also explains why *any*
  mixing is a caution rather than a refusal).
- `n_builds` below the floor → cell reported, not plotted.

And one that is not a refusal but reads like one: if the permutation null says
`NOT INFORMATIVE`, the front is still drawn, with the diagnosis printed on it.
Hiding it would be worse.

---

## References

Verified against a primary or authoritative secondary source:

- **Bentley, J. L., Kung, H. T., Schkolnick, M., & Thompson, C. D.** *On the
  Average Number of Maxima in a Set of Vectors and Applications.* JACM
  25(4):536–543, Oct 1978. doi:10.1145/322092.322095.
- **Brockhoff, D., & Zitzler, E.** *Are All Objectives Necessary? On
  Dimensionality Reduction in Evolutionary Multiobjective Optimization.* PPSN
  IX, 2006; and *Dimensionality Reduction in Multiobjective Optimization: The
  Minimum Objective Subset Problem*, 2007. δ-MOSS and k-EMOSS, both NP-hard,
  exact and greedy algorithms published.
- **Laumanns, M., Thiele, L., Deb, K., & Zitzler, E.** *Combining Convergence
  and Diversity in Evolutionary Multiobjective Optimization.* *Evolutionary
  Computation* 10(3):263–282, 2002. Additive and multiplicative ε-dominance.
- **Chan, C.-Y., Jagadish, H. V., Tan, K.-L., Tung, A. K. H., & Zhang, Z.**
  *Finding k-Dominant Skylines in High Dimensional Space.* SIGMOD 2006,
  503–514. doi:10.1145/1142473.1142530.
- **Chan, C.-Y., Jagadish, H. V., Tan, K.-L., Tung, A. K. H., & Zhang, Z.**
  *On High Dimensional Skylines.* EDBT 2006, LNCS 3896. Skyline frequency over
  `2^d − 1` subspaces.
- **Branke, J., Deb, K., Dierolf, H., & Osswald, M.** *Finding Knees in
  Multi-objective Optimization.* PPSN VIII, 2004.
- **Blasco, X., Herrero, J. M., Sanchis, J., & Martínez, M.** *A new graphical
  visualization of n-dimensional Pareto front for decision-making in
  multiobjective optimization.* *Information Sciences* 178(20):3908–3924, 2008.
  Level diagrams.
- **Fonseca, C. M., Guerreiro, A. P., López-Ibáñez, M., & Paquete, L.** *On the
  Computation of the Empirical Attainment Function.* EMO 2011.
- **Efficient Computation of Probabilistic Dominance in Multi-objective
  Optimization.** *ACM TELO*, doi:10.1145/3469801.
- **Ma, Y., Zhang, Z., Cheng, R., Jin, Y., & Tan, K. C.** *ParetoLens: A Visual
  Analytics Framework for Exploring Solution Sets of Multi-objective
  Evolutionary Algorithms.* arXiv:2501.02857, 6 Jan 2025. The current
  interactive state of the art, and the reason this tool stays static and
  self-contained rather than competing with it.

Carried from the plan and **not re-verified**; check before any of these
reaches a slide:

- Ishibuchi et al., *Evol. Comput.* 26(3), 2018 (hypervolume reference point).
- Ishibuchi, Tsukamoto & Nojima, IEEE CEC 2008 (many-objective degeneracy).
- Fonseca, Paquete & López-Ibáñez, CEC 2006 (3D hypervolume dimension sweep).
- Jaimes & Coello, objective reduction by feature selection, 2008.
- Deb & Saxena, PCA-based objective reduction, 2006.
- Aitchison log-ratio programme; Egozcue et al., ILR, *Math. Geosciences*, 2003.
- Charles, Aparicio & Zhu, *EJOR* 279(3), 2019 (DEA unit-count rules of thumb).
