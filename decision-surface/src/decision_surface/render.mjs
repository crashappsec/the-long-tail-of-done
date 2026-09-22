#!/usr/bin/env node
// analysis.json -> one self-contained HTML file. No dependencies, no fetches,
// no CDN: inline SVG, inline CSS, and nothing that needs a network or a build
// step. A team must be able to open the output on a locked-down laptop and mail
// it to a sceptic.
//
// This file COMPUTES NOTHING. Every number it draws was decided by surface.py
// and every ordering it uses was fixed by stage 2 of references/front.md. If a
// picture and the JSON disagree, the JSON is right and this file has a bug.
//
// Order matters and is the order in references/pictures.md: the diagnostics
// header first, then the six 2D companions that do the analysis, and only then
// the two 3D heroes. The 2D pictures were built first for the same reason they
// are printed first -- they are the ones that can be read.
//
//     node render.mjs analysis.json out.html
//
// The output also carries a findings block, `<script id="findings">`, holding
// every conclusion the page states. tests/test_render.py swaps two adjacent
// axes, re-renders, and asserts that block is byte-identical: if a conclusion
// moves when the axis order does, it was an axis-order artefact and not a
// finding.

import { readFileSync, writeFileSync } from "node:fs";

// ---------------------------------------------------------------------------
// Palette. Tracks the deck's --q-* hues so a lifted picture matches the ones
// already there, but self-contained so this file needs no stylesheet.
// ---------------------------------------------------------------------------

const SERIES = ["#3b6ef5", "#e6399b", "#f5c542", "#2fd48f", "#9b6bf5", "#f57c42"];
const IMPACT_COLOURS = {
  impactful: "#2fd48f",
  low_impact: "#7fd8b8",
  liability: "#e6399b",
  carried: "#3b6ef5",
  sunk: "#8a8f98",
  unresolved: "url(#hatch)",
};
const QUESTION_COLOURS = {
  features: "#3b6ef5",
  correctness: "#e6399b",
  performance: "#f5c542",
  economics: "#2fd48f",
};
const IMPACT_ORDER = ["impactful", "low_impact", "liability", "carried", "sunk", "unresolved"];

// ---------------------------------------------------------------------------
// Tiny SVG helpers
// ---------------------------------------------------------------------------

const esc = (s) =>
  String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

const fmt = (v, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "\u2014"; // em dash: no value
  const n = Number(v);
  if (Math.abs(n) >= 1000) return n.toFixed(0);
  if (Math.abs(n) >= 10) return n.toFixed(1);
  return n.toFixed(digits);
};
const pct = (v) => (v === null || v === undefined ? "\u2014" : `${Math.round(v * 100)}%`);

function scale(domain, range) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}

