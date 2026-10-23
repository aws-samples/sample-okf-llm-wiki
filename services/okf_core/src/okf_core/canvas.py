"""Canvas (dashboard) config contract: schema, widget catalog, three-layer lint.

A canvas is a persisted, dial-driven dashboard: a grid of tiles, each binding
an Attested Computation's ``@parameters`` to dashboard-level dials (or fixed
values) and mapping the result columns onto the app's declarative
``renderChart`` spec via a small encoding grammar. Tiles are CONFIG, not code
— the renderer is a fixed per-widget binder in the UI (``ui/src/lib``), so
nothing model-authored executes at view time. This module owns the config's
invariants, pure and offline (no AWS, no agent deps), so the canvas CI
runtime's ``lint_dashboard`` tool, the serving path, and the tests share one
implementation — exactly the ``okf_core.lint`` arrangement, whose
finding/report model it reuses.

Lint runs as three isolated layers (steps in a :class:`~okf_core.lint.LintReport`):

1. ``static``   — shape alone: schema, widget catalog + channel slot
                  contracts, shape ops, formats, dial/tile uniqueness and
                  references, layout grid.
2. ``contract`` — against the referenced :class:`~okf_core.computations.Computation`
                  docs, nothing executed: computations exist, binds cover the
                  required parameters, fixed values and dial defaults satisfy
                  every bound parameter's declared constraints, one type per
                  dial.
3. ``live``     — through an injected executor: encoding/shape fields exist in
                  the result columns, numeric channels hold numbers, post-shape
                  row counts respect the widget caps, empty results at dial
                  defaults/extremes surface as warnings.

The split mirrors the computation trust model's contract-vs-evidence line:
static + contract failures REFUSE a config; live findings are about the data
a valid config meets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from okf_core.computations import (
    Computation,
    check_constraints,
    normalize_value,
)
from okf_core.lint import LintFinding, LintReport, LintStep

CANVAS_VERSION = 1

MAX_TILES = 24
MAX_DIALS = 8
MAX_SHAPE_OPS = 4
GRID_COLS = 12
MAX_TITLE = 120
MAX_LABEL = 40
# Live-layer execution budget: defaults for every tile first, then dial
# extremes while budget remains. A canvas that cannot be lint-executed inside
# this many runs is telling you something about the canvas.
MAX_LIVE_RUNS = 32

_IDENT_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][\w-]*$")
_FORMAT_RE = re.compile(
    r"^(text|number|integer|percent|date|datetime|currency:[A-Z]{3})$"
)

_SCALAR_TYPES = (str, int, float, bool)


# ---------------------------------------------------------------------------
# DynamoDB item conventions (control API + canvas CI runtime share these).
# ISOLATION is structural: the caller's sub lives in the partition key, so a
# query can only ever see its own canvases — the annotations pattern.
# ---------------------------------------------------------------------------

CANVAS_PK_PREFIX = "CANVAS#"
CANVAS_JOB_PK_PREFIX = "CANVASJOB#"

# Canvas lifecycle: the CI runtime CREATES the canvas in one step (the
# lint-gated save_canvas — no draft/submit two-phase); only a human flips
# created <-> verified (through the control API, identity from the JWT). A
# revision over a verified canvas returns it to created: re-verification is a
# fresh human decision.
STATUS_CREATED = "created"
STATUS_VERIFIED = "verified"
CANVAS_STATUSES = (STATUS_CREATED, STATUS_VERIFIED)

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_STATUSES = (JOB_QUEUED, JOB_RUNNING, JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED)
JOB_ACTIVE_STATUSES = (JOB_QUEUED, JOB_RUNNING)
# Job rows expire (DynamoDB TTL on `expires_at`, epoch seconds) — they are
# history, read only by the chat's jobs drawer; canvases themselves never
# carry the attribute. Mirrors the job-thread checkpoint TTL.
JOB_TTL_SECONDS = 30 * 86400


def canvas_pk(user_sub: str) -> str:
    return f"{CANVAS_PK_PREFIX}{user_sub}"


def canvas_job_pk(user_sub: str) -> str:
    return f"{CANVAS_JOB_PK_PREFIX}{user_sub}"


# ---------------------------------------------------------------------------
# Widget catalog — the opinionated surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Channel:
    """One encoding slot a widget accepts.

    ``multi`` channels take ``{"fields": [...]}`` (several measures);
    ``numeric`` channels are value-checked by the live layer.
    """

    required: bool = False
    multi: bool = False
    numeric: bool = False


@dataclass(frozen=True)
class Widget:
    """Slot contract + option vocabulary + row-count sanity caps for one
    widget type. ``warn_rows``/``max_rows`` apply to the POST-shape row count
    (what actually renders)."""

    channels: dict[str, Channel]
    options: frozenset[str]
    warn_rows: int
    max_rows: int


# `references` (lines/bands at literal values) is legal on every widget
# that has cartesian axes — validated structurally in _check_option_values.
_AXIS_OPTS = frozenset({"xLabel", "yLabel", "axes", "references"})

# The original chart types match ``charts.py`` SUPPORTED_CHART_TYPES (the
# binder maps each onto the same renderChart spec); the catalog then EXCEEDS
# the chat vocabulary with the BI staples chat has no need for: table, kpi,
# pivot, and the combo/heatmap/waterfall/funnel/gauge/histogram/boxplot set
# (rendered by the same iframe helpers, canvas-only by design).
WIDGETS: dict[str, Widget] = {
    "bar": Widget(
        channels={
            "x": Channel(required=True),
            "y": Channel(required=True, multi=True, numeric=True),
            "series": Channel(),
        },
        options=_AXIS_OPTS | {"stacked", "horizontal"},
        warn_rows=60,
        max_rows=500,
    ),
    "line": Widget(
        channels={
            "x": Channel(required=True),
            "y": Channel(required=True, multi=True, numeric=True),
            "series": Channel(),
        },
        options=_AXIS_OPTS | {"horizontal"},
        warn_rows=200,
        max_rows=2000,
    ),
    "area": Widget(
        channels={
            "x": Channel(required=True),
            "y": Channel(required=True, multi=True, numeric=True),
            "series": Channel(),
        },
        options=_AXIS_OPTS | {"stacked", "horizontal"},
        warn_rows=200,
        max_rows=2000,
    ),
    "pie": Widget(
        channels={
            "category": Channel(required=True),
            "value": Channel(required=True, numeric=True),
        },
        options=frozenset(),
        warn_rows=12,
        max_rows=50,
    ),
    "doughnut": Widget(
        channels={
            "category": Channel(required=True),
            "value": Channel(required=True, numeric=True),
        },
        options=frozenset(),
        warn_rows=12,
        max_rows=50,
    ),
    "polarArea": Widget(
        channels={
            "category": Channel(required=True),
            "value": Channel(required=True, numeric=True),
        },
        options=frozenset(),
        warn_rows=12,
        max_rows=50,
    ),
    "radar": Widget(
        channels={
            "category": Channel(required=True),
            "y": Channel(required=True, multi=True, numeric=True),
        },
        options=frozenset(),
        warn_rows=12,
        max_rows=30,
    ),
    "scatter": Widget(
        channels={
            "x": Channel(required=True, numeric=True),
            "y": Channel(required=True, numeric=True),
            "series": Channel(),
        },
        options=_AXIS_OPTS,
        warn_rows=1000,
        max_rows=2000,
    ),
    "bubble": Widget(
        channels={
            "x": Channel(required=True, numeric=True),
            "y": Channel(required=True, numeric=True),
            "r": Channel(required=True, numeric=True),
            "series": Channel(),
        },
        options=_AXIS_OPTS,
        warn_rows=200,
        max_rows=500,
    ),
    "sankey": Widget(
        channels={
            "from": Channel(required=True),
            "to": Channel(required=True),
            "flow": Channel(required=True, numeric=True),
        },
        options=frozenset(),
        warn_rows=60,
        max_rows=200,
    ),
    "treemap": Widget(
        channels={
            "label": Channel(required=True),
            "value": Channel(required=True, numeric=True),
            "group": Channel(),
        },
        options=frozenset(),
        warn_rows=60,
        max_rows=200,
    ),
    "table": Widget(
        # No columns channel -> every result column, in result order.
        channels={"columns": Channel(multi=True)},
        options=frozenset(),
        warn_rows=100,
        max_rows=200,
    ),
    "kpi": Widget(
        # One row, one big number; ``delta`` renders as the small
        # change-vs-reference figure next to it.
        channels={
            "value": Channel(required=True, numeric=True),
            "delta": Channel(numeric=True),
        },
        options=frozenset(),
        warn_rows=1,
        max_rows=1,
    ),
    "combo": Widget(
        # Mixed bars + lines over one x; `lines` ride a second y-scale when
        # `y2Label` is set (else they share the left axis).
        channels={
            "x": Channel(required=True),
            "bars": Channel(required=True, multi=True, numeric=True),
            "lines": Channel(required=True, multi=True, numeric=True),
        },
        options=_AXIS_OPTS | {"stacked", "y2Label"},
        warn_rows=200,
        max_rows=2000,
    ),
    "heatmap": Widget(
        channels={
            "x": Channel(required=True),
            "y": Channel(required=True),
            "value": Channel(required=True, numeric=True),
        },
        options=_AXIS_OPTS,
        warn_rows=400,
        max_rows=2000,
    ),
    "waterfall": Widget(
        # `value` rows are DELTAS from the SQL; optional `total` names a
        # truthy column marking running-total rows (drawn from zero). The
        # binder only converts deltas to bar positions — never derives values.
        channels={
            "x": Channel(required=True),
            "value": Channel(required=True, numeric=True),
            "total": Channel(),
        },
        options=_AXIS_OPTS,
        warn_rows=30,
        max_rows=100,
    ),
    "funnel": Widget(
        channels={
            "stage": Channel(required=True),
            "value": Channel(required=True, numeric=True),
        },
        options=frozenset(),
        warn_rows=12,
        max_rows=50,
    ),
    "gauge": Widget(
        # One number against a bounded range: numeric `max` option required,
        # `min` optional (default 0) — validated in _check_option_values.
        channels={"value": Channel(required=True, numeric=True)},
        options=frozenset({"min", "max"}),
        warn_rows=1,
        max_rows=1,
    ),
    "histogram": Widget(
        # PRE-BINNED IN SQL (floor/width_bucket): `bin` is the bucket label,
        # `count` its height. The renderer never bins raw values.
        channels={
            "bin": Channel(required=True),
            "count": Channel(required=True, numeric=True),
        },
        options=_AXIS_OPTS,
        warn_rows=100,
        max_rows=500,
    ),
    "boxplot": Widget(
        # STATISTICS COME FROM THE SQL: five-number summary per category
        # (approx_percentile/percentile_cont). The renderer never computes
        # quartiles from raw values.
        channels={
            "category": Channel(required=True),
            "min": Channel(required=True, numeric=True),
            "q1": Channel(required=True, numeric=True),
            "median": Channel(required=True, numeric=True),
            "q3": Channel(required=True, numeric=True),
            "max": Channel(required=True, numeric=True),
        },
        options=_AXIS_OPTS,
        warn_rows=30,
        max_rows=200,
    ),
    "pivot": Widget(
        # Long rows -> matrix layout (values unchanged). TOTALS COME FROM THE
        # SQL (GROUP BY ROLLUP): rows whose group columns are NULL render as
        # subtotal/total rows — the tile never sums cells client-side.
        # `rules` = declarative conditional formatting on values columns.
        channels={
            "rows": Channel(required=True, multi=True),
            "columns": Channel(),
            "values": Channel(required=True, multi=True, numeric=True),
        },
        options=frozenset({"rules"}),
        warn_rows=200,
        max_rows=1000,
    ),
}

# Widgets whose value semantics are a share of a whole: negative values are
# renderable but almost certainly a config mistake — warned, not refused.
_NONNEGATIVE_WIDGETS = frozenset(
    {"pie", "doughnut", "polarArea", "treemap", "sankey", "funnel", "histogram", "gauge"}
)  # waterfall is deliberately absent: its deltas are negative by design


# ---------------------------------------------------------------------------
# Skeleton — the requirements-time tile plan
# ---------------------------------------------------------------------------
#
# Captured in CHAT, before any authoring job runs: the chat agent and the user
# agree on each tile's title, widget type, and what it represents, plus the
# dials — and that plan rides the job payload as REQUIREMENTS for the CI
# runtime (which owns encodings, SQL, and layout detail). Deliberately looser
# than a canvas config: no bindings, no encodings — just enough structure to
# render a wireframe the user can react to. Validated here so the vocabulary
# (widget types, layout units) can never drift from the catalog above.

SKELETON_MAX_WIDGETS = 24
SKELETON_MAX_DIALS = 12
_SKELETON_DEFAULT_LAYOUT = {"w": 6, "h": 4}


def normalize_skeleton(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate + normalize a skeleton spec; returns ``(normalized, errors)``.

    ``normalized`` keeps only the known keys (title / widgets / dials, each
    widget's title / widget / represents / layout, each dial's name / label /
    filters) with layout defaults filled — the exact dict that should ride the
    job payload. ``errors`` empty means valid; messages are corrective (they
    name the offending entry and the accepted vocabulary)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["the spec must be an object with title + widgets (+ dials)"]

    title = str(raw.get("title") or "").strip()
    if not title:
        errors.append("spec.title: a short human dashboard name is required")

    widgets_raw = raw.get("widgets")
    widgets: list[dict[str, Any]] = []
    if not isinstance(widgets_raw, list) or not widgets_raw:
        errors.append("spec.widgets: at least one widget is required")
        widgets_raw = []
    if len(widgets_raw) > SKELETON_MAX_WIDGETS:
        errors.append(f"spec.widgets: at most {SKELETON_MAX_WIDGETS} widgets")
        widgets_raw = widgets_raw[:SKELETON_MAX_WIDGETS]
    for i, w in enumerate(widgets_raw):
        where = f"widgets[{i}]"
        if not isinstance(w, dict):
            errors.append(f"{where}: each widget must be an object")
            continue
        wtitle = str(w.get("title") or "").strip()
        if not wtitle:
            errors.append(f"{where}.title: a business-language tile title is required")
        wtype = str(w.get("widget") or "").strip()
        if wtype not in WIDGETS:
            errors.append(
                f"{where}.widget: {wtype!r} is not in the catalog "
                f"({', '.join(sorted(WIDGETS))})"
            )
        represents = str(w.get("represents") or "").strip()
        if not represents:
            errors.append(
                f"{where}.represents: one line on what this tile shows is required"
            )
        layout = dict(_SKELETON_DEFAULT_LAYOUT)
        lraw = w.get("layout")
        if isinstance(lraw, dict):
            for k in ("w", "h"):
                if lraw.get(k) is None:
                    continue
                try:
                    v = int(lraw[k])
                except (TypeError, ValueError):
                    errors.append(f"{where}.layout.{k}: an integer (grid units)")
                    continue
                if not 1 <= v <= 12:
                    errors.append(f"{where}.layout.{k}: 1..12 (12-column grid)")
                else:
                    layout[k] = v
        elif lraw is not None:
            errors.append(f"{where}.layout: an object like {{'w': 6, 'h': 4}}")
        widgets.append(
            {"title": wtitle, "widget": wtype, "represents": represents, "layout": layout}
        )

    dials_raw = raw.get("dials") or []
    dials: list[dict[str, Any]] = []
    if not isinstance(dials_raw, list):
        errors.append("spec.dials: a list of {name, label, filters}")
        dials_raw = []
    if len(dials_raw) > SKELETON_MAX_DIALS:
        errors.append(f"spec.dials: at most {SKELETON_MAX_DIALS} dials")
        dials_raw = dials_raw[:SKELETON_MAX_DIALS]
    seen: set[str] = set()
    for i, d in enumerate(dials_raw):
        where = f"dials[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{where}: each dial must be an object")
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            errors.append(f"{where}.name: a short identifier is required")
        elif name in seen:
            errors.append(f"{where}.name: duplicate dial {name!r}")
        seen.add(name)
        dials.append(
            {
                "name": name,
                "label": str(d.get("label") or "").strip() or name,
                "filters": str(d.get("filters") or "").strip(),
            }
        )

    return {"title": title, "widgets": widgets, "dials": dials}, errors


# ---------------------------------------------------------------------------
# Parsed model
# ---------------------------------------------------------------------------


@dataclass
class Dial:
    """One dashboard-level control. The CONTROL TYPE is not stored — the UI
    derives it from the bound parameter's contract (enum → dropdown, min/max →
    slider, date → picker, evidence domains → type-ahead), so the config can't
    contradict the computation doc."""

    name: str
    default: Any
    label: str = ""


@dataclass
class Tile:
    id: str
    title: str
    widget: str
    # A tile's data source is EITHER a bundle computation (slug) or an inline
    # ad-hoc query (`sql` + optional inline `parameters`, the same typed
    # contract shape computation docs declare — see adhoc_computation).
    computation: str = ""
    sql: str = ""
    parameters: list[Any] | None = None
    # name -> {"dial": <dial name>} | {"value": <scalar>}
    bind: dict[str, dict[str, Any]] = field(default_factory=dict)
    shape: list[dict[str, Any]] = field(default_factory=list)
    # channel -> {"fields": [..], "label": str|None, "format": str|None};
    # single-field channels normalize to a one-element fields list.
    encoding: dict[str, dict[str, Any]] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, int] = field(default_factory=dict)


@dataclass
class Canvas:
    title: str
    data_domain: str
    dataset: str
    description: str = ""
    dials: list[Dial] = field(default_factory=list)
    tiles: list[Tile] = field(default_factory=list)

    def dial(self, name: str) -> Dial | None:
        return next((d for d in self.dials if d.name == name), None)


def source_runtime(source: dict[str, Any] | None) -> str:
    """The execution runtime for ad-hoc tile SQL under a dataset's source
    descriptor: redshift sources execute on Redshift, everything else (glue,
    or no mapping at all) on Athena. Single-sourced here so the CI lint, the
    tile-run path, and the column-domain endpoint can't disagree."""
    return "redshift" if (source or {}).get("type") == "redshift" else "athena"


