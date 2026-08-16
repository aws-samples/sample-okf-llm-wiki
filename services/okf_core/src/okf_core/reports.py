"""Report contract: block schema, id/key conventions, the HTML composer.

A *report* is an ATOMIC artifact: the chat agent authors
an ordered list of BLOCKS (markdown / chart / table / kpi — CONFIG, not code:
nothing model-authored executes at view time), and this module's composer
renders them into ONE self-contained HTML document — inline stylesheet, chart
images baked as data URIs, per-figure provenance badges, a print stylesheet
the PDF pass reuses. Reports are immutable: a re-run is a new report, never
an edit.

This module owns the pure invariants (no AWS, no agent deps) so the chat
agent's ``create_report`` tool, the control API's serving path, and the tests share one
implementation — the ``okf_core.lint`` arrangement:

- ``lint_blocks``   — structural refusal: schema, caps, provenance shapes.
                      Any error refuses the save (there is no "draft with
                      warnings" — reports are atomic or they are nothing).
- ``compose_html``  — deterministic blocks → HTML. No clock, no randomness,
                      no network: timestamps arrive as arguments and chart
                      images arrive pre-rendered (the runtime rasterizes each
                      spec through the app's own chart renderer in headless
                      Chromium), so the output is snapshot-testable byte for
                      byte.

Markdown rendering is INJECTED (``md_render=``) to keep this package
dep-minimal; the default lazily imports ``markdown_it`` (a declared dependency
of the chat service, and this package's test extra) configured with raw
HTML *disabled* — agent-authored markdown must never smuggle markup into the
composed document. Everything else passes through ``html.escape``.

The composed HTML is rendered inside a sandboxed ``<iframe>`` with NO
``allow-scripts`` (and printed to PDF by headless Chromium), so the composer
must emit strictly static markup: no <script>, no external fetches, no
webfonts — the stylesheet is inline and the font stack is system-native.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Callable

REPORTS_VERSION = 1

MAX_REPORT_TITLE = 120
MAX_BLOCKS = 48
MAX_MARKDOWN_BYTES = 20_000
MAX_TABLE_COLS = 12
MAX_TABLE_ROWS = 100
MAX_SERIES = 10
# Total data points across a chart block's series — a report figure is an
# illustration, not a data dump; heavy shaping belongs in the SQL.
MAX_CHART_POINTS = 2_000
MAX_LABEL = 80
MAX_SQL_BYTES = 20_000
# Composed-HTML ceiling (data-URI PNGs add up). save_report refuses above it;
# env-overridable at the service layer, this is the shared default.
MAX_REPORT_BYTES = 8_000_000

BLOCK_TYPES = ("markdown", "chart", "table", "kpi")

# The renderChart spec types a chart block may carry. Must stay in step with
# SUPPORTED_CHART_TYPES in services/chat/src/chat/charts.py and the renderer
# branches in ui/src/lib/chartIframe.js — the runtime rasterizes blocks
# through that exact renderer.
CHART_SPEC_TYPES = (
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

PROVENANCE_KINDS = ("computation", "adhoc_sql")

_SLUG_RE = re.compile(r"^[A-Za-z0-9][\w-]*$")
# Compact UTC stamp used in report ids and S3 prefixes: sortable, no
# separators that need escaping anywhere (DDB sk, S3 key, URL path).
_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
_SUFFIX_RE = re.compile(r"^[a-f0-9]{8}$")

_SCALAR_TYPES = (str, int, float, bool)


# ---------------------------------------------------------------------------
# Report id / S3 conventions (chat runtime + control API share these).
# There is NO database row: the public report id is COMPOSITE — it carries
# its own coordinates — so serving resolves S3 keys from the id alone, and
# ``present_report`` round-trips an opaque token. Artifacts live OUTSIDE the
# mounted ``okf/`` bundle prefix (the verification-overlay precedent), so
# they never meet the harvest lease or the reindex pipeline.
# ---------------------------------------------------------------------------

REPORT_ID_PREFIX = "rep"
_REPORT_ID_SEP = "~"


def make_report_id(domain: str, dataset: str, stamp: str, suffix: str) -> str:
    """The public report id: ``rep~<domain>~<dataset>~<stamp>~<suffix>``."""
    return _REPORT_ID_SEP.join((REPORT_ID_PREFIX, domain, dataset, stamp, suffix))


def parse_report_id(report_id: Any) -> dict[str, str] | None:
    """Parse a public report id; ``None`` for anything malformed.

    Strict on every segment — the id arrives from URL paths and model tool
    calls, and it feeds DDB keys and S3 prefixes, so nothing shapeless may
    pass through.
    """
    if not isinstance(report_id, str):
        return None
    parts = report_id.split(_REPORT_ID_SEP)
    if len(parts) != 5 or parts[0] != REPORT_ID_PREFIX:
        return None
    _, domain, dataset, stamp, suffix = parts
    if not (_SLUG_RE.match(domain) and _SLUG_RE.match(dataset)):
        return None
    if not (_STAMP_RE.match(stamp) and _SUFFIX_RE.match(suffix)):
        return None
    return {"domain": domain, "dataset": dataset, "stamp": stamp, "suffix": suffix}


def report_s3_prefix(domain: str, dataset: str, stamp: str, suffix: str) -> str:
    """S3 prefix for one report's artifacts."""
    return f"reports/{domain}/{dataset}/{stamp}-{suffix}"


