"""The ``render_chart`` tool — the agent's way to show a visual in the chat.

Unlike every other chat tool, ``render_chart`` does no server work: the model
writes a small piece of **JavaScript "script code"** that draws a chart, and the
RENDERING happens in the browser. The tool call is the transport — its ``code``
argument is what the UI executes — and the tool's return value is just an
acknowledgement the model reads so it can keep writing its answer ("the chart is
shown, carry on") without waiting on the browser.

Why a tool at all (not a ``<chart>`` markdown tag like some assistants use): tool
calls arrive on the wire as fully-assembled, argument-parsed objects (see
``server.process_stream_data``), so the UI gets the whole chart spec atomically
instead of trying to parse a half-streamed tag out of the answer text. It also
rides the exact typed-chunk path the rest of the tools already use — the UI lifts
the ``render_chart`` call out of the tool timeline and renders it inline (see
``ui .../buildMessageBlocks.js``).

Confinement (the load-bearing safety property): the model's ``code`` is executed
in a **sandboxed ``<iframe>``** on the UI side (``sandbox="allow-scripts"`` with NO
``allow-same-origin``, a strict CSP, and a bundled Chart.js). That iframe is both
the crash boundary (a bad chart can't take down the app) and the security boundary
(model-authored JS can never reach the parent page, its DOM, or the Cognito token
in it). None of that lives here — this module only defines the tool and the
authoring contract the model sees.

We deliberately do NOT round-trip the render result back to the model (no
human-in-the-loop interrupt): the chart model is accurate enough that the added
latency + machinery isn't worth it. If a chart fails to render, the UI shows a
contained error in place; the model isn't told. Keep this module dependency-light
(only ``langchain_core``) so it imports in the unit venv.
"""

from __future__ import annotations

import json
from typing import Any

# The chart types the bundled renderer (Chart.js) supports. Kept here so the tool
# description, the ack, and the tests all name the SAME set — the UI's renderChart
# helper maps each onto a Chart.js config.
SUPPORTED_CHART_TYPES = (
    "bar",
    "line",
    "area",
    "pie",
    "doughnut",
    "radar",
    "scatter",
    "bubble",
    "polarArea",
    "sankey",
    "treemap",
    "combo",
    "heatmap",
    "waterfall",
    "funnel",
    "gauge",
    "histogram",
    "boxplot",
)