def adhoc_computation(
    tile_id: str, sql: str, parameters: list[Any] | None, runtime: str = "athena"
) -> tuple[Any, list[str]]:
    """Synthesize a :class:`~okf_core.computations.Computation` from a tile's
    inline SQL — ``(comp, errors)``, comp None when errors exist.

    An ad-hoc tile IS an unattested computation defined inline: by routing it
    through ``parse_computation_text`` it inherits the ENTIRE computation
    contract for free — exactly one read-only SELECT/WITH statement, declared
    parameters == the ``@holes`` in the SQL, typed constraints, no authoring
    placeholders. That is what keeps "arbitrary SQL" safe to execute and
    bindable to dials.
    """
    import yaml

    from okf_core.computations import parse_computation_text

    fm: dict[str, Any] = {
        "type": "Attested Computation",
        "title": f"inline query (tile {tile_id})",
        "description": "ad-hoc canvas tile query",
        "timestamp": "inline",
        # The runtime routes execution AND literal dialect (render_literal) —
        # a hardcoded "athena" made every ad-hoc tile dead on redshift-mapped
        # datasets. Callers derive it from the dataset's source descriptor
        # (source_runtime); anything unrecognized falls back to athena.
        "runtime": runtime if runtime in ("athena", "redshift") else "athena",
        "parameters": parameters or [],
        "verified": None,
        "verified_by": None,
        "verified_sha256": None,
    }
    text = (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
        + "---\n\n# Computation\n\n```sql\n"
        + sql.strip()
        + "\n```\n"
    )
    comp, errors = parse_computation_text(
        f"references/computations/_adhoc_{tile_id}.md", text
    )
    return comp, [f"inline sql: {e}" for e in errors]