def report_html_key(prefix: str) -> str:
    return f"{prefix}/report.html"


def report_pdf_key(prefix: str) -> str:
    return f"{prefix}/report.pdf"


def report_blocks_key(prefix: str) -> str:
    return f"{prefix}/blocks.json"


# ---------------------------------------------------------------------------
# Block lint — structural refusal
# ---------------------------------------------------------------------------


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, _SCALAR_TYPES)


def _check_provenance(prov: Any, where: str, errors: list[str]) -> None:
    if prov is None:
        return
    if not isinstance(prov, dict):
        errors.append(f"{where}: provenance must be an object")
        return
    kind = prov.get("kind")
    if kind not in PROVENANCE_KINDS:
        errors.append(
            f"{where}: provenance.kind must be one of {list(PROVENANCE_KINDS)}"
        )
        return
    if kind == "computation":
        slug = prov.get("slug")
        if not (isinstance(slug, str) and _SLUG_RE.match(slug)):
            errors.append(f"{where}: computation provenance requires a slug")
        params = prov.get("params")
        if params is not None and not isinstance(params, dict):
            errors.append(f"{where}: provenance.params must be an object")
        content_hash = prov.get("content_hash")
        if content_hash is not None and not isinstance(content_hash, str):
            errors.append(f"{where}: provenance.content_hash must be a string")
    else:  # adhoc_sql
        sql = prov.get("sql")
        if not (isinstance(sql, str) and sql.strip()):
            errors.append(f"{where}: adhoc_sql provenance requires the sql text")
        elif len(sql.encode("utf-8")) > MAX_SQL_BYTES:
            errors.append(f"{where}: provenance.sql exceeds {MAX_SQL_BYTES} bytes")


def _check_chart(block: dict, where: str, errors: list[str]) -> None:
    title = block.get("title")
    if not (isinstance(title, str) and title.strip()):
        errors.append(f"{where}: chart blocks require a title")
    elif len(title) > MAX_REPORT_TITLE:
        errors.append(f"{where}: title exceeds {MAX_REPORT_TITLE} chars")
    spec = block.get("spec")
    if not isinstance(spec, dict):
        errors.append(f"{where}: chart blocks require a spec object")
        return
    if spec.get("type") not in CHART_SPEC_TYPES:
        errors.append(
            f"{where}: spec.type must be one of {list(CHART_SPEC_TYPES)}"
        )
    series = spec.get("series")
    if not (isinstance(series, list) and series):
        errors.append(f"{where}: spec.series must be a non-empty array")
        return
    if len(series) > MAX_SERIES:
        errors.append(f"{where}: more than {MAX_SERIES} series")
    points = 0
    for j, s in enumerate(series):
        if not isinstance(s, dict):
            errors.append(f"{where}: series[{j}] must be an object")
            continue
        data = s.get("data")
        if not isinstance(data, list):
            errors.append(f"{where}: series[{j}].data must be an array")
            continue
        points += len(data)
    if points > MAX_CHART_POINTS:
        errors.append(
            f"{where}: {points} data points exceed the {MAX_CHART_POINTS} cap "
            "— aggregate further in SQL"
        )
    labels = spec.get("labels")
    if labels is not None:
        if not isinstance(labels, list) or not all(_is_scalar(v) for v in labels):
            errors.append(f"{where}: spec.labels must be an array of scalars")


