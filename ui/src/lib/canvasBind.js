// Canvas tile binder: (tile config, execution result) -> the declarative
// renderChart spec chartIframe.js already renders. This is the FIXED
// per-widget renderer the design contracts on — tiles are config, never code,
// so this module plus the widget catalog is the entire rendering surface.
//
// Shape-op semantics MUST match okf_core/canvas.py `apply_shape` exactly (the
// CI lint counts post-shape rows with the Python twin): stable sort, null
// LAST whatever the direction, numeric-LOOKING strings compare numerically
// (Athena returns every value as a string — "45" must never sort above
// "123"), topN = sort desc by the column then head(n).
//
// `table` and `kpi` widgets are not charts — CanvasView renders them natively;
// this module still shapes their rows (shapeResult) but bindChart refuses them.

/** Numeric coercion for values that may arrive as Athena strings. */
export function asNumber(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return v;
  if (typeof v === "boolean") return null;
  const n = Number(v);
  return Number.isFinite(n) && String(v).trim() !== "" ? n : null;
}

function sortKey(v) {
  // [group, numeric, text]: numbers (group 0) before text (group 1).
  if (typeof v !== "boolean") {
    const n = typeof v === "number" || typeof v === "string" ? Number(v) : NaN;
    if (Number.isFinite(n) && String(v).trim() !== "") return [0, n, ""];
    if (typeof v === "string") return [1, 0, v];
  }
  return [1, 0, String(v)];
}

function compareKeys(a, b) {
  if (a[0] !== b[0]) return a[0] - b[0];
  if (a[0] === 0) return a[1] - b[1];
  return a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0;
}

function sortedBy(rows, colIndex, desc) {
  const present = rows.filter((r) => r[colIndex] !== null && r[colIndex] !== undefined);
  const absent = rows.filter((r) => r[colIndex] === null || r[colIndex] === undefined);
  // Decorate with the original index for a stable sort (Array.sort stability
  // is spec-guaranteed in modern engines, but the decoration also lets us
  // flip direction without disturbing ties, mirroring Python's sort).
  const decorated = present.map((r, i) => [sortKey(r[colIndex]), i, r]);
  decorated.sort((x, y) => {
    const c = compareKeys(x[0], y[0]);
    return (desc ? -c : c) || x[1] - y[1];
  });
  return decorated.map((d) => d[2]).concat(absent);
}

/** Column lookup, case-insensitive (Athena lowercases result labels). */
function colIndex(columns, name) {
  const target = String(name).toLowerCase();
  return columns.findIndex((c) => String(c).toLowerCase() === target);
}

/** Apply a tile's shape ops (sort | topN | limit) to result rows. */
export function applyShape(ops, columns, rows) {
  let out = rows.slice();
  for (const op of ops || []) {
    if (op.op === "sort") {
      out = sortedBy(out, colIndex(columns, op.by), op.dir === "desc");
    } else if (op.op === "topN") {
      out = sortedBy(out, colIndex(columns, op.by), true).slice(0, op.n);
    } else if (op.op === "limit") {
      out = out.slice(0, op.n);
    }
  }
  return out;
}

/** {columns, rows} -> shaped rows for a tile (the one entry point CanvasView
 * and the kpi/table renderers share, so counts can't diverge from lint). */
export function shapeResult(tile, result) {
  const columns = result?.columns || [];
  return {
    columns,
    rows: applyShape(tile.shape || [], columns, result?.rows || []),
  };
}

function fields(tile, channel) {
  const enc = tile.encoding?.[channel];
  if (!enc) return [];
  return enc.fields || (enc.field ? [enc.field] : []);
}

function channelLabel(tile, channel, fallback) {
  return tile.encoding?.[channel]?.label || fallback;
}

function cell(row, columns, name) {
  const i = colIndex(columns, name);
  return i >= 0 ? row[i] : null;
}

/** Group rows by a column's value, preserving first-seen order. */
function groupBy(rows, columns, name) {
  const order = [];
  const groups = new Map();
  for (const row of rows) {
    const key = String(cell(row, columns, name));
    if (!groups.has(key)) {
      groups.set(key, []);
      order.push(key);
    }
    groups.get(key).push(row);
  }
  return order.map((key) => [key, groups.get(key)]);
}