function extent(values) {
  const present = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (!present.length) return [0, 1];
  let lo = Math.min(...present);
  let hi = Math.max(...present);
  if (lo === hi) {
    lo -= 0.5;
    hi += 0.5;
  }
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

const svg = (attrs, body) =>
  `<svg ${Object.entries(attrs).map(([k, v]) => `${k}="${esc(v)}"`).join(" ")}>${body}</svg>`;
const tag = (name, attrs = {}, body = "") => {
  const rendered = Object.entries(attrs)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}="${esc(v)}"`)
    .join(" ");
  return `<${name}${rendered ? " " + rendered : ""}>${body}</${name}>`;
};
// An SVG <title> describes its PARENT element, so a tooltip has to be a child
// of the mark it belongs to. Emitted as a sibling it becomes the title of the
// whole picture, and only the first one counts -- which is a silent way to
// ship a chart where nothing is inspectable.
const marked = (markup, tooltip) =>
  tag("g", {}, markup + tag("title", {}, esc(tooltip)));
function contiguous(rows, present) {
  const segments = [];
  let segment = [];
  for (const row of rows) {
    if (present(row)) segment.push(row);
    else if (segment.length) { segments.push(segment); segment = []; }
  }
  if (segment.length) segments.push(segment);
  return segments;
}
const text = (x, y, body, cls = "tick", extra = {}) =>
  tag("text", { x, y, class: cls, ...extra }, esc(body));

const DEFS = `<defs><pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="transparent"/><line x1="0" y1="0" x2="0" y2="6" stroke="currentColor" stroke-opacity="0.45" stroke-width="2"/></pattern><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/></marker></defs>`;

// ---------------------------------------------------------------------------
// Reading the analysis. Nothing derived, only selected.
// ---------------------------------------------------------------------------

function plottedCells(analysis) {
  // Cells below the build floor are listed in the header and appear in no plot.
  // Never drawn faintly: faint reads as "less", and the claim is "not enough
  // evidence".
  return analysis.cells.filter((c) => !c.below_floor);
}

function axisOrder(analysis) {
  const declared = analysis.stage2?.axis_order;
  const objectives = analysis.run.objectives;
  if (!declared) return objectives;
  // Never re-derived here, and never alphabetical: stage 2 fixed it once.
  const known = declared.filter((n) => objectives.includes(n));
  return [...known, ...objectives.filter((n) => !known.includes(n))];
}

function comparisonsOf(analysis, kind) {
  return (analysis.comparisons || []).filter((c) => c.kind === kind && !c.front_not_drawn);
}

function primaryComparisons(analysis) {
  const primary = comparisonsOf(analysis, analysis.primary_comparison);
  return primary.length ? primary : comparisonsOf(analysis, "within_group");
}

function seriesColour(analysis, group) {
  const index = analysis.groups.indexOf(group);
  return SERIES[(index < 0 ? 0 : index) % SERIES.length];
}

// ---------------------------------------------------------------------------
// Findings: every conclusion the page states, in a stable order, independent of
// axis order. The axis-swap test compares this and nothing else.
// ---------------------------------------------------------------------------

function findings(analysis) {
  const sortedObjectives = [...analysis.run.objectives].sort();
  return {
    tool: "decision-surface",
    run_objectives: sortedObjectives,
    mode: analysis.mode,
    gate: {
      u: analysis.gate?.u ?? null,
      L_days: analysis.gate?.L_days ?? null,
      u_source: analysis.gate?.u_source ?? null,
    },
    structural_overlaps: [...(analysis.stage0?.overlaps || [])].sort(),
    refusals: (analysis.diagnostics?.refusals || []).map((r) => r.code).sort(),
    cautions: (analysis.diagnostics?.cautions || []).map((c) => c.code).sort(),
    threshold_verdict: analysis.sensitivity?.verdict ?? null,
    comparisons: (analysis.comparisons || [])
      .map((c) => ({
        kind: c.kind,
        label: c.label,
        front: [...(c.front || [])].sort(),
        front_not_drawn: Boolean(c.front_not_drawn),
        null_model: c.null_model?.verdict ?? null,
        degeneracy: c.degeneracy_layer1_fraction ?? null,
        knee: c.relaxation_ladder?.knees?.knee ?? null,
        skyline_frequency: c.relaxation_ladder?.skyline_frequency?.scores ?? null,
        p_on_front: c.p_on_front ?? null,
      }))
      .sort((a, b) => (a.kind + a.label).localeCompare(b.kind + b.label)),
    travel: (analysis.stage7?.direction_of_travel || [])
      .map((t) => ({
        group: t.group,
        from: t.from,
        to: t.to,
        magnitude: t.magnitude ?? null,
        angle: t.angle_to_ideal_deg ?? null,
        reading: t.reading ?? null,
      }))
      .sort((a, b) => (a.group + a.from).localeCompare(b.group + b.from)),
    mix_movement: (analysis.stage7?.mix_movement || [])
      .map((m) => ({
        group: m.group,
        from: m.from,
        to: m.to,
        aitchison: m.aitchison_distance ?? null,
        top_log_ratio: m.top_log_ratio?.ratio ?? null,
      }))
      .sort((a, b) => (a.group + a.from).localeCompare(b.group + b.from)),
    persistence: analysis.stage7?.persistence ?? null,
  };
}

// ---------------------------------------------------------------------------
// 0. Diagnostics header. Not negotiable, and above everything.
// ---------------------------------------------------------------------------

function diagnostics(analysis) {
  const run = analysis.run;
  const gate = analysis.gate || {};
  const versions = Object.entries(run.metric_versions || {})
    .filter(([name]) => run.objectives.includes(name))
    .map(([name, version]) => `${name} v${version}`)
    .join(", ");

  const rows = [
    ["objectives", `${run.objectives.join(", ")} \u2014 ${versions}`],
    ["mode", `${analysis.mode} \u00b7 primary comparison: ${analysis.primary_comparison}`],
    ["usage threshold u", `${fmt(gate.u)} \u2014 ${gate.u_source}`],
    ["resolution lag L", `${gate.L_days} days \u00b7 observation edge ${String(gate.as_of || "").slice(0, 10)}`],
    ["windows", `${(analysis.windows || []).join(", ")} \u00b7 ${run.params?.window_spec || "month"}`],
    ["normalisation", run.params?.normalisation || ""],
    ["uncertainty", `${analysis.stage6?.mode} \u00b7 ${analysis.stage6?.replicates} replicates \u00b7 seed ${run.params?.seed}`],
    ["hypervolume reference", `${run.params?.hv_reference_point} per axis, in min-max normalised minimise-space`],
  ];

  const overlaps = analysis.stage0?.overlaps || [];
  const refusals = analysis.diagnostics?.refusals || [];
  const cautions = analysis.diagnostics?.cautions || [];
  const warnings = analysis.diagnostics?.warnings || [];
  const belowFloor = analysis.cells.filter((c) => c.below_floor);

  const gateShare = analysis.cells
    .filter((c) => (c.gate_unavailable_share || 0) > 0)
    .map((c) => `${c.label} ${pct(c.gate_unavailable_share)}`);
  const unclassified = analysis.cells
    .filter((c) => (c.question_unclassified_share || 0) > 0)
    .map((c) => `${c.label} ${pct(c.question_unclassified_share)}`);

  return `<section class="panel">
<h2>What this run is</h2>
<table class="kv">${rows
    .map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`)
    .join("")}</table>
${
  overlaps.length
    ? `<div class="note note--overlap"><strong>Structural overlap, computed from the declared formulas before any data was read.</strong><ul>${overlaps
        .map((o) => `<li><code>${esc(o)}</code></li>`)
        .join("")}</ul>An objective and a glyph arm here are the same measurement, so those spokes are greyed in every radar on this page and the reader can see which arms are not independent evidence. Stage 2 measures how large the overlap actually is; both checks are on this page.</div>`
    : `<div class="note">No structural overlap: no objective consumes a part of a mix it is drawn beside.</div>`
}
${refusals
    .map(
      (r) =>
        `<div class="note note--refuse"><strong>REFUSED \u00b7 ${esc(r.code)}</strong><br/>${esc(r.message)}</div>`
    )
    .join("")}
${cautions
    .map(
      (c) =>
        `<div class="note note--caution"><strong>CAUTION \u00b7 ${esc(c.code)}</strong><br/>${esc(c.message)}</div>`
    )
    .join("")}
${
  belowFloor.length
    ? `<div class="note note--caution"><strong>Below the build floor of ${esc(
        analysis.run.params?.min_builds
      )}, reported and not plotted:</strong> ${belowFloor
        .map((c) => `${esc(c.label)} (n=${c.n_builds})`)
        .join(", ")}</div>`
    : ""
}
${
  gateShare.length
    ? `<div class="note"><strong>Usage gate unavailable</strong> for some builds: ${esc(
        gateShare.join(", ")
      )}. Those builds stayed <em>impactful</em> with the gate flagged \u2014 "no usage signal" and "no usage" are different claims, and demoting an unmeasured build to liability manufactures waste.</div>`
    : ""
}
${
  unclassified.length
    ? `<div class="note note--caution"><strong>Question outside the enum</strong> for some builds: ${esc(
        unclassified.join(", ")
      )}. Those cells' question mixes sum to less than 1 by exactly that share and are <em>not</em> renormalised to close the gap \u2014 a mix that quietly adds up to 1 after dropping a part is the thing this tool refuses to do.</div>`
    : ""
}
${
  warnings.length
    ? `<details class="note"><summary>${warnings.length} input warning(s)</summary><ul>${warnings
        .map((w) => `<li><code>${esc(w.code)}</code> ${esc(w.message)}</li>`)
        .join("")}</ul></details>`
    : ""
}
<table class="cells"><thead><tr><th>cell</th><th>n</th>${analysis.run.objectives
    .map((n) => `<th>${esc(n)}</th>`)
    .join("")}<th>layer</th><th>P(front)</th><th>lead time source</th></tr></thead><tbody>${analysis.cells
    .map(
      (c) =>
        `<tr class="${c.below_floor ? "floored" : ""}"><td>${esc(c.label)}</td><td>${c.n_builds}</td>${analysis.run.objectives
          .map((n) => `<td>${fmt(c.objectives[n])}</td>`)
          .join("")}<td>${c.layer ?? "\u2014"}</td><td>${
          c.p_on_front === null || c.p_on_front === undefined ? "\u2014" : pct(c.p_on_front)
        }</td><td>${esc(
          (c.lead_time_sources || []).join("+") +
            (c.lead_time_fallback_share ? ` (${pct(c.lead_time_fallback_share)} to deploy)` : "")
        )}</td></tr>`
    )
    .join("")}</tbody></table>
<p class="foot">A dash is a value that was not observed, never a zero. <code>P(front)</code> is a dash where the cell could not be resampled at all.</p>
</section>`;
}

// ---------------------------------------------------------------------------
// 1. Pairwise fronts, with the 2D staircase and the attainment bands.
//    Dominance is readable here and nowhere in the cube.
// ---------------------------------------------------------------------------

function staircase(points, sx, sy, W, H, opts = {}) {
  // The 2D attainment surface of a minimising front: step right along x, down
  // to the next y. Drawn to the panel edge so the dominated region is visibly
  // a region and not a line.
  const front = points
    .filter((p) => p.x !== null && p.y !== null && p.x !== undefined && p.y !== undefined)
    .sort((a, b) => a.x - b.x);
  if (!front.length) return "";
  const kept = [];
  let best = Infinity;
  for (const p of front) {
    if (p.y < best) {
      kept.push(p);
      best = p.y;
    }
  }
  let d = `M ${sx(kept[0].x)} ${H}`;
  d += ` L ${sx(kept[0].x)} ${sy(kept[0].y)}`;
  for (let i = 1; i < kept.length; i += 1) {
    d += ` L ${sx(kept[i].x)} ${sy(kept[i - 1].y)}`;
    d += ` L ${sx(kept[i].x)} ${sy(kept[i].y)}`;
  }
  d += ` L ${W} ${sy(kept[kept.length - 1].y)}`;
  return tag("path", {
    d,
    class: opts.class || "stair",
    opacity: opts.opacity === undefined ? null : opts.opacity,
  });
}

// Drawn in min-max normalised space, on purpose. The bootstrap's attainment
// bands are produced per replicate, each replicate normalised by its own
// spread, so there is no exact way back to dollars -- and a band drawn against
// one scale while the markers use another is a picture of two different things.
// Normalised space puts them in one coordinate system; the raw range is printed
// on each axis, every marker carries its raw values in a tooltip, and the
// header table lists all of them.
function pairPanel(analysis, comparison, xName, yName, cells, history) {
  const W = 340;
  const H = 268;
  const pad = { l: 46, r: 14, t: 18, b: 56 };
  const rows = cells.filter((c) => comparison.cells.includes(c.label));
  const bands = comparison.attainment?.bands?.[`${xName}|${yName}`] || [];
  const domainX = extent([0, 1, ...bands.map((b) => b.x)]);
  const domainY = extent([0, 1, ...bands.flatMap((b) => [b.p10, b.p50, b.p90])]);
  const sx = scale(domainX, [pad.l, W - pad.r]);
  const sy = scale(domainY, [H - pad.b, pad.t]);
  const at = (cell, name) => cell.objectives_norm?.[name];

  let body = DEFS;
  body += [0, 0.25, 0.5, 0.75, 1]
    .map((f) => tag("line", { x1: pad.l, y1: sy(f), x2: W - pad.r, y2: sy(f), class: "grid" }))
    .join("");

  // Stage 7.2, the front over time: one staircase per window, oldest palest.
  // The attainment-surface idea applied to windows instead of algorithm runs.
  history.forEach((past, i) => {
    const pastPoints = (past.front || [])
      .map((label) => cells.find((c) => c.label === label))
      .filter(Boolean)
      .map((cell) => ({ x: at(cell, xName), y: at(cell, yName) }))
      .filter((p) => p.x !== null && p.x !== undefined && p.y !== null && p.y !== undefined);
    if (pastPoints.length < 1) return;
    const isCurrent = i === history.length - 1;
    body += staircase(pastPoints, sx, sy, W - pad.r, H - pad.b, {
      class: isCurrent ? "stair" : "stair stair--past",
      opacity: isCurrent ? 1 : 0.2 + (0.5 * i) / Math.max(1, history.length - 1),
    });
  });

  if (bands.length > 1) {
    for (const segment of contiguous(bands, (b) => b.p90 != null && b.p10 != null)) {
      const upper = segment.map((b) => `${sx(b.x)},${sy(b.p90)}`).join(" ");
      const lower = [...segment].reverse().map((b) => `${sx(b.x)},${sy(b.p10)}`).join(" ");
      body += tag("polygon", { points: `${upper} ${lower}`, class: "band" });
    }
    for (const segment of contiguous(bands, (b) => b.p50 != null)) {
      body += tag("polyline", {
        points: segment.map((b) => `${sx(b.x)},${sy(b.p50)}`).join(" "),
        class: "band-median",
      });
    }
  }

  body += tag("line", { x1: pad.l, y1: H - pad.b, x2: W - pad.r, y2: H - pad.b, class: "axis" });
  body += tag("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: H - pad.b, class: "axis" });

  const front = comparison.front || [];
  for (const cell of rows) {
    const x = at(cell, xName);
    const y = at(cell, yName);
    // A null is a gap, never a point at the origin.
    if (x === null || y === null || x === undefined || y === undefined) continue;
    const onFront = front.includes(cell.label);
    const colour = seriesColour(analysis, cell.group);
    const probability = comparison.p_on_front?.[cell.label];
    if (probability !== null && probability !== undefined) {
      body += tag("circle", {
        cx: sx(x),
        cy: sy(y),
        r: 4 + 7 * probability,
        fill: "none",
        stroke: colour,
        "stroke-opacity": 0.3,
      });
    }
    const mark = tag("circle", {
      cx: sx(x),
      cy: sy(y),
      r: 5,
      fill: onFront ? colour : "var(--bg)",
      stroke: colour,
      "stroke-width": onFront ? 1 : 1.6,
      "stroke-opacity": onFront ? 1 : 0.55,
    });
    body += marked(
      mark,
      `${cell.label}${onFront ? " (front)" : ""}  ${xName} ${fmt(
        cell.objectives[xName]
      )}  ${yName} ${fmt(cell.objectives[yName])}  P(front) ${
        probability === null || probability === undefined ? "\u2014" : pct(probability)
      }`
    );
  }

  const range = (name) => {
    const norm = analysis.stage1?.normalisation?.[name] || {};
    return `${fmt(norm.lo)} \u2013 ${fmt(norm.hi)}`;
  };
  body += text(pad.l, H - 30, `${xName} \u2192 worse`, "tick");
  body += text(pad.l, H - 16, range(xName), "micro");
  body += text(6, pad.t - 6, `${yName} \u2191 worse \u00b7 ${range(yName)}`, "micro");
  body += text(W - pad.r, pad.t + 4, "ideal \u2199", "micro", { "text-anchor": "end" });

  return `<figure>${svg(
    {
      viewBox: `0 0 ${W} ${H}`,
      class: "chart",
      role: "img",
      "aria-label": `${xName} against ${yName} in normalised units, with the two-dimensional Pareto staircase and one paler staircase per earlier window.`,
    },
    body
  )}</figure>`;
}

function pairwiseFronts(analysis) {
  const order = axisOrder(analysis);
  const cells = plottedCells(analysis);
  const comparisons = primaryComparisons(analysis);
  if (!comparisons.length) {
    return `<section class="panel"><h2>Pairwise fronts</h2><div class="note note--refuse">No comparison produced a front, so there is nothing to project. See the refusals above.</div></section>`;
  }
  const pairs = [];
  for (let i = 0; i < order.length; i += 1) {
    for (let j = i + 1; j < order.length; j += 1) pairs.push([order[i], order[j]]);
  }

  // Across groups the comparisons are one per window, so the latest window
  // carries the markers and the earlier windows are overlaid as paler
  // staircases -- three panels instead of three per window, and the movement of
  // the surface becomes visible rather than something to flip between.
  const isAcross = comparisons[0].kind === "across_groups";
  const blocks = isAcross
    ? (() => {
        const current = comparisons[comparisons.length - 1];
        return `<div class="pairs-row"><h3>${esc(current.label)}, with every earlier window's front behind it</h3>
<p class="q">${esc(current.question)}</p>
<div class="pairs">${pairs
          .map(([x, y]) => pairPanel(analysis, current, x, y, cells, comparisons))
          .join("")}</div></div>`;
      })()
    : comparisons
        .map(
          (comparison) => `<div class="pairs-row"><h3>${esc(comparison.label)}</h3>
<p class="q">${esc(comparison.question)}</p>
<div class="pairs">${pairs
            .map(([x, y]) => pairPanel(analysis, comparison, x, y, cells, [comparison]))
            .join("")}</div></div>`
        )
        .join("");

  return `<section class="panel"><h2>Pairwise fronts</h2>
<p class="lede">Every pair of objectives, with the 2D staircase bounding the dominated region \u2014 dominance is readable here and nowhere in the cube. Filled markers are on the front; hollow markers are beaten on both axes at once. The halo is <code>P(on front)</code> over the bootstrap and the pale band is the 10\u201390% attainment band, which is the honest version of "where the front is". Paler staircases behind are earlier windows, oldest palest: that is the front moving.</p>
${blocks}
<p class="foot">Drawn in min-max normalised units so the markers and the bootstrap band share one coordinate system; each axis prints its raw range and every marker carries its raw values, which are also all in the table above. Axis order is fixed by the conflict screen (stage 2) and reused by every picture on this page, including the radars \u2014 a radar is a parallel-coordinates plot in polar coordinates and inherits the same axis-order dependence, so ordering once is what keeps the two consistent.</p>
</section>`;
}

// ---------------------------------------------------------------------------
// 2. Front-membership timeline. The picture most teams will actually use.
// ---------------------------------------------------------------------------

function membershipTimeline(analysis) {
  const groups = analysis.groups;
  const windows = analysis.windows;
  const cellW = 118;
  const cellH = 46;
  const padL = 132;
  const padT = 44;
  const W = padL + windows.length * cellW + 62;  // room for the persistence column
  const H = padT + groups.length * cellH + 54;

  const byLabel = new Map(analysis.cells.map((c) => [c.label, c]));
  const layerFill = (layer) => {
    if (layer === null || layer === undefined) return "var(--none)";
    const shades = ["#2fd48f", "#7fd8b8", "#b9e3d2", "#d7e2dd", "#e6e8ea"];
    return shades[Math.min(layer - 1, shades.length - 1)];
  };

  let body = DEFS;
  body += windows
    .map((w, i) => text(padL + i * cellW + cellW / 2, padT - 14, w, "tick", { "text-anchor": "middle" }))
    .join("");

  groups.forEach((group, gi) => {
    const y = padT + gi * cellH;
    body += text(padL - 12, y + cellH / 2 + 4, group, "tick", { "text-anchor": "end" });
    body += tag("rect", {
      x: padL - 118,
      y: y + cellH / 2 - 5,
      width: 8,
      height: 10,
      fill: seriesColour(analysis, group),
    });
    windows.forEach((window, wi) => {
      const label = `${group}/${window}`;
      const cell = byLabel.get(label);
      const x = padL + wi * cellW;
      if (!cell) {
        body += tag("rect", { x, y, width: cellW - 6, height: cellH - 8, class: "missing" });
        body += text(x + (cellW - 6) / 2, y + cellH / 2 + 4, "no builds", "micro", {
          "text-anchor": "middle",
        });
        return;
      }
      if (cell.below_floor) {
        body += tag("rect", { x, y, width: cellW - 6, height: cellH - 8, class: "missing" });
        body += text(x + (cellW - 6) / 2, y + cellH / 2 + 4, `n=${cell.n_builds} < floor`, "micro", {
          "text-anchor": "middle",
        });
        return;
      }
      const probability =
        cell.p_on_front === null || cell.p_on_front === undefined ? "\u2014" : pct(cell.p_on_front);
      body += marked(
        tag("rect", {
          x,
          y,
          width: cellW - 6,
          height: cellH - 8,
          fill: layerFill(cell.layer),
          stroke: cell.layer === 1 ? seriesColour(analysis, group) : "var(--border)",
          "stroke-width": cell.layer === 1 ? 2 : 1,
          rx: 2,
        }) +
          text(x + 8, y + 19, `layer ${cell.layer ?? "\u2014"}`, "micro dark") +
          text(x + 8, y + 33, `P ${probability}`, "micro dark"),
        `${label}  n=${cell.n_builds}  layer ${cell.layer ?? "none"}  P(front) ${probability}`
      );
    });
  });

  const persistence = analysis.stage7?.persistence || {};
  body += text(padL - 12, H - 26, "persistence", "tick", { "text-anchor": "end" });
  if (persistence.note) {
    body += text(padL, H - 26, persistence.note, "micro");
  } else {
    groups.forEach((group, gi) => {
      const entry = persistence[group];
      if (!entry) return;
      body += text(
        padL - 12,
        padT + gi * cellH + cellH - 2,
        "",
        "micro"
      );
      body += text(
        padL + windows.length * cellW + 8,
        padT + gi * cellH + cellH / 2 + 4,
        `${entry.windows_on_front}/${entry.windows_considered}`,
        "micro"
      );
    });
    body += text(padL, H - 26, "right-hand column: windows on the front out of windows considered", "micro");
  }

  return `<section class="panel"><h2>Front membership over time</h2>
<p class="lede">One row per group, one column per window, shaded by non-domination <em>layer</em> rather than by a flag: when layer 1 swells, the layers still separate. Darkest is layer 1. <code>P</code> is the bootstrap probability that the cell is on the front at all.</p>
${svg({ viewBox: `0 0 ${W} ${H}`, class: "chart wide", role: "img", "aria-label": "A grid of groups by windows, shaded by non-domination layer." }, body)}
</section>`;
}

// ---------------------------------------------------------------------------
// 3. Composition over time, stacked areas. The tube's honest twin.
// ---------------------------------------------------------------------------

function compositionSmallMultiple(analysis, group, mixKey, classes, colours) {
  const windows = analysis.windows;
  const W = 300;
  const H = 170;
  const pad = { l: 34, r: 10, t: 16, b: 30 };
  const cells = windows.map((w) =>
    plottedCells(analysis).find((c) => c.group === group && c.window === w && classes.every((k) => c[mixKey]?.[k] != null))
  );
  const sx = scale([0, Math.max(1, windows.length - 1)], [pad.l, W - pad.r]);
  const sy = scale([0, 1], [H - pad.b, pad.t]);

  let body = DEFS;
  body += [0, 0.25, 0.5, 0.75, 1]
    .map((f) => tag("line", { x1: pad.l, y1: sy(f), x2: W - pad.r, y2: sy(f), class: "grid" }))
    .join("");

  // Stacked from the bottom in a fixed class order, so the bands are comparable
  // between small multiples.
  const cumulative = windows.map(() => 0);
  for (const klass of classes) {
    const upper = [];
    const lower = [];
    windows.forEach((_, i) => {
      const cell = cells[i];
      if (!cell) return;
      const value = cell[mixKey]?.[klass] ?? 0;
      lower.push([sx(i), sy(cumulative[i])]);
      cumulative[i] += value;
      upper.push([sx(i), sy(cumulative[i])]);
    });
    if (!upper.length) continue;
    const points = [...upper, ...lower.reverse()].map(([x, y]) => `${x},${y}`).join(" ");
    body += tag("polygon", {
      points,
      fill: colours[klass],
      "fill-opacity": klass === "unresolved" ? 1 : 0.85,
      stroke: "var(--bg)",
      "stroke-width": 0.5,
    });
  }

  windows.forEach((w, i) => {
    if (i % 2 === 0 || windows.length <= 4) {
      body += text(sx(i), H - 12, w.slice(2), "micro", { "text-anchor": "middle" });
    }
    const cell = cells[i];
    if (!cell) {
      body += tag("line", { x1: sx(i), y1: pad.t, x2: sx(i), y2: H - pad.b, class: "gapline" });
    }
  });
  body += text(6, sy(1) + 4, "100%", "micro");
  body += text(6, sy(0) + 4, "0", "micro");
  body += text(pad.l, pad.t - 4, group, "tick");

  return `<figure class="multiple">${svg(
    { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${group}: ${mixKey} over time as a stacked area.` },
    body
  )}</figure>`;
}

function compositions(analysis) {
  const greyed = analysis.stage0?.greyed_spokes || {};
  const legend = (classes, colours) =>
    `<div class="legend">${classes
      .map(
        (k) =>
          `<span class="key"><i style="background:${
            k === "unresolved" ? "repeating-linear-gradient(45deg,var(--fg) 0 2px,transparent 2px 5px)" : colours[k]
          }"></i>${esc(k)}${greyed[k] ? " <em>(overlaps an objective)</em>" : ""}</span>`
      )
      .join("")}</div>`;

  return `<section class="panel"><h2>Composition over time</h2>
<p class="lede">The impact mix and the question mix, per group, as stacked areas. Same data as the Kiviat tube below, with the values readable and nothing occluded. <strong>If the two disagree, this one wins.</strong> <code>unresolved</code> is hatched because it is the part of the composition that is missing rather than something the team produced \u2014 a build younger than <code>L</code> at the observation edge has not been classified yet.</p>
${legend(IMPACT_ORDER, IMPACT_COLOURS)}
<div class="multiples">${analysis.groups
    .map((g) => compositionSmallMultiple(analysis, g, "impact_mix", IMPACT_ORDER, IMPACT_COLOURS))
    .join("")}</div>
${legend(Object.keys(QUESTION_COLOURS), QUESTION_COLOURS)}
<div class="multiples">${analysis.groups
    .map((g) =>
      compositionSmallMultiple(
        analysis,
        g,
        "question_mix",
        Object.keys(QUESTION_COLOURS),
        QUESTION_COLOURS
      )
    )
    .join("")}</div>
<p class="foot">A vertical rule marks a window with no plottable cell. The mix is never renormalised to close a gap: the parts sum to 1 with <code>unresolved</code> included, which is what makes "we shipped no waste" distinguishable from "we have not looked yet".</p>
${mixMovementTable(analysis)}
</section>`;
}

function mixMovementTable(analysis) {
  const rows = (analysis.stage7?.mix_movement || []).filter((m) => m.available);
  if (!rows.length) return "";
  return `<table class="kv wide"><thead><tr><th>group</th><th>from</th><th>to</th><th>Aitchison distance</th><th>largest moving log-ratio</th></tr></thead><tbody>${rows
    .map(
      (m) =>
        `<tr><td>${esc(m.group)}</td><td>${esc(m.from)}</td><td>${esc(m.to)}</td><td>${fmt(
          m.aitchison_distance
        )}</td><td><code>${esc(m.top_log_ratio?.ratio || "\u2014")}</code> ${
          m.top_log_ratio ? (m.top_log_ratio.direction > 0 ? "\u2193" : "\u2191") : ""
        }</td></tr>`
    )
    .join("")}</tbody></table>
<p class="foot">One honest scalar for "the mix moved". A mix is a composition, so "liability rose 4 points" also means three other things moved and does not say which; the Aitchison distance is immune to that sum constraint and the log-ratio names what actually moved.</p>`;
}

// ---------------------------------------------------------------------------
// 4. Parallel coordinates, in the axis order stage 2 fixed.
// ---------------------------------------------------------------------------

function parallelCoordinates(analysis) {
  const order = axisOrder(analysis);
  const cells = plottedCells(analysis);
  const W = 760;
  const H = 300;
  const pad = { l: 70, r: 90, t: 30, b: 54 };
  const ax = scale([0, Math.max(1, order.length - 1)], [pad.l, W - pad.r]);
  const ay = scale([0, 1], [H - pad.b, pad.t]);

  let body = DEFS;
  order.forEach((name, i) => {
    body += tag("line", { x1: ax(i), y1: pad.t, x2: ax(i), y2: H - pad.b, class: "axis" });
    body += text(ax(i), H - pad.b + 18, name, "tick", { "text-anchor": "middle" });
    const norm = analysis.stage1?.normalisation?.[name] || {};
    body += text(ax(i), H - pad.b + 32, `${fmt(norm.lo)} \u2013 ${fmt(norm.hi)}`, "micro", {
      "text-anchor": "middle",
    });
  });
  body += text(pad.l - 12, pad.t + 4, "worse", "micro", { "text-anchor": "end" });
  body += text(pad.l - 12, H - pad.b, "better", "micro", { "text-anchor": "end" });

  for (const cell of cells) {
    const values = order.map((n) => cell.objectives_norm?.[n]);
    // A null breaks the line rather than dropping it to the floor.
    const segments = [];
    let current = [];
    values.forEach((v, i) => {
      if (v === null || v === undefined) {
        if (current.length > 1) segments.push(current);
        current = [];
      } else {
        current.push(`${ax(i)},${ay(1 - v)}`);
      }
    });
    if (current.length > 1) segments.push(current);
    const colour = seriesColour(analysis, cell.group);
    const onFront = cell.layer === 1;
    if (!segments.length) continue;
    body += marked(
      segments
        .map((segment) =>
          tag("polyline", {
            points: segment.join(" "),
            fill: "none",
            stroke: colour,
            "stroke-width": onFront ? 2.4 : 1,
            "stroke-opacity": onFront ? 0.95 : 0.3,
          })
        )
        .join(""),
      `${cell.label}${onFront ? " (front)" : ""}  ` +
        order
          .map((n) => `${n} ${fmt(cell.objectives[n])}`)
          .join("  ")
    );
  }

  analysis.groups.forEach((group, i) => {
    body += tag("rect", {
      x: W - pad.r + 16,
      y: pad.t + i * 18,
      width: 10,
      height: 10,
      fill: seriesColour(analysis, group),
    });
    body += text(W - pad.r + 32, pad.t + i * 18 + 9, group, "micro");
  });

  return `<section class="panel"><h2>Parallel coordinates</h2>
<p class="lede">Every cell across every objective, min-max normalised, better at the bottom. Bold lines are on the front of the primary comparison. Axis order is the conflict screen's, most-conflicting pairs adjacent \u2014 the same order the radars use.</p>
${svg({ viewBox: `0 0 ${W} ${H}`, class: "chart wide", role: "img", "aria-label": "Parallel coordinates over the objectives." }, body)}
${conflictTable(analysis)}
</section>`;
}

function conflictTable(analysis) {
  const verdicts = analysis.stage2?.verdicts || [];
  if (!verdicts.length) return "";
  const objectiveVsMix = analysis.stage2?.objective_vs_descriptor || {};
  const mixParts = IMPACT_ORDER.map((k) => `share(${k})`);
  return `<details class="note"><summary>Conflict screen \u00b7 Kendall \u03c4-b over pooled cells</summary>
<table class="kv wide"><thead><tr><th>pair</th><th>\u03c4</th><th>verdict</th></tr></thead><tbody>${verdicts
    .map(
      (v) =>
        `<tr><td>${esc(v.a)} \u00b7 ${esc(v.b)}</td><td>${fmt(v.tau)}</td><td class="v-${esc(
          v.verdict
        )}">${esc(v.verdict)}</td></tr>`
    )
    .join("")}</tbody></table>
<p class="foot">|\u03c4| \u2265 0.8 is redundant, \u03c4 \u2264 \u22120.4 is conflicting and is where the trade-off lives, and everything else is independent. \u03c4-b rather than \u03c4-a, because cells tie on cost and lead time constantly and \u03c4-a would read a tie as disagreement.</p>
<table class="kv wide"><thead><tr><th>objective vs mix part</th>${mixParts
    .map((p) => `<th>${esc(p)}</th>`)
    .join("")}</tr></thead><tbody>${Object.entries(objectiveVsMix)
    .map(
      ([objective, row]) =>
        `<tr><td>${esc(objective)}</td>${mixParts
          .map((p) => `<td>${fmt(row[p])}</td>`)
          .join("")}</tr>`
    )
    .join("")}</tbody></table>
<p class="foot">This is the <em>measured</em> half of the overlap check. The declared half is in the header, computed from the formulas before any data was read. A run reports both, and a declared overlap that does not show up here means one of the two metrics is not measuring what its name says.</p></details>`;
}

// ---------------------------------------------------------------------------
// 5. Level diagrams: each objective against distance to the ideal point.
// ---------------------------------------------------------------------------

function levelDiagrams(analysis) {
  const comparisons = primaryComparisons(analysis);
  if (!comparisons.length) return "";
  const order = axisOrder(analysis);
  const byLabel = new Map(analysis.cells.map((c) => [c.label, c]));

  const panels = comparisons
    .map((comparison) => {
      const levels = comparison.level_diagram || {};
      const entries = Object.entries(levels);
      if (!entries.length) return "";
      const distances = entries.map(([, v]) => v.l2);
      const charts = order
        .map((name) => {
          const W = 240;
          const H = 190;
          const pad = { l: 46, r: 12, t: 14, b: 34 };
          const values = entries.map(([label]) => byLabel.get(label)?.objectives_norm?.[name]);
          const sx = scale(extent(values), [pad.l, W - pad.r]);
          const sy = scale([0, Math.max(...distances) * 1.05 || 1], [H - pad.b, pad.t]);
          let body = DEFS;
          body += tag("line", { x1: pad.l, y1: H - pad.b, x2: W - pad.r, y2: H - pad.b, class: "axis" });
          body += tag("line", { x1: pad.l, y1: pad.t, x2: pad.l, y2: H - pad.b, class: "axis" });
          entries.forEach(([label, level]) => {
            const cell = byLabel.get(label);
            const v = cell?.objectives_norm?.[name];
            if (v === null || v === undefined) return;
            body += marked(
              tag("circle", {
                cx: sx(v),
                cy: sy(level.l2),
                r: 4.5,
                fill: comparison.front?.includes(label)
                  ? seriesColour(analysis, cell.group)
                  : "var(--dim)",
              }),
              `${label}  ${name} ${fmt(v)}  distance to ideal ${fmt(level.l2)}`
            );
          });
          body += text(pad.l, H - 8, name, "micro");
          body += text(8, pad.t + 4, "\u2016ideal\u2016", "micro");
          return `<figure class="multiple">${svg(
            { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `Level diagram for ${name}.` },
            body
          )}</figure>`;
        })
        .join("");
      return `<div class="pairs-row"><h3>${esc(comparison.label)}</h3><div class="multiples">${charts}</div></div>`;
    })
    .join("");

  return `<section class="panel"><h2>Level diagrams</h2>
<p class="lede">Each objective against Euclidean distance to the ideal point (Blasco, Herrero, Sanchis &amp; Mart\u00ednez, <em>Inf. Sci.</em> 178(20), 2008). Low on the vertical axis is close to ideal. This is where ranges, gaps and clustering show up \u2014 all of which a front alone hides.</p>
${panels}
</section>`;
}

// ---------------------------------------------------------------------------
// 6. Threshold sensitivity strip.
// ---------------------------------------------------------------------------

function sensitivityStrip(analysis) {
  const sensitivity = analysis.sensitivity;
  if (!sensitivity?.available) {
    return `<section class="panel"><h2>Threshold sensitivity</h2><div class="note">${esc(
      sensitivity?.reason || "not computed"
    )}</div></section>`;
  }
  const levels = ["0.5u", "u", "2u"];
  const W = 620;
  const H = 150;
  const barW = 150;
  const pad = { l: 40, t: 34 };
  let body = DEFS;
  levels.forEach((level, i) => {
    const mix = sensitivity.levels[level].overall;
    const x = pad.l + i * (barW + 46);
    let y = pad.t;
    body += text(x + barW / 2, pad.t - 14, `${level} = ${fmt(sensitivity.levels[level].u)}`, "tick", {
      "text-anchor": "middle",
    });
    for (const klass of IMPACT_ORDER) {
      const share = mix[klass] || 0;
      const h = share * 90;
      if (h > 0) {
        body += tag("rect", {
          x,
          y,
          width: barW,
          height: h,
          fill: IMPACT_COLOURS[klass],
          "fill-opacity": klass === "unresolved" ? 1 : 0.85,
        });
        if (h > 12) {
          body += text(x + 6, y + h / 2 + 4, `${klass} ${pct(share)}`, "micro dark");
        }
      }
      y += h;
    }
  });

  return `<section class="panel"><h2>Threshold sensitivity</h2>
<p class="lede">The impact mix at half, one and twice the usage threshold. <code>u</code> defaults to the median usage across served builds \u2014 relative and self-calibrating, because an absolute default is a fiction across teams.</p>
${svg({ viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "The impact mix at three usage thresholds." }, body)}
<div class="note ${sensitivity.verdict.startsWith("PROVISIONAL") ? "note--caution" : ""}"><strong>${esc(
    sensitivity.verdict
  )}</strong>${
    sensitivity.flips.length
      ? `<br/>Parts that move 10 points or more: ${esc(
          sensitivity.flips.slice(0, 8).map((f) => `${f.cell} ${f.part}`).join(", ")
        )}${sensitivity.flips.length > 8 ? ", \u2026" : ""}`
      : ""
  }</div>
<p class="foot">A mix that flips between these three is an artefact of the threshold rather than a property of the team, and any finding resting on it is provisional.</p>
</section>`;
}

// ---------------------------------------------------------------------------
// 7. V1: the trajectory cube. Isometric, and only because a trajectory through
//    a three-objective trade-off space is a shape whose direction is the
//    finding. Anything needing a value read has a 2D companion above.
// ---------------------------------------------------------------------------

// The deck's convention (../../../slides.md:1319-1330): 30-degree isometric,
// +x right-and-up, +z left-and-up, y straight up. Same numbers, so a lifted
// picture matches the ones already there.
const ISO = { kx: 126.3, ky: 72.9, kv: 200 };

function isoPoint(origin, x, y, z) {
  return [
    origin[0] + (x - z) * ISO.kx,
    origin[1] - (x + z) * ISO.ky - y * ISO.kv,
  ];
}

function trajectoryCube(analysis) {
  // The cube's spatial assignment is the run's objective order (cost, lead
  // time, debt by default), NOT stage 2's axis order. Stage 2 orders spokes so
  // that conflicting pairs sit adjacent, which is the right rule for a radar
  // and a parallel-coordinates plot and the wrong one for three named spatial
  // axes a reader has to keep straight between runs. The mapping is printed
  // under the picture either way.
  const order = analysis.run.objectives;
  if (order.length < 3) {
    return `<section class="panel"><h2>Trajectory cube</h2><div class="note">The cube needs three objectives; this run has ${order.length}. The pairwise fronts above carry everything.</div></section>`;
  }
  if (!analysis.diagnostics?.cube_drawable) {
    const caution = (analysis.diagnostics?.cautions || []).find((c) => c.code === "CUBE_CLUTTER_CAP");
    return `<section class="panel"><h2>Trajectory cube</h2><div class="note note--refuse"><strong>Not drawn.</strong> ${esc(
      caution?.message || "above the clutter cap"
    )}</div></section>`;
  }

  const [xName, yName, zName] = order;
  const W = 720;
  const H = 420;
  const origin = [352, 352];
  const cells = plottedCells(analysis);
  const windows = analysis.windows;
  const drawnWindows = windows.slice(-4); // the cube draws the latest four
  const leadMetric = (analysis.stage0?.objectives || []).find((m) =>
    m.expression === "median(lead_time_days)" && m.direction === "min" && order.slice(0, 3).includes(m.name));
  const p90Metric = (analysis.stage0?.descriptors || []).find((m) => m.expression === "p90(lead_time_days)");
  const leadIndex = leadMetric ? order.indexOf(leadMetric.name) : -1;
  const leadRange = analysis.stage1?.normalisation?.[leadMetric?.name];
  const p90Coordinate = (cell) => {
    const p90 = cell.descriptors?.[p90Metric?.name];
    if (leadIndex < 0 || !leadRange || leadRange.lo == null || p90 == null) return null;
    const span = leadRange.hi - leadRange.lo;
    return (span ? 0 : 0.5) + (p90 - leadRange.lo) / (span || Math.max(1, Math.abs(leadRange.lo)));
  };
  const displaySpans = [1, 1, 1];
  if (leadIndex >= 0) displaySpans[leadIndex] = Math.max(1, ...cells.map((c) => p90Coordinate(c) ?? 0));

  const at = (cell) => {
    const x = cell.objectives_norm?.[xName];
    const y = cell.objectives_norm?.[yName];
    const z = cell.objectives_norm?.[zName];
    if (x === null || y === null || z === null || x === undefined || y === undefined || z === undefined)
      return null;
    return { x: x / displaySpans[0], y: y / displaySpans[1], z: z / displaySpans[2] };
  };

  let body = DEFS;

  // Floor plane and grid: the only depth cue that survives a projector.
  const corners = [
    isoPoint(origin, 0, 0, 0),
    isoPoint(origin, 1, 0, 0),
    isoPoint(origin, 1, 0, 1),
    isoPoint(origin, 0, 0, 1),
  ];
  body += tag("polygon", {
    points: corners.map((p) => p.join(",")).join(" "),
    class: "floor",
  });
  for (let f = 0.25; f < 1; f += 0.25) {
    const a = isoPoint(origin, f, 0, 0);
    const b = isoPoint(origin, f, 0, 1);
    const c = isoPoint(origin, 0, 0, f);
    const d = isoPoint(origin, 1, 0, f);
    body += tag("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: "isogrid" });
    body += tag("line", { x1: c[0], y1: c[1], x2: d[0], y2: d[1], class: "isogrid" });
  }
  const axes = [
    [isoPoint(origin, 0, 0, 0), isoPoint(origin, 1, 0, 0), xName, "start"],
    [isoPoint(origin, 0, 0, 0), isoPoint(origin, 0, 0, 1), zName, "end"],
    [isoPoint(origin, 0, 0, 0), isoPoint(origin, 0, 1, 0), yName, "middle"],
  ];
  for (const [a, b, name, anchor] of axes) {
    body += tag("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: "isoaxis" });
    const dx = anchor === "start" ? 10 : anchor === "end" ? -10 : 0;
    body += text(b[0] + dx, b[1] + (anchor === "middle" ? -10 : 4), `${name} \u2192 worse`, "tick", {
      "text-anchor": anchor,
    });
  }
  const good = isoPoint(origin, 0, 0, 0);
  body += tag("circle", { cx: good[0], cy: good[1], r: 4, class: "ideal" });
  body += text(good[0], good[1] + 20, "ideal", "micro", { "text-anchor": "middle" });

  // The axis-aligned attainment surface over the front of the latest window:
  // for a minimising point p the dominated region is the box from p to the far
  // corner, so the boundary facing the ideal point is its three faces at
  // x=p.x, y=p.y and z=p.z. The union of those facets over the front IS the
  // staircase, and it falls out of the same dimension sweep as the hypervolume
  // rather than needing a second implementation.
  const latest = primaryComparisons(analysis).slice(-1)[0];
  if (latest?.front?.length) {
    const frontCells = latest.front
      .map((label) => cells.find((c) => c.label === label))
      .filter(Boolean)
      .map((cell) => ({ cell, at: at(cell) }))
      .filter((p) => p.at);
    for (const p of frontCells) {
      const { x, y, z } = p.at;
      // Drawn as a wireframe, not filled. Filled facets read as a surface and
      // then occlude the trajectories, which is the precise failure mode that
      // makes 3D suspect in the first place; the edges carry the same shape and
      // hide nothing.
      const edges = [
        // the floor-facing step, at this cell's y
        [[x, y, z], [1, y, z]],
        [[x, y, z], [x, y, 1]],
        [[x, y, 1], [1, y, 1]],
        [[1, y, z], [1, y, 1]],
        // the two vertical risers, at this cell's x and z
        [[x, y, z], [x, 1, z]],
        [[x, y, 1], [x, 1, 1]],
        [[1, y, z], [1, 1, z]],
      ];
      for (const [a, b] of edges) {
        const pa = isoPoint(origin, a[0], a[1], a[2]);
        const pb = isoPoint(origin, b[0], b[1], b[2]);
        body += tag("line", { x1: pa[0], y1: pa[1], x2: pb[0], y2: pb[1], class: "attain" });
      }
    }
  }

  // Trajectories: one polyline per group over its windows. Labels and glyphs
  // are placed against a claimed-rectangle list, because on correlated axes
  // (cost and lead time move together here) the isometric x offset is x - z and
  // the polylines crowd the vertical -- so anything unplaced lands on top of a
  // neighbour.
  const claimed = [];
  const place = (x, y, w, h) => {
    const candidates = [
      [x + 12, y - 10],
      [x + 12, y + 16],
      [x - w - 12, y - 10],
      [x - w - 12, y + 16],
      [x + 12, y - 34],
      [x - w - 12, y - 34],
      [x + 12, y + 40],
    ];
    for (const [cx, cy] of candidates) {
      const box = [cx, cy - h, cx + w, cy];
      const clash = claimed.some(
        (c) => !(box[2] < c[0] || box[0] > c[2] || box[3] < c[1] || box[1] > c[3])
      );
      if (!clash) {
        claimed.push(box);
        return [cx, cy];
      }
    }
    return null;
  };

  for (const group of analysis.groups) {
    const path = drawnWindows
      .map((w) => cells.find((c) => c.group === group && c.window === w))
      .filter(Boolean)
      .map((cell) => ({ cell, at: at(cell) }))
      .filter((p) => p.at);
    if (!path.length) continue;
    const colour = seriesColour(analysis, group);

    // Floor-plane shadow first, so the trajectory reads above it.
    const shadow = path.map((p) => isoPoint(origin, p.at.x, 0, p.at.z).join(",")).join(" ");
    if (path.length > 1) {
      body += tag("polyline", { points: shadow, class: "shadow" });
    }

    path.forEach((p, i) => {
      const point = isoPoint(origin, p.at.x, p.at.y, p.at.z);
      const floor = isoPoint(origin, p.at.x, 0, p.at.z);
      body += tag("line", {
        x1: point[0],
        y1: point[1],
        x2: floor[0],
        y2: floor[1],
        class: "drop",
      });
      const p90 = p90Coordinate(p.cell);
      if (p90 !== null) {
        const coordinates = [p.at.x, p.at.y, p.at.z];
        coordinates[leadIndex] = p90 / displaySpans[leadIndex];
        const tail = isoPoint(origin, ...coordinates);
        body += marked(tag("line", {
          x1: point[0],
          y1: point[1],
          x2: tail[0],
          y2: tail[1],
          class: "whisker",
          "data-median": p.cell.objectives[leadMetric.name],
          "data-p90": p.cell.descriptors[p90Metric.name],
        }), `${p.cell.label}: median ${fmt(p.cell.objectives[leadMetric.name])} days; P90 ${fmt(p.cell.descriptors[p90Metric.name])} days`);
      }
      if (i > 0) {
        const previous = isoPoint(origin, path[i - 1].at.x, path[i - 1].at.y, path[i - 1].at.z);
        body += tag("line", {
          x1: previous[0],
          y1: previous[1],
          x2: point[0],
          y2: point[1],
          stroke: colour,
          "stroke-width": 2,
          "marker-end": "url(#arrow)",
          "stroke-opacity": 0.9,
        });
      }
      const isLatest = i === path.length - 1;
      const probability = p.cell.p_on_front;
      if (probability !== null && probability !== undefined) {
        body += tag("circle", {
          cx: point[0],
          cy: point[1],
          r: 5 + 8 * probability,
          fill: "none",
          stroke: colour,
          "stroke-opacity": 0.25,
        });
      }
      body += marked(
        tag("circle", {
          cx: point[0],
          cy: point[1],
          r: isLatest ? 6.5 : 4.5,
          fill: isLatest ? colour : "var(--bg)",
          stroke: colour,
          "stroke-width": 1.6,
          "fill-opacity": isLatest ? 1 : 0.9,
        }),
        `${p.cell.label}  ${xName} ${fmt(p.cell.objectives[xName])}  ${yName} ${fmt(
          p.cell.objectives[yName]
        )}  ${zName} ${fmt(p.cell.objectives[zName])}`
      );
      if (isLatest) {
        const spot = place(point[0], point[1], 8 * group.length, 12);
        if (spot) {
          body += text(spot[0], spot[1], group, "micro dark");
          body += tag("line", {
            x1: point[0],
            y1: point[1],
            x2: spot[0] - 2,
            y2: spot[1] - 4,
            class: "leader",
          });
        }
      }
    });

    // Billboarded radar glyphs on the first and latest window only: always
    // parallel to the picture plane and at constant size, because a glyph that
    // rotates with the scene stops being comparable to its neighbours -- which
    // is the only thing a glyph is for.
    const glyphCells = path.length > 1 ? [path[0], path[path.length - 1]] : [path[0]];
    for (const p of glyphCells) {
      const point = isoPoint(origin, p.at.x, p.at.y, p.at.z);
      const spot = place(point[0] + 8, point[1] + 18, 38, 38);
      if (!spot) continue;
      body += tag("line", {
        x1: point[0],
        y1: point[1],
        x2: spot[0] + 18,
        y2: spot[1] - 18,
        class: "leader",
      });
      body += radarGlyph(analysis, p.cell, spot[0] + 18, spot[1] - 18, 17);
    }
  }

  const travel = (analysis.stage7?.direction_of_travel || []).filter((t) => t.available);

  return `<section class="panel"><h2>Trajectory cube</h2>
<p class="lede"><strong>The good corner is the origin</strong>: all three axes minimise. Each group is a polyline over its windows, latest filled and earlier hollow, with an arrowhead per segment \u2014 evolution is the path, not an axis, which is what lets calendar time and three objectives share one frame. The pale steps are the 3-objective attainment surface bounding the dominated region.</p>
<div class="cube-wrap">${svg(
    { viewBox: `0 0 ${W} ${H}`, class: "chart wide", role: "img", "aria-label": `An isometric cube with axes ${xName}, ${yName} and ${zName}, all minimising, and one polyline per group over its windows.` },
    body
  )}</div>
<p class="foot">Axis mapping: <code>x = ${esc(xName)}</code>, <code>y = ${esc(yName)}</code> (vertical), <code>z = ${esc(zName)}</code>, in the order the objectives were given rather than the radars' spoke order, so the three named axes stay in the same place between runs. Isometric, not perspective, so lengths stay comparable. Drop-lines and a greyed floor-plane shadow provide depth cues. P90 whiskers follow the lead-time axis on the median's scale, extended to include the tail; without them a 3D scatter is a picture of nothing. 3D is justified here only because a trajectory through a trade-off space is a shape and its direction is the finding \u2014 every value on this chart is readable in the 2D panels above (Munzner, <em>Visualization Analysis and Design</em>, 2014). Glyphs are billboarded and each has a printed twin below (Fuchs, Isenberg, Bezerianos &amp; Keim, <em>IEEE TVCG</em>, Jul 2017: star glyphs compare silhouettes, they do not carry values).</p>
${
  travel.length
    ? `<table class="kv wide"><thead><tr><th>group</th><th>from</th><th>to</th><th>\u2016\u0394\u2016</th><th>angle to ideal</th><th>reading</th></tr></thead><tbody>${travel
        .map(
          (t) =>
            `<tr><td>${esc(t.group)}</td><td>${esc(t.from)}</td><td>${esc(t.to)}</td><td>${fmt(
              t.magnitude
            )}</td><td>${fmt(t.angle_to_ideal_deg, 0)}\u00b0</td><td>${esc(t.reading)}</td></tr>`
        )
        .join("")}</tbody></table><p class="foot">The arrows, as numbers. Displacement in min-max normalised objective space, and the angle between it and the direction of the ideal point: improving on everything together is a small angle, and trading one objective against another is a large one.</p>`
    : ""
}
</section>`;
}

function radarGlyph(analysis, cell, cx, cy, r) {
  if (!IMPACT_ORDER.every((k) => cell.impact_mix?.[k] != null)) return "";
  const greyed = analysis.stage0?.greyed_spokes || {};
  const parts = IMPACT_ORDER;
  const step = (Math.PI * 2) / parts.length;
  let body = "";
  body += tag("circle", { cx, cy, r, class: "glyphring" });
  const points = [];
  parts.forEach((klass, i) => {
    const share = cell.impact_mix?.[klass] ?? 0;
    const angle = -Math.PI / 2 + i * step;
    // Radius linear in the share. Values are printed elsewhere and area
    // comparison is never invited: polygon area under equal-angle spokes is
    // the sum of r_i * r_{i+1} * sin(theta), so no radius mapping makes area
    // track a composition.
    const rr = 3 + share * (r - 3);
    points.push(`${cx + Math.cos(angle) * rr},${cy + Math.sin(angle) * rr}`);
    const outer = [cx + Math.cos(angle) * r, cy + Math.sin(angle) * r];
    body += tag("line", {
      x1: cx,
      y1: cy,
      x2: outer[0],
      y2: outer[1],
      class: greyed[klass] ? "spoke spoke--grey" : "spoke",
    });
  });
  body += tag("polygon", { points: points.join(" "), class: "glyph" });
  return marked(
    body,
    `${cell.label} impact mix: ${IMPACT_ORDER.map(
      (k) => `${k} ${pct(cell.impact_mix?.[k])}`
    ).join(", ")}`
  );
}

// ---------------------------------------------------------------------------
// 8. V2: the Kiviat tube with rails.
// ---------------------------------------------------------------------------

function kiviatTube(analysis, group, mixKey, classes, colours, titleText) {
  const windows = analysis.windows.slice(-4);
  const cells = windows.map((w) =>
    plottedCells(analysis).find((c) => c.group === group && c.window === w && classes.every((k) => c[mixKey]?.[k] != null))
  );
  if (!cells.filter(Boolean).length) return "";

  const W = 470;
  const H = 420;
  const ringR = 62;
  const step = (Math.PI * 2) / classes.length;
  const greyed = analysis.stage0?.greyed_spokes || {};
  const colour = seriesColour(analysis, group);
  // Rings sit in planes perpendicular to the tube axis, so under isometric
  // projection every ring foreshortens identically and the silhouettes stay
  // comparable. That property is the whole reason for isometric and it dies
  // under perspective.
  const depth = 44;
  const lift = 20;
  // Index 0 is the oldest window and sits furthest back; the LATEST window is
  // the near, opaque ring at the bottom left. Drawing it the other way round
  // put the opaque "current" ring on the oldest month.
  const centreOf = (i) => [148 + (windows.length - 1 - i) * depth, 138 + i * lift];

  let body = DEFS;
  // Painter's algorithm, back to front, so the near ring wins every overlap.
  for (let i = 0; i < cells.length; i += 1) {
    const cell = cells[i];
    const [cx, cy] = centreOf(i);
    const isNear = i === cells.length - 1;
    const opacity = isNear ? 1 : 0.25 + (0.35 * i) / Math.max(1, cells.length - 1);
    body += tag("circle", { cx, cy, r: ringR, class: "glyphring", opacity });
    if (!cell) {
      body += text(cx, cy + 4, "no cell", "micro", { "text-anchor": "middle" });
      continue;
    }
    classes.forEach((klass, k) => {
      const angle = -Math.PI / 2 + k * step;
      body += tag("line", {
        x1: cx,
        y1: cy,
        x2: cx + Math.cos(angle) * ringR,
        y2: cy + Math.sin(angle) * ringR,
        class: greyed[klass] ? "spoke spoke--grey" : "spoke",
        opacity,
      });
    });

    // `unresolved` is a gap in the ring, not a spoke: it is the part of the
    // composition that is missing, and a spoke would let it read as something
    // the team produced.
    const unresolved = cell[mixKey]?.unresolved ?? 0;
    const drawn = classes.filter((k) => k !== "unresolved");
    const vertices = drawn.map((klass) => {
      const angle = -Math.PI / 2 + classes.indexOf(klass) * step;
      const rr = (cell[mixKey]?.[klass] ?? 0) * ringR;
      return [cx + Math.cos(angle) * rr, cy + Math.sin(angle) * rr];
    });
    if (unresolved > 0) {
      body += tag("polyline", {
        points: vertices.map((p) => p.join(",")).join(" "),
        fill: "none",
        stroke: colour,
        "stroke-opacity": opacity,
        "stroke-width": isNear ? 2.2 : 1.2,
      });
      const gapAngle = -Math.PI / 2 + classes.indexOf("unresolved") * step;
      body += tag("line", {
        x1: cx,
        y1: cy,
        x2: cx + Math.cos(gapAngle) * ringR * unresolved,
        y2: cy + Math.sin(gapAngle) * ringR * unresolved,
        class: "gapspoke",
        opacity,
      });
    } else {
      body += tag("polygon", {
        points: vertices.map((p) => p.join(",")).join(" "),
        fill: colour,
        "fill-opacity": isNear ? 0.22 : opacity * 0.22,
        stroke: colour,
        "stroke-opacity": opacity,
        "stroke-width": isNear ? 2.2 : 1.2,
      });
    }
    if (isNear || i === 0) {
      // Two labels, not one per ring: the receding rings sit where a third and
      // fourth label would go, and the printed twin below names every window
      // anyway.
      body += text(
        cx + (isNear ? -ringR - 6 : ringR + 6),
        cy + (isNear ? ringR - 2 : -ringR + 8),
        `${isNear ? "latest " : "oldest "}${cell.window}`,
        "micro",
        { "text-anchor": isNear ? "end" : "start" }
      );
    }
    body += marked(
      tag("circle", { cx, cy, r: 3, fill: colour, "fill-opacity": opacity }),
      `${cell.label}: ${classes.map((k) => `${k} ${pct(cell[mixKey]?.[k])}`).join(", ")}`
    );
  }
  // Spoke names once, around the near ring, rather than a set of values per
  // ring: the values live in the printed twin below, which is what makes the
  // glyph legal (a star glyph compares silhouettes, it does not carry values).
  const [nx, ny] = centreOf(cells.length - 1);
  classes.forEach((klass, k) => {
    const angle = -Math.PI / 2 + k * step;
    const lx = nx + Math.cos(angle) * (ringR + 10);
    const ly = ny + Math.sin(angle) * (ringR + 10) + 3;
    body += text(lx, ly, klass + (greyed[klass] ? " *" : ""), "micro", {
      "text-anchor": Math.cos(angle) < -0.2 ? "end" : Math.cos(angle) > 0.2 ? "start" : "middle",
    });
  });

  // Rails: the objectives as aligned strips on the same time axis. They are
  // magnitudes, so a spoke would both make them unreadable and mix an
  // objective into a descriptor plot.
  const railTop = 322;
  const railX = scale([0, Math.max(1, windows.length - 1)], [150, 150 + (windows.length - 1) * 66]);
  analysis.run.objectives.forEach((name, oi) => {
    const y = railTop + oi * 26;
    body += text(6, y + 4, name, "micro");
    windows.forEach((w, i) => {
      const cell = cells[i];
      const v = cell?.objectives_norm?.[name];
      if (v === null || v === undefined) {
        body += text(railX(i), y + 4, "\u2014", "micro", { "text-anchor": "middle" });
        return;
      }
      body += tag("rect", {
        x: railX(i) - 28,
        y: y - 9,
        width: 56,
        height: 18,
        fill: colour,
        "fill-opacity": 0.1 + 0.55 * v,
        stroke: "var(--border)",
      });
      body += text(railX(i), y + 4, fmt(cell.objectives[name]), "micro", {
        "text-anchor": "middle",
      });
    });
  });
  body += text(6, railTop - 28, "rails: objectives on the same time axis, darker is worse", "micro");
  windows.forEach((w, i) => {
    body += text(railX(i), railTop - 12, w, "micro", { "text-anchor": "middle" });
  });

  // The printed twin. Every glyph on this page has one.
  const twin = `<table class="twin"><thead><tr><th>${esc(titleText)}</th>${windows
    .map((w) => `<th>${esc(w)}</th>`)
    .join("")}</tr></thead><tbody>${classes
    .map(
      (klass) =>
        `<tr class="${greyed[klass] ? "greyed" : ""}"><td>${esc(klass)}${
          greyed[klass] ? " *" : ""
        }</td>${cells
          .map((cell) => `<td>${cell ? pct(cell[mixKey]?.[klass]) : "\u2014"}</td>`)
          .join("")}</tr>`
    )
    .join("")}</tbody></table>`;

  return `<figure><figcaption>${esc(group)} \u00b7 ${esc(titleText)}</figcaption>${svg(
    {
      viewBox: `0 0 ${W} ${H}`,
      class: "chart",
      role: "img",
      "aria-label": `A Kiviat tube for ${group}: ${windows.length} radar cross-sections of the ${titleText} receding along a time axis, latest nearest, with objective rails beneath.`,
    },
    body
  )}${twin}</figure>`;
}

function kiviatSection(analysis) {
  const groups = analysis.groups.slice(0, 6);
  return `<section class="panel"><h2>Kiviat tube</h2>
<p class="lede">A stack of radar cross-sections along a time axis with the enclosing surface implied \u2014 Hackstadt &amp; Malony, <em>IEEE CG&amp;A</em> 15(4), Jul 1995; the 3D form is Kerren &amp; Jusufi, SOFTVIS 2010, doi:10.1145/1879211.1879241. They solved occlusion with transparency plus a scrollable opaque slice, which a static page does not have, so the ring count is capped at four instead. Front ring opaque with every value printed, rear rings at falling opacity, painted back to front.</p>
<p class="lede"><strong>Two tubes side by side, never one carrying both mixes.</strong> A 6-spoke and a 4-spoke polygon in one polar plane have vertices that align on no axis, and readers compare them anyway. Radius is linear in the share and the values are printed: polygon area under equal-angle spokes is \u03a3 r\u1d62\u00b7r\u1d62\u208a\u2081\u00b7sin \u03b8, so no radius mapping makes area track a composition \u2014 area is not a channel here. Both mixes are shares on a common 0\u20131 scale, so the usual arbitrary-per-axis-normalisation complaint does not apply.</p>
<div class="tubes">${groups
    .map(
      (group) =>
        kiviatTube(analysis, group, "impact_mix", IMPACT_ORDER, IMPACT_COLOURS, "impact mix") +
        kiviatTube(
          analysis,
          group,
          "question_mix",
          Object.keys(QUESTION_COLOURS),
          QUESTION_COLOURS,
          "question mix"
        )
    )
    .join("")}</div>
<p class="foot">Cost and debt get rails rather than spokes, on the same time axis: they are magnitudes, and a spoke would both make them unreadable and mix an objective into a descriptor plot. Greyed spokes are the ones a structural overlap found (see the header) \u2014 those arms are not independent evidence.</p>
</section>`;
}

// ---------------------------------------------------------------------------
// 9. The ladder and the null model, as text. These are the findings.
// ---------------------------------------------------------------------------

function ladderSection(analysis) {
  const blocks = (analysis.comparisons || [])
    .map((comparison) => {
      if (comparison.front_not_drawn) {
        return `<div class="pairs-row"><h3>${esc(comparison.label)}</h3>${comparison.refusals
          .map(
            (r) =>
              `<div class="note note--refuse"><strong>REFUSED \u00b7 ${esc(
                r.code
              )}</strong><br/>${esc(r.message)}</div>`
          )
          .join("")}</div>`;
      }
      const nullModel = comparison.null_model || {};
      const ladder = comparison.relaxation_ladder || {};
      const kSweep = ladder.k_dominance?.sweep || [];
      const cycles = kSweep.flatMap((s) => s.cycles || []);
      return `<div class="pairs-row"><h3>${esc(comparison.label)}</h3>
<p class="q">${esc(comparison.question)}</p>
<div class="note ${nullModel.verdict === "NOT INFORMATIVE" ? "note--caution" : ""}">
<strong>Null model \u00b7 ${esc(nullModel.verdict || "n/a")}</strong><br/>
${esc(nullModel.reading || "")}<br/>
<code>${comparison.front?.length ?? 0} of ${comparison.cells.length} cells on layer 1</code>.
Permutation null over ${esc(nullModel.replicates)} shuffles: mean ${fmt(
        nullModel.permutation_mean
      )}, 5\u201395% ${esc(nullModel.permutation_p05)}\u2013${esc(
        nullModel.permutation_p95
      )}, observed at percentile ${fmt(nullModel.percentile)}.
Closed form <code>A(n,d)</code> = ${fmt(nullModel.closed_form_A_n_d)} for n=${esc(
        nullModel.n_cells
      )}, d=${esc(nullModel.n_objectives)} (Bentley, Kung, Schkolnick &amp; Thompson, <em>JACM</em> 25(4):536\u2013543, Oct 1978) \u2014 the permutation version is the default because it needs no independence assumption and handles ties; the closed form is the check on the permutation code.
</div>
<table class="kv wide"><thead><tr><th>rung</th><th>parameter</th><th>result</th></tr></thead><tbody>
${
  ladder.epsilon_dominance
    ? `<tr><td>\u03b5-dominance<br/><span class="micro">Laumanns, Thiele, Deb &amp; Zitzler, <em>Evol. Comput.</em> 10(3), 2002</span></td><td>${Object.entries(
        ladder.epsilon_dominance.epsilon
      )
        .map(([k, v]) => `${esc(k)} ${fmt(v)}`)
        .join("<br/>")}<br/><span class="micro">source: ${esc(
        ladder.epsilon_dominance.epsilon_source
      )}</span></td><td>${ladder.epsilon_dominance.size} cell(s): ${esc(
        ladder.epsilon_dominance.front.join(", ")
      )}<br/><span class="micro">${esc(ladder.epsilon_dominance.note)}</span></td></tr>`
    : ""
}
<tr><td>k-dominance<br/><span class="micro">Chan et al., SIGMOD 2006, 503\u2013514</span></td><td>k swept from d down</td><td>${kSweep
        .map((s) => `k=${s.k}: ${s.size} cell(s)`)
        .join("<br/>")}${
        cycles.length
          ? `<br/><span class="micro">cycles reported rather than broken: ${esc(
              cycles.map((c) => c.join(" \u2192 ")).join("; ")
            )} \u2014 k-dominance is not transitive, so the result is a set and not an order</span>`
          : `<br/><span class="micro">no cycles found; k-dominance is not transitive, so they are checked for</span>`
      }</td></tr>
<tr><td>skyline frequency<br/><span class="micro">Chan et al., EDBT 2006</span></td><td>${esc(
        ladder.skyline_frequency?.subspaces
      )} non-empty objective subspaces</td><td>${Object.entries(
        ladder.skyline_frequency?.scores || {}
      )
        .sort((a, b) => b[1] - a[1])
        .map(([label, score]) => `${esc(label)} ${score}/${ladder.skyline_frequency.subspaces}`)
        .join("<br/>")}<br/><span class="micro">a continuous interestingness score, not a binary: on the front in 40 of 63 subspaces is a different claim from 3 of 63</span></td></tr>
<tr><td>knee<br/><span class="micro">Branke, Deb, Dierolf &amp; Osswald, PPSN VIII, 2004</span></td><td>${esc(
        ladder.knees?.method || "n/a"
      )}</td><td>${esc(ladder.knees?.knee || "\u2014")}${
        ladder.knees?.marginal_utility
          ? `<br/><span class="micro">marginal utility, share of uniformly sampled simplex weightings won: ${Object.entries(
              ladder.knees.marginal_utility
            )
              .sort((a, b) => b[1] - a[1])
              .map(([label, share]) => `${esc(label)} ${pct(share)}`)
              .join(", ")}</span>`
          : ""
      }</td></tr>
<tr><td>weighted sum<br/><span class="micro">computed because teams will do it anyway</span></td><td>${Object.entries(
        ladder.weighted_sum?.weights || {}
      )
        .map(([k, v]) => `${esc(k)} ${fmt(v)}`)
        .join("<br/>")}</td><td>${Object.entries(ladder.weighted_sum?.scores || {})
        .filter(([, v]) => v !== null)
        .sort((a, b) => a[1] - b[1])
        .map(([label, score]) => `${esc(label)} ${fmt(score)}`)
        .join("<br/>")}<br/><span class="micro">${esc(
        ladder.weighted_sum?.warning || ""
      )}</span></td></tr>
<tr><td>hypervolume<br/><span class="micro">reference point is part of the result</span></td><td>reference ${fmt(
        comparison.hypervolume?.reference_point
      )} per axis</td><td>${fmt(comparison.hypervolume?.value, 3)}<br/><span class="micro">${esc(
        comparison.hypervolume?.method || ""
      )}</span></td></tr>
</tbody></table></div>`;
    })
    .join("");

  const stage3 = analysis.stage3 || {};
  const ladderRungs = stage3.delta_moss?.ladder || [];

  return `<section class="panel"><h2>The relaxation ladder</h2>
<p class="lede">Applied in order when a front is degenerate, every rung a named relaxation with its parameter reported rather than an ad-hoc filter. Reducing objectives comes first because it is the only rung that does not weaken the meaning of "dominated".</p>
<div class="note"><strong>Objective reduction \u00b7 stage 3</strong><br/>
Greedy correlation clustering kept <code>${esc(
    (stage3.greedy_reduction?.kept || []).join(", ")
  )}</code>${
    (stage3.greedy_reduction?.dropped || []).length
      ? ` and dropped <code>${esc(stage3.greedy_reduction.dropped.join(", "))}</code>`
      : " and dropped nothing"
  }.<br/>
\u03b4-MOSS (Brockhoff &amp; Zitzler): ${ladderRungs
    .map((r) => `${r.subset.length} axes \u2192 \u03b4 ${fmt(r.delta, 3)}`)
    .join(" \u00b7 ")}<br/>
<span class="micro">${esc(stage3.delta_moss?.note || "")}</span><br/>
<span class="micro">${esc(stage3.pca_note || "")}</span></div>
${blocks}
</section>`;
}

// ---------------------------------------------------------------------------
// Assembly
// ---------------------------------------------------------------------------

const CSS = `
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #14161a; --dim: #b8bdc4; --muted: #5b626c;
  --border: #d9dde2; --grid: #eef0f3; --card: #f7f8fa; --none: #f0f1f3;
  --series: #3b6ef5;
  --refuse: #e6399b; --caution: #f5c542; --overlap: #9b6bf5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101216; --fg: #e8eaed; --dim: #4a5058; --muted: #98a0aa;
    --border: #2a2f36; --grid: #1c2026; --card: #171a1f; --none: #191c21;
  }
}
:root[data-theme="dark"] {
  --bg: #101216; --fg: #e8eaed; --dim: #4a5058; --muted: #98a0aa;
  --border: #2a2f36; --grid: #1c2026; --card: #171a1f; --none: #191c21;
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #14161a; --dim: #b8bdc4; --muted: #5b626c;
  --border: #d9dde2; --grid: #eef0f3; --card: #f7f8fa; --none: #f0f1f3;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 96px; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 30px; line-height: 1.15; margin: 0 0 6px; letter-spacing: 0; overflow-wrap: anywhere; }
h2 { font-size: 20px; margin: 0 0 10px; letter-spacing: 0; }
h3 { font-size: 15px; margin: 22px 0 4px; color: var(--muted); font-weight: 600; }
.sub { color: var(--muted); margin: 0 0 28px; }
.panel {
  border: 1px solid var(--border); border-radius: 8px; padding: 20px 22px;
  margin: 0 0 22px; background: var(--card);
}
.lede { margin: 0 0 14px; max-width: 92ch; }
.q { color: var(--muted); font-style: italic; margin: 0 0 8px; }
.foot { color: var(--muted); font-size: 13px; margin: 12px 0 0; max-width: 100ch; }
.micro { font-size: 11px; fill: var(--muted); color: var(--muted); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.92em; }
.note {
  border-left: 3px solid var(--border); background: var(--bg); padding: 10px 14px;
  margin: 12px 0; border-radius: 0 4px 4px 0; font-size: 14px;
}
.note--refuse { border-left-color: var(--refuse); }
.note--caution { border-left-color: var(--caution); }
.note--overlap { border-left-color: var(--overlap); }
.note ul { margin: 6px 0 0; padding-left: 20px; }
table { border-collapse: collapse; font-size: 13px; }
table.kv { margin: 8px 0; }
table.kv th { text-align: left; color: var(--muted); font-weight: 500; padding: 3px 16px 3px 0; vertical-align: top; }
table.kv td { padding: 3px 16px 3px 0; vertical-align: top; }
table.wide { width: 100%; }
table.wide th, table.wide td { border-bottom: 1px solid var(--border); padding: 6px 10px 6px 0; }
table.cells { width: 100%; margin-top: 14px; }
table.cells th { text-align: left; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); padding: 6px 10px 6px 0; }
table.cells td { padding: 4px 10px 4px 0; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
table.cells tr.floored td { color: var(--muted); font-style: italic; }
.v-conflicting { color: var(--refuse); }
.v-redundant { color: var(--caution); }
.scroll { overflow-x: auto; max-width: 100%; }
.chart { display: block; width: 100%; height: auto; max-width: 100%; color: var(--fg); }
.chart.wide { max-width: 100%; }
/* Grid rather than flex: a flex row gives its last item all the leftover
   width, which blows one small multiple up to four times its neighbours and
   destroys the only thing a small multiple is for. */
.pairs { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr)); gap: 10px; }
.multiples { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(230px, 100%), 1fr)); gap: 12px; }
.tubes { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(430px, 100%), 1fr)); gap: 20px; }
.pairs figure, .multiples figure, .tubes figure { margin: 0; min-width: 0; }
.twin { width: 100%; margin-top: 6px; }
.twin th, .twin td { border-bottom: 1px solid var(--grid); padding: 2px 6px 2px 0; font-size: 11px; }
.twin th { color: var(--muted); font-weight: 500; text-align: right; }
.twin th:first-child { text-align: left; }
.twin td { text-align: right; font-variant-numeric: tabular-nums; }
.twin td:first-child { text-align: left; }
.twin tr.greyed td:first-child { color: var(--muted); font-style: italic; }
figcaption { font-size: 13px; color: var(--muted); margin-bottom: 4px; }
.cube-wrap { overflow-x: auto; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin: 8px 0 12px; }
.legend .key { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
.legend em { font-style: normal; opacity: 0.75; }
text.tick { font-size: 11px; fill: var(--muted); }
text.dark { fill: var(--fg); }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--border); stroke-width: 1; }
.stair { fill: var(--series); fill-opacity: 0.07; stroke: var(--series); stroke-width: 1.2; stroke-opacity: 0.5; }
.band { fill: var(--series); fill-opacity: 0.09; stroke: none; }
.band-median { fill: none; stroke: var(--series); stroke-width: 1; stroke-dasharray: 3 3; stroke-opacity: 0.7; }
.missing { fill: var(--none); stroke: var(--border); stroke-dasharray: 3 3; }
.gapline { stroke: var(--border); stroke-dasharray: 2 3; }
.floor { fill: var(--grid); stroke: var(--border); stroke-width: 1; }
.isogrid { stroke: var(--border); stroke-opacity: 0.35; }
.isoaxis { stroke: var(--muted); stroke-width: 1.2; }
.ideal { fill: var(--fg); }
.attain { fill: none; stroke: var(--series); stroke-opacity: 0.32; stroke-width: 1; stroke-dasharray: 5 4; }
.shadow { fill: none; stroke: var(--dim); stroke-width: 1.4; stroke-dasharray: 4 3; }
.drop { stroke: var(--dim); stroke-width: 1; stroke-dasharray: 2 3; }
.whisker { stroke: var(--muted); stroke-width: 2.2; stroke-opacity: 0.6; }
.glyphring { fill: none; stroke: var(--border); }
.glyph { fill: var(--series); fill-opacity: 0.25; stroke: var(--series); stroke-width: 1.4; }
.spoke { stroke: var(--border); stroke-width: 0.8; }
.spoke--grey { stroke: var(--dim); stroke-width: 2; stroke-dasharray: 2 2; }
.gapspoke { stroke: var(--fg); stroke-width: 2; stroke-dasharray: 3 2; stroke-opacity: 0.5; }
.leader { stroke: var(--dim); stroke-width: 0.8; }
.stair--past { fill: none; stroke: var(--series); stroke-width: 1; }
details summary { cursor: pointer; color: var(--muted); }
.note, .foot, .lede, .sub { overflow-wrap: anywhere; }
@media (max-width: 600px) {
  body { padding: 20px 12px 48px; }
  .panel { padding: 16px 12px; }
}
`;

function render(analysis) {
  const generated = analysis.run?.ts || "";
  const body = [
    diagnostics(analysis),
    pairwiseFronts(analysis),
    membershipTimeline(analysis),
    compositions(analysis),
    parallelCoordinates(analysis),
    levelDiagrams(analysis),
    sensitivityStrip(analysis),
    ladderSection(analysis),
    trajectoryCube(analysis),
    kiviatSection(analysis),
  ].join("\n").replaceAll("<table", '<div class="scroll"><table')
    .replaceAll("</table>", "</table></div>");

  return `<style>${CSS}</style>