def _check_table(block: dict, where: str, errors: list[str]) -> None:
    columns = block.get("columns")
    if not (
        isinstance(columns, list)
        and columns
        and all(isinstance(c, str) and c.strip() for c in columns)
    ):
        errors.append(f"{where}: table blocks require non-empty string columns")
        return
    if len(columns) > MAX_TABLE_COLS:
        errors.append(f"{where}: more than {MAX_TABLE_COLS} columns")
    rows = block.get("rows")
    if not isinstance(rows, list):
        errors.append(f"{where}: table blocks require a rows array")
        return
    if len(rows) > MAX_TABLE_ROWS:
        errors.append(
            f"{where}: {len(rows)} rows exceed the {MAX_TABLE_ROWS} cap "
            "— a report table is an exhibit, not an export"
        )
    for j, row in enumerate(rows):
        if not (isinstance(row, list) and len(row) == len(columns)):
            errors.append(f"{where}: rows[{j}] must have {len(columns)} cells")
        elif not all(_is_scalar(v) for v in row):
            errors.append(f"{where}: rows[{j}] cells must be scalars")
    title = block.get("title")
    if title is not None and not (
        isinstance(title, str) and 0 < len(title) <= MAX_REPORT_TITLE
    ):
        errors.append(f"{where}: title must be 1..{MAX_REPORT_TITLE} chars")


def _check_kpi(block: dict, where: str, errors: list[str]) -> None:
    label = block.get("label")
    if not (isinstance(label, str) and 0 < len(label.strip()) <= MAX_LABEL):
        errors.append(f"{where}: kpi blocks require a label (1..{MAX_LABEL} chars)")
    value = block.get("value")
    if not _is_scalar(value) or value is None:
        # The VALUE is the point of a KPI; the agent formats it and the
        # composer renders it verbatim — a report is a snapshot, so there is
        # no live data that would need a format grammar.
        errors.append(f"{where}: kpi blocks require a scalar value")
    delta = block.get("delta")
    if delta is not None and not isinstance(delta, (int, float)):
        errors.append(f"{where}: kpi delta must be a number")
    delta_label = block.get("delta_label")
    if delta_label is not None and not (
        isinstance(delta_label, str) and len(delta_label) <= MAX_LABEL
    ):
        errors.append(f"{where}: kpi delta_label must be ≤{MAX_LABEL} chars")


def lint_blocks(title: Any, blocks: Any) -> list[str]:
    """Structural check of a report's title + blocks. Returns error strings;
    any error refuses the save. Deliberately schema-only — whether a chart
    RENDERS is proven by the runtime's rasterize step, not guessed here."""
    errors: list[str] = []
    if not (isinstance(title, str) and title.strip()):
        errors.append("report title is required")
    elif len(title) > MAX_REPORT_TITLE:
        errors.append(f"report title exceeds {MAX_REPORT_TITLE} chars")
    if not isinstance(blocks, list) or not blocks:
        errors.append("blocks must be a non-empty array")
        return errors
    if len(blocks) > MAX_BLOCKS:
        errors.append(f"{len(blocks)} blocks exceed the {MAX_BLOCKS} cap")
    for i, block in enumerate(blocks):
        where = f"blocks[{i}]"
        if not isinstance(block, dict):
            errors.append(f"{where}: must be an object")
            continue
        btype = block.get("type")
        if btype not in BLOCK_TYPES:
            errors.append(f"{where}: type must be one of {list(BLOCK_TYPES)}")
            continue
        if btype == "markdown":
            md = block.get("md")
            if not (isinstance(md, str) and md.strip()):
                errors.append(f"{where}: markdown blocks require md text")
            elif len(md.encode("utf-8")) > MAX_MARKDOWN_BYTES:
                errors.append(f"{where}: md exceeds {MAX_MARKDOWN_BYTES} bytes")
        elif btype == "chart":
            _check_chart(block, where, errors)
        elif btype == "table":
            _check_table(block, where, errors)
        else:
            _check_kpi(block, where, errors)
        if btype != "markdown":
            _check_provenance(block.get("provenance"), where, errors)
    return errors


