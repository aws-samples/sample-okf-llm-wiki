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
// Plugin controllers for the two non-core chart types (see vendor/README.md).
// Their UMD builds self-register against the global `Chart`, so inlining them
// after the core lib is all the wiring the frame needs.
import CHART_SANKEY_SRC from "@/vendor/chartjs-chart-sankey.min.js?raw"
import CHART_TREEMAP_SRC from "@/vendor/chartjs-chart-treemap.min.js?raw"

// The palette + UI tokens we resolve from the app theme and hand to the frame.
// --chart-1..5 are the series palette; the rest style axes/legend/tooltip so the
// chart reads as part of the UI. Names map to CSS custom properties on :root.
const PALETTE_VARS = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5"]
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
    chart: ["59, 130, 246", "16, 185, 129", "245, 158, 11", "139, 92, 246", "236, 72, 153"],
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
  // radar/scatter/bubble/polarArea plus the inlined plugin types sankey/treemap,
  // horizontal bar/line/area (spec.horizontal → indexAxis "y"), and mixed charts
  // (a per-series 'type' of bar|line|area overlaid on the base); anything else
  // throws a clear error the wrapper surfaces.
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
          borderColor: rgb(P.card || "255,255,255"),
          borderWidth: 2
        }] },
        options: polar
          ? { plugins: { legend: { position: "right" } },
              scales: { r: { grid: { color: gridColor(0.16) }, ticks: { display: false } } } }
          : { plugins: { legend: { position: "right" } } }
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
        options: { scales: scalesLinear }
      };
    }

    if (type === "sankey") {
      // Flow diagram (chartjs-chart-sankey, inlined + self-registered in this
      // frame). One series; data = [{from, to, flow}] edges. Nodes are colored
      // by first appearance so a node keeps ONE palette color on both sides of
      // its flows; the link is a gradient between its endpoints' colors.
      var sk = series[0] || { data: [] };
      var flows = sk.data || [];
      var nodes = [];
      flows.forEach(function (f) {
        if (f && f.from != null && nodes.indexOf(f.from) === -1) nodes.push(f.from);
        if (f && f.to != null && nodes.indexOf(f.to) === -1) nodes.push(f.to);
      });
      var nodeColor = function (name) {
        return rgb(seriesColor(Math.max(0, nodes.indexOf(name))), 0.85);
      };
      return {
        type: "sankey",
        data: { datasets: [{
          label: sk.name || "",
          data: flows,
          colorFrom: function (c) { return nodeColor(c.dataset.data[c.dataIndex].from); },
          colorTo: function (c) { return nodeColor(c.dataset.data[c.dataIndex].to); },
          colorMode: "gradient",
          borderWidth: 0,
          color: rgb(P.foreground || "23,23,23") // node label text
        }] },
        options: { plugins: { legend: { display: false } } }
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
            // Only the LEAVES carry color (a solid group-hue tint). The group
            // header rect is left TRANSPARENT — its saturated fill otherwise
            // competes with the leaves it contains; the caption + the shared
            // leaf hue already convey the grouping. Leaves still read as one
            // color family per group via nodeIdx.
            var isHeader = hasGroups && o.group === "group";
            return isHeader ? "transparent" : rgb(seriesColor(nodeIdx(c)), 0.75);
          },
          labels: {
            display: true,
            color: "rgba(255,255,255,0.95)",
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
        options: { plugins: { legend: { display: false } } }
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
        options: { scales: { r: { grid: { color: gridColor(0.16) }, angleLines: { color: gridColor(0.16) }, ticks: { display: false } } } }
      };
    }

    // area = a filled line chart. A series may carry its OWN type (bar|line|
    // area) to overlay on the base — Chart.js mixed charts — e.g. monthly bars
    // with a cumulative line. spec.horizontal flips the index axis (ranked
    // "top N" lists with long category names read better as horizontal bars).
    var isArea = type === "area";
    var chartType = isArea ? "line" : type;
    if (chartType !== "line" && chartType !== "bar") {
      throw new Error("unsupported chart type: " + JSON.stringify(type));
    }
    var horizontal = !!spec.horizontal;
    // Value-axis chrome (the faint gridlines — ticks are already hidden).
    // Horizontal bars default to NONE: they're usually ranked lists where the
    // category labels carry the story and gridlines are just noise; values
    // live in the tooltip. spec.axes overrides either way (axes: true keeps
    // gridlines on a horizontal chart, axes: false strips them vertically).
    var showAxes = spec.axes != null ? !!spec.axes : !horizontal;
    if (!showAxes) {
      (horizontal ? scalesHorizontal.x : scalesLinear.y).grid = { display: false };
    }
    return {
      type: chartType,
      data: { labels: labels, datasets: series.map(function (s, i) {
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
          borderWidth: 2,
          borderRadius: (sType === "bar") ? 4 : 0,
          tension: 0.3,
          pointRadius: (sType === "line") ? 2 : 0
        };
        if (sIsArea) base.fill = true;
        return base;
      }) },
      options: horizontal
        ? { indexAxis: "y", scales: scalesHorizontal }
        : { scales: scalesLinear }
    };
  }

  var _chart = null;
  // Crisp-on-zoom: the canvas is a raster, so it blurs when the browser zooms past
  // the ratio it was drawn at. We (a) draw at an ELEVATED devicePixelRatio (>= 2, or
  // the device's own if higher) so there's resolution headroom, and (b) re-render
  // when the effective ratio changes (page zoom on the desktop bumps
  // window.devicePixelRatio; pinch-zoom bumps visualViewport.scale) so it re-rasters
  // sharp at the new zoom instead of upscaling the old bitmap.
  function targetRatio() {
    var base = window.devicePixelRatio || 1;
    var vp = (window.visualViewport && window.visualViewport.scale) || 1;
    return Math.min(4, Math.max(2, base * vp));
  }
  var _lastRatio = 0;
  var _lastSpec = null;
  window.renderChart = function (el, spec) {
    if (!window.Chart) throw new Error("charting library failed to load");
    if (!el) throw new Error("renderChart needs the provided canvas element");
    applyDefaults(window.Chart);
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
  })();
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
      // Let Chart.js lay out, then report success + height.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { post({ status: "ok" }); reportHeight(); });
      });
    } catch (err) {
      post({ status: "error", error: (err && err.message) ? String(err.message) : "chart failed to render" });
    }
    window.addEventListener("resize", reportHeight);
  })();
  `
}

// Build the full srcdoc for one chart. `code` is the agent's script; `palette` is
// the resolved rgb token set from resolveChartPalette(); `fontFamily` matches the
// app so text in the frame reads consistently.
export function buildChartSrcdoc({ code, palette, fontFamily }) {
  const lib = neutralizeScriptClose(CHART_JS_SRC)
  const sankey = neutralizeScriptClose(CHART_SANKEY_SRC)
  const treemap = neutralizeScriptClose(CHART_TREEMAP_SRC)
  const paletteJson = neutralizeScriptClose(JSON.stringify(palette || {}))
  const family = (fontFamily || "system-ui, sans-serif").replace(/"/g, "'")
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
  #chartbox { position: relative; width: 100%; height: 340px; }
  body { font-family: ${family}; -webkit-font-smoothing: antialiased; }
  canvas { max-width: 100%; }
</style>
</head>
<body>
<div id="wrap"><div id="chartbox"><canvas id="okf-canvas"></canvas></div></div>
<script>window.__OKF_PALETTE__ = ${paletteJson};</script>
<script>${lib}</script>
<script>${sankey}</script>
<script>${treemap}</script>
<script>${helperSource()}</script>
<script>${bootstrapSource(code)}</script>
</body>
</html>`
}