const CHART_WIDGETS = new Set([
  "bar", "line", "area", "pie", "doughnut", "polarArea", "radar",
  "scatter", "bubble", "sankey", "treemap",
  "combo", "heatmap", "waterfall", "funnel", "gauge", "histogram", "boxplot",
]);

// Truthy test for a `total`-flag column that may arrive as an Athena string.
const isTruthy = (v) =>
  v === true || v === 1 || ["true", "t", "1", "yes"].includes(String(v).toLowerCase());

/**
 * Bind one CHART tile's shaped result into a renderChart spec.
 * Throws on table/kpi (CanvasView owns those) — callers gate on isChart().
 */
export function isChart(widget) {
  return CHART_WIDGETS.has(widget);
}

export function bindChart(tile, result) {
  if (!isChart(tile.widget)) {
    throw new Error(`widget ${tile.widget} is not a chart`);
  }
  const { columns, rows } = shapeResult(tile, result);
  const spec = { type: tile.widget, title: tile.title };
  const opts = tile.options || {};
  for (const key of ["stacked", "horizontal", "xLabel", "yLabel", "axes", "y2Label", "references"]) {
    if (opts[key] !== undefined) spec[key] = opts[key];
  }

  const w = tile.widget;
  if (w === "combo") {
    const x = fields(tile, "x")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, x)));
    const mk = (f, extra) => ({
      name: f,
      data: rows.map((r) => asNumber(cell(r, columns, f))),
      ...extra,
    });
    // Lines ride the right-hand scale only when the author labelled it —
    // otherwise both families share the left axis.
    const y2 = Boolean(tile.options?.y2Label);
    spec.series = [
      ...fields(tile, "bars").map((f) => mk(f, { type: "bar" })),
      ...fields(tile, "lines").map((f) => mk(f, { type: "line", y2 })),
    ];
    return spec;
  }
  if (w === "heatmap") {
    const [x, y, v] = ["x", "y", "value"].map((c) => fields(tile, c)[0]);
    const xCats = [];
    const yCats = [];
    const data = rows.map((r) => {
      const px = String(cell(r, columns, x));
      const py = String(cell(r, columns, y));
      if (!xCats.includes(px)) xCats.push(px);
      if (!yCats.includes(py)) yCats.push(py);
      return { x: px, y: py, v: asNumber(cell(r, columns, v)) };
    });
    spec.xCats = xCats;
    spec.yCats = yCats;
    spec.series = [{ name: tile.title, data }];
    return spec;
  }
  if (w === "waterfall") {
    const x = fields(tile, "x")[0];
    const val = fields(tile, "value")[0];
    const tot = fields(tile, "total")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, x)));
    // DELTAS straight from the query; the iframe converts them to floating
    // bar positions (layout arithmetic — never new values).
    spec.series = [{ name: tile.title, data: rows.map((r) => asNumber(cell(r, columns, val))) }];
    if (tot) spec.totals = rows.map((r) => isTruthy(cell(r, columns, tot)));
    return spec;
  }
  if (w === "funnel") {
    const stage = fields(tile, "stage")[0];
    const val = fields(tile, "value")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, stage)));
    spec.series = [{ name: tile.title, data: rows.map((r) => asNumber(cell(r, columns, val))) }];
    return spec;
  }
  if (w === "gauge") {
    const val = fields(tile, "value")[0];
    const v = rows.length ? asNumber(cell(rows[0], columns, val)) : null;
    spec.series = [{ name: tile.title, data: [v] }];
    spec.min = tile.options?.min ?? 0;
    spec.max = tile.options?.max;
    // Pre-format here (the iframe has no formatValue): the center text obeys
    // the value channel's declared format.
    spec.text = formatValue(v, tile.encoding?.value?.format);
    return spec;
  }
  if (w === "histogram") {
    const bin = fields(tile, "bin")[0];
    const count = fields(tile, "count")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, bin)));
    spec.series = [{ name: channelLabel(tile, "count", count), data: rows.map((r) => asNumber(cell(r, columns, count))) }];
    return spec;
  }
  if (w === "boxplot") {
    const cat = fields(tile, "category")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, cat)));
    const g = (r, c) => asNumber(cell(r, columns, fields(tile, c)[0]));
    // Five-number summaries from the SQL — never derived from raw values.
    spec.series = [{
      name: tile.title,
      data: rows.map((r) => ({
        min: g(r, "min"), q1: g(r, "q1"), median: g(r, "median"),
        q3: g(r, "q3"), max: g(r, "max"),
      })),
    }];
    return spec;
  }
  if (w === "pie" || w === "doughnut" || w === "polarArea") {
    const cat = fields(tile, "category")[0];
    const val = fields(tile, "value")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, cat)));
    spec.series = [
      {
        name: channelLabel(tile, "value", val),
        data: rows.map((r) => asNumber(cell(r, columns, val))),
      },
    ];
    return spec;
  }
  if (w === "radar") {
    const cat = fields(tile, "category")[0];
    spec.labels = rows.map((r) => String(cell(r, columns, cat)));
    spec.series = fields(tile, "y").map((f) => ({
      name: f,
      data: rows.map((r) => asNumber(cell(r, columns, f))),
    }));
    return spec;
  }
  if (w === "scatter" || w === "bubble") {
    const x = fields(tile, "x")[0];
    const y = fields(tile, "y")[0];
    const r = fields(tile, "r")[0];
    const point = (row) => {
      const p = {
        x: asNumber(cell(row, columns, x)),
        y: asNumber(cell(row, columns, y)),
      };
      if (w === "bubble") p.r = asNumber(cell(row, columns, r));
      return p;
    };
    const seriesField = fields(tile, "series")[0];
    spec.series = seriesField
      ? groupBy(rows, columns, seriesField).map(([name, group]) => ({
          name,
          data: group.map(point),
        }))
      : [{ name: channelLabel(tile, "y", y), data: rows.map(point) }];
    return spec;
  }
  if (w === "sankey") {
    const [from, to, flow] = ["from", "to", "flow"].map((c) => fields(tile, c)[0]);
    spec.series = [
      {
        name: tile.title,
        data: rows.map((r) => ({
          from: String(cell(r, columns, from)),
          to: String(cell(r, columns, to)),
          flow: asNumber(cell(r, columns, flow)),
        })),
      },
    ];
    return spec;
  }
  if (w === "treemap") {
    const label = fields(tile, "label")[0];
    const value = fields(tile, "value")[0];
    const group = fields(tile, "group")[0];
    spec.series = [
      {
        name: tile.title,
        data: rows.map((r) => {
          const leaf = {
            label: String(cell(r, columns, label)),
            value: asNumber(cell(r, columns, value)),
          };
          if (group) leaf.group = String(cell(r, columns, group));
          return leaf;
        }),
      },
    ];
    return spec;
  }

  // bar | line | area — labels from x; series either WIDE (one per y field)
  // or LONG (single y split by the series column, data aligned to the label
  // axis, holes as null).
  const x = fields(tile, "x")[0];
  const yFields = fields(tile, "y");
  const seriesField = fields(tile, "series")[0];
  if (seriesField) {
    const labels = [];
    for (const row of rows) {
      const l = String(cell(row, columns, x));
      if (!labels.includes(l)) labels.push(l);
    }
    spec.labels = labels;
    spec.series = groupBy(rows, columns, seriesField).map(([name, group]) => {
      const byLabel = new Map(
        group.map((row) => [
          String(cell(row, columns, x)),
          asNumber(cell(row, columns, yFields[0])),
        ])
      );
      return { name, data: labels.map((l) => byLabel.get(l) ?? null) };
    });
  } else {
    spec.labels = rows.map((r) => String(cell(r, columns, x)));
    spec.series = yFields.map((f) => ({
      name: f,
      data: rows.map((r) => asNumber(cell(r, columns, f))),
    }));
  }
  return spec;
}

/** Format one value per the channel's declared format (kpi + table cells). */
export function formatValue(v, format) {
  if (v === null || v === undefined) return "—";
  const n = asNumber(v);
  if (!format || n === null) return String(v);
  if (format === "integer") return Math.round(n).toLocaleString();
  if (format === "number") return n.toLocaleString();
  if (format === "percent") return `${(n * 100).toFixed(1)}%`;
  if (format.startsWith("currency:")) {
    const currency = format.slice("currency:".length);
    try {
      return n.toLocaleString(undefined, { style: "currency", currency });
    } catch {
      return `${n.toLocaleString()} ${currency}`;
    }
  }
  return String(v);
}
