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
)

# The authoring contract the model sees. This is where the whole "how to write a
# chart" instruction lives — NOT in the system prompt — so the base agent never
# advertises charting rules it can't act on, and the SYSTEM_PROMPT stays a static,
# brace-free cacheable prefix (see chat.graph). It documents the exact global the
# sandboxed iframe exposes (``renderChart(el, spec)``), the spec shape, the palette
# variables, and the house rules that keep charts on-brand.
RENDER_CHART_DESC = """Render a data visualization (chart) inline in your answer, shown to the user in the chat.

Call this when a chart communicates the answer better than prose or a table — comparisons across categories, a trend over time, parts of a whole, or a distribution. Prefer a small markdown table for a handful of exact numbers; reach for a chart when the SHAPE of the data is the point. Do not announce the chart ("here is a chart…") — just call the tool where it belongs in your answer, then continue explaining what it shows. One chart per distinct point; don't over-visualize.

HOW IT RENDERS: your `code` runs inside a sandboxed browser frame that already has a Chart.js-backed helper on the global scope:

    renderChart(el, spec)

`el` is the <canvas> your code must draw into (already in the DOM — do not create your own). `spec` is a plain object:

    renderChart(el, {
      type: "bar",                       // bar | line | area | pie | doughnut | radar | scatter | bubble | polarArea | sankey | treemap
      title: "Race wins by constructor", // optional heading shown above the chart
      labels: ["Ferrari", "McLaren", "Mercedes"],   // x-axis / category labels
      series: [                          // one entry per data series
        { name: "Wins", data: [243, 183, 125] }
      ],
      // optional: stacked: true (bar/area), yLabel: "Wins", xLabel: "Team",
      // horizontal: true (bar/line/area — swaps the axes; use for long category
      // names or ranked "top N" lists, which read better as horizontal bars),
      // axes: true|false (show/hide the value-axis gridlines; horizontal charts
      // default to false — a clean ranked list — vertical charts to true)
    });

For scatter, each series' `data` is an array of {x, y} points and `labels` is omitted. For bubble, points are {x, y, r} (r = radius in px, scale it to your third dimension). polarArea is like pie/doughnut (one series; each slice's RADIUS encodes the value).

For sankey (FLOW between stages/categories — sources, transitions, allocations), one series whose `data` is [{from, to, flow}] edges (node names as strings, flow = the magnitude); `labels` is omitted. For treemap (share-of-total across many items, optionally grouped), one series whose `data` is [{label, value}] leaves — add a `group` field ({label, value, group}) for one level of nesting; `labels` is omitted.

MIXED charts: give an individual series its own `type` to overlay it on the base type — e.g. monthly bars with a cumulative line: type: "bar", series: [{ name: "Monthly", data: [...] }, { name: "Cumulative", type: "line", data: [...] }]. Per-series `type` accepts bar | line | area.

Your `code` is the BODY of a function that receives `el` — write statements, not a module. Example value for the `code` argument:

    renderChart(el, { type: "bar", title: "Race wins", labels: ["Ferrari","McLaren"], series: [{ name: "Wins", data: [243, 183] }] });

DESIGN — match the app's visual language, don't fight it:
- Do NOT hard-code colors. The helper applies the app's chart palette automatically (CSS variables --chart-1 … --chart-5) and the current light/dark theme. Only set a color if the user explicitly asks.
- Keep it clean: no chartjunk, no 3-D, no gratuitous gridlines. The helper already sets sensible axis/legend/tooltip defaults that match the UI.
- Give every chart a short, descriptive `title` and label axes when the unit isn't obvious.

DATA — the chart is only as truthful as its numbers. Use REAL values you obtained from the wiki or a tool result (e.g. run_sql), never invented or "rough" figures. If you don't have the numbers, get them first or answer in prose instead. Cite the underlying wiki docs in your prose exactly as you normally would; the chart itself doesn't take a citation.

Args:
  code: JavaScript that calls renderChart(el, spec) to draw the chart into the provided `el` canvas. Real data only.
  title: A short human title for the chart (also shown if the chart fails to render). Keep it under ~80 chars.
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