# ---------------------------------------------------------------------------
# Composer — deterministic blocks → self-contained HTML
# ---------------------------------------------------------------------------

# Document styling, not app theming: a report is a portable artifact that gets
# emailed, printed, and PDF'd, so it renders on a plain light surface with a
# system font stack regardless of the app's theme — and NOTHING is fetched
# (the iframe it renders in has no allow-scripts and the PDF pass must not
# depend on the network).
_CSS = """\
:root { color-scheme: light;
  --rpt-bg: #fff; --rpt-fg: #1c1917; --rpt-muted: #78716c; --rpt-soft: #57534e;
  --rpt-src: #44403c; --rpt-border: #e7e5e4; --rpt-border-strong: #d6d3d1;
  --rpt-code-bg: #f5f5f4; --rpt-faint: #a8a29e; --rpt-link: #0369a1;
  --rpt-up: #047857; --rpt-down: #b91c1c; }
/* The viewer flips this attribute when the APP is in dark mode, so the report
   blends with the panel it sits in — values mirror ui/src/index.css .dark
   tokens verbatim. Standalone open / PDF never set it: the artifact prints
   light. */
:root[data-theme="dark"] { color-scheme: dark;
  --rpt-bg: oklch(0.218 0 0); --rpt-fg: oklch(0.987 0 0);
  --rpt-muted: oklch(0.723 0 0); --rpt-soft: oklch(0.82 0 0);
  --rpt-src: oklch(0.87 0 0); --rpt-border: oklch(1 0 0 / 10%);
  --rpt-border-strong: oklch(1 0 0 / 20%); --rpt-code-bg: oklch(0.269 0 0);
  --rpt-faint: oklch(0.556 0 0); --rpt-link: #7dd3fc;
  --rpt-up: #34d399; --rpt-down: #f87171; }
* { box-sizing: border-box; }
body { margin: 0; padding: 40px 48px 56px; background: var(--rpt-bg); color: var(--rpt-fg);
  /* Inter first for the PDF: the chat image installs it (Dockerfile) so
     headless Chromium doesn't fall back to DejaVu Sans — the rest of the
     stack serves the in-app viewer/downloads on machines without Inter. */
  font: 15px/1.65 Inter, ui-sans-serif, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 760px; margin: 0 auto; }
header.rpt { max-width: 760px; margin: 0 auto 28px; border-bottom: 1px solid var(--rpt-border); padding-bottom: 20px; }
header.rpt h1 { font-size: 26px; line-height: 1.25; margin: 0 0 10px; letter-spacing: -0.01em; }
header.rpt .meta { color: var(--rpt-muted); font-size: 13px; }
header.rpt .meta span + span::before { content: '·'; margin: 0 7px; color: var(--rpt-border-strong); }
header.rpt .request { margin: 14px 0 0; padding: 10px 14px; border-left: 3px solid var(--rpt-border);
  color: var(--rpt-soft); font-size: 13.5px; }
h2 { font-size: 19px; margin: 34px 0 10px; letter-spacing: -0.01em; }
h3 { font-size: 16px; margin: 26px 0 8px; }
p { margin: 10px 0; }
a { color: var(--rpt-link); }
code { background: var(--rpt-code-bg); border-radius: 4px; padding: 1px 5px; font-size: 13px;
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; }
pre { background: var(--rpt-code-bg); border-radius: 8px; padding: 12px 14px; overflow-x: auto;
  font-size: 12.5px; line-height: 1.5; }
pre code { background: none; padding: 0; }
blockquote { margin: 12px 0; padding: 2px 16px; border-left: 3px solid var(--rpt-border); color: var(--rpt-soft); }
ul, ol { padding-left: 26px; }
/* Thin, transparent-track scrollbar — the report is its OWN document (an
   inert iframe in the app), so it never sees index.css's global rule; embed
   the same slim pill here so the panel doesn't sprout a heavy native bar
   (pre/table overflow panes get it too). */
* { scrollbar-width: thin; scrollbar-color: var(--rpt-border) transparent; }
*::-webkit-scrollbar { width: 8px; height: 8px; }
*::-webkit-scrollbar-track { background: transparent; }
*::-webkit-scrollbar-thumb { background-color: var(--rpt-border); border-radius: 9999px;
  border: 2px solid transparent; background-clip: padding-box; }
*::-webkit-scrollbar-thumb:hover { background-color: var(--rpt-muted); background-clip: padding-box; }
figure { margin: 26px 0; }
/* Chart PNGs are TRANSPARENT and rendered per theme (chart-light/chart-dark
   pairs) — no plate, no border: the figure sits directly on the page surface,
   and the theme attribute picks which PNG shows. PDF/standalone never set
   data-theme, so the light PNG prints on white. */
figure img { width: 100%; height: auto; display: block; }
img.chart-dark { display: none; }
[data-theme="dark"] img.chart-light { display: none; }
[data-theme="dark"] img.chart-dark { display: block; }
figcaption { margin-top: 8px; color: var(--rpt-soft); font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
.md table { margin: 14px 0; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--rpt-border); }
th { color: var(--rpt-soft); font-weight: 600; border-bottom-color: var(--rpt-border-strong); }
tbody tr:last-child td { border-bottom: none; }
figure.tbl table { border: 1px solid var(--rpt-border); border-radius: 10px; overflow: hidden; border-spacing: 0; }
.kpi-row { display: flex; gap: 14px; flex-wrap: wrap; margin: 26px 0; }
.kpi { flex: 1 1 150px; border: 1px solid var(--rpt-border); border-radius: 10px; padding: 14px 16px; }
.kpi .label { color: var(--rpt-muted); font-size: 12.5px; }
.kpi .value { font-size: 24px; font-weight: 650; letter-spacing: -0.01em; margin-top: 2px; }
.kpi .delta { font-size: 12.5px; margin-top: 4px; color: var(--rpt-muted); }
.kpi .delta.up { color: var(--rpt-up); }
.kpi .delta.down { color: var(--rpt-down); }
sup.note { font-size: 10px; margin-left: 3px; }
sup.note a { color: var(--rpt-muted); text-decoration: none; }
.appendix { margin-top: 44px; border-top: 1px solid var(--rpt-border); padding-top: 16px;
  color: var(--rpt-soft); font-size: 12.5px; }
.appendix h2 { font-size: 12px; margin: 0 0 10px; color: var(--rpt-faint); font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase; }
.appendix ol { margin: 0; padding-left: 20px; }
.appendix li { margin: 6px 0; }
.appendix .src { color: var(--rpt-src); font-weight: 550; }
.appendix pre { margin: 6px 0 2px; font-size: 11.5px; }
footer.rpt { max-width: 760px; margin: 44px auto 0; border-top: 1px solid var(--rpt-border);
  padding-top: 14px; color: var(--rpt-faint); font-size: 12px; }
@media print {
  /* Not 0: 1px card borders (kpi/tbl) at the exact printable edge get
     clipped by page.pdf — a few px of slack keeps them whole. */
  body { padding: 0 3px; }
  figure, .kpi-row, figure.tbl { break-inside: avoid; }
  header.rpt { break-after: avoid; }
  a { color: inherit; text-decoration: none; }
  /* The PDF is the shareable executive copy: the methodology appendix (and
     the footnote marks that point into it) stays in the HTML viewer, where
     the evidence is auditable, and off the printed page. */
  .appendix, sup.note { display: none; }
}
"""


