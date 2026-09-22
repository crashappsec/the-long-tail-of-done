# Pictures

`render.mjs` turns one `analysis.json` into one self-contained HTML file:
inline SVG, inline CSS, no fetches, no CDN, no dependencies. It computes
nothing. Every number it draws was decided by `surface.py` and every ordering
it uses was fixed by stage 2 of `front.md`. If a picture and the JSON disagree,
the JSON is right and the renderer has a bug.

---

## 1. Why the literal ask does not fit one frame

The request was: x = time, y = debt, z = cost, with overlapping radars. Count
the encodings — 3 spatial axes + 6 impact spokes + 4 question spokes + a
sequencing variable = **14 quantities in one static frame**. Two of the named
axes are already spatial, so the radar plane has nowhere to live, and calendar
time cannot be an axis while trajectories *through* cost/lead-time/debt are the
thing being drawn.

Nothing is dropped. **Two pictures share one normalisation and one axis order**,
each answering a different question, plus six 2D companions that do the
analysis.

Against 3D sits Munzner's *no unjustified 3D*: for abstract data, depth is
imprecise, occlusion hides data, perspective distorts comparison, and 2D
position beats every 3D channel under Stevens' power law. The justification
here is narrow, and it is stated on the page itself:

> A trajectory through a three-objective trade-off space is a shape, and its
> direction is the finding.

Anything that requires a value to be **read** gets a 2D companion in the same
file. That is the deal, and §4 is it being kept.

---

## 2. V1 — the trajectory cube

| Quantity | Channel |
|---|---|
| `cost` | x, minimised |
| `lead_time` | y, minimised |
| `debt` | z, minimised |
| group | polyline identity + hue |
| window | position along the polyline; fill on the latest, hollow and dimmed earlier |
| direction of travel | arrowhead on each segment |
| `P(on front)` | vertex halo radius |
| impact mix | billboarded radar glyph, first and latest window only |
| `lead_time_p90` | whisker along the lead-time axis, on the median's scale |

- **The good corner is the origin.** Said on the page, because a reader's first
  question about a 3D scatter is which way is better.
- **Each group is a polyline over its windows.** Evolution is the path, not an
  axis — which is exactly what makes the picture fit the data.
- **Pareto surface** as the axis-aligned staircase bounding the dominated
  region: the 3-objective attainment surface. Its facets fall out of the same
  dimension sweep used for 3D hypervolume (Fonseca, Paquete & López-Ibáñez,
  CEC 2006), so there is one implementation and not two.
- **Radar glyphs are billboarded** — always parallel to the picture plane, at
  constant size. A glyph that rotates with the scene stops being comparable to
  its neighbours, which is the only thing a glyph is for.
- **Depth honesty, non-negotiable.** Isometric projection (no perspective, so
  lengths stay comparable), drop-lines to the floor plane, a greyed
  floor-plane shadow of every trajectory, and a P90 whisker along the lead-time
  axis. That axis extends to include P90, transforming both endpoints with the
  same scale. No whisker is drawn when the objective is not median lead time
  or the descriptor is not P90 lead time.
- **Clutter cap: 6 groups × 4 windows.** Above it the tool refuses and names
  what to split out. Glyphs on the first and latest window only, unless
  `--glyphs all`.
- **Self-comparison mode** draws one polyline and the front is over its own
  vertices. Still legible, still useful, and it is the default case.

The isometric convention matches the original Long Tail of Done deck so a
lifted picture can sit next to its charts without being redrawn. Rendering
does not read or require the deck.

---

## 3. V2 — the Kiviat tube with rails

A stack of radar cross-sections along a time axis with the enclosing surface
drawn is a **Kiviat tube** — Hackstadt & Malony, *IEEE CG&A* 15(4), Jul 1995.
They hit occlusion and solved it with transparency plus a scrollable opaque 2D
slice; a static page has no such interaction, so the ring count is capped
instead.

Attribution, corrected: Pinzger, Gall, Fischer & Lanza, *Visualizing Multiple
Evolution Metrics*, SoftVis 2005, put multiple releases into a **2D** Kiviat.
The **3D** one is Kerren & Jusufi, *3D Kiviat Diagrams for the Interactive
Analysis of Software Metric Trends*, SOFTVIS 2010,
doi:10.1145/1879211.1879241 (DBLP `conf/softvis/KerrenJ10`).

- **One tube per group per composition, two tubes side by side** — never one
  tube carrying both mixes. A 6-spoke and a 4-spoke polygon in one polar plane
  have vertices that align on no axis, and readers will compare them anyway.
- **3–5 rings.** Front ring opaque with values printed, rear rings at falling
  opacity, painter's algorithm back to front, every ring labelled with its
  window.
- **Rings sit in planes perpendicular to the tube axis**, so under isometric
  projection every ring foreshortens *identically* and the silhouettes stay
  comparable. That property is the entire reason for isometric, and it dies
  under perspective.
- **Cost and debt get rails, not spokes** — two aligned strip charts beneath
  the tube on the same time axis. They are magnitudes; a spoke would both make
  them unreadable and mix an objective into a descriptor plot (`front.md`
  stage 0).
- **`unresolved` is a gap in the ring, not a spoke.** It is the part of the
  composition that is missing; a spoke lets it read as something the team
  produced.
- **Radius is linear in the share, values are printed, area comparison is
  never invited.** Polygon area under equal-angle spokes is
  `Σ rᵢ·rᵢ₊₁·sin θ`, so neither linear nor √ radius makes area track a
  composition. The choice between them was a false choice; the fix is to stop
  pretending area is a channel.
- Mixes are shares, already on a common 0–1 scale, so the usual
  arbitrary-per-axis-normalisation complaint does not apply. **Say so on the
  chart**, because readers will assume it does.