# The authoring contract the model sees. This is where the whole "how to write a
# chart" instruction lives — NOT in the system prompt — so the base agent never
# advertises charting rules it can't act on, and the SYSTEM_PROMPT stays a static,
# brace-free cacheable prefix (see chat.graph). It documents the exact global the
# sandboxed iframe exposes (``renderChart(el, spec)``), the spec shape, the palette
# variables (``--chart-1`` … ``--chart-10`` — keep in step with ``PALETTE_VARS`` in
# ui/src/lib/chartIframe.js), and the house rules that keep charts on-brand.
RENDER_CHART_DESC = """Render a data visualization (chart) inline in your answer, shown to the user in the chat.

Call this for comparisons across categories, a trend over time, parts of a whole, a distribution. A handful of exact numbers belongs in a small markdown table; reach for a chart when the SHAPE of the data is the point. Don't announce it ("here is a chart…") — call the tool where it belongs, then continue explaining what it shows. One chart per distinct point; don't over-visualize.

HOW IT RENDERS: your `code` is the BODY of a function receiving `el` — statements, not a module. It runs in a sandboxed browser frame with a Chart.js-backed helper on the global scope:

    renderChart(el, spec)

`el` is the <canvas> to draw into — already in the DOM, don't create your own. `spec` is a plain object:

    renderChart(el, {
      type: "bar",                       // bar | line | area | pie | doughnut | radar | scatter | bubble | polarArea
                                         // | sankey | treemap | combo | heatmap | waterfall | funnel | gauge | histogram | boxplot
      title: "Race wins by constructor", // optional heading above the chart
      labels: ["Ferrari", "McLaren", "Mercedes"],   // category / x-axis labels
      series: [                          // one entry per series
        { name: "Wins", data: [243, 183, 125] }
      ],
      // optional: stacked: true (bar/area), yLabel: "Wins", xLabel: "Team",
      // horizontal: true (bar/line/area — swaps the axes; use it for long
      // category names and ranked "top N" lists),
      // axes: true|false (value-axis gridlines; defaults false on horizontal
      // charts — a clean ranked list — true on vertical),
      // references: [{value, label}] — dashed target line on the value axis
      // ({from, to, label} draws a shaded band instead; axis: "x" to pin it
      // to the x axis) — cartesian charts only
    });

Types whose `data` shape differs, all omitting `labels`: scatter takes [{x, y}]; bubble [{x, y, r}] (r = radius in px, scaled to your third dimension); sankey (flow between stages) one series of [{from, to, flow}] edges, node names as strings, flow = magnitude; treemap (share of total) one series of [{label, value}] leaves plus an optional `group` for one nesting level. polarArea takes one series like pie, each slice's RADIUS encoding the value. heatmap (two categories x a magnitude) takes one series of [{x, y, v}] cells PLUS `xCats`/`yCats` arrays on the spec fixing each axis's category order (a null `v` renders as a visible "no data" cell — don't substitute 0). gauge (one value against a range) takes series: [{ data: [value] }] plus `min`/`max` on the spec and an optional `text` — the pre-formatted center label (e.g. "87%"); without it the raw number is drawn.

Single-series types that KEEP `labels`: waterfall — data = the signed DELTAS in order (the helper computes the running-sum bar positions; add `totals: [...]` on the spec, a boolean per label, to mark rows that are running TOTALS — drawn from zero in neutral ink); funnel — labels = the stages in order, data = each stage's size (the tooltip adds % of the first stage); histogram — labels = bucket labels you already computed (bin in SQL, e.g. width_bucket), data = the counts, drawn gapless; boxplot — data = one {min, q1, median, q3, max} object per label, the five-number summary computed in SQL (e.g. approx_percentile), never raw rows.

MIXED charts: type "combo" draws bars + lines over one x-axis with a per-series `type` (bar | line | area) — monthly bars with a cumulative line is type: "combo", series: [{ name: "Monthly", type: "bar", data: [...] }, { name: "Cumulative", type: "line", data: [...] }]. When a line's unit differs from the bars' (counts vs. a rate), set y2: true on that series and `y2Label` on the spec — it rides a right-hand value axis. A per-series `type` on a plain bar/line base overlays the same way.

A complete `code` value:

    renderChart(el, { type: "bar", title: "Race wins", labels: ["Ferrari","McLaren"], series: [{ name: "Wins", data: [243, 183] }] });

DESIGN — match the app's visual language:
- Colors are NOT configurable in the spec: the helper applies the app's chart palette (CSS variables --chart-1 … --chart-10) and the current light/dark theme itself, and ignores any color you set.
- Keep it clean: no chartjunk, no 3-D, no gratuitous gridlines. The helper already sets UI-matching axis/legend/tooltip defaults.
- Give every chart a short, descriptive `title`, and label axes when the unit isn't obvious.

DATA — a chart is only as truthful as its numbers. Use REAL values from the wiki or a tool result (e.g. run_sql), never invented or "rough" figures; if you don't have them, get them first or answer in prose. Cite the underlying docs in your prose as usual; the chart itself takes no citation.

Args:
  code: statements that call renderChart(el, spec). Real data only.
  title: a short human title, also shown if the chart fails to render (~80 chars max).
"""


def render_chart_ack(title: str) -> dict[str, Any]:
    """The tool's return value — an acknowledgement, not a render result.

    The browser renders the chart from the tool CALL (the ``code`` arg); this ack
    is what flows back to the MODEL so it knows the visual was handed off and can
    continue its answer. Deliberately carries no success/failure signal: rendering
    happens after this returns, out-of-band, and we don't round-trip the outcome.
    """
    return {
        "status": "rendered",
        "title": title,
        "note": (
            "The chart has been displayed to the user inline. Continue your answer; "
            "describe what the chart shows in prose. Do not repeat the raw numbers "
            "unless a specific value matters."
        ),
    }


def make_chart_tool() -> Any:
    """Wrap ``render_chart`` as a LangChain StructuredTool for the chat agent.

    Pure and dependency-light: the tool just validates it received a code string
    and returns the ack. All rendering + confinement is on the UI side. Returns a
    ``StructuredTool`` with the authoring contract as its description (that text is
    the model's only spec for how to author a chart).
    """
    from langchain_core.tools import StructuredTool

    def render_chart(code: str, title: str = "") -> str:
        # The ack is returned as a JSON string (like the other tools' results) so
        # process_stream_data / the UI treat it uniformly. The `code` is not
        # executed here — it is the payload the UI runs in its sandboxed frame.
        if not isinstance(code, str) or not code.strip():
            return json.dumps(
                {
                    "status": "error",
                    "error": "render_chart requires non-empty `code` that calls renderChart(el, spec).",
                }
            )
        return json.dumps(render_chart_ack(title or "Chart"))

    return StructuredTool.from_function(
        func=render_chart,
        name="render_chart",
        description=RENDER_CHART_DESC,
    )