def resolve_tile_computation(
    tile: Tile, computations: dict[str, Any], adhoc_runtime: str = "athena"
) -> tuple[Any, list[LintFinding]]:
    """The Computation a tile executes: the bundle doc for slug tiles, the
    synthesized inline one for sql tiles — ``(comp, findings)``.
    ``adhoc_runtime`` (see :func:`source_runtime`) routes inline SQL to the
    dataset's engine; bundle docs carry their own."""
    where = f"tile `{tile.id}`"
    if tile.sql:
        comp, errors = adhoc_computation(
            tile.id, tile.sql, tile.parameters, adhoc_runtime
        )
        return comp, [_err("canvas-adhoc", where, e) for e in errors]
    comp = computations.get(tile.computation)
    if comp is None:
        return None, [
            _err(
                "canvas-computation", where,
                f"computation `{tile.computation}` does not exist in this dataset",
            )
        ]
    return comp, []


def dial_values(canvas: Canvas) -> dict[str, Any]:
    """The canvas's initial dial state (every dial carries a default)."""
    return {d.name: d.default for d in canvas.dials}


def tile_values(tile: Tile, dials: dict[str, Any]) -> dict[str, Any]:
    """A tile's parameter values under the given dial state: fixed binds
    verbatim, dial binds looked up. The serving path and the live lint share
    this so they cannot disagree about what a tile executes."""
    out: dict[str, Any] = {}
    for name, bind in tile.bind.items():
        if "value" in bind:
            out[name] = bind["value"]
        else:
            out[name] = dials[bind["dial"]]
    return out


# ---------------------------------------------------------------------------
# Layer 1: static
# ---------------------------------------------------------------------------