- Spokes are greyed where stage 0 found a structural overlap with an
  objective.

---

## 4. The 2D companions, which do the analysis

Same HTML file, below the hero pictures. Not optional, and **built first**.

1. **Pairwise fronts, 3 panels** — cost×time, cost×debt, time×debt, each with
   the 2D staircase. Dominance is readable here and nowhere in V1.
2. **Front-membership timeline** — one row per group, one column per window,
   shaded by non-domination *layer*, annotated with `P(on front)`. This is the
   evolution of the front, and it is the picture most teams will actually use.
3. **Composition over time, stacked areas** — one small multiple per group,
   impact mix plus `unresolved`. The tube's honest twin: same data, values
   readable, no occlusion. **If the two disagree, the stacked area wins.**
4. **Parallel coordinates** over all objectives, axis order fixed by stage 2.
   A radar *is* a PCP in polar coordinates and inherits the same axis-order
   dependence, so ordering once and reusing it keeps the two consistent.
5. **Level diagrams** (Blasco, Herrero, Sanchis & Martínez, *Inf. Sci.*
   178(20):3908–3924, 2008) — each objective against distance to the ideal
   point. Shows ranges, gaps and closeness to ideal, all of which a front
   alone hides.
6. **Threshold-sensitivity strip** — impact mix at `0.5u`, `u`, `2u` side by
   side (`../SCHEMA.md` §2). Small, and it pre-empts the first question anyone
   asks.

Plus a **diagnostics header**, which is not negotiable and sits above
everything: objectives and their formula versions, `u`, `L`, the window spec,
`n_builds` per cell, the gate-unavailable share, the permutation null verdict,
the structural overlap report, and every active refusal or caution.

### Static, not animated

Evidence-backed, not taste:

- Robertson, Fernandez, Fisher, Lee & Stasko, *IEEE TVCG* 14(6):1325–1332,
  2008 (VIS Test-of-Time 2018) — animation suits presentation, small multiples
  beat it for analysis.
- Albo, Lanir, Bak & Rafaeli, *Off the Radar*, *IEEE TVCG* 22(1):569–578, 2016
  — static time encoding beat dynamic encoding for radial composite indicators
  specifically, which is precisely the object in §3.

---

## 5. Rules the renderer follows

- **Axis order comes from `analysis.json`.** Never re-derived, never
  alphabetical, never the order the user typed.
- **Glyphs never carry a value alone.** Fuchs, Isenberg, Bezerianos & Keim, *A
  Systematic Review of Experimental Studies on Data Glyphs*, *IEEE TVCG*, Jul
  2017 — 64 user studies; star and radar glyphs are silhouette-comparison
  instruments, not value-reading instruments. Every glyph in this file has a
  printed twin.
- **Nulls are drawn as gaps, never as zeros.** A missing cost is a hole in the
  line, not a point at the origin. Same rule as the schema.
- **Cells below the `n_builds` floor are listed in the diagnostics header and
  absent from every plot.** Never plotted faintly — faint means "less", and
  the claim is "not enough evidence".
- **Every chart that uses a weighted metric prints the weights.**
- **No blank lines inside an SVG element** in any output destined for
  `slides.md`: a blank line ends a raw-HTML block in Markdown and silently
  drops the rest.
- Slide-bound output is 1280×720 and uses the deck's existing `--q-*` and
  impact colours and `.chart` classes.

---

## 6. Validation

- **Axis-swap test, in CI.** Swap two adjacent spokes, redraw, assert the
  emitted findings JSON is unchanged. If a conclusion moves, it was an
  axis-order artefact and not a finding. `tests/test_render.py` runs it over
  the committed fixture.
- **Null-versus-zero test.** A fixture cell with a null objective must render
  a gap and must not render a point at the origin.
- **Floor test.** A cell below `min_builds` must appear in the diagnostics
  header and in no `<polyline>`, `<circle>` or `<rect>` data mark.

---

## 7. References

- **Hackstadt, S. T., & Malony, A. D.** *Visualizing Parallel Programs and
  Performance.* *IEEE CG&A* 15(4):12–14, Jul 1995. Kiviat tube. Carried from
  the plan, not re-verified.
- **Kerren, A., & Jusufi, I.** *3D Kiviat Diagrams for the Interactive Analysis
  of Software Metric Trends.* SOFTVIS 2010. doi:10.1145/1879211.1879241.
- **Pinzger, M., Gall, H., Fischer, M., & Lanza, M.** *Visualizing Multiple
  Evolution Metrics.* SoftVis 2005. Multi-release Kiviat, in 2D.
- **Munzner, T.** *Visualization Analysis and Design*, 2014. "No unjustified
  3D".
- **Fuchs, J., Isenberg, P., Bezerianos, A., & Keim, D.** *A Systematic Review
  of Experimental Studies on Data Glyphs.* *IEEE TVCG*, Jul 2017.
  doi:10.1109/TVCG.2016.2549018.
- **Robertson, G., Fernandez, R., Fisher, D., Lee, B., & Stasko, J.**
  *Effectiveness of Animation in Trend Visualization.* *IEEE TVCG*
  14(6):1325–1332, 2008.
- **Albo, Y., Lanir, J., Bak, P., & Rafaeli, S.** *Off the Radar: Comparative
  Evaluation of Radial Visualization Solutions for Composite Indicators.*
  *IEEE TVCG* 22(1):569–578, 2016.
- **Blasco, X., Herrero, J. M., Sanchis, J., & Martínez, M.** *Information
  Sciences* 178(20):3908–3924, 2008. Level diagrams.
- **Fonseca, C. M., Paquete, L., & López-Ibáñez, M.** *An improved
  dimension-sweep algorithm for the hypervolume indicator.* CEC 2006. Carried
  from the plan, not re-verified.