def _default_md() -> Callable[[str], str]:
    # Lazy so the package's import graph stays dependency-light; the chat
    # service (and this package's dev extra) declare markdown-it-py. The
    # "js-default" preset keeps raw HTML DISABLED — agent markdown cannot
    # smuggle markup — while tables/code/lists all render.
    from markdown_it import MarkdownIt

    return MarkdownIt("js-default").render


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _note_mark(note: int | None) -> str:
    """A discreet footnote marker — the executive-readable body never shows
    engineering provenance; the numbered note points at the methodology
    appendix where the audit trail lives."""
    if note is None:
        return ""
    return f'<sup class="note"><a href="#note-{note}">{note}</a></sup>'


def _appendix(notes: list[tuple[int, str, dict]]) -> str:
    """The auto-generated "Notes & methodology" appendix: one numbered entry
    per provenance-carrying figure. This is WHERE the trust story renders —
    verified computations named, ad-hoc SQL disclosed verbatim — so the body
    reads as a professional report while the evidence stays auditable."""
    if not notes:
        return ""
    items = []
    for n, label, prov in notes:
        if prov.get("kind") == "computation":
            slug = _esc(prov.get("slug"))
            params = prov.get("params")
            ptxt = (
                f", parameters <code>{_esc(json.dumps(params, sort_keys=True))}</code>"
                if isinstance(params, dict) and params
                else ""
            )
            chash = prov.get("content_hash")
            htxt = f" (content hash {_esc(str(chash)[:12])})" if chash else ""
            body = f"verified computation <code>{slug}</code>{ptxt}{htxt}."
        else:
            body = (
                "direct read-only query:"
                f"<pre><code>{_esc(prov.get('sql'))}</code></pre>"
            )
        items.append(
            f'<li id="note-{n}"><span class="src">{_esc(label)}</span> — {body}</li>'
        )
    return (
        '<section class="appendix"><h2>Notes &amp; methodology</h2>'
        f"<ol>{''.join(items)}</ol></section>"
    )