def _err(code: str, path: str, message: str) -> LintFinding:
    return LintFinding(severity="error", code=code, path=path, message=message)


def _warn(code: str, path: str, message: str) -> LintFinding:
    return LintFinding(severity="warning", code=code, path=path, message=message)


def _norm_channel(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """One encoding channel -> ``{"fields", "label", "format"}`` or an error.
    Accepts ``{"field": "a"}`` or ``{"fields": ["a", "b"]}``."""
    if not isinstance(raw, dict):
        return None, "must be a mapping"
    unknown = sorted(set(raw) - {"field", "fields", "label", "format"})
    if unknown:
        return None, f"unknown key(s) {', '.join(unknown)}"
    if ("field" in raw) == ("fields" in raw):
        return None, "needs exactly one of `field` / `fields`"
    if "field" in raw:
        fields = [raw["field"]]
    else:
        fields = raw["fields"]
        if not isinstance(fields, list) or not fields:
            return None, "`fields` must be a non-empty list"
    for f in fields:
        if not isinstance(f, str) or not f.strip():
            return None, "field names must be non-empty strings"
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        return None, "`label` must be a string"
    fmt = raw.get("format")
    if fmt is not None and (
        not isinstance(fmt, str) or not _FORMAT_RE.fullmatch(fmt)
    ):
        return None, (
            "`format` must be text|number|integer|percent|date|datetime|"
            "currency:<ISO code>"
        )
    return {"fields": [f.strip() for f in fields], "label": label, "format": fmt}, None


def _check_shape_ops(raw: Any, where: str) -> tuple[list[dict[str, Any]], list[LintFinding]]:
    findings: list[LintFinding] = []
    if raw is None:
        return [], findings
    if not isinstance(raw, list):
        return [], [_err("canvas-shape", where, "`shape` must be a list of ops")]
    if len(raw) > MAX_SHAPE_OPS:
        return [], [
            _err(
                "canvas-shape",
                where,
                f"at most {MAX_SHAPE_OPS} shape ops (got {len(raw)}) — heavier "
                f"shaping belongs in the computation's SQL",
            )
        ]
    ops: list[dict[str, Any]] = []
    for i, op in enumerate(raw):
        w = f"{where} shape[{i}]"
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            findings.append(_err("canvas-shape", w, "each op is a mapping with `op`"))
            continue
        kind = op["op"]
        if kind == "sort":
            extra = sorted(set(op) - {"op", "by", "dir"})
            direction = op.get("dir", "asc")
            if extra or not isinstance(op.get("by"), str) or direction not in ("asc", "desc"):
                findings.append(
                    _err("canvas-shape", w, "sort takes `by` (column) and optional `dir`: asc|desc")
                )
                continue
            ops.append({"op": "sort", "by": op["by"], "dir": direction})
        elif kind == "topN":
            extra = sorted(set(op) - {"op", "n", "by"})
            n = op.get("n")
            if extra or not isinstance(op.get("by"), str) or not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 100:
                findings.append(
                    _err("canvas-shape", w, "topN takes `n` (1..100) and `by` (column)")
                )
                continue
            ops.append({"op": "topN", "n": n, "by": op["by"]})
        elif kind == "limit":
            extra = sorted(set(op) - {"op", "n"})
            n = op.get("n")
            if extra or not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 1000:
                findings.append(_err("canvas-shape", w, "limit takes `n` (1..1000)"))
                continue
            ops.append({"op": "limit", "n": n})
        else:
            findings.append(
                _err("canvas-shape", w, f"unknown op `{kind}` — sort | topN | limit")
            )
    return (ops, findings)


def _check_bind(raw: Any, where: str) -> tuple[dict[str, dict[str, Any]], list[LintFinding]]:
    findings: list[LintFinding] = []
    if raw is None:
        return {}, findings
    if not isinstance(raw, dict):
        return {}, [_err("canvas-bind", where, "`bind` must be a mapping of parameter -> source")]
    out: dict[str, dict[str, Any]] = {}
    for name, src in raw.items():
        w = f"{where} bind `{name}`"
        if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
            findings.append(_err("canvas-bind", where, f"bad parameter name {name!r}"))
            continue
        if not isinstance(src, dict) or ("dial" in src) == ("value" in src) or set(src) - {"dial", "value"}:
            findings.append(
                _err("canvas-bind", w, 'must be exactly {"dial": <name>} or {"value": <scalar>}')
            )
            continue
        if "dial" in src:
            if not isinstance(src["dial"], str):
                findings.append(_err("canvas-bind", w, "`dial` must name a dial"))
                continue
            out[name] = {"dial": src["dial"]}
        else:
            v = src["value"]
            if not isinstance(v, _SCALAR_TYPES):
                findings.append(_err("canvas-bind", w, "`value` must be a scalar"))
                continue
            out[name] = {"value": v}
    return out, findings


def _check_layout(raw: Any, where: str) -> tuple[dict[str, int], list[LintFinding]]:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"x", "y", "w", "h"}
        or not all(isinstance(raw[k], int) and not isinstance(raw[k], bool) for k in ("x", "y", "w", "h"))
    ):
        return {}, [_err("canvas-layout", where, "`layout` must be {x, y, w, h} integers")]
    x, y, w, h = raw["x"], raw["y"], raw["w"], raw["h"]
    if not (0 <= x < GRID_COLS and 1 <= w <= GRID_COLS and x + w <= GRID_COLS):
        return {}, [
            _err("canvas-layout", where, f"tile must fit the {GRID_COLS}-column grid (x + w <= {GRID_COLS})")
        ]
    if y < 0 or not 1 <= h <= GRID_COLS:
        return {}, [_err("canvas-layout", where, f"`y` >= 0 and 1 <= `h` <= {GRID_COLS}")]
    return {"x": x, "y": y, "w": w, "h": h}, []


def layout_overlaps(
    placed: list[tuple[str, dict[str, int]]],
) -> list[tuple[str, str]]:
    """Pairs of tile ids whose ``{x, y, w, h}`` rectangles intersect."""
    out: list[tuple[str, str]] = []
    for i, (id_a, a) in enumerate(placed):
        for id_b, b in placed[i + 1 :]:
            if (
                a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]
            ):
                out.append((id_a, id_b))
    return out


