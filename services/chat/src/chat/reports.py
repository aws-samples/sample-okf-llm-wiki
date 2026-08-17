"""The report tools: ``create_report`` (the atomic gate) + ``present_report``.

The MAIN chat agent authors reports — no delegated runtime, no job rows: the
conversation itself is the research loop (ambiguity gets resolved with the
human, which is the point), and the report is the artifact the agent
assembles from evidence it ALREADY gathered in this thread.

``create_report`` is atomic and render-verified: lint the blocks
(``okf_core.reports``), rasterize every chart through the real renderer
(headless Chromium over the app's chart harness — a chart that cannot render
refuses the whole save), compose ONE self-contained HTML, print the PDF from
that same HTML, and put all artifacts under the composite report id's S3
prefix. No database row — the id carries its own coordinates, and
``blocks.json`` carries the metadata.

``present_report`` is an inert transport exactly like ``render_chart``: the
UI lifts the CALL into an inline report card pinned to the bottom of the AI
turn (click → inline side panel + PDF download); the return value is just
the ack the model reads so it keeps writing. ``create_report`` stays an
ordinary step in the thinking timeline — presenting is ``present_report``'s
job, after every save and for RE-showing an existing report later.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import StructuredTool

from okf_core import reports as rp

log = logging.getLogger(__name__)

# The report-authoring methodology is vendored beside the service at
# services/chat/skills/report-authoring/SKILL.md and served by the generic
# ``read_skill`` tool (chat.skills) — pulled on demand rather than riding
# every turn's system prompt, since most turns never write a report.


def make_report_tools(
    s3,
    renderer,
    *,
    bundle_bucket: str,
    user_sub: str,
    dataset_scope: dict[str, str] | None = None,
    max_report_bytes: int = rp.MAX_REPORT_BYTES,
):
    """Build the report pair for one run.

    ``renderer`` is a ``chat.report_render.ChartRenderer`` (or None on a
    deployment without the harness/Chromium — chart blocks are then refused
    with a clear error; markdown/table/kpi reports still work). Closure-based
    like the other chat tools; ``user_sub`` is recorded as the author in the
    self-describing ``blocks.json``.
    """

    def _scope() -> tuple[str, str] | None:
        if dataset_scope:
            return dataset_scope["data_domain"], dataset_scope["dataset"]
        return None

    def _create(
        title: str, blocks_json: str, data_domain: str = "", dataset: str = ""
    ) -> Any:
        pinned = _scope()
        if pinned:
            data_domain, dataset = pinned
        if not (data_domain or "").strip() or not (dataset or "").strip():
            return {
                "error": "name the dataset — data_domain + dataset (see list_domains)"
            }
        data_domain, dataset = data_domain.strip(), dataset.strip()
        try:
            blocks = json.loads(blocks_json)
        except ValueError as e:
            return {"error": f"blocks_json is not valid JSON: {e}"}
        errors = rp.lint_blocks(title, blocks)
        if errors:
            return {"error": "; ".join(errors[:12])}

        chart_indexes = [i for i, b in enumerate(blocks) if b.get("type") == "chart"]
        # index → {"light": uri, "dark": uri}: the renderer rasterizes each
        # chart once per theme (transparent PNGs) and the composer emits both.
        images: dict[int, dict[str, str]] = {}
        if chart_indexes:
            if renderer is None:
                return {
                    "error": "no chart renderer is available on this deployment — "
                    "compose without chart blocks (tables/KPIs render fine)"
                }
            charts = [
                {
                    "spec": blocks[i]["spec"],
                    "height": int(blocks[i].get("height") or 340),
                }
                for i in chart_indexes
            ]
            # This render IS the verification: console errors, a frame that
            # never settles, or a plugin throw all raise here and refuse the
            # save with the failing chart named.
            try:
                rendered = renderer.render_charts(charts)
            except Exception as e:  # noqa: BLE001 - corrective, not a crash
                log.warning("report chart render failed", exc_info=True)
                return {"error": f"{type(e).__name__}: {e} — fix the chart and retry"}
            images = dict(zip(chart_indexes, rendered))

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        suffix = uuid.uuid4().hex[:8]
        report_id = rp.make_report_id(data_domain, dataset, stamp, suffix)
        generated_at = now.isoformat(timespec="seconds")
        try:
            html = rp.compose_html(
                title,
                blocks,
                images,
                domain=data_domain,
                dataset=dataset,
                generated_at=generated_at,
                report_id=report_id,
            )
        except ValueError as e:
            return {"error": str(e)}
        size = len(html.encode("utf-8"))
        if size > max_report_bytes:
            return {
                "error": f"the composed report is {size} bytes (cap "
                f"{max_report_bytes}) — fewer/lighter charts or shorter tables"
            }

        pdf_bytes: bytes | None = None
        if renderer is not None:
            try:
                pdf_bytes = renderer.pdf(html)
            except Exception:  # noqa: BLE001 - the PDF is a convenience copy
                log.warning("pdf render failed; report ships HTML-only", exc_info=True)

        prefix = rp.report_s3_prefix(data_domain, dataset, stamp, suffix)
        s3.put_object(
            Bucket=bundle_bucket,
            Key=rp.report_blocks_key(prefix),
            Body=rp.blocks_document(
                title,
                blocks,
                request="",
                domain=data_domain,
                dataset=dataset,
                created_by=user_sub,
                generated_at=generated_at,
            ).encode("utf-8"),
            ContentType="application/json",
        )
        s3.put_object(
            Bucket=bundle_bucket,
            Key=rp.report_html_key(prefix),
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )
        if pdf_bytes:
            s3.put_object(
                Bucket=bundle_bucket,
                Key=rp.report_pdf_key(prefix),
                Body=pdf_bytes,
                ContentType="application/pdf",
            )
        return {
            "report_id": report_id,
            "title": title,
            "blocks": len(blocks),
            "pdf": bool(pdf_bytes),
            "note": (
                "saved — now call present_report with this report_id to show "
                "it to the user, then continue with a 2–4 sentence summary of "
                "the findings; do NOT paste the report's contents"
            ),
        }

    def _present(report_id: str, title: str = "") -> Any:
        if rp.parse_report_id(report_id) is None:
            return {
                "error": f"{report_id!r} is not a report id — use the exact "
                "report_id a create_report call returned"
            }
        return {
            "status": "presented",
            "report_id": report_id,
            "title": title or "Report",
            "note": (
                "the report card is now displayed at the end of your response — "
                "continue your answer; do NOT paste the report's contents"
            ),
        }

    def _guarded(fn, name: str):
        # Same containment as the consumption tools: an unexpected raise (an
        # S3 throttle, expired creds) must come back as a tool RESULT the
        # model can react to, not abort the whole run.
        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> Any:
            try:
                return fn(**kwargs)
            except Exception as e:  # noqa: BLE001 - feedback, not a crash
                log.warning("chat tool %s failed", name, exc_info=True)
                return {"error": f"{name} failed: {type(e).__name__}: {e}"}

        return wrapper

    unscoped = (
        ""
        if dataset_scope
        else " `data_domain`/`dataset` name the dataset (see list_domains)."
    )
    create_doc = inspect.cleandoc(
        """Compose and save an immutable HTML report from the evidence you
        gathered IN THIS CONVERSATION. Read read_skill("report-authoring")
        FIRST (once per conversation) for the required structure and language — narrative + charts + tables + KPIs,
        downloadable as PDF. The user sees it inline as a card. Use it when
        the user asks for a report/write-up; a saved report cannot be edited
        (create a new one), so agree on scope first and only include numbers
        you actually derived here — never estimates.

        The save is ATOMIC and render-verified: blocks are validated, every
        chart is rendered through the real chart renderer (a chart that fails
        refuses the save with the error named), and the PDF is printed from
        the same HTML. On an error result, fix the named block and call again.
        Saving does NOT display anything — after a successful save, call
        present_report with the returned report_id to show it to the user.

        `blocks_json` is a JSON array, rendered in order. Block types:
        {"type":"markdown","md":"..."} — narrative (headings/lists/tables ok);
        {"type":"chart","title","spec",provenance?} — spec is EXACTLY a
        renderChart spec (same contract as render_chart: type/labels/series/…);
        {"type":"table","title"?,"columns":[...],"rows":[[...]],provenance?} —
        an exhibit, ≤100 rows; {"type":"kpi","label","value","delta"?,
        "delta_label"?,provenance?} — adjacent kpis render as one card row.
        provenance (on any figure showing query-derived numbers — REQUIRED
        practice): {"kind":"computation","slug","params"?} or
        {"kind":"adhoc_sql","sql"}. It renders as a numbered note in the
        report's methodology appendix — NEVER in the body, which stays
        executive-clean business language (see the report-authoring skill).
        Real values only, exactly as the queries returned them."""
    )
    present_doc = inspect.cleandoc(
        """Display a report to the user for viewing and download: renders a
        card at the END of your response (opens an inline side panel, with
        PDF download).
        Call it once after every successful create_report, and to re-show a
        report from an earlier conversation. Never paste report contents into
        chat; present the card and summarize."""
    )

    return [
        StructuredTool.from_function(
            _guarded(_create, "create_report"),
            name="create_report",
            description=create_doc + unscoped,
        ),
        StructuredTool.from_function(
            _guarded(_present, "present_report"),
            name="present_report",
            description=present_doc,
        ),
    ]
