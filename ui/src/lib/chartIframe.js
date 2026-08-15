// Builds the sandboxed <iframe srcdoc> that renders a chart the agent authored.
//
// The agent's render_chart tool call carries a `code` string — JavaScript that
// calls `renderChart(el, spec)` (see services/chat/.../charts.py for the authoring
// contract the model writes against). We run that code in a FROZEN, sandboxed
// iframe so a bad chart can neither crash the app nor touch the page:
//
//   - sandbox="allow-scripts" WITHOUT allow-same-origin → the frame is a unique
//     opaque origin. Model JS cannot read our DOM, our cookies, or the Cognito
//     access token that lives in the parent window. This is the security boundary.
//   - a strict CSP (default-src 'none', connect-src 'none') → the frame can run its
//     inline scripts and draw to a canvas, but cannot fetch, beacon, or exfiltrate.
//   - Chart.js is INLINED (the frame can't fetch it) from the vendored UMD build.
//
// The frame can't read the parent's computed theme (opaque origin), so we resolve
// the app's chart palette + a few UI tokens to concrete rgb triples HERE (via a
// 1x1 canvas painter — robust for oklch tokens, which getComputedStyle returns
// unresolved) and inject them, so charts match the app's look in light and dark.
//
// The frame reports its rendered height + success/error back via postMessage; the
// React wrapper (ChartFrame.jsx) sizes the iframe and shows a contained error.

import CHART_JS_SRC from "@/vendor/chart.umd.min.js?raw"
// Plugin controllers/plugins for the non-core chart types (see
// vendor/README.md). sankey/treemap self-register against the global `Chart`;
// annotation/matrix/boxplot expose globals and are registered explicitly in
// renderChart (Chart.register dedupes, so re-registering is safe).
import CHART_SANKEY_SRC from "@/vendor/chartjs-chart-sankey.min.js?raw"
import CHART_TREEMAP_SRC from "@/vendor/chartjs-chart-treemap.min.js?raw"
import CHART_ANNOTATION_SRC from "@/vendor/chartjs-plugin-annotation.min.js?raw"
import CHART_MATRIX_SRC from "@/vendor/chartjs-chart-matrix.min.js?raw"
import CHART_BOXPLOT_SRC from "@/vendor/chartjs-chart-boxplot.min.js?raw"

// The palette + UI tokens we resolve from the app theme and hand to the frame.
// --chart-1..10 are the series palette; the rest style axes/legend/tooltip so
// the chart reads as part of the UI. Names map to CSS custom properties on
// :root. ORDER MATTERS: series take these in sequence (seriesColor), and that
// order is the CVD-safety mechanism — see the --chart-1 comment in index.css.
const PALETTE_VARS = [
  "--chart-1",
  "--chart-2",
  "--chart-3",
  "--chart-4",
  "--chart-5",
  "--chart-6",
  "--chart-7",
  "--chart-8",
  "--chart-9",
  "--chart-10",
]
const UI_VARS = {
  foreground: "--foreground",
  mutedForeground: "--muted-foreground",
  border: "--border",
  card: "--card",
  background: "--background",
}

// Resolve a CSS color expression (e.g. "var(--chart-1)", "oklch(...)") to a
// concrete "r, g, b" string by painting it on a 1x1 canvas and reading the pixel.
// This works for ANY color the browser supports — including oklch, which
// getComputedStyle returns verbatim (unresolved) rather than as rgb. Returns null
// if the color can't be painted (so the caller can fall back).
function resolveColorToRgb(ctx, expr) {
  try {
    ctx.clearRect(0, 0, 1, 1)
    ctx.fillStyle = "#000"
    ctx.fillStyle = expr // ignored if invalid → stays #000, but we detect below
    // Paint on a known-different backdrop so an ignored (invalid) color is visible
    // as the backdrop, not a false black. Two passes with different backdrops
    // disambiguate a genuine black from an invalid color.
    ctx.fillStyle = expr
    ctx.fillRect(0, 0, 1, 1)
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
    return `${r}, ${g}, ${b}`
  } catch {
    return null
  }
}

// Read a CSS custom property off :root as an authored expression, then resolve it
// to concrete rgb via the canvas painter. `wrap` builds the expression to paint.
function resolveVar(ctx, rootStyle, cssVar) {
  const raw = rootStyle.getPropertyValue(cssVar).trim()
  if (!raw) return null
  // Paint the raw value directly (it's a full color like "oklch(...)"/"#fff").
  return resolveColorToRgb(ctx, raw)
}

// Snapshot the app's chart palette + UI tokens as concrete rgb triples. Called by
// ChartFrame each time it (re)builds a chart, so a light/dark theme switch that
// changes the tokens produces a rebuilt frame with the new colors.
export function resolveChartPalette() {
  const fallback = {
    // The LIGHT-mode palette as literals — used only when the theme tokens
    // can't be resolved (no document / no 2d context), so a chart still draws
    // in the right colors instead of Chart.js defaults.
    chart: [
      "0, 127, 164",
      "235, 104, 51",
      "31, 175, 122",
      "235, 160, 2",
      "233, 125, 166",
      "127, 145, 16",
      "115, 77, 190",
      "48, 134, 57",
      "58, 108, 206",
      "211, 57, 73",
    ],
    foreground: "23, 23, 23",
    mutedForeground: "115, 115, 115",
    border: "229, 229, 229",
    card: "255, 255, 255",
    background: "255, 255, 255",
  }
  if (typeof document === "undefined") return fallback
  const canvas = document.createElement("canvas")
  canvas.width = canvas.height = 1
  const ctx = canvas.getContext("2d", { willReadFrequently: true })
  if (!ctx) return fallback
  const rootStyle = getComputedStyle(document.documentElement)

  const chart = PALETTE_VARS.map(
    (v, i) => resolveVar(ctx, rootStyle, v) || fallback.chart[i]
  )
  const out = { chart }
  for (const [key, cssVar] of Object.entries(UI_VARS)) {
    out[key] = resolveVar(ctx, rootStyle, cssVar) || fallback[key]
  }
  return out
}

// Neutralize a "</script>" sequence so an embedded string can't close the inline
// <script> element early (an HTML-parser breakout, distinct from CSP). Applied to
// both the vendored lib and the model's code before embedding in srcdoc.
function neutralizeScriptClose(s) {
  return String(s).replace(/<\/(script)/gi, "<\\/$1")
}