def _render_kpi(block: dict, note: int | None = None) -> str:
    delta = block.get("delta")
    delta_html = ""
    if isinstance(delta, (int, float)):
        cls = "up" if delta > 0 else "down" if delta < 0 else ""
        arrow = "▲" if delta > 0 else "▼" if delta < 0 else "•"
        label = block.get("delta_label")
        suffix = f" {_esc(label)}" if label else ""
        delta_html = (
            f'<div class="delta {cls}">{arrow} {_esc(delta)}{suffix}</div>'
        )
    return (
        '<div class="kpi">'
        f'<div class="label">{_esc(block.get("label"))}{_note_mark(note)}</div>'
        f'<div class="value">{_esc(block.get("value"))}</div>'
        f"{delta_html}</div>"
    )


def _render_table(block: dict, note: int | None = None) -> str:
    columns = block.get("columns") or []
    rows = block.get("rows") or []
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>"
        for row in rows
    )
    title = block.get("title")
    caption = ""
    if title or note is not None:
        caption = (
            f"<figcaption>{_esc(title) if title else ''}{_note_mark(note)}</figcaption>"
        )
    return (
        '<figure class="tbl">'
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        f"{caption}</figure>"
    )


def _chart_images(image: Any) -> tuple[str, str] | None:
    """Normalize a chart's ``images`` entry to ``(light, dark)`` data URIs.

    The runtime's two-pass renderer hands ``{"light": uri, "dark": uri}``; a
    bare string is the light-only back-compat shape (older callers/tests) and
    serves both themes. ``None`` for anything else — the composer refuses it.
    """
    if isinstance(image, str):
        return (image, image) if image.startswith("data:image/") else None
    if isinstance(image, dict):
        light = image.get("light")
        if not (isinstance(light, str) and light.startswith("data:image/")):
            return None
        dark = image.get("dark", light)
        if not (isinstance(dark, str) and dark.startswith("data:image/")):
            return None
        return (light, dark)
    return None


def _render_chart(
    block: dict, image: tuple[str, str], note: int | None = None
) -> str:
    # Both theme PNGs ship in the figure; the stylesheet's data-theme rules
    # display exactly one (light is also the no-attribute/PDF default).
    light, dark = image
    alt = _esc(block.get("title"))
    return (
        "<figure>"
        f'<img class="chart-light" src="{light}" alt="{alt}"/>'
        f'<img class="chart-dark" src="{dark}" alt="{alt}"/>'
        f"<figcaption>{_esc(block.get('title'))}{_note_mark(note)}</figcaption>"
        "</figure>"
    )


