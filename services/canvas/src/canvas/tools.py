"""The canvas agent's tools: the authoring quartet (lint / save / render /
submit) plus the wiki-context subset reused from ``ConsumptionTools``.

Everything closes over one :class:`ToolContext` — the job's dataset scope, the
DDB canvas row, and the in-flight draft — so the agent never passes ids around
and a tool can't cross into another user's rows. The lint tool is the
``okf_core.canvas`` three-layer lint with a live executor built on the SAME
``run_computation`` path the serving surfaces use.
"""

from __future__ import annotations

import functools
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from langchain_core.tools import tool

from okf_core import canvas as cv
from okf_core.lint import LintReport

from canvas import jobs


def _corrective(fn):
    """A raising tool must become a CORRECTIVE result, never a crash.

    This agent has no ToolErrorMiddleware (that's harvest code); without a
    catch, one bad call — a read_page on a key that doesn't exist, a
    NoSuchKey from any S3 read — propagates out of agent.invoke and kills the
    whole job (seen live). The model reads the error string and self-corrects.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — the job must outlive any tool
            return {"error": f"{type(e).__name__}: {e} — adjust the call and retry"}

    return wrapper


@dataclass
class ToolContext:
    """Everything the tool closures need. ``renderer`` is injectable — the
    Playwright implementation in the runtime image, a fake in tests, None when
    the image lacks a browser (render degrades to unavailable). ``execute`` is
    the OBJECT-based executor (comp, values) -> {columns, rows} the runner
    builds on computation_run — the same callable runs bundle computations and
    synthesized ad-hoc tiles, and raises when the engine refuses."""

    ddb: Any
    canvas_table: str
    user_sub: str
    data_domain: str
    dataset: str
    consumption: Any  # ConsumptionTools
    load_computation: Callable[[str], Any]  # slug -> Computation | None
    execute: Callable[[Any, dict[str, Any]], dict[str, Any]] | None = None
    renderer: Any = None
    canvas_id: str = ""
    saved: bool = False
    max_live_runs: int = cv.MAX_LIVE_RUNS
    # Engine for inline-SQL tiles (cv.source_runtime of the dataset's source
    # descriptor) — bundle computations carry their own runtime.
    adhoc_runtime: str = "athena"
    # True for revision jobs: saves must not resurrect a canvas the user
    # deleted while the job ran (write_canvas_row's require_existing).
    revising: bool = False
    # The canvas row's ``updated_at`` as this job last saw (revision seed) or
    # wrote it — every save is conditional on it (jobs.write_canvas_row), so
    # the user's drag/rename/delete during the run is never overwritten.
    row_version: str = ""


def _report_dict(report: LintReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "steps": [
            {
                "name": s.name,
                "status": s.status,
                "note": s.note,
                "findings": [f.to_dict() for f in s.findings],
            }
            for s in report.steps
        ],
    }


def _computations_for(ctx: ToolContext, raw: Any) -> dict[str, Any]:
    slugs = set()
    for tile in (raw.get("tiles") or []) if isinstance(raw, dict) else []:
        slug = ((tile or {}).get("data") or {}).get("computation")
        if isinstance(slug, str):
            slugs.add(slug)
    out = {}
    for slug in sorted(slugs):
        comp = ctx.load_computation(slug)
        if comp is not None:
            out[slug] = comp
    return out


def _save_conflict(ctx: ToolContext) -> dict[str, Any]:
    """The conditional save refused: the row moved since this job last saw
    it. Gone ⇒ the user deleted it — never resurrect. Present ⇒ the user
    edited it (layout drag, rename) — hand back their title + layout so the
    agent re-applies its change ON TOP, and adopt the current version so the
    retried save lands."""
    row = jobs.get_canvas_row(
        ctx.ddb, table=ctx.canvas_table, user_sub=ctx.user_sub,
        canvas_id=ctx.canvas_id,
    )
    if row is None:
        return {
            "saved": False,
            "error": "this canvas was deleted while the job ran — nothing "
            "was saved; finish and report that to the user",
        }
    ctx.row_version = row.get("updated_at", {}).get("S", "")
    try:
        current = json.loads(row.get("config", {}).get("S", "") or "{}")
    except ValueError:
        current = {}
    return {
        "saved": False,
        "error": "the user edited this canvas while the job ran — nothing "
        "was saved. Keep THEIR title and tile layout below (unless the "
        "request explicitly asks to change them), merge your changes on top, "
        "and call save_canvas again",
        "current_title": current.get("title", ""),
        "current_layouts": {
            str(t.get("id")): t.get("layout")
            for t in current.get("tiles") or []
            if isinstance(t, dict)
        },
    }


def _live_executor(ctx: ToolContext):
    if ctx.execute is None:
        def missing(_comp: Any, _values: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("no executor wired into this runtime")

        return missing
    return ctx.execute


# Inline-image budget per render: enough for light+dark at the primary
# viewport without ballooning the context/checkpoints (the S3 offload absorbs
# checkpoint size, but model tokens are the real cost).
_MAX_INLINE_IMAGES = 2
_MAX_IMAGE_BYTES = 1_500_000


def _render_blocks(out: dict[str, Any]) -> list[Any]:
    """The renderer's result as tool-message CONTENT BLOCKS: a text summary
    plus the screenshots as inline base64 images — the model must SEE the
    pixels (the sandbox has no real filesystem, so path-based reads were a
    dead end, observed live as the agent globbing for PNGs it could never
    find)."""
    import base64
    from pathlib import Path

    blocks: list[Any] = [
        {
            "type": "text",
            "text": json.dumps(
                {k: v for k, v in out.items() if k != "screenshots"}
                | {"screenshots_inline": True}
            ),
        }
    ]
    shots = out.get("screenshots") or []
    # Prefer one per theme at the primary (widest) viewport.
    primary = [s for s in shots if s.get("width") == max((s.get("width") or 0) for s in shots)] or shots
    for shot in primary[:_MAX_INLINE_IMAGES]:
        try:
            data = Path(shot["path"]).read_bytes()
        except OSError:
            continue
        if len(data) > _MAX_IMAGE_BYTES:
            blocks.append({
                "type": "text",
                "text": f"(screenshot {shot.get('theme')} too large to inline)",
            })
            continue
        blocks.append({
            "type": "image",
            "source_type": "base64",
            "mime_type": "image/png",
            "data": base64.b64encode(data).decode("ascii"),
        })
        blocks.append({
            "type": "text",
            "text": f"^ {shot.get('theme')} theme, {shot.get('width')}px",
        })
    return blocks


def _parse_config(config_json: str) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = json.loads(config_json)
    except ValueError as e:
        return None, f"config is not valid JSON: {e}"
    if not isinstance(raw, dict):
        return None, "config must be a JSON object"
    return raw, ""


def _scope_guard(ctx: ToolContext, raw: dict[str, Any]) -> str:
    if (
        raw.get("data_domain") != ctx.data_domain
        or raw.get("dataset") != ctx.dataset
    ):
        return (
            f"this job authors for {ctx.data_domain}/{ctx.dataset} — the config "
            f"names {raw.get('data_domain')}/{raw.get('dataset')}"
        )
    return ""


def build_canvas_tools(ctx: ToolContext) -> list[Any]:
    """The authoring quartet. Returned alongside the wiki tools by
    :func:`build_all_tools`."""

    @tool
    @_corrective
    def lint_canvas(config_json: str) -> dict[str, Any]:
        """Validate a dashboard config WITHOUT saving it: static shape checks,
        contract checks against the computation docs, then live execution of
        every tile at the dial defaults and dial extremes. Returns the layered
        report — fix every error before save_canvas; treat warnings as
        judgment calls. Run this after every substantive config change.

        Args:
          config_json: the full canvas config as a JSON string.
        """
        raw, err = _parse_config(config_json)
        if err:
            return {"ok": False, "error": err}
        scope_err = _scope_guard(ctx, raw)
        if scope_err:
            return {"ok": False, "error": scope_err}
        report = cv.lint_canvas(
            raw,
            _computations_for(ctx, raw),
            _live_executor(ctx),
            max_live_runs=ctx.max_live_runs,
            adhoc_runtime=ctx.adhoc_runtime,
        )
        return _report_dict(report)

    @tool
    @_corrective
    def render_canvas(config_json: str, dials_json: str = "") -> list[Any]:
        """Render a canvas config in the headless browser harness and get the
        SCREENSHOTS back inline (light + dark theme) plus any console/render
        errors — inspect the returned images before saving: legibility
        problems (overlapping labels, unreadable legends, a wrong chart type
        for the data's shape) only show up here.

        Args:
          config_json: the full canvas config as a JSON string.
          dials_json: optional JSON object of dial overrides; empty = the
            config's dial defaults.
        """
        raw, err = _parse_config(config_json)
        if err:
            return [{"type": "text", "text": json.dumps({"rendered": False, "error": err})}]
        if ctx.renderer is None:
            return [{"type": "text", "text": json.dumps({
                "rendered": False,
                "error": "no browser available in this runtime — visual "
                "inspection skipped; rely on lint_canvas and state in your "
                "final summary that the canvas was not visually inspected",
            })}]
        overrides: dict[str, Any] = {}
        if dials_json.strip():
            try:
                overrides = json.loads(dials_json)
            except ValueError as e:
                return [{"type": "text", "text": json.dumps(
                    {"rendered": False, "error": f"dials_json is not JSON: {e}"}
                )}]
        canvas, findings = cv.parse_canvas(raw)
        if canvas is None:
            return [{"type": "text", "text": json.dumps({
                "rendered": False,
                "error": "config does not parse",
                "findings": [f.to_dict() for f in findings][:10],
            })}]
        dials = {**cv.dial_values(canvas), **overrides}
        execute = _live_executor(ctx)
        comps = _computations_for(ctx, raw)
        results: dict[str, Any] = {}
        tile_errors: dict[str, str] = {}
        for tile in canvas.tiles:
            # Same ad-hoc runtime as lint/save: a default-athena rebuild on a
            # Redshift dataset hashes differently (memo miss) and is refused.
            comp, _rf = cv.resolve_tile_computation(tile, comps, ctx.adhoc_runtime)
            try:
                if comp is None:
                    raise RuntimeError("tile has no resolvable query")
                results[tile.id] = execute(comp, cv.tile_values(tile, dials))
            except Exception as e:  # noqa: BLE001 — a dead tile still renders (its error) for inspection
                results[tile.id] = {"columns": [], "rows": [], "error": str(e)}
                tile_errors[tile.id] = str(e)[:500]
        out = ctx.renderer.render(config=raw, results=results, dials=dials)
        if tile_errors:
            # Named in the text block too: a failed tile is a DATA problem,
            # not a layout one — the agent must not "fix" the layout for it.
            out = {**out, "tile_errors": tile_errors}
        return _render_blocks(out)

    @tool
    @_corrective
    def save_canvas(config_json: str) -> dict[str, Any]:
        """CREATE the canvas — one step, no draft/submit dance. Runs the FULL
        lint (live execution included) as the gate: any error refuses the
        save. On success the canvas is live on the user's Canvas page
        immediately; call it again with an updated config to overwrite (a
        revision job reuses the existing canvas id).

        Args:
          config_json: the full canvas config as a JSON string.
        """
        raw, err = _parse_config(config_json)
        if err:
            return {"saved": False, "error": err}
        scope_err = _scope_guard(ctx, raw)
        if scope_err:
            return {"saved": False, "error": scope_err}
        report = cv.lint_canvas(
            raw,
            _computations_for(ctx, raw),
            _live_executor(ctx),
            max_live_runs=ctx.max_live_runs,
            adhoc_runtime=ctx.adhoc_runtime,
        )
        errors = [f for f in report.findings if f.severity == "error"]
        if errors or any(s.status == "failed" for s in report.steps):
            return {
                "saved": False,
                "error": "lint gate failed — fix every error and retry",
                "findings": [f.to_dict() for f in errors[:20]],
            }
        ctx.canvas_id = ctx.canvas_id or uuid.uuid4().hex
        try:
            ctx.row_version = jobs.write_canvas_row(
                ctx.ddb,
                table=ctx.canvas_table,
                user_sub=ctx.user_sub,
                canvas_id=ctx.canvas_id,
                title=str(raw.get("title") or ""),
                data_domain=ctx.data_domain,
                dataset=ctx.dataset,
                status=cv.STATUS_CREATED,
                config=raw,
                expected_updated_at=ctx.row_version or None,
                require_existing=ctx.revising or ctx.saved,
            )
        except jobs.CanvasRowConflict:
            return _save_conflict(ctx)
        ctx.saved = True
        warnings = [f.to_dict() for f in report.findings if f.severity == "warning"]
        return {"saved": True, "canvas_id": ctx.canvas_id, "warnings": warnings[:20]}

    return [lint_canvas, render_canvas, save_canvas]


# ---------------------------------------------------------------------------
# Wiki context (the ConsumptionTools subset, scoped to the job's dataset)
# ---------------------------------------------------------------------------


def build_wiki_tools(ctx: ToolContext) -> list[Any]:
    c = ctx.consumption
    d, ds = ctx.data_domain, ctx.dataset

    @tool
    @_corrective
    def read_page(concept_id: str) -> Any:
        """Read one wiki page of this dataset by concept id (e.g.
        'tables/races' or 'references/metrics/points_per_race')."""
        return c.read_page(data_domain=d, dataset=ds, concept_id=concept_id)

    @tool
    @_corrective
    def list_directory(path: str = "") -> Any:
        """List the dataset's wiki tree at `path` ('' = the bundle root)."""
        return c.list_directory(data_domain=d, dataset=ds, path=path)

    @tool
    @_corrective
    def semantic_search(query: str, top_k: int = 8) -> Any:
        """Semantic search over this dataset's wiki (business terms work)."""
        return c.semantic_search(query=query, data_domain=d, dataset=ds, top_k=top_k)

    @tool
    @_corrective
    def grep(pattern: str, max_results: int = 100) -> Any:
        """Regex search doc BODIES across this dataset's wiki (case-insensitive;
        each match carries its concept id + line)."""
        return c.grep(
            pattern=pattern, data_domain=d, dataset=ds, max_results=max_results
        )

    @tool
    @_corrective
    def get_backlinks(concept_id: str) -> Any:
        """Every doc in this dataset linking TO the given concept."""
        return c.get_backlinks(data_domain=d, dataset=ds, concept_id=concept_id)

    @tool
    @_corrective
    def list_computations() -> Any:
        """The dataset's Attested Computations with verification status —
        the ONLY queries a dashboard tile may bind."""
        return c.list_computations(data_domain=d, dataset=ds)

    @tool
    @_corrective
    def describe_computation(name: str) -> Any:
        """One computation's contract: typed parameters (enum/min/max/
        defaults — these become the dial controls), frozen SQL, evidence
        domains. `name` is the SLUG (the `computation` field from
        list_computations, e.g. `season_final_driver_standings`) — never the
        human title."""
        return c.describe_computation(name=name, data_domain=d, dataset=ds)

    @tool
    @_corrective
    def run_computation(name: str, parameters_json: str = "") -> Any:
        """Execute a computation with parameter VALUES (JSON object) and get
        the receipt: columns, rows, executed_sql, warnings. Use it to probe
        result shapes before binding a tile.

        Args:
          name: the computation SLUG (the `computation` field from
            list_computations, e.g. `season_final_driver_standings`) — never
            the human title.
          parameters_json: JSON object of parameter values ('' = defaults).
        """
        try:
            values = json.loads(parameters_json) if parameters_json.strip() else {}
        except ValueError as e:
            return {"error": f"parameters_json is not JSON: {e}"}
        return c.run_computation(
            name=name, data_domain=d, dataset=ds, parameters=values
        )

    return [
        read_page, list_directory, semantic_search, grep, get_backlinks,
        list_computations, describe_computation, run_computation,
    ]


def build_all_tools(ctx: ToolContext) -> list[Any]:
    return build_wiki_tools(ctx) + build_canvas_tools(ctx)