// The in-frame helper (as source text): defines renderChart(el, spec) on top of
// the inlined Chart.js global, applying the injected palette + theme so every
// chart matches the app. Kept as a template string (not a real function) because
// it must run INSIDE the frame, not in this bundle.
function helperSource() {
  return `
  // Concrete rgb triples injected by the parent (see resolveChartPalette).
  var P = window.__OKF_PALETTE__ || {};
  var SERIES = P.chart || ["59,130,246"];
  function rgb(triple, a) {
    return a == null ? "rgb(" + triple + ")" : "rgba(" + triple + ", " + a + ")";
  }
  function seriesColor(i) { return SERIES[i % SERIES.length]; }

  // HOVER emphasis for solid categorical fills: Chart.js's default hover shift
  // (helpers.getHoverColor) moves the color a few percent — it reads as "no
  // change" on our theme fills in BOTH modes. Mix the fill a quarter of the
  // way toward the theme FOREGROUND instead: clearly darker in light mode,
  // clearly lighter in dark, with no alpha games (an alpha'd hover would blend
  // with the page behind the frame and drift between themes).
  function mixTriple(a, b, t) {
    var pa = String(a).split(",").map(Number);
    var pb = String(b).split(",").map(Number);
    return pa.map(function (v, i) { return Math.round(v + (pb[i] - v) * t); }).join(", ");
  }
  function hoverTriple(triple) {
    return mixTriple(triple, P.foreground || "23,23,23", 0.25);
  }

  // Locale separators for TOOLTIP values ("3545697" -> "3,545,697"): Chart.js
  // formats axis ticks with Intl but tooltips print the raw parsed number.
  // Non-numeric values pass through untouched.
  function fmtNum(v) {
    var n = typeof v === "number" ? v
      : (typeof v === "string" && v.trim() !== "" ? Number(v) : NaN);
    if (typeof n !== "number" || !isFinite(n)) return v == null ? "" : String(v);
    return n.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }

  // WCAG relative luminance of an "r,g,b" triple — picks readable label ink for
  // text drawn ON a solid series fill (treemap tiles). Threshold 0.19 is where
  // white-on-fill and dark-on-fill contrast cross over.
  function relLumOf(triple) {
    var lin = triple.split(",").map(function (n) {
      var c = Number(n) / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
  }
  function inkOn(triple) {
    return relLumOf(triple) < 0.19 ? "rgba(255,255,255,0.95)" : "rgba(0,0,0,0.78)";
  }

  // Gridline / axis color. We DON'T use --border here: in light mode that token is
  // near-white (~oklch 0.925), so gridlines on the near-white page were invisible
  // ("can't distinguish the lines"). --muted-foreground is a mid-gray with contrast
  // against BOTH the light and dark surfaces, so a low alpha of it gives a subtle
  // gridline that's actually visible in both themes.
  var GRID = P.mutedForeground || P.border || "115,115,115";
  function gridColor(a) { return rgb(GRID, a); }

  // App-matched Chart.js defaults: text in the app's foreground, subtle gridlines,
  // legend/tooltip that read as part of the UI. Font family inherits the frame's
  // (set on <body> to the app stack).
  function applyDefaults(Chart) {
    var C = Chart.defaults;
    C.color = rgb(P.foreground || "23,23,23");
    C.borderColor = gridColor(0.28);
    C.font.family = getComputedStyle(document.body).fontFamily;
    C.font.size = 12;
    C.plugins.legend.labels.color = rgb(P.mutedForeground || "115,115,115");
    C.plugins.legend.labels.boxWidth = 12;
    C.plugins.legend.labels.boxHeight = 12;
    C.plugins.legend.labels.usePointStyle = true;
    C.plugins.tooltip.backgroundColor = rgb(P.card || "255,255,255");
    C.plugins.tooltip.titleColor = rgb(P.foreground || "23,23,23");
    C.plugins.tooltip.bodyColor = rgb(P.foreground || "23,23,23");
    C.plugins.tooltip.borderColor = rgb(P.border || "229,229,229");
    C.plugins.tooltip.borderWidth = 1;
    C.plugins.tooltip.padding = 8;
    C.plugins.tooltip.cornerRadius = 8;
    C.plugins.tooltip.displayColors = true;
    C.plugins.tooltip.usePointStyle = true;
    C.maintainAspectRatio = false;
    C.responsive = true;
  }

  // Map the agent's spec → a Chart.js config. Colors always come from the palette
  // (the spec's own colors are ignored unless the model set them explicitly, which
  // the authoring contract discourages). Supports bar/line/area/pie/doughnut/
  // radar/scatter/bubble/polarArea, the inlined plugin types sankey/treemap/
  // heatmap (matrix)/boxplot, the derived types combo/waterfall/funnel/gauge/
  // histogram (built from bar/doughnut primitives), horizontal bar/line/area
  // (spec.horizontal → indexAxis "y"), and mixed charts (a per-series 'type' of
  // bar|line|area overlaid on the base); anything else throws a clear error the
  // wrapper surfaces. This set must stay in step with SUPPORTED_CHART_TYPES in
  // services/chat/src/chat/charts.py and CHART_WIDGETS in lib/canvasBind.js.
  // options.references -> chartjs-plugin-annotation config: dashed lines at
  // literal values, translucent boxes for from/to bands. Values are config
  // LITERALS (lint-validated) — nothing is derived here.
  function referenceAnnotations(spec) {
    var refs = spec.references;
    if (!refs || !refs.length) return null;
    var out = {};
    refs.forEach(function (ref, i) {
      var axis = ref.axis === "x" ? "x" : "y";
      var labelCfg = ref.label ? {
        display: true, content: String(ref.label),
        position: axis === "y" ? "end" : "start",
        backgroundColor: rgb(P.card || "255,255,255", 0.85),
        color: rgb(P.mutedForeground || "115,115,115"),
        font: { size: 10, weight: "600" }, padding: 3
      } : { display: false };
      if (ref.value != null) {
        out["ref" + i] = {
          type: "line", scaleID: axis, value: ref.value,
          borderColor: rgb(P.foreground || "23,23,23", 0.55),
          borderWidth: 1.5, borderDash: [5, 4], label: labelCfg
        };
      } else {
        var box = { type: "box", backgroundColor: rgb(SERIES[0], 0.08), borderWidth: 0, label: labelCfg };
        if (axis === "y") { box.yMin = ref.from; box.yMax = ref.to; }
        else { box.xMin = ref.from; box.xMax = ref.to; }
        out["ref" + i] = box;
      }
    });
    return out;
  }

  function toConfig(spec) {
    if (!spec || typeof spec !== "object") throw new Error("chart spec must be an object");
    var type = spec.type;
    var labels = spec.labels || [];
    var series = spec.series || [];
    if (!Array.isArray(series) || series.length === 0) throw new Error("chart spec needs a non-empty 'series' array");

    // shadcn-style axes: NO axis border lines, NO tick labels on the VALUE axis
    // (values live in the tooltip), only faint gridlines along it; CATEGORY
    // labels in muted gray. spec.horizontal swaps which physical axis is which
    // (indexAxis "y"), so the two configs mirror each other.
    var mutedTicks = { color: rgb(P.mutedForeground || "115,115,115") };
    var scalesLinear = {
      x: {
        title: spec.xLabel ? { display: true, text: spec.xLabel } : { display: false },
        stacked: !!spec.stacked,
        grid: { display: false },
        border: { display: false },
        ticks: mutedTicks
      },
      y: {
        title: spec.yLabel ? { display: true, text: spec.yLabel } : { display: false },
        stacked: !!spec.stacked,
        beginAtZero: true,
        grid: { color: gridColor(0.14) },
        border: { display: false },
        ticks: { display: false }
      }
    };
    var scalesHorizontal = {
      x: {
        title: spec.xLabel ? { display: true, text: spec.xLabel } : { display: false },
        stacked: !!spec.stacked,
        beginAtZero: true,
        grid: { color: gridColor(0.14) },
        border: { display: false },
        ticks: { display: false }
      },
      y: {
        title: spec.yLabel ? { display: true, text: spec.yLabel } : { display: false },
        stacked: !!spec.stacked,
        grid: { display: false },
        border: { display: false },
        ticks: mutedTicks
      }
    };

    // Shared finishing for the early-return cartesian branches (heatmap,
    // waterfall, boxplot, scatter/bubble): the same 'references' -> annotation
    // and 'axes' -> value-gridline toggles the bar/line fallthrough applies.
    // An option the lint blesses must RENDER on every widget that declares it
    // — these branches used to return before either was applied, so a target
    // line the author (and the CI's screenshot loop) asked for never appeared.
    function applyCartesianOpts(opts, spec) {
      var annos = referenceAnnotations(spec);
      if (annos) {
        opts.plugins = opts.plugins || {};
        opts.plugins.annotation = { annotations: annos };
      }
      if (spec.axes != null && !spec.axes && opts.scales && opts.scales.y) {
        opts.scales.y.grid = { display: false };
      }
      return opts;
    }

    // Slice/spoke tooltips (pie/doughnut/polarArea/radar): the category name
    // plus the locale-formatted value (defaults printed the raw number).
    function _sliceLabel(c) {
      var p = c.parsed;
      var v = p != null && typeof p === "object" ? p.r : p;
      return (c.label ? c.label + ": " : "") + fmtNum(v);
    }

    // A SQL NULL is a data fact, not a zero: coercing it fabricates a number
    // the query never returned (the module contract is "the values shown are
    // the query's numbers untouched"). Null/NaN map to null — Chart.js and
    // the branch code render a gap instead.
    function numOrNull(v) {
      if (v == null || v === "") return null;
      var n = Number(v);
      return isFinite(n) ? n : null;
    }

    if (type === "heatmap") {
      // series[0].data = [{x, y, v}]; first-seen category orders injected as
      // xCats/yCats; cell color = primary ramp scaled to the value range.
      // Null cells paint a faint neutral (visibly "no data", never a
      // saturated "low") and don't shape the ramp.
      var hd = (series[0] || {}).data || [];
      var xs = spec.xCats || [], ys = spec.yCats || [];
      var vmax = -Infinity, vmin = Infinity;
      hd.forEach(function (pt) {
        var v = numOrNull(pt && pt.v);
        if (v == null) return;
        if (v > vmax) vmax = v; if (v < vmin) vmin = v;
      });
      if (!isFinite(vmin)) { vmin = 0; vmax = 1; }
      return {
        type: "matrix",
        data: { datasets: [{
          label: spec.title || "",
          data: hd,
          backgroundColor: function (c) {
            var v = numOrNull((c.raw || {}).v);
            if (v == null) return rgb(P.mutedForeground || "115,115,115", 0.07);
            var t = vmax > vmin ? (v - vmin) / (vmax - vmin) : 1;
            return rgb(SERIES[0], 0.12 + 0.78 * t);
          },
          borderColor: rgb(P.card || "255,255,255"),
          borderWidth: 1,
          // Hover = an OUTLINE, not a fill shift: the cell's fill intensity
          // encodes the value, so changing it on hover would lie about the data.
          hoverBorderColor: rgb(P.foreground || "23,23,23"),
          hoverBorderWidth: 2,
          width: function (c) { var a = c.chart.chartArea; return a ? Math.max(4, (a.right - a.left) / Math.max(1, xs.length) - 2) : 8; },
          height: function (c) { var a = c.chart.chartArea; return a ? Math.max(4, (a.bottom - a.top) / Math.max(1, ys.length) - 2) : 8; }
        }] },
        options: applyCartesianOpts({
          scales: {
            x: { type: "category", labels: xs, offset: true, grid: { display: false }, border: { display: false }, ticks: mutedTicks,
                 title: spec.xLabel ? { display: true, text: spec.xLabel } : { display: false } },
            y: { type: "category", labels: ys, offset: true, grid: { display: false }, border: { display: false }, ticks: mutedTicks,
                 title: spec.yLabel ? { display: true, text: spec.yLabel } : { display: false } }
          },
          plugins: { legend: { display: false }, tooltip: { callbacks: {
            title: function () { return ""; },
            label: function (c) { var r = c.raw || {}; return r.x + " / " + r.y + ": " + (r.v == null ? "no data" : fmtNum(r.v)); }
          } } }
        }, spec)
      };
    }

    if (type === "waterfall") {
      // Floating bars from SQL DELTAS: bar POSITIONS are running sums (layout
      // arithmetic only); the values shown are the query's numbers untouched.
      // spec.totals[i] marks running-total rows (drawn from zero, neutral ink).
      var wd = ((series[0] || {}).data || []).map(numOrNull);
      var totals = spec.totals || [];
      var run = 0;
      var bars = wd.map(function (d, i) {
        if (d == null) return null; // unknown delta: a GAP, not a +0 step
        if (totals[i]) { run = d; return [0, d]; }
        var seg = [run, run + d]; run += d; return seg;
      });
      var wcolors = wd.map(function (d, i) {
        if (d == null) return "transparent";
        if (totals[i]) return rgb(P.foreground || "23,23,23", 0.7);
        return d >= 0 ? rgb(SERIES[2], 0.9) : rgb(SERIES[9], 0.9);
      });
      // Hover: hue toward foreground for the +/- bars; totals (already
      // foreground ink) just firm up their alpha.
      var whover = wd.map(function (d, i) {
        if (d == null) return "transparent";
        if (totals[i]) return rgb(P.foreground || "23,23,23", 0.95);
        return rgb(hoverTriple(d >= 0 ? SERIES[2] : SERIES[9]), 0.9);
      });
      var wOpts = applyCartesianOpts({
        scales: scalesLinear,
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          label: function (c) { return (totals[c.dataIndex] ? "total: " : "") + fmtNum(wd[c.dataIndex]); }
        } } }
      }, spec);
      return {
        type: "bar",
        data: { labels: labels, datasets: [{ label: spec.title || "", data: bars, backgroundColor: wcolors, hoverBackgroundColor: whover, borderWidth: 0, borderRadius: 3 }] },
        options: wOpts
      };
    }

    if (type === "funnel") {
      // Symmetric floating horizontal bars — a funnel with no plugin. Bars
      // center on 0 spanning [-v/2, +v/2]; the value axis is hidden (values
      // live in the tooltip, with the %-of-first conversion).
      var fd = ((series[0] || {}).data || []).map(numOrNull);
      var first = 1;
      for (var fi = 0; fi < fd.length; fi++) {
        if (fd[fi] != null) { first = fd[fi] || 1e-9; break; }
      }
      // Stage fade shared by fill + hover: the hover keeps each stage's alpha
      // (the fade IS the funnel reading) and shifts only the hue.
      var fAlpha = fd.map(function (_, i) {
        return Math.max(0.25, 1 - i * (0.65 / Math.max(1, fd.length - 1)));
      });
      return {
        type: "bar",
        data: { labels: labels, datasets: [{
          label: spec.title || "",
          data: fd.map(function (v) { return v == null ? null : [-v / 2, v / 2]; }),
          backgroundColor: fd.map(function (_, i) { return rgb(SERIES[0], fAlpha[i]); }),
          hoverBackgroundColor: fd.map(function (_, i) { return rgb(hoverTriple(SERIES[0]), fAlpha[i]); }),
          borderWidth: 0, borderRadius: 4, barPercentage: 0.98, categoryPercentage: 0.95
        }] },
        options: {
          indexAxis: "y",
          scales: {
            x: { display: false },
            y: { grid: { display: false }, border: { display: false }, ticks: mutedTicks }
          },
          plugins: { legend: { display: false }, tooltip: { callbacks: {
            label: function (c) {
              var v = fd[c.dataIndex];
              return fmtNum(v) + " (" + Math.round((v / first) * 100) + "% of " + (labels[0] || "first") + ")";
            }
          } } }
        }
      };
    }

    if (type === "gauge") {
      // Semicircle doughnut over [min, max]; the pre-formatted value is drawn
      // in the arc's mouth by the okfGaugeText plugin.
      var gmin = Number(spec.min) || 0;
      var gmax = Number(spec.max);
      var gval = Number(((series[0] || {}).data || [])[0]);
      var frac = Math.max(0, Math.min(1, (gval - gmin) / ((gmax - gmin) || 1)));
      return {
        type: "doughnut",
        data: { labels: [spec.title || "", ""], datasets: [{
          data: [frac, 1 - frac],
          backgroundColor: [rgb(SERIES[0]), rgb(P.mutedForeground || "115,115,115", 0.15)],
          borderWidth: 0
        }] },
        options: {
          rotation: -90, circumference: 180, cutout: "72%",
          okfGauge: { text: spec.text != null ? String(spec.text) : String(gval) },
          plugins: { legend: { display: false }, tooltip: { enabled: false } }
        }
      };
    }

    if (type === "boxplot") {
      // PRE-COMPUTED five-number summaries per category (from the SQL) — the
      // plugin renders stats, it never derives them from raw values.
      return {
        type: "boxplot",
        data: { labels: labels, datasets: [{
          label: spec.title || "",
          data: (series[0] || {}).data || [],
          backgroundColor: rgb(SERIES[0], 0.25),
          hoverBackgroundColor: rgb(SERIES[0], 0.45),
          borderColor: rgb(SERIES[0]),
          borderWidth: 1.5,
          outlierBackgroundColor: rgb(SERIES[0])
        }] },
        options: applyCartesianOpts(
          { scales: scalesLinear, plugins: { legend: { display: false } } },
          spec
        )
      };
    }

    if (type === "pie" || type === "doughnut" || type === "polarArea") {
      var s0 = series[0] || { data: [] };
      // polarArea slices overlap the radial grid, so they get a light alpha
      // (like radar fills); pie/doughnut slices stay solid.
      var polar = type === "polarArea";
      return {
        type: type,
        data: { labels: labels, datasets: [{
          label: s0.name || "",
          data: s0.data || [],
          backgroundColor: (s0.data || []).map(function (_, i) { return rgb(seriesColor(i), polar ? 0.7 : undefined); }),
          hoverBackgroundColor: (s0.data || []).map(function (_, i) { return rgb(hoverTriple(seriesColor(i)), polar ? 0.7 : undefined); }),
          borderColor: rgb(P.card || "255,255,255"),
          borderWidth: 2
        }] },
        options: polar
          ? { plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: _sliceLabel } },
              },
              scales: { r: { grid: { color: gridColor(0.16) }, ticks: { display: false } } } }
          : { plugins: { legend: { position: "right" }, tooltip: { callbacks: { label: _sliceLabel } } } }
      };
    }

    if (type === "scatter" || type === "bubble") {
      // Same shape; bubble points carry {x, y, r} and get a light alpha so
      // overlapping bubbles stay readable.
      var bubble = type === "bubble";
      return {
        type: type,
        data: { datasets: series.map(function (s, i) {
          return { label: s.name || ("Series " + (i + 1)), data: s.data || [],
                   backgroundColor: rgb(seriesColor(i), bubble ? 0.6 : undefined),
                   borderColor: rgb(seriesColor(i)) };
        }) },
        options: applyCartesianOpts({
          scales: scalesLinear,
          plugins: { tooltip: { callbacks: {
            label: function (c) {
              var p = c.parsed || {};
              var r = bubble && c.raw && c.raw.r != null ? ", r " + fmtNum(c.raw.r) : "";
              return (c.dataset.label ? c.dataset.label + ": " : "")
                + "(" + fmtNum(p.x) + ", " + fmtNum(p.y) + r + ")";
            }
          } } }
        }, spec)
      };
    }

    if (type === "sankey") {
      // Flow diagram (chartjs-chart-sankey, inlined + self-registered in this
      // frame). One series; data = [{from, to, flow}] edges. Nodes are colored
      // by first appearance so a node keeps ONE palette color on both sides of
      // its flows; the link is a gradient between its endpoints' colors.
      // Optional per-series extras:
      //   colors: { nodeName: paletteIndex | "muted" } — semantic node colors
      //     (a pass node can be the palette's green regardless of appearance
      //     order);
      //   column: { nodeName: columnIndex } — pin nodes to stages, so a sink
      //     that terminates early ("Passed") sits beside its stage peers
      //     instead of being pushed to the last column.
      var sk = series[0] || { data: [] };
      var flows = sk.data || [];
      var nodes = [];
      flows.forEach(function (f) {
        if (f && f.from != null && nodes.indexOf(f.from) === -1) nodes.push(f.from);
        if (f && f.to != null && nodes.indexOf(f.to) === -1) nodes.push(f.to);
      });
      var colorSpec = sk.colors || {};
      var nodeColor = function (name) {
        // FULL opacity: an alpha fill here blends with the page behind the
        // frame, so the same series color rendered lighter in light mode and
        // darker in dark mode — the color must be the theme token itself.
        var c = colorSpec[name];
        if (c === "muted") return rgb(P.mutedForeground || "115,115,115");
        if (typeof c === "number") return rgb(SERIES[c % SERIES.length]);
        if (typeof c === "string" && c.indexOf(",") > -1) return rgb(c); // raw "r, g, b"
        return rgb(seriesColor(Math.max(0, nodes.indexOf(name))));
      };
      var skDs = {
        label: sk.name || "",
        data: flows,
        colorFrom: function (c) { return nodeColor(c.dataset.data[c.dataIndex].from); },
        colorTo: function (c) { return nodeColor(c.dataset.data[c.dataIndex].to); },
        colorMode: "gradient",
        borderWidth: 0,
        color: rgb(P.foreground || "23,23,23") // node label text
      };
      if (sk.column) skDs.column = sk.column;
      if (sk.priority) skDs.priority = sk.priority;
      // Layout knobs (plugin options): nodePadding spaces the nodes within a
      // column — larger values shrink the flow scale, so a single-source
      // sankey branches visibly instead of drawing one edge-to-edge block.
      if (sk.nodePadding != null) skDs.nodePadding = sk.nodePadding;
      if (sk.nodeWidth != null) skDs.nodeWidth = sk.nodeWidth;
      return {
        type: "sankey",
        data: { datasets: [skDs] },
        options: { plugins: { legend: { display: false }, tooltip: { callbacks: {
          label: function (c) {
            var f = (c.dataset.data || [])[c.dataIndex] || {};
            return f.from + " → " + f.to + ": " + fmtNum(f.flow);
          }
        } } } }
      };
    }

    if (type === "treemap") {
      // Share-of-total rectangles (chartjs-chart-treemap). One series; data =
      // [{label, value}] leaves, optionally with a 'group' field for one
      // nesting level. PLUGIN SEMANTICS (non-obvious, from its group() source):
      // at every node, raw._data.label is that LEVEL's value (the group name on
      // a header rect, the leaf name on a leaf rect) and raw._data.group is the
      // grouping KEY NAME ("group" on headers / "label" on leaves) — so colors
      // key off a precomputed label→group map, never off _data.group.
      var tm = series[0] || { data: [] };
      var items = tm.data || [];
      // Optional colors: { label: paletteIndex | "muted" } — semantic tile
      // colors (pass-green / gap-red) instead of appearance order.
      var tmColors = tm.colors || {};
      function tileTriple(label, fallbackIdx) {
        var c = tmColors[label];
        if (c === "muted") return P.mutedForeground || "115,115,115";
        if (typeof c === "number") return SERIES[c % SERIES.length];
        if (typeof c === "string" && c.indexOf(",") > -1) return c; // raw "r, g, b"
        return seriesColor(fallbackIdx);
      }
      var hasGroups = items.some(function (d) { return d && d.group != null; });
      var groupNames = [];
      var groupOf = {}; // any node label (group OR leaf name) → palette index
      items.forEach(function (d) {
        var g = d && (hasGroups ? d.group : d.label);
        if (g != null && groupNames.indexOf(g) === -1) groupNames.push(g);
      });
      groupNames.forEach(function (g, i) { groupOf[g] = i; });
      items.forEach(function (d) {
        if (d && d.label != null && groupOf[d.label] == null) {
          groupOf[d.label] = hasGroups ? (groupOf[d.group] || 0) : 0;
        }
      });
      var nodeIdx = function (c) {
        var o = (c.raw && c.raw._data) || {};
        return groupOf[o.label] != null ? groupOf[o.label] : 0;
      };
      return {
        type: "treemap",
        data: { datasets: [{
          label: tm.name || "",
          tree: items,
          key: "value",
          groups: hasGroups ? ["group", "label"] : ["label"],
          // No border strokes — separation comes from spacing alone (the gaps
          // show the page surface), which reads cleaner on the flat tints.
          borderWidth: 0,
          borderRadius: 4,
          spacing: 1.5,
          backgroundColor: function (c) {
            if (c.type !== "data") return "transparent";
            var o = (c.raw && c.raw._data) || {};
            // Only the LEAVES carry color. The group header rect is left
            // TRANSPARENT — its saturated fill otherwise competes with the
            // leaves it contains; the caption + the shared leaf hue already
            // convey the grouping. Leaves still read as one color family per
            // group via nodeIdx. FULL opacity on the fill: the old 0.75 alpha
            // blended the tile with the page behind the frame, so the same
            // palette slot rendered as a light tint in light mode and a dark
            // shade in dark mode — the tile must BE the theme's series color.
            var isHeader = hasGroups && o.group === "group";
            return isHeader ? "transparent" : rgb(tileTriple(o.label, nodeIdx(c)));
          },
          labels: {
            display: true,
            // Per-tile ink: solid fills from the categorical palette span
            // light (amber) to dark (cyan) — fixed white text would wash out
            // on the light ones, so pick by the FILL's luminance.
            color: function (c) {
              var o = (c.raw && c.raw._data) || {};
              return c.type === "data"
                ? inkOn(tileTriple(o.label, nodeIdx(c)))
                : "transparent";
            },
            formatter: function (c) {
              var o = (c.raw && c.raw._data) || {};
              if (o.label == null) return "";
              // Two lines: name + value (the number is the point of a treemap).
              var v = o.value != null ? o.value : c.raw && c.raw.v;
              return typeof v === "number"
                ? [String(o.label), v.toLocaleString()]
                : String(o.label);
            }
          },
          captions: {
            display: true,
            // The header band is now transparent (draws on the chart/page
            // surface, not a filled rect), so the caption uses the app
            // foreground rather than white — readable in both themes.
            color: rgb(P.foreground || "23,23,23"),
            // The default caption prints the grouping KEY NAME ("group") —
            // show the actual group value (this level's label) instead.
            formatter: function (c) {
              var o = (c.raw && c.raw._data) || {};
              return o.label != null ? String(o.label) : "";
            }
          }
        }] },
        options: { plugins: { legend: { display: false }, tooltip: { callbacks: {
          // The plugin default titles "value" (the key name) and prints the
          // raw number — show "name: 1,284,099" per hovered level instead.
          title: function () { return ""; },
          label: function (c) {
            var o = (c.raw && c.raw._data) || {};
            var v = c.raw && c.raw.v;
            var name = o.label != null ? String(o.label) : "";
            return name ? name + ": " + fmtNum(v) : fmtNum(v);
          }
        } } } }
      };
    }

    if (type === "radar") {
      return {
        type: "radar",
        data: { labels: labels, datasets: series.map(function (s, i) {
          return { label: s.name || ("Series " + (i + 1)), data: s.data || [],
                   backgroundColor: rgb(seriesColor(i), 0.2), borderColor: rgb(seriesColor(i)), borderWidth: 2,
                   pointBackgroundColor: rgb(seriesColor(i)) };
        }) },
        // Radial tick labels (the backdropped numbers up the first spoke) are
        // hidden like the y-axis: values live in the tooltip. Point labels
        // (the category names around the web) stay.
        options: {
          scales: { r: { grid: { color: gridColor(0.16) }, angleLines: { color: gridColor(0.16) }, ticks: { display: false } } },
          plugins: { tooltip: { callbacks: {
            label: function (c) {
              return (c.dataset.label ? c.dataset.label + ": " : "")
                + fmtNum(c.parsed && c.parsed.r);
            }
          } } }
        }
      };
    }

    // area = a filled line chart. A series may carry its OWN type (bar|line|
    // area) to overlay on the base — Chart.js mixed charts — e.g. monthly bars
    // with a cumulative line. spec.horizontal flips the index axis (ranked
    // "top N" lists with long category names read better as horizontal bars).
    var isArea = type === "area";
    var isHisto = type === "histogram";
    // combo = bars + lines over one x (per-series types set by the binder);
    // histogram = a gapless bar over pre-binned buckets.
    var chartType = isArea ? "line" : (type === "combo" || isHisto) ? "bar" : type;
    if (chartType !== "line" && chartType !== "bar") {
      throw new Error("unsupported chart type: " + JSON.stringify(type));
    }
    var horizontal = !!spec.horizontal;
    // xTicks:false hides the INDEX-axis tick labels (a per-solve or per-item
    // series has no readable category names — the tooltip carries identity).
    if (spec.xTicks === false) {
      (horizontal ? scalesHorizontal.y : scalesLinear.x).ticks = { display: false };
    }
    // Value-axis chrome (the faint gridlines — ticks are already hidden).
    // Horizontal bars default to NONE: they're usually ranked lists where the
    // category labels carry the story and gridlines are just noise; values
    // live in the tooltip. spec.axes overrides either way (axes: true keeps
    // gridlines on a horizontal chart, axes: false strips them vertically).
    var showAxes = spec.axes != null ? !!spec.axes : !horizontal;
    if (!showAxes) {
      (horizontal ? scalesHorizontal.x : scalesLinear.y).grid = { display: false };
    }
    var datasets = series.map(function (s, i) {
      // Per-series override for mixed charts; anything unrecognized falls
      // back to the spec's base type rather than throwing mid-chart.
      var sIsArea = s.type ? s.type === "area" : isArea;
      var sType = s.type === "bar" ? "bar" : s.type === "line" || sIsArea ? "line" : chartType;
      var base = {
        type: sType,
        label: s.name || ("Series " + (i + 1)),
        data: s.data || [],
        borderColor: rgb(seriesColor(i)),
        backgroundColor: (sType === "bar") ? rgb(seriesColor(i)) : rgb(seriesColor(i), sIsArea ? 0.2 : 1),
        // Bars get NO border: borderColor == fill so it adds nothing, and
        // Chart.js paints a bar border as a second fill whose antialiased
        // seam lets the page background bleed through as a thin inset line
        // (visible on dark fills, worse at the elevated zoom ratios the
        // crispness re-raster uses). Lines keep 2 — that IS the stroke.
        borderWidth: (sType === "bar") ? 0 : 2,
        borderRadius: (sType === "bar") ? 4 : 0,
        // Bars (incl. histogram buckets + combo bars) get the explicit hover
        // fill; lines/areas keep the default point-radius growth.
        hoverBackgroundColor: (sType === "bar") ? rgb(hoverTriple(seriesColor(i))) : undefined,
        tension: 0.3,
        pointRadius: (sType === "line") ? 2 : 0,
        // Mixed charts: lines/areas draw ABOVE bars (Chart.js draws higher
        // 'order' first, i.e. further back) — a combo's trend line was
        // disappearing behind the columns.
        order: (sType === "bar") ? 2 : 1
      };
      if (sIsArea) base.fill = true;
      // Combo lines may ride the right-hand scale (see y2 below).
      if (s.y2) base.yAxisID = "y2";
      if (isHisto) { base.barPercentage = 1; base.categoryPercentage = 1; base.borderRadius = 0; }
      // A reference line (e.g. "average"): dashed, no points, no area.
      if (s.dashed) {
        base.borderDash = [6, 4];
        base.pointRadius = 0;
        base.pointHitRadius = 0;
        base.fill = false;
        base.borderWidth = 1.5;
      }
      // points:false — a dense series reads as a shape, not a dot cloud (the
      // spec's markers carry the callout dots instead).
      if (s.points === false) base.pointRadius = 0;
      // badge: "21.7s" — a value pill pinned to the chart's left edge at this
      // series' (first) y — the okfBadges plugin draws it.
      if (s.badge != null) base.okfBadge = String(s.badge);
      return base;
    });

    // spec.markers = [{index, value, label|label[]}] — callout points (min/max
    // extremes) drawn ON the series: a prominent dot + a dashed drop-line to
    // the x-axis (the okfMarkerLines plugin), with the marker's own tooltip
    // text (the question behind the extreme) instead of the numeric default.
    var markerTooltip = null;
    if (Array.isArray(spec.markers) && spec.markers.length && !horizontal) {
      var mData = labels.map(function () { return null; });
      var mLabels = {};
      spec.markers.forEach(function (m) {
        if (!m || m.index == null) return;
        mData[m.index] = m.value;
        if (m.label != null) mLabels[m.index] = m.label;
      });
      datasets.push({
        type: "line",
        label: "extremes",
        data: mData,
        showLine: false,
        spanGaps: false,
        pointRadius: 5,
        pointHoverRadius: 7,
        pointBackgroundColor: rgb(P.foreground || "23,23,23"),
        pointBorderColor: rgb(P.card || "255,255,255"),
        pointBorderWidth: 1.5,
        borderWidth: 0,
        okfMarker: true,
        okfLabels: mLabels
      });
      markerTooltip = true;
    }

    var lineOpts = horizontal
      ? { indexAxis: "y", scales: scalesHorizontal }
      : { scales: scalesLinear };
    // Line/area hover: nearest-x, no intersect — a dense (or dotless,
    // points:false) series stays hoverable anywhere along its length, with the
    // tooltip titled by that x's label.
    if (chartType === "line") {
      lineOpts.interaction = { mode: "index", intersect: false };
    }
    lineOpts.plugins = lineOpts.plugins || {};
    if (markerTooltip) {
      lineOpts.plugins.legend = { labels: { filter: function (item) { return item.text !== "extremes"; } } };
    }
    // One label callback for every cartesian chart: marker callouts keep
    // their own text; everything else gets the VALUE-axis number with locale
    // separators (fmtNum) — the default tooltip printed it raw.
    lineOpts.plugins.tooltip = {
      callbacks: {
        label: function (ctx) {
          var l = ctx.dataset && ctx.dataset.okfLabels && ctx.dataset.okfLabels[ctx.dataIndex];
          if (l != null) return l;
          var p = ctx.parsed || {};
          var v = horizontal ? p.x : p.y;
          return (ctx.dataset.label ? ctx.dataset.label + ": " : "") + fmtNum(v);
        }
      }
    };
    // A combo's lines on the RIGHT scale (grid off — the left grid rules).
    if (!horizontal && series.some(function (s) { return s.y2; })) {
      lineOpts.scales = Object.assign({}, lineOpts.scales);
      lineOpts.scales.y2 = {
        position: "right",
        title: spec.y2Label ? { display: true, text: spec.y2Label } : { display: false },
        beginAtZero: true,
        grid: { display: false },
        border: { display: false },
        ticks: { display: false }
      };
    }
    var annos = referenceAnnotations(spec);
    if (annos) {
      lineOpts.plugins = lineOpts.plugins || {};
      lineOpts.plugins.annotation = { annotations: annos };
    }
    return {
      type: chartType,
      data: { labels: labels, datasets: datasets },
      options: lineOpts
    };
  }

  // Dashed drop-lines from each marker point (spec.markers) down to the x-axis
  // — the "line emerging from the peak" callout. Registered per-chart via the
  // okfMarker flag, so ordinary charts pay nothing.
  var okfBadges = {
    id: "okfBadges",
    afterDatasetsDraw: function (chart) {
      var area = chart.chartArea;
      chart.data.datasets.forEach(function (ds, di) {
        if (!ds.okfBadge) return;
        var meta = chart.getDatasetMeta(di);
        var pt = (meta.data || [])[0];
        if (!pt) return;
        var ctx = chart.ctx;
        var text = ds.okfBadge;
        ctx.save();
        ctx.font = "600 10px " + getComputedStyle(document.body).fontFamily;
        var w = Math.ceil(ctx.measureText(text).width) + 12;
        var h = 17;
        var x = area.left + 2;
        var yy = Math.max(area.top + h / 2, Math.min(area.bottom - h / 2, pt.y));
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x, yy - h / 2, w, h, 6);
        else ctx.rect(x, yy - h / 2, w, h);
        ctx.fillStyle = rgb(P.card || "255,255,255");
        ctx.fill();
        ctx.strokeStyle = gridColor(0.5);
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = rgb(P.foreground || "23,23,23");
        ctx.textBaseline = "middle";
        ctx.fillText(text, x + 6, yy + 0.5);
        ctx.restore();
      });
    }
  };

  var okfMarkerLines = {
    id: "okfMarkerLines",
    afterDatasetsDraw: function (chart) {
      var y = chart.scales && chart.scales.y;
      if (!y) return;
      chart.data.datasets.forEach(function (ds, di) {
        if (!ds.okfMarker) return;
        var meta = chart.getDatasetMeta(di);
        var ctx = chart.ctx;
        (meta.data || []).forEach(function (pt, i) {
          if (ds.data[i] == null || !pt) return;
          ctx.save();
          ctx.strokeStyle = gridColor(0.55);
          ctx.setLineDash([4, 3]);
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(pt.x, pt.y);
          ctx.lineTo(pt.x, y.getPixelForValue(y.min));
          ctx.stroke();
          ctx.restore();
        });
      });
    }
  };

  var _chart = null;
  // Crisp-on-zoom: the canvas is a raster, so it blurs when the browser zooms past
  // the ratio it was drawn at. We (a) draw at an ELEVATED devicePixelRatio (>= 2, or
  // the device's own if higher) so there's resolution headroom, and (b) re-render
  // when the effective ratio changes (page zoom on the desktop bumps
  // window.devicePixelRatio; pinch-zoom bumps visualViewport.scale) so it re-rasters
  // sharp at the new zoom instead of upscaling the old bitmap.
  // Pinch-zoom scale forwarded by the HOST page (see the "zoom" message below):
  // pinch zoom is a visual-viewport scale only the TOP-LEVEL window can see —
  // in this frame devicePixelRatio is unchanged and visualViewport.scale reads 1.
  var _hostScale = 1;
  function targetRatio() {
    var base = window.devicePixelRatio || 1;
    var vp = (window.visualViewport && window.visualViewport.scale) || 1;
    if (_hostScale > vp) vp = _hostScale; // max, not product — never double-count
    // Ceiling 8 = crisp up to 400% browser zoom on a 2x display. The cost is
    // backing-store memory (CSS w x h x ratio^2 x 4B — ~70MB for a chat-sized
    // chart at 8) but it's TRANSIENT: zooming back down re-rasters smaller.
    // A cap of 4 looked identical at 300%+ zoom no matter how faithfully the
    // ratio watchers re-rendered — the clamp, not stale rasters, was the blur.
    return Math.min(8, Math.max(2, base * vp));
  }
  // Draws the gauge's pre-formatted value in the semicircle's mouth.
  var okfGaugeText = {
    id: "okfGaugeText",
    afterDraw: function (chart) {
      var g = chart.options && chart.options.okfGauge;
      if (!g || !g.text) return;
      var area = chart.chartArea;
      var ctx = chart.ctx;
      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = rgb(P.foreground || "23,23,23");
      ctx.font = "600 " + Math.max(16, Math.round((area.bottom - area.top) / 4)) + "px " + getComputedStyle(document.body).fontFamily;
      ctx.fillText(g.text, (area.left + area.right) / 2, area.bottom - 4);
      ctx.restore();
    }
  };

  var _lastRatio = 0;
  var _lastSpec = null;
  window.renderChart = function (el, spec) {
    if (!window.Chart) throw new Error("charting library failed to load");
    if (!el) throw new Error("renderChart needs the provided canvas element");
    applyDefaults(window.Chart);
    try { window.Chart.register(okfMarkerLines, okfBadges, okfGaugeText); } catch (e) {}
    // annotation/matrix/boxplot UMDs expose globals without reliably
    // self-registering; register() dedupes, so this is safe every call.
    try {
      var annP = window["chartjs-plugin-annotation"];
      if (annP) window.Chart.register(annP.default || annP);
      var mxP = window["chartjs-chart-matrix"];
      if (mxP) window.Chart.register(mxP.MatrixController, mxP.MatrixElement);
      var bxP = window.ChartBoxPlot;
      if (bxP) window.Chart.register(bxP.BoxPlotController, bxP.BoxAndWiskers);
    } catch (e) {}
    if (_chart) { _chart.destroy(); _chart = null; }
    _lastSpec = spec;
    var cfg = toConfig(spec);
    if (spec && spec.title) {
      cfg.options = cfg.options || {};
      cfg.options.plugins = cfg.options.plugins || {};
      cfg.options.plugins.title = { display: true, text: spec.title, color: rgb(P.foreground || "23,23,23"),
        font: { size: 13, weight: "600" }, padding: { bottom: 12 } };
    }
    cfg.options = cfg.options || {};
    _lastRatio = targetRatio();
    cfg.options.devicePixelRatio = _lastRatio;
    _chart = new window.Chart(el, cfg);
    return _chart;
  };

  // Watch for zoom changes and re-draw at the new pixel ratio. A plain
  // chart.resize() reuses the cached ratio, so when the ratio itself changed we
  // rebuild the whole chart (cheap — it's already in memory) to re-raster sharp.
  (function () {
    var scheduled = false;
    function onZoom() {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(function () {
        scheduled = false;
        if (!_chart || !_lastSpec) return;
        var r = targetRatio();
        if (Math.abs(r - _lastRatio) > 0.01) {
          var el = document.getElementById("okf-canvas");
          if (el) window.renderChart(el, _lastSpec);
        } else {
          _chart.resize();
        }
      });
    }
    window.addEventListener("resize", onZoom);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", onZoom);
      window.visualViewport.addEventListener("scroll", onZoom);
    }
    // Desktop Cmd+/- zoom changes devicePixelRatio WITHOUT firing either
    // listener above inside this frame (the frame's CSS-px size is unchanged),
    // and dPR changes emit no event of their own — so the chart stayed at its
    // birth raster and the browser upscaled the bitmap (blurry). The standard
    // detector: a one-shot matchMedia on the CURRENT resolution fires when it
    // stops matching; re-register at each new ratio to keep watching.
    (function watchDpr() {
      try {
        var mq = window.matchMedia("(resolution: " + (window.devicePixelRatio || 1) + "dppx)");
        var rearm = function () {
          onZoom();
          watchDpr();
        };
        if (mq.addEventListener) mq.addEventListener("change", rearm, { once: true });
        else if (mq.addListener) mq.addListener(rearm); // older WebKit
      } catch (e) {}
    })();
    // Pinch zoom: invisible to every watcher above (dPR unchanged, THIS frame's
    // visualViewport.scale stays 1, and nothing resizes — pinch doesn't reflow).
    // The host page watches its top-level visualViewport and forwards the scale
    // (ChartFrame's pinch-zoom bridge). Only the parent window is trusted — a
    // sibling chart frame reaching us via window.parent.frames fails the
    // e.source check, and the model's own code already runs in-frame anyway.
    window.addEventListener("message", function (e) {
      if (e.source !== window.parent) return;
      var d = e.data;
      if (!d || d.source !== "okf-chart-host" || d.type !== "zoom") return;
      var s = Number(d.scale);
      if (!isFinite(s) || s <= 0 || Math.abs(s - _hostScale) < 0.01) return;
      _hostScale = s;
      onZoom();
    });
  })();

  // Export: the host asks for a PNG of the current chart (the kebab menu's
  // copy/download — see lib/visualExport.js). The visible raster is tuned to
  // the SCREEN (its ratio tracks zoom/dPR), so we re-render OFFSCREEN at the
  // requested export ratio instead of reading the live canvas, composite the
  // theme surface underneath (the frame body is transparent — a bare export
  // would paste invisibly on white or dark), and post the dataURL back. The
  // host may pass a title (canvas tiles keep theirs in the card header, not
  // the spec) so the exported image is self-describing.
  window.addEventListener("message", function (e) {
    if (e.source !== window.parent) return;
    var d = e.data;
    if (!d || d.source !== "okf-chart-host" || d.type !== "export") return;
    function reply(msg) {
      try { parent.postMessage(Object.assign({ source: "okf-chart", status: "export", id: d.id }, msg), "*"); } catch (err) {}
    }
    var tmp = null;
    try {
      if (!_chart || !_lastSpec) { reply({ error: "no chart rendered yet" }); return; }
      var w = Math.max(1, Math.round(_chart.width));
      var h = Math.max(1, Math.round(_chart.height));
      var scale = Math.min(4, Math.max(1, Number(d.scale) || 2));
      var c = document.createElement("canvas");
      c.width = w; c.height = h; // responsive:false → these ARE the display size
      var cfg = toConfig(_lastSpec);
      cfg.options = cfg.options || {};
      cfg.options.plugins = cfg.options.plugins || {};
      var t = _lastSpec.title || d.title;
      if (t) {
        cfg.options.plugins.title = { display: true, text: String(t), color: rgb(P.foreground || "23,23,23"),
          font: { size: 13, weight: "600" }, padding: { bottom: 12 } };
      }
      cfg.options.responsive = false;
      cfg.options.animation = false; // draw synchronously so toDataURL sees ink
      cfg.options.devicePixelRatio = scale;
      tmp = new window.Chart(c, cfg);
      // bg "transparent" skips the surface fill (clipboard/SVG exports paste
      // onto arbitrary backgrounds); note the INK stays theme-resolved — a
      // dark-mode chart carries near-white text wherever it lands.
      if (d.bg !== "transparent") {
        var ctx = c.getContext("2d");
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.globalCompositeOperation = "destination-over";
        ctx.fillStyle = rgb((d.bg === "background" ? P.background : P.card) || "255,255,255");
        ctx.fillRect(0, 0, c.width, c.height);
        ctx.restore();
      }
      reply({ dataUrl: c.toDataURL("image/png"), width: w, height: h });
    } catch (err) {
      reply({ error: (err && err.message) ? String(err.message) : "export failed" });
    } finally {
      if (tmp) { try { tmp.destroy(); } catch (err) {} }
    }
  });
  `
}