def compose_html(
    title: str,
    blocks: list[dict],
    images: dict[int, dict[str, str] | str],
    *,
    domain: str,
    dataset: str,
    generated_at: str,
    request: str = "",
    report_id: str = "",
    md_render: Callable[[str], str] | None = None,
) -> str:
    """Render validated blocks into one self-contained HTML document.

    ``images`` maps a chart block's INDEX to its pre-rendered transparent
    PNGs — ``{"light": uri, "dark": uri}`` from the runtime's two-pass
    rasterize, or a bare ``data:image/*`` URI (light-only back-compat). The
    runtime renders each spec through the harness before composing, so a
    chart that cannot render can never reach a report. Raises ``ValueError``
    on lint errors or a missing/non-data-URI image; callers treat that as
    refusal, same contract as ``lint_blocks``.
    """
    errors = lint_blocks(title, blocks)
    chart_images: dict[int, tuple[str, str]] = {}
    for i, block in enumerate(blocks if isinstance(blocks, list) else []):
        if isinstance(block, dict) and block.get("type") == "chart":
            pair = _chart_images(images.get(i))
            if pair is None:
                errors.append(f"blocks[{i}]: chart has no rendered image")
            else:
                chart_images[i] = pair
    if errors:
        raise ValueError("; ".join(errors))

    render_md = md_render or _default_md()
    out: list[str] = []
    kpi_run: list[str] = []
    # Numbered methodology notes, assigned in block order: the body carries
    # only a small footnote marker; the appendix carries the audit trail.
    notes: list[tuple[int, str, dict]] = []

    def note_for(block: dict, label: str) -> int | None:
        prov = block.get("provenance")
        if not isinstance(prov, dict):
            return None
        n = len(notes) + 1
        notes.append((n, label, prov))
        return n

    def flush_kpis() -> None:
        if kpi_run:
            out.append(f'<section class="kpi-row">{"".join(kpi_run)}</section>')
            kpi_run.clear()

    for i, block in enumerate(blocks):
        btype = block["type"]
        if btype == "kpi":
            # Consecutive KPIs group into one row of cards — the agent orders
            # blocks; adjacency IS the layout instruction.
            kpi_run.append(_render_kpi(block, note_for(block, str(block.get("label")))))
            continue
        flush_kpis()
        if btype == "markdown":
            out.append(f'<section class="md">{render_md(block["md"])}</section>')
        elif btype == "chart":
            out.append(
                _render_chart(
                    block, chart_images[i], note_for(block, str(block.get("title")))
                )
            )
        else:
            out.append(
                _render_table(
                    block, note_for(block, str(block.get("title") or "Table"))
                )
            )
    flush_kpis()

    request_html = (
        f'<div class="request">{_esc(request)}</div>' if request.strip() else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f"<title>{_esc(title)}</title>"
        f"<style>{_CSS}</style></head><body>"
        '<header class="rpt">'
        f"<h1>{_esc(title)}</h1>"
        '<div class="meta">'
        f"<span>{_esc(domain)} / {_esc(dataset)}</span>"
        f"<span>{_esc(generated_at)}</span>"
        "</div>"
        f"{request_html}"
        "</header>"
        f"<main>{''.join(out)}{_appendix(notes)}</main>"
        '<footer class="rpt">'
        f"Generated by Data Wiki · {_esc(report_id) if report_id else 'report'}"
        "</footer>"
        "</body></html>"
    )


def blocks_document(
    title: str,
    blocks: list[dict],
    *,
    request: str,
    domain: str = "",
    dataset: str = "",
    created_by: str = "",
    generated_at: str = "",
) -> str:
    """The persisted ``blocks.json`` — the report's SOURCE, stored beside the
    composed HTML so future re-styling/re-rendering never needs the agent.
    Self-describing (coordinates + authorship) because reports have no
    database row."""
    return json.dumps(
        {
            "version": REPORTS_VERSION,
            "title": title,
            "request": request,
            "domain": domain,
            "dataset": dataset,
            "created_by": created_by,
            "generated_at": generated_at,
            "blocks": blocks,
        },
        ensure_ascii=False,
        indent=2,
    )