def apply_layout_patch(
    config: dict[str, Any], layouts: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Patch ONLY tile geometry on a stored config; ``(patched, errors)``.

    The one config mutation a human makes directly (dragging/resizing tiles
    on the Canvas page) — everything else still enters through the CI's
    lint-gated save. ``layouts`` maps tile id -> ``{x, y, w, h}``; each entry
    is validated by the same ``_check_layout`` the lint uses, ids must name
    existing tiles, and the patch can touch nothing but ``tile["layout"]`` by
    construction — data, SQL, and encodings are structurally out of reach.
    Geometry can't change what the numbers mean, which is why a verified
    canvas keeps its status through this patch (the caller's contract)."""
    import copy

    errors: list[str] = []
    if not isinstance(layouts, dict) or not layouts:
        return None, ["layouts must be a non-empty object of tile id -> {x, y, w, h}"]
    known = {
        t.get("id")
        for t in (config.get("tiles") or [])
        if isinstance(t, dict) and t.get("id")
    }
    checked: dict[str, dict[str, int]] = {}
    for tile_id, raw in layouts.items():
        if tile_id not in known:
            errors.append(f"unknown tile {tile_id!r}")
            continue
        layout, findings = _check_layout(raw, f"tile `{tile_id}`")
        if findings:
            errors.extend(f"{f.path}: {f.message}" for f in findings)
            continue
        checked[tile_id] = layout
    if errors:
        return None, errors
    patched = copy.deepcopy(config)
    for t in patched.get("tiles") or []:
        if isinstance(t, dict) and t.get("id") in checked:
            t["layout"] = checked[t["id"]]
    final = [
        (t["id"], t["layout"])
        for t in patched.get("tiles") or []
        if isinstance(t, dict) and isinstance(t.get("layout"), dict)
        and {"x", "y", "w", "h"} <= set(t["layout"])
    ]
    overlaps = layout_overlaps(final)
    if overlaps:
        return None, [f"tile `{a}` overlaps tile `{b}`" for a, b in overlaps]
    return patched, []


_RULE_COLORS = frozenset({"emerald", "amber", "red", "sky", "muted"})


def _check_option_values(
    widget: str, options: dict[str, Any], where: str
) -> list[LintFinding]:
    """Value-level checks for STRUCTURED options. Key membership is already
    gated by the widget catalog; this validates the shapes so the binder can
    render them blindly: `references` (annotation lines/bands at literal
    values), pivot `rules` (declarative conditional formatting), and gauge
    bounds."""

    # Config LITERALS must be real numbers — the live layer's _is_numeric is
    # for data cells (where null is a data fact, not a type violation).
    def _num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    findings: list[LintFinding] = []
    refs = options.get("references")
    if refs is not None:
        if not isinstance(refs, list):
            findings.append(_err("canvas-options", where, "`references` must be a list"))
            refs = []
        for i, ref in enumerate(refs):
            r = f"references[{i}]"
            if not isinstance(ref, dict):
                findings.append(_err("canvas-options", where, f"{r}: must be an object"))
                continue
            unknown = sorted(set(ref) - {"axis", "value", "from", "to", "label"})
            if unknown:
                findings.append(
                    _err("canvas-options", where, f"{r}: unknown key(s) {', '.join(unknown)}")
                )
            if ref.get("axis", "y") not in ("x", "y"):
                findings.append(_err("canvas-options", where, f'{r}: `axis` is "x" or "y"'))
            has_value = "value" in ref
            has_band = "from" in ref or "to" in ref
            if has_value == has_band or ("from" in ref) != ("to" in ref):
                findings.append(
                    _err(
                        "canvas-options", where,
                        f"{r}: give `value` (a line) OR `from`+`to` (a band)",
                    )
                )
            for k in ("value", "from", "to"):
                if k in ref and not _num(ref[k]):
                    findings.append(_err("canvas-options", where, f"{r}: `{k}` must be numeric"))
            if "label" in ref and not isinstance(ref["label"], str):
                findings.append(_err("canvas-options", where, f"{r}: `label` must be a string"))
    rules = options.get("rules")
    if rules is not None:
        if not isinstance(rules, list):
            findings.append(_err("canvas-options", where, "`rules` must be a list"))
            rules = []
        for i, rule in enumerate(rules):
            r = f"rules[{i}]"
            if not isinstance(rule, dict):
                findings.append(_err("canvas-options", where, f"{r}: must be an object"))
                continue
            unknown = sorted(set(rule) - {"field", "min", "max", "color"})
            if unknown:
                findings.append(
                    _err("canvas-options", where, f"{r}: unknown key(s) {', '.join(unknown)}")
                )
            if not isinstance(rule.get("field"), str) or not rule.get("field"):
                findings.append(
                    _err("canvas-options", where, f"{r}: `field` names a values column")
                )
            if "min" not in rule and "max" not in rule:
                findings.append(_err("canvas-options", where, f"{r}: give `min` and/or `max`"))
            for k in ("min", "max"):
                if k in rule and not _num(rule[k]):
                    findings.append(_err("canvas-options", where, f"{r}: `{k}` must be numeric"))
            if rule.get("color") not in _RULE_COLORS:
                findings.append(
                    _err(
                        "canvas-options", where,
                        f"{r}: `color` is one of {', '.join(sorted(_RULE_COLORS))}",
                    )
                )
    if widget == "gauge":
        gmin = options.get("min", 0)
        gmax = options.get("max")
        if not _num(gmax):
            findings.append(
                _err(
                    "canvas-options", where,
                    "gauge needs a numeric `max` option (and optional `min`, default 0)",
                )
            )
        elif not _num(gmin) or float(gmin) >= float(gmax):
            findings.append(_err("canvas-options", where, "gauge needs numeric `min` < `max`"))
    return findings


def parse_canvas(raw: Any) -> tuple[Canvas | None, list[LintFinding]]:
    """The STATIC layer: shape-check a raw config. Returns ``(canvas, findings)``
    — canvas is None when any finding is an error. Warnings (layout overlap)
    ride along either way."""
    findings: list[LintFinding] = []
    if not isinstance(raw, dict):
        return None, [_err("canvas-schema", "", "config must be a JSON object")]
    if raw.get("canvas_version") != CANVAS_VERSION:
        return None, [
            _err("canvas-schema", "", f"`canvas_version` must be {CANVAS_VERSION}")
        ]
    allowed = {
        "canvas_version", "title", "description", "data_domain", "dataset",
        "dials", "tiles",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        findings.append(_err("canvas-schema", "", f"unknown key(s): {', '.join(unknown)}"))

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
        findings.append(_err("canvas-schema", "", f"`title` is required (<= {MAX_TITLE} chars)"))
        title = ""
    description = raw.get("description") or ""
    if not isinstance(description, str):
        findings.append(_err("canvas-schema", "", "`description` must be a string"))
        description = ""
    domain, dataset = raw.get("data_domain"), raw.get("dataset")
    for key, v in (("data_domain", domain), ("dataset", dataset)):
        if not isinstance(v, str) or not v.strip():
            findings.append(_err("canvas-schema", "", f"`{key}` is required"))

    # Dials.
    dials: list[Dial] = []
    raw_dials = raw.get("dials") or []
    if not isinstance(raw_dials, list) or len(raw_dials) > MAX_DIALS:
        findings.append(_err("canvas-dial", "", f"`dials` must be a list of at most {MAX_DIALS}"))
        raw_dials = []
    seen_dials: set[str] = set()
    for i, d in enumerate(raw_dials):
        where = f"dials[{i}]"
        if not isinstance(d, dict) or set(d) - {"name", "label", "default"}:
            findings.append(_err("canvas-dial", where, "dial takes name, default, optional label"))
            continue
        name = d.get("name")
        if not isinstance(name, str) or not _IDENT_RE.fullmatch(name):
            findings.append(_err("canvas-dial", where, "`name` must be an identifier"))
            continue
        if name in seen_dials:
            findings.append(_err("canvas-dial", where, f"duplicate dial `{name}`"))
            continue
        seen_dials.add(name)
        if "default" not in d or not isinstance(d["default"], _SCALAR_TYPES):
            # The default IS the canvas's initial state — without one the
            # view has nothing to execute on first load.
            findings.append(_err("canvas-dial", f"dial `{name}`", "`default` (scalar) is required"))
            continue
        label = d.get("label") or ""
        if not isinstance(label, str) or len(label) > MAX_LABEL:
            findings.append(_err("canvas-dial", f"dial `{name}`", f"`label` must be a string (<= {MAX_LABEL} chars)"))
            continue
        dials.append(Dial(name=name, default=d["default"], label=label))

    # Tiles.
    tiles: list[Tile] = []
    raw_tiles = raw.get("tiles")
    if not isinstance(raw_tiles, list) or not raw_tiles or len(raw_tiles) > MAX_TILES:
        findings.append(_err("canvas-tile", "", f"`tiles` must be a non-empty list of at most {MAX_TILES}"))
        raw_tiles = []
    seen_tiles: set[str] = set()
    bound_dials: set[str] = set()
    for i, t in enumerate(raw_tiles):
        where = f"tiles[{i}]"
        if not isinstance(t, dict):
            findings.append(_err("canvas-tile", where, "tile must be a mapping"))
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not _IDENT_RE.fullmatch(tid):
            findings.append(_err("canvas-tile", where, "`id` must be an identifier"))
            continue
        where = f"tile `{tid}`"
        if tid in seen_tiles:
            findings.append(_err("canvas-tile", where, "duplicate tile id"))
            continue
        seen_tiles.add(tid)
        unknown = sorted(set(t) - {"id", "title", "widget", "data", "shape", "encoding", "options", "layout"})
        if unknown:
            findings.append(_err("canvas-tile", where, f"unknown key(s): {', '.join(unknown)}"))
            continue
        ttitle = t.get("title")
        if not isinstance(ttitle, str) or not ttitle.strip() or len(ttitle) > MAX_TITLE:
            findings.append(_err("canvas-tile", where, f"`title` is required (<= {MAX_TITLE} chars)"))
            continue
        widget_name = t.get("widget")
        spec = WIDGETS.get(widget_name) if isinstance(widget_name, str) else None
        if spec is None:
            findings.append(
                _err("canvas-widget", where, f"unknown widget {widget_name!r} — one of: {', '.join(sorted(WIDGETS))}")
            )
            continue

        data = t.get("data")
        if not isinstance(data, dict) or set(data) - {"computation", "sql", "parameters", "bind"}:
            findings.append(
                _err(
                    "canvas-data", where,
                    "`data` takes computation OR sql (+ optional parameters), plus bind",
                )
            )
            continue
        slug = data.get("computation")
        sql = data.get("sql")
        if (slug is None) == (sql is None):
            findings.append(
                _err("canvas-data", where, "`data` needs exactly one of `computation` / `sql`")
            )
            continue
        if slug is not None and (
            not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug)
        ):
            findings.append(_err("canvas-data", where, "`data.computation` must be a computation slug"))
            continue
        if sql is not None and (not isinstance(sql, str) or not sql.strip()):
            findings.append(_err("canvas-data", where, "`data.sql` must be a non-empty string"))
            continue
        raw_params = data.get("parameters")
        if raw_params is not None and (sql is None or not isinstance(raw_params, list)):
            findings.append(
                _err(
                    "canvas-data", where,
                    "`data.parameters` is a list and only valid with `data.sql` "
                    "(slug tiles take their contract from the computation doc)",
                )
            )
            continue
        bind, bfind = _check_bind(data.get("bind"), where)
        findings += bfind
        if any(f.severity == "error" for f in bfind):
            continue
        for src in bind.values():
            if "dial" in src:
                if src["dial"] not in seen_dials:
                    findings.append(_err("canvas-bind", where, f"bind references unknown dial `{src['dial']}`"))
                else:
                    bound_dials.add(src["dial"])

        shape, sfind = _check_shape_ops(t.get("shape"), where)
        findings += sfind
        if any(f.severity == "error" for f in sfind):
            continue

        raw_enc = t.get("encoding") or {}
        if not isinstance(raw_enc, dict):
            findings.append(_err("canvas-encoding", where, "`encoding` must be a mapping"))
            continue
        encoding: dict[str, dict[str, Any]] = {}
        enc_ok = True
        for ch_name, ch_raw in raw_enc.items():
            ch_spec = spec.channels.get(ch_name)
            if ch_spec is None:
                findings.append(
                    _err(
                        "canvas-encoding", where,
                        f"widget `{widget_name}` has no channel `{ch_name}` — "
                        f"channels: {', '.join(sorted(spec.channels))}",
                    )
                )
                enc_ok = False
                continue
            norm, err = _norm_channel(ch_raw)
            if err:
                findings.append(_err("canvas-encoding", where, f"channel `{ch_name}`: {err}"))
                enc_ok = False
                continue
            if not ch_spec.multi and len(norm["fields"]) != 1:
                findings.append(
                    _err("canvas-encoding", where, f"channel `{ch_name}` takes exactly one field")
                )
                enc_ok = False
                continue
            encoding[ch_name] = norm
        for ch_name, ch_spec in spec.channels.items():
            if ch_spec.required and ch_name not in encoding:
                findings.append(
                    _err("canvas-encoding", where, f"widget `{widget_name}` requires channel `{ch_name}`")
                )
                enc_ok = False
        # Wide XOR long: several y measures already make the series; a series
        # column on top would mean two competing series dimensions.
        if (
            "series" in encoding
            and "y" in encoding
            and len(encoding["y"]["fields"]) > 1
        ):
            findings.append(
                _err("canvas-encoding", where, "`series` requires a single `y` field (wide XOR long)")
            )
            enc_ok = False
        if not enc_ok:
            continue

        options = t.get("options") or {}
        if not isinstance(options, dict):
            findings.append(_err("canvas-options", where, "`options` must be a mapping"))
            continue
        bad_opts = sorted(set(options) - spec.options)
        if bad_opts:
            allowed_opts = ", ".join(sorted(spec.options)) or "none"
            findings.append(
                _err(
                    "canvas-options", where,
                    f"widget `{widget_name}` does not take option(s) "
                    f"{', '.join(bad_opts)} — allowed: {allowed_opts}",
                )
            )
            continue
        vfind = _check_option_values(widget_name, options, where)
        findings += vfind
        if any(f.severity == "error" for f in vfind):
            continue

        layout, lfind = _check_layout(t.get("layout"), where)
        findings += lfind
        if any(f.severity == "error" for f in lfind):
            continue

        tiles.append(
            Tile(
                id=tid, title=ttitle.strip(), widget=widget_name,
                computation=slug or "", sql=(sql or "").strip(),
                parameters=raw_params, bind=bind, shape=shape,
                encoding=encoding, options=dict(options), layout=layout,
            )
        )

    for d in dials:
        if d.name not in bound_dials:
            findings.append(
                _err("canvas-dial", f"dial `{d.name}`", "no tile binds this dial — remove it or bind it")
            )

    # Overlap: the page places tiles at EXPLICIT grid cells, so overlapping
    # cards paint over each other. parse only WARNS (a stored config must
    # still read and run its tiles); every WRITE path refuses it — lint_canvas
    # promotes these to errors (the CI's save gate) and apply_layout_patch
    # rejects them (the human editor).
    for id_a, id_b in layout_overlaps([(t.id, t.layout) for t in tiles]):
        findings.append(
            _warn("canvas-layout", f"tile `{id_a}`", f"overlaps tile `{id_b}`")
        )

    if any(f.severity == "error" for f in findings):
        return None, findings
    return (
        Canvas(
            title=title.strip(), description=description,
            data_domain=str(domain), dataset=str(dataset),
            dials=dials, tiles=tiles,
        ),
        findings,
    )


# ---------------------------------------------------------------------------
# Layer 2: contract (vs the computation docs)
# ---------------------------------------------------------------------------


def check_contract(
    canvas: Canvas,
    computations: dict[str, Computation],
    adhoc_runtime: str = "athena",
) -> list[LintFinding]:
    """Validate binds and dials against the referenced computations' declared
    contracts. Nothing executes. ``computations`` maps slug -> parsed doc
    (missing slugs are findings, not KeyErrors)."""
    findings: list[LintFinding] = []
    # dial name -> list of (tile id, param dict) it must satisfy.
    dial_params: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    for tile in canvas.tiles:
        where = f"tile `{tile.id}`"
        comp, rfindings = resolve_tile_computation(tile, computations, adhoc_runtime)
        findings += rfindings
        if comp is None:
            continue
        declared = {p["name"]: p for p in comp.parameters}
        for name in sorted(set(tile.bind) - set(declared)):
            legal = ", ".join(sorted(declared)) or "none — it takes no parameters"
            findings.append(
                _err("canvas-bind", where, f"`{tile.computation}` has no parameter `{name}` (declared: {legal})")
            )
        for p in comp.parameters:
            if p["required"] and p["name"] not in tile.bind:
                findings.append(
                    _err("canvas-bind", where, f"required parameter `{p['name']}` ({p['type']}) is not bound")
                )
        for name, src in tile.bind.items():
            p = declared.get(name)
            if p is None:
                continue
            if "value" in src:
                normalized, err = normalize_value(p["type"], src["value"])
                err = err or check_constraints(p, normalized)
                if err:
                    findings.append(_err("canvas-bind", where, f"fixed value for `{name}`: {err}"))
            else:
                dial_params.setdefault(src["dial"], []).append((tile.id, p))

    for dial in canvas.dials:
        bound = dial_params.get(dial.name, [])
        where = f"dial `{dial.name}`"
        types = sorted({p["type"] for _, p in bound})
        if len(types) > 1:
            findings.append(
                _err(
                    "canvas-dial", where,
                    f"bound to parameters of mixed types ({', '.join(types)}) — "
                    f"one dial drives one value type",
                )
            )
            continue
        # The default must satisfy EVERY bound parameter's contract — it is
        # the value the canvas executes on first load.
        for tile_id, p in bound:
            normalized, err = normalize_value(p["type"], dial.default)
            err = err or check_constraints(p, normalized)
            if err:
                findings.append(
                    _err("canvas-dial", where, f"default violates `{p['name']}` on tile `{tile_id}`: {err}")
                )
    return findings


# ---------------------------------------------------------------------------
# Layer 3: live (through an injected executor)
# ---------------------------------------------------------------------------

# execute(comp: Computation, values) -> {"columns": [...], "rows": [[...]]}
# Object-based (not slug-based) so the SAME executor runs bundle computations
# and synthesized ad-hoc tiles — computation_run.run_computation takes the
# object either way.
Executor = Callable[[Any, dict[str, Any]], dict[str, Any]]


def _sort_key(v: Any) -> tuple[int, float, str]:
    """Comparable-across-types sort key: numbers first (numeric-LOOKING
    strings count — Athena returns every value as a string, and a topN by
    revenue must never sort '45' above '123'), then text, booleans as text."""
    if not isinstance(v, bool):
        if isinstance(v, (int, float)):
            return (0, float(v), "")
        if isinstance(v, str):
            try:
                return (0, float(v), "")
            except ValueError:
                return (1, 0.0, v)
    return (1, 0.0, str(v))


def apply_shape(
    ops: list[dict[str, Any]], columns: list[str], rows: list[list[Any]]
) -> list[list[Any]]:
    """Apply a tile's shape ops. The JS binder implements the same semantics
    (ui/src/lib/canvasBind.js) — stable sort, None LAST whatever the
    direction, topN = sort desc by the column then head(n)."""
    lower = [c.lower() for c in columns]

    def sorted_by(items: list[list[Any]], name: str, desc: bool) -> list[list[Any]]:
        i = lower.index(name.lower())
        present = [r for r in items if r[i] is not None]
        absent = [r for r in items if r[i] is None]
        present.sort(key=lambda r: _sort_key(r[i]), reverse=desc)
        return present + absent

    out = list(rows)
    for op in ops:
        if op["op"] == "sort":
            out = sorted_by(out, op["by"], op.get("dir") == "desc")
        elif op["op"] == "topN":
            out = sorted_by(out, op["by"], True)[: op["n"]]
        elif op["op"] == "limit":
            out = out[: op["n"]]
    return out


def _fields_used(tile: Tile) -> list[str]:
    fields = [f for ch in tile.encoding.values() for f in ch["fields"]]
    fields += [op["by"] for op in tile.shape if "by" in op]
    return fields


def _is_numeric(v: Any) -> bool:
    if v is None:
        return True  # nulls are a data fact, not a type violation
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        try:
            float(v)
            return True
        except ValueError:
            return False
    return False


def _check_result(
    tile: Tile, result: dict[str, Any], *, label: str
) -> list[LintFinding]:
    """All data-vs-config checks for one executed tile result."""
    findings: list[LintFinding] = []
    where = f"tile `{tile.id}`"
    spec = WIDGETS[tile.widget]
    columns = [str(c) for c in result.get("columns") or []]
    rows = [list(r) for r in result.get("rows") or []]
    lower = {c.lower(): i for i, c in enumerate(columns)}

    missing = sorted({f for f in _fields_used(tile) if f.lower() not in lower})
    if missing:
        findings.append(
            _err(
                "canvas-columns", where,
                f"{label}: result has no column(s) {', '.join(missing)} — "
                f"returned: {', '.join(columns) or 'none'}",
            )
        )
        return findings  # every later check reads those columns

    shaped = apply_shape(tile.shape, columns, rows)

    if not shaped:
        findings.append(_warn("canvas-empty", where, f"{label}: empty result — the tile renders blank"))
        return findings
    if len(shaped) > spec.max_rows:
        findings.append(
            _err(
                "canvas-rows", where,
                f"{label}: {len(shaped)} rows exceed the `{tile.widget}` cap of "
                f"{spec.max_rows} — aggregate in the computation or add topN/limit",
            )
        )
    elif len(shaped) > spec.warn_rows:
        findings.append(
            _warn("canvas-rows", where, f"{label}: {len(shaped)} rows is a lot for `{tile.widget}` (legibility)")
        )
    if tile.widget == "kpi" and len(shaped) > 1:
        # max_rows already made this an error; the message says what renders.
        findings.append(_warn("canvas-rows", where, f"{label}: kpi renders the FIRST row only"))

    sample = shaped[:50]
    for ch_name, ch_spec in WIDGETS[tile.widget].channels.items():
        enc = tile.encoding.get(ch_name)
        if enc is None or not ch_spec.numeric:
            continue
        for f in enc["fields"]:
            i = lower[f.lower()]
            bad = next((r[i] for r in sample if not _is_numeric(r[i])), None)
            if bad is not None:
                findings.append(
                    _err(
                        "canvas-numeric", where,
                        f"{label}: channel `{ch_name}` field `{f}` holds "
                        f"non-numeric value {bad!r}",
                    )
                )
                break
            if tile.widget in _NONNEGATIVE_WIDGETS and any(
                r[i] is not None and _is_numeric(r[i]) and float(r[i]) < 0 for r in sample
            ):
                findings.append(
                    _warn(
                        "canvas-numeric", where,
                        f"{label}: `{f}` has negative values — `{tile.widget}` encodes a share of a whole",
                    )
                )
    return findings


def _passes_constraints(p: dict[str, Any], value: Any) -> bool:
    normalized, err = normalize_value(p["type"], value)
    return not (err or check_constraints(p, normalized))


def _dial_extremes(
    canvas: Canvas,
    computations: dict[str, Computation],
    adhoc_runtime: str = "athena",
) -> dict[str, list[Any]]:
    """Per dial: the contract-extreme values worth probing — the TIGHTEST
    bounds every bound parameter accepts (max of the declared mins, min of
    the declared maxes). Pooling the raw per-parameter extremes probed tile
    B at tile A's wider min, manufacturing a guaranteed resolve failure the
    author could never lint away (a dial may legally drive computations with
    different declared ranges — only the default is contract-checked).
    Candidates that still violate any bound constraint (disjoint ranges —
    already an error via the default check) are dropped, not probed. Enums
    are not probed — every member is as legal as the default, and N members
    would eat the run budget for nothing."""
    out: dict[str, list[Any]] = {}
    for dial in canvas.dials:
        params: list[dict[str, Any]] = []
        for tile in canvas.tiles:
            comp, _f = resolve_tile_computation(tile, computations, adhoc_runtime)
            if comp is None:
                continue
            for name, src in tile.bind.items():
                if src.get("dial") != dial.name:
                    continue
                p = next((q for q in comp.parameters if q["name"] == name), None)
                if p:
                    params.append(p)
        mins = [p["min"] for p in params if "min" in p]
        maxes = [p["max"] for p in params if "max" in p]
        candidates: list[Any] = []
        try:
            if mins:
                candidates.append(max(mins))
            if maxes and min(maxes) not in candidates:
                candidates.append(min(maxes))
        except TypeError:
            continue  # mixed bound types — already a contract error; don't probe
        bounds = [v for v in candidates if all(_passes_constraints(p, v) for p in params)]
        if bounds:
            out[dial.name] = bounds
    return out


def check_live(
    canvas: Canvas,
    computations: dict[str, Computation],
    execute: Executor,
    *,
    max_runs: int = MAX_LIVE_RUNS,
    adhoc_runtime: str = "athena",
) -> tuple[list[LintFinding], int]:
    """Execute every tile at the dial defaults, then at each dial's contract
    extremes while the run budget lasts. Returns ``(findings, runs_used)``.
    An executor raise is a finding on that tile, never an exception — one bad
    tile must not hide the rest."""
    findings: list[LintFinding] = []
    runs = 0
    defaults = dial_values(canvas)

    def _comp_for(tile: Tile):
        comp, _f = resolve_tile_computation(tile, computations, adhoc_runtime)
        return comp  # a None here was already a contract finding

    def run_tile(tile: Tile, comp: Any, dials: dict[str, Any], label: str) -> None:
        nonlocal runs
        runs += 1
        try:
            result = execute(comp, tile_values(tile, dials))
        except Exception as e:  # noqa: BLE001 — engine failures are findings
            findings.append(
                _err("canvas-execute", f"tile `{tile.id}`", f"{label}: execution failed: {e}")
            )
            return
        findings.extend(_check_result(tile, result, label=label))

    for tile in canvas.tiles:
        comp = _comp_for(tile)
        if comp is None:
            continue
        if runs >= max_runs:
            findings.append(
                _warn("canvas-budget", "", f"live run budget ({max_runs}) exhausted — remaining tiles unchecked")
            )
            return findings, runs
        run_tile(tile, comp, defaults, "at dial defaults")

    for dial_name, extremes in _dial_extremes(canvas, computations, adhoc_runtime).items():
        for value in extremes:
            probing = {**defaults, dial_name: value}
            for tile in canvas.tiles:
                comp = _comp_for(tile)
                if comp is None:
                    continue
                if not any(src.get("dial") == dial_name for src in tile.bind.values()):
                    continue
                if runs >= max_runs:
                    findings.append(
                        _warn("canvas-budget", "", f"live run budget ({max_runs}) exhausted — extreme probes truncated")
                    )
                    return findings, runs
                run_tile(tile, comp, probing, f"at dial `{dial_name}` = {value!r}")
    return findings, runs


# ---------------------------------------------------------------------------
# The three layers as one report
# ---------------------------------------------------------------------------


def lint_canvas(
    raw: Any,
    computations: dict[str, Computation] | None = None,
    execute: Executor | None = None,
    *,
    max_live_runs: int = MAX_LIVE_RUNS,
    adhoc_runtime: str = "athena",
) -> LintReport:
    """Run the layers in order as isolated steps. ``contract`` needs the
    parsed computations; ``live`` additionally needs an executor — each is
    ``skipped`` when its input is absent, and both are skipped when an
    earlier layer errored (their checks would compound one root cause).
    ``adhoc_runtime`` (from :func:`source_runtime`) routes inline-SQL tiles
    to the dataset's engine."""
    report = LintReport()

    canvas, static_findings = parse_canvas(raw)
    # Overlap is tolerated on READ (parse warns) but refused on WRITE: the
    # page places tiles at explicit cells, so overlapping cards hide each
    # other. Promoting here gates every save.
    static_findings = [
        _err(f.code, f.path, f.message + " — tiles may not overlap")
        if f.code == "canvas-layout" and f.severity == "warning"
        and "overlaps" in f.message
        else f
        for f in static_findings
    ]
    static_errors = any(f.severity == "error" for f in static_findings)
    if static_errors:
        canvas = None
    report.steps.append(
        LintStep(
            name="static",
            status="issues" if static_findings else "ok",
            findings=static_findings,
        )
    )

    if canvas is None or computations is None:
        note = "static errors" if static_errors else "no computations provided"
        report.steps.append(LintStep(name="contract", status="skipped", note=note))
        report.steps.append(LintStep(name="live", status="skipped", note=note))
        return report

    contract_findings = check_contract(canvas, computations, adhoc_runtime)
    report.steps.append(
        LintStep(
            name="contract",
            status="issues" if contract_findings else "ok",
            findings=contract_findings,
        )
    )
    if any(f.severity == "error" for f in contract_findings):
        report.steps.append(LintStep(name="live", status="skipped", note="contract errors"))
        return report

    if execute is None:
        report.steps.append(LintStep(name="live", status="skipped", note="no executor provided"))
        return report

    live_findings, runs = check_live(
        canvas, computations, execute,
        max_runs=max_live_runs, adhoc_runtime=adhoc_runtime,
    )
    report.steps.append(
        LintStep(
            name="live",
            status="issues" if live_findings else "ok",
            note=f"{runs} run(s)",
            findings=live_findings,
        )
    )
    return report