// The bootstrap that runs the agent's code + reports height/status to the parent.
// Wrapped in try/catch so a throwing chart becomes a clean error message, not a
// silent blank frame. `el` is the canvas the model draws into.
function bootstrapSource(userCode) {
  return `
  (function () {
    function post(msg) { try { parent.postMessage(Object.assign({ source: "okf-chart" }, msg), "*"); } catch (e) {} }
    function reportHeight() {
      var h = Math.ceil(document.getElementById("wrap").getBoundingClientRect().height);
      post({ status: "size", height: h });
    }
    try {
      var el = document.getElementById("okf-canvas");
      (function (el) {
        ${neutralizeScriptClose(userCode)}
      })(el);
      // Let Chart.js lay out, then report success + height. rAF is the happy
      // path; hidden tabs PAUSE rAF entirely (the chart still draws — only the
      // report would stall until the tab is foregrounded, and the parent's
      // watchdog would misread that as a failure), so a timer fallback reports
      // too — throttled timers still fire in hidden tabs. First one wins.
      var _reportedOk = false;
      function reportOk() {
        if (_reportedOk) return;
        _reportedOk = true;
        post({ status: "ok" });
        reportHeight();
      }
      requestAnimationFrame(function () {
        requestAnimationFrame(reportOk);
      });
      setTimeout(reportOk, 1500);
    } catch (err) {
      post({ status: "error", error: (err && err.message) ? String(err.message) : "chart failed to render" });
    }
    window.addEventListener("resize", reportHeight);
  })();
  `
}