<main>
<h1>Decision surface \u00b7 ${esc(analysis.run.run_id)}</h1>
<p class="sub">${esc(analysis.run.objectives.join(" \u00b7 "))} \u2014 ${esc(
    analysis.mode
  )} \u00b7 generated ${esc(generated)} \u00b7 static and self-contained: no fetches, no CDN, nothing to install.</p>
${body}
<section class="panel"><h2>Why this page is static</h2>
<p class="lede">Animation suits presentation and small multiples beat it for analysis (Robertson, Fernandez, Fisher, Lee &amp; Stasko, <em>IEEE TVCG</em> 14(6):1325\u20131332, 2008 \u2014 VIS Test-of-Time 2018). For radial composite indicators specifically, a static time encoding beat a dynamic one (Albo, Lanir, Bak &amp; Rafaeli, <em>Off the Radar</em>, <em>IEEE TVCG</em> 22(1):569\u2013578, 2016), which is exactly the object in the Kiviat section. An interactive framework for this job exists and is better at being interactive \u2014 ParetoLens, arXiv:2501.02857, 6 Jan 2025 \u2014 so this page stays a file you can mail to a sceptic.</p>
</section>
</main>
<script type="application/json" id="findings">${JSON.stringify(findings(analysis), null, 2)
    .replace(/</g, "\\u003c")
    .replace(/\u2028/g, "\\u2028")}</script>`;
}

// ---------------------------------------------------------------------------

function main(argv) {
  const [input, output] = argv;
  if (!input) {
    process.stderr.write("usage: node render.mjs analysis.json [out.html]\n");
    return 2;
  }
  const analysis = JSON.parse(readFileSync(input, "utf8"));
  const html = render(analysis);
  const target = output || input.replace(/\.json$/, "") + ".html";
  writeFileSync(target, html, "utf8");
  const cells = analysis.cells?.length ?? 0;
  const drawn = plottedCells(analysis).length;
  process.stdout.write(
    `rendered ${cells} cell(s), ${drawn} plotted, ${
      (analysis.comparisons || []).length
    } comparison(s) -> ${target}\n`
  );
  return 0;
}

process.exitCode = main(process.argv.slice(2));
