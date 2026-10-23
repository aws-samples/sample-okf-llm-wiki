# Canvas authoring

The widget catalog, when to use each, and the house rules that make a
dashboard verifiable. The lint tool enforces the mechanical half of this;
the judgment half is yours.

## The catalog — pick by the QUESTION, not the data

| Widget | Use when | Channels (required in bold) |
|---|---|---|
| `kpi` | one number answers the question | **value**, delta |
| `line` / `area` | a trend over an ordered axis | **x**, **y** (multi), series |
| `bar` | comparison across categories, ranked lists (horizontal) | **x**, **y** (multi), series |
| `pie` / `doughnut` | parts of ONE whole, ≤ ~8 slices | **category**, **value** |
| `polarArea` | parts of a whole where magnitude matters more than share | **category**, **value** |
| `radar` | one entity profiled across ≤ ~10 dimensions | **category**, **y** (multi) |
| `scatter` | correlation between two measures | **x**, **y**, series |
| `bubble` | correlation + a third magnitude | **x**, **y**, **r**, series |
| `sankey` | flow between stages | **from**, **to**, **flow** |
| `treemap` | share of total across many categories (where pie fails) | **label**, **value**, group |
| `table` | exact values the user will read off | columns |
| `combo` | bars + a trend line over one x (`y2Label` puts lines on a right scale) | **x**, **bars** (multi), **lines** (multi) |
| `pivot` | matrix with totals/subtotals + conditional formatting | **rows** (multi), columns, **values** (multi) |
| `heatmap` | intensity across two categories | **x**, **y**, **value** |
| `waterfall` | how deltas compose a total (SQL emits DELTAS; optional `total` flag column) | **x**, **value**, total |
| `funnel` | stage-to-stage conversion, values descending | **stage**, **value** |
| `gauge` | one number against a bounded range (`min`/`max` options, `max` required) | **value** |
| `histogram` | a distribution — PRE-BINNED IN SQL | **bin**, **count** |
| `boxplot` | spread per category — five-number summary FROM SQL | **category**, **min q1 median q3 max** |

Defaults that read well: a KPI row on top (h=3), the main trend beneath it
(w=8–12, h=4–5), breakdowns below. 12-column grid; don't overlap tiles.

## Dials

- A dial is a QUESTION the user will genuinely re-ask with different values
  (which year? which brand?). Parameters that only have one sensible value
  become fixed `{"value": ...}` binds, not dials.
- One dial may drive several tiles — bind them all; that's the point.
- Every dial needs a `default` the whole dashboard makes sense at. The
  control type comes from the parameter contract (enum → dropdown, min/max →
  bounded input, date → picker, timestamp → datetime picker, boolean →
  toggle) — you never declare it.
- Declare `column: "table.column"` on every parameter that filters by a
  column's VALUES (ad-hoc `data.parameters` and computation docs alike).
  That one field buys both surfaces: the executor warns on out-of-domain
  values (typo hints), and the dial renders as a type-ahead fed by the
  profile's observed values — never hand-enumerate them. Reserve a closed
  `enum` for small, stable sets the profile marked exhaustive; a free-text
  dial with no `column` is a last resort.

## Data discipline

- Probe before you bind: `run_computation` and look at the actual columns,
  row counts, and value ranges. Design against reality, not the doc's prose.
- Computations are called by SLUG — the `computation` field from
  `list_computations` (e.g. `season_final_driver_standings`) — never their
  human title. The same slug goes into a tile's `data.computation`.
- Ad-hoc tiles (`data.sql` + `data.parameters`) are for questions no
  computation covers: ONE read-only SELECT/WITH statement, `@holes` exactly
  matching the declared parameters (same typed contract as computation docs —
  enum/min/max become the dial controls). Ground every table and column in
  the wiki before writing SQL; prefer a computation when one exists (it is
  frozen and human-verified — an ad-hoc query is only as good as this job).
- Shaping belongs in the computation's SQL. `shape` ops are presentation
  trims only (sort for reading order, topN for ranked lists, limit as a
  guard) — if you need a derived metric, that's a computation gap to report,
  not a workaround to build.
- Wide vs long: several measures per row → multiple `y` fields; one measure
  split by a category column → single `y` + `series`. Never both.
- DERIVATION STAYS IN THE SQL — the tiles only lay out and format:
  histogram bins come from `floor`/`width_bucket`; boxplot five-number
  summaries from `approx_percentile`; pivot totals/subtotals from
  `GROUP BY ROLLUP` (NULL group cells render as Total rows — never expect
  the tile to sum cells). Which totals you get is the GROUPING shape:
  `ROLLUP (row, col)` = row totals + grand total only; add per-COLUMN totals
  with `GROUPING SETS ((row, col), (row), (col), ())`. `COALESCE` genuinely
  null group values to a label ('unknown') — a real NULL masquerades as a
  Total row. Declare a `format` on pivot `values` (integer/number/percent/
  currency) so cells don't render raw engine floats ("454.0"). A waterfall's rows are DELTAS (bar positions are
  computed, values never are).
- `references` (cartesian widgets): `[{axis, value | from+to, label}]` —
  literal target lines / threshold bands. Literals only; a computed
  reference (e.g. "the mean") belongs in the SQL as its own series.
- pivot `rules`: `[{field, min?, max?, color: emerald|amber|red|sky|muted}]`
  — first matching rule tints the cell; use for over/under-target reads.
- Prefer VERIFIED computations. Using an unverified one is fine but say so
  in your final summary — the human verifying the dashboard should know.

## Visual inspection (render_canvas)

The screenshots show the TILES exactly as users get them (same components,
same width cap, both themes) — and deliberately nothing else: the page
header and the dial-controls bar are constant chrome, not tile-quality
signal, and the controls are DERIVED from your parameter contracts (lint
already validated them). Don't spend turns trying to see a dropdown in the
screenshots — it isn't there by design.

The screenshots come back INLINE in the tool result — one per theme at the
page's reading width. Judge each screenshot as a reviewer: axis labels
present where units aren't obvious; legends readable; no label collisions;
dark theme as legible as light; the chart type still right given the real
data's shape
(an 80-point "pie" means you picked the wrong widget, not the wrong cap).
Check the dial extremes the lint probed — an empty tile at min/max is a
warning you must either fix (tighter dial bounds via a different default) or
consciously accept and mention.

## Language

Titles and dial labels are business language — no table names, no SQL-isms.
`"DtC revenue by month"`, not `"sum(rev) FROM dtc_orders"`.