// Build the full srcdoc for one chart. `code` is the agent's script; `palette` is
// the resolved rgb token set from resolveChartPalette(); `fontFamily` matches the
// app so text in the frame reads consistently. `height` is the chart box's CSS
// height — 340 is the chat's reading size; dashboard-style widgets (the
// benchmark summary) pass smaller to stay square-ish.
export function buildChartSrcdoc({ code, palette, fontFamily, height = 340 }) {
  const lib = neutralizeScriptClose(CHART_JS_SRC)
  const sankey = neutralizeScriptClose(CHART_SANKEY_SRC)
  const treemap = neutralizeScriptClose(CHART_TREEMAP_SRC)
  const annotation = neutralizeScriptClose(CHART_ANNOTATION_SRC)
  const matrix = neutralizeScriptClose(CHART_MATRIX_SRC)
  const boxplot = neutralizeScriptClose(CHART_BOXPLOT_SRC)
  const paletteJson = neutralizeScriptClose(JSON.stringify(palette || {}))
  const family = (fontFamily || "system-ui, sans-serif").replace(/"/g, "'")
  // Scrollbar tints for the frame's own document (see the <style> note).
  const scrollThumb = palette?.border || "229, 229, 229"
  const scrollThumbHover = palette?.mutedForeground || "115, 115, 115"
  // CSP: no default sources, inline scripts/styles only (we embed everything), NO
  // network of any kind (connect/img/font/frame all denied) — the frame draws to a
  // canvas and talks to the parent solely via postMessage. This is defense-in-depth
  // on top of the opaque-origin sandbox.
  const csp =
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
    "connect-src 'none'; img-src 'none'; font-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  html, body { margin: 0; padding: 0; background: transparent; }
  #wrap { padding: 4px 2px; box-sizing: border-box; }
  #chartbox { position: relative; width: 100%; height: ${Number(height) || 340}px; }
  body { font-family: ${family}; -webkit-font-smoothing: antialiased; }
  canvas { max-width: 100%; }
  /* The app's global thin scrollbar (index.css), re-declared here because a
     srcdoc iframe is a SEPARATE document — page CSS (and its vars) never
     reach it. Palette literals are the theme tokens resolved at build time. */
  * { scrollbar-width: thin; scrollbar-color: rgb(${scrollThumb}) transparent; }
  *::-webkit-scrollbar { width: 8px; height: 8px; }
  *::-webkit-scrollbar-track { background: transparent; }
  *::-webkit-scrollbar-thumb {
    background-color: rgb(${scrollThumb});
    border-radius: 9999px;
    border: 2px solid transparent;
    background-clip: padding-box;
  }
  *::-webkit-scrollbar-thumb:hover {
    background-color: rgb(${scrollThumbHover});
    background-clip: padding-box;
  }
</style>
</head>
<body>
<div id="wrap"><div id="chartbox"><canvas id="okf-canvas"></canvas></div></div>
<script>window.__OKF_PALETTE__ = ${paletteJson};</script>
<script>${lib}</script>
<script>${sankey}</script>
<script>${treemap}</script>
<script>${annotation}</script>
<script>${matrix}</script>
<script>${boxplot}</script>
<script>${helperSource()}</script>
<script>${bootstrapSource(code)}</script>
</body>
</html>`
}
