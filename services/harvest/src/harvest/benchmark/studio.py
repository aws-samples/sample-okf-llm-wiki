"""Benchmark Studio runtime modes: ``benchmark`` and ``aggregate_annotations``.

Hosted on the harvest runtime (the container already owns the model factory,
the Athena source, and the whole ``harvest/benchmark/`` package) but sharing
NOTHING with a harvest run: a benchmark **writes nothing to the bundle**, takes
**no harvest lease** (concurrent with harvests and with other benchmark runs),
and **doesn't use the S3-Files mount** — the wiki snapshot is materialized
straight from S3 (:mod:`.s3_snapshot`), live or pinned to a bundle version.

Failures are LOUD: unlike the retired RI setup (which silently degraded to a
plain harvest), a run that can't fetch/parse its questions, materialize its
snapshot, or build its source FAILS the report — status ``failed`` on the
``REPORT#`` row with the error, surfaced in the UI.

``aggregate_annotations`` is the on-demand second act: it reads a completed
report's judge-written annotation candidates, dedupes/merges them with a ReAct
aggregator agent (running on the report's configured JUDGE model — decided:
consolidation quality should match review quality), and writes the final set
back onto the report for the human to select and apply. The aggregator emits
each final annotation through a ``write_final_annotation`` TOOL into an in-run
store (structured by construction — no fence-parsing failure modes), with
read-only wiki tools to verify a ``concept_id`` target actually exists; if it
tries to finish with an empty store while candidates exist, it is nudged once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okf_core import benchmark_report as br
from okf_core.benchmark_questions import load_questions
from okf_core.paths import parse_concept_id

from harvest.benchmark.report_store import (
    RowProgress,
    headline_kpis,
    persist_report_artifacts,
    update_report_row,
)
from harvest.benchmark.s3_snapshot import default_bucket, materialize_snapshots
from harvest.status import build_registry_client

log = logging.getLogger("harvest.benchmark.studio")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _s3_client():
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region)


def _get_text(s3, bucket: str, key: str, version_id: str = "") -> str:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    body = s3.get_object(**kwargs)["Body"].read()
    return body.decode("utf-8") if isinstance(body, bytes) else body


# The grading path's Athena knobs. Defaults match the historical hard-coded
# behavior (60s timeout) plus a sane cap; both are per-QUERY, benchmark-only —
# the harvest's own sample_rows/run_sql tools are untouched.
_GRADER_DEFAULT_TIMEOUT_S = 60.0
_GRADER_DEFAULT_MAX_ROWS = 50000


def _grader_timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get("OKF_BENCHMARK_GRADER_TIMEOUT_S", "")))
    except (TypeError, ValueError):
        return _GRADER_DEFAULT_TIMEOUT_S


def _grader_max_rows() -> int:
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_GRADER_MAX_ROWS", "")))
    except (TypeError, ValueError):
        return _GRADER_DEFAULT_MAX_ROWS


def _grading_execute(source):
    """The grader's ``execute``: POSITIONAL rows, grading timeout, row cap.

    Positional (not the dict rows the harvest tools use) because header-keyed
    dicts collapse duplicate SELECT labels (``SELECT r.name, c.name``) into one
    cell — a silent mis-grade. The row cap turns a pathological result set into
    a classified grading failure instead of an unbounded buffer.
    """
    timeout_s = _grader_timeout_s()
    max_rows = _grader_max_rows()

    def execute(sql: str):
        _header, rows = source.run_query(
            sql, timeout_s=timeout_s, max_rows=max_rows, positional=True
        )
        return rows

    return execute


def validate_benchmark_payload(payload: dict) -> str | None:
    """Payload sanity for ``mode=benchmark`` (the Control API validated deeper)."""
    for key in ("data_domain", "dataset", "report_id", "questions_key"):
        if not payload.get(key):
            return f"benchmark mode requires '{key}'"
    if not br.is_valid_report_id(payload["report_id"]):
        return f"invalid report_id: {payload['report_id']!r}"
    try:
        br.validate_checks(payload.get("checks"))
    except br.BenchmarkRunConfigError as e:
        return str(e)
    return None


def run_benchmark_report(payload: dict, session_id: str | None = None) -> None:
    """Execute one standalone benchmark run end-to-end (thread entry point).

    Moves the REPORT# row ``queued → running → complete``/``failed``. Every
    failure lands on the row (bounded detail) — the row is how the UI learns a
    run died; the exception then propagates to the entrypoint's job logger.
    """
    data_domain = payload["data_domain"]
    dataset = payload["dataset"]
    report_id = payload["report_id"]
    registry = build_registry_client()

    def row(attrs: dict[str, Any]) -> None:
        update_report_row(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            report_id=report_id,
            attrs=attrs,
        )

    try:
        row(
            {
                "status": br.STATUS_RUNNING,
                "runtime_session_id": session_id or "",
                "started_at": _now_iso(),
            }
        )
        report_doc, traces_doc = _execute(payload, report_id)
        bucket = default_bucket()
        persist_report_artifacts(
            bucket=bucket,
            data_domain=data_domain,
            dataset=dataset,
            report_id=report_id,
            report_doc=report_doc,
            traces_doc=traces_doc,
            # Lets the store skip the PUTs when the human deleted the report
            # mid-run — otherwise the artifacts would orphan behind no row.
            registry=registry,
        )
        row(
            {
                "status": br.STATUS_COMPLETE,
                "completed_at": _now_iso(),
                "phase": "done",
                **headline_kpis(report_doc),
            }
        )
        log.info("Benchmark report %s complete (%s/%s).", report_id, data_domain, dataset)
    except Exception as e:  # noqa: BLE001 - loud failure: the row carries the error
        row(
            {
                "status": br.STATUS_FAILED,
                "detail": f"{type(e).__name__}: {e}"[:1024],
                "completed_at": _now_iso(),
            }
        )
        raise


def _write_judge_traces(judge_dir: str, questions_by_id: dict, attempts: list) -> None:
    """Lay every attempt's solve trace into the judge tree as markdown files.

    ``.traces/<check>/q<###>-run<N>.md`` — full captured fidelity (see
    ``trace.render_markdown``), every question and every run, passing and
    failing alike, so the judge can ``grep`` ACROSS solvers for systemic
    patterns ("did any run find this doc?"), not just read its own case's
    inline summaries. Gold-free by construction (solvers are gold-blind).
    Best-effort: a disk hiccup must not fail the report — the judge still has
    the inline renderings.
    """
    from harvest.benchmark.trace import render_markdown

    count = 0
    for a in attempts:
        if a.trace is None:
            continue
        # Per-attempt try: one unwritable/oversized trace must not drop every
        # trace after it in the loop.
        try:
            q = questions_by_id.get(a.q_id)
            body = render_markdown(
                a.trace,
                question=q.question if q else "",
                check=a.check,
                run_index=a.run_index,
                outcome=a.outcome.value if a.outcome is not None else "",
                reason=a.reason,
                prediction=a.prediction,
            )
            check_dir = Path(judge_dir) / ".traces" / a.check
            check_dir.mkdir(parents=True, exist_ok=True)
            path = check_dir / f"q{a.q_id:03d}-run{a.run_index + 1}.md"
            path.write_text(body, encoding="utf-8")
            count += 1
        except Exception:  # noqa: BLE001 - best-effort per file
            log.warning(
                "Could not write judge trace for q%d/%s run %d (continuing).",
                a.q_id, a.check, a.run_index + 1, exc_info=True,
            )
    log.info("Benchmark: wrote %d solve trace file(s) for the judge.", count)


def _execute(payload: dict, report_id: str) -> tuple[dict, dict]:
    """The run body: questions → snapshot → models/tools → engine → report doc."""
    import shutil

    from harvest.agent import _build_model, resolve_model_config
    from harvest.benchmark.checks import solver_protocol
    from harvest.benchmark.grader import Grader
    from harvest.benchmark.judge import (
        make_behavior_grader,
        make_behavior_reviewer,
        make_judge,
    )
    from harvest.benchmark.solver import make_ask_human_tool, make_solver
    from harvest.clients import build_source
    from harvest.code_interpreter import sandbox_session
    from harvest.source_tools import make_source_tools

    data_domain = payload["data_domain"]
    dataset = payload["dataset"]
    checks = br.validate_checks(payload.get("checks"))
    runs = br.coerce_runs(payload.get("runs"))
    version_id = str(payload.get(br.FIELD_VERSION_ID) or "")

    bucket = default_bucket()
    s3 = _s3_client()

    # Questions: a fetch/parse failure or an empty set FAILS the report (loud).
    # The Control API pins the CSV's S3 VersionId at start time so a re-upload
    # mid-run can't swap the graded set; absent (older payloads) → latest.
    loaded = load_questions(
        _get_text(
            s3,
            bucket,
            payload["questions_key"],
            version_id=str(payload.get(br.FIELD_QUESTIONS_VERSION_ID) or ""),
        )
    )
    if not loaded.questions:
        raise ValueError("the question set has no valid questions")

    solver_dir = tempfile.mkdtemp(prefix=f"okf-bench-solver-{report_id}-")
    judge_dir = tempfile.mkdtemp(prefix=f"okf-bench-judge-{report_id}-")
    try:
        doc_count = materialize_snapshots(
            s3,
            bucket=bucket,
            data_domain=data_domain,
            dataset=dataset,
            version_id=version_id,
            solver_dir=solver_dir,
            judge_dir=judge_dir,
        )
        log.info(
            "Benchmark %s: materialized %d doc(s) (version=%s).",
            report_id,
            doc_count,
            version_id or "current",
        )

        source = build_source(dataset, source=payload.get("source"))
        grader = Grader(_grading_execute(source))

        solver_cfg = resolve_model_config(
            payload.get(br.FIELD_SOLVER_MODEL), payload.get(br.FIELD_SOLVER_EFFORT)
        )
        judge_cfg = resolve_model_config(
            payload.get(br.FIELD_JUDGE_MODEL), payload.get(br.FIELD_JUDGE_EFFORT)
        )
        # The solver SURFACES its reasoning: the thinking is the heart of the
        # solve trace — the judge reads it to diagnose failures and the report
        # UI shows it per attempt. The judge doesn't (nothing renders judge
        # thinking; the summary tokens would be pure cost).
        solver_model = _build_model(
            solver_cfg["model"],
            solver_cfg["effort"],
            solver_cfg["max_tokens"],
            surface_reasoning=True,
        )
        judge_model = _build_model(
            judge_cfg["model"], judge_cfg["effort"], judge_cfg["max_tokens"]
        )

        # The Behavior check's opt-in live SQL (run config; default off). The
        # prompt variant and the run_sql grant travel together via
        # solver_protocol — SQL EX solvers stay data-blind whatever the flag
        # says. run_sql only (no sample_rows): the ask is query access, and the
        # wiki's docs are where sample-shaped knowledge should come from.
        behavior_live_sql = bool(payload.get(br.FIELD_BEHAVIOR_LIVE_SQL))

        def make_solve(spec):
            proto = solver_protocol(
                spec,
                behavior_live_sql=behavior_live_sql,
                # The run's real SQL dialect — a Redshift dataset's solver must
                # not be told to write Athena/Trino (there is no source-type
                # gate on benchmarks, unlike cross mode).
                dialect=source.prompt_profile.dialect,
            )
            extra = []
            if proto.wants_sql:
                extra.extend(
                    t
                    for t in make_source_tools(source)
                    if getattr(t, "name", "") == "run_sql"
                )
            if proto.wants_ask:
                # Behavior only: the terminal ask_human escalation — a "should
                # ask" expectation becomes a structural outcome (the CALL ends
                # the run and is recorded as the answer).
                extra.append(make_ask_human_tool())
            return make_solver(
                solver_model,
                solver_dir,
                system_prompt=proto.prompt,
                parse=spec.parse,
                extra_tools=extra or None,
            )

        # The judge's `.context/` copy can hold binary uploads (PDF/DOCX/…)
        # that read_file only base64-encodes — the same Code Interpreter
        # sandbox the harvester uses makes them readable via run_code. Only
        # started when the judge tree actually has context files (most
        # benchmarks don't); best-effort like everywhere else — no sandbox
        # just means the judge reads text context only.
        judge_context = Path(judge_dir) / ".context"
        has_context = judge_context.is_dir() and any(
            p.is_file() for p in judge_context.rglob("*")
        )
        judge_sandbox_cm = (
            sandbox_session(judge_dir, label="Benchmark judge")
            if has_context
            else contextlib.nullcontext()
        )

        with judge_sandbox_cm as judge_sandbox:
            # The judge's diagnostician toolset over the JUDGE tree (wiki +
            # .metadata/ + .context/) plus live data (+ run_code when the
            # sandbox came up). Factory-deferred, mirroring the solver, so
            # deepagents imports happen at first judge use. The behavior
            # grader is the SAME judge model + toolset wearing its other hat
            # (per-run grading instead of failure review).
            def judge_tools():
                return _judge_toolset(judge_dir, source, judge_sandbox)

            judge = make_judge(judge_model, judge_tools)
            grade_behavior = make_behavior_grader(judge_model, judge_tools)
            review_behavior = make_behavior_reviewer(judge_model, judge_tools)

            return _run_engine(
                report_id=report_id,
                checks=checks,
                runs=runs,
                loaded=loaded,
                make_solve=make_solve,
                grader=grader,
                judge=judge,
                grade_behavior=grade_behavior,
                review_behavior=review_behavior,
                judge_dir=judge_dir,
                data_domain=data_domain,
                dataset=dataset,
                solver_cfg=solver_cfg,
                judge_cfg=judge_cfg,
                version_id=version_id,
                behavior_live_sql=behavior_live_sql,
            )
    finally:
        shutil.rmtree(solver_dir, ignore_errors=True)
        shutil.rmtree(judge_dir, ignore_errors=True)


def _judge_toolset(judge_dir: str, source, sandbox) -> list:
    """The judge hats' shared tools: read-only files over the judge tree,
    live source access, and — when a sandbox is up — run_code for binary
    ``.context/`` uploads."""
    from harvest.benchmark.solver import make_readonly_file_tools
    from harvest.code_interpreter import make_run_code_tool
    from harvest.source_tools import make_source_tools

    tools = [
        *make_readonly_file_tools(judge_dir, scope="dataset"),
        *make_source_tools(source),
    ]
    if sandbox is not None:
        tools.append(make_run_code_tool(sandbox))
    return tools


def _run_engine(
    *,
    report_id,
    checks,
    runs,
    loaded,
    make_solve,
    grader,
    judge,
    grade_behavior,
    review_behavior,
    judge_dir,
    data_domain,
    dataset,
    solver_cfg,
    judge_cfg,
    version_id,
    behavior_live_sql,
) -> tuple[dict, dict]:
    """Progress row + config recap + the async engine run (inside the judge
    sandbox's lifetime — the judge may call run_code until the report is done)."""
    from harvest.benchmark.report_run import execute_report
    from harvest.status import build_registry_client

    progress = RowProgress(
        build_registry_client(),
        data_domain=data_domain,
        dataset=dataset,
        report_id=report_id,
        total_runs=runs,
    )

    config_recap = {
        "data_domain": data_domain,
        "dataset": dataset,
        br.FIELD_CHECKS: checks,
        br.FIELD_RUNS: runs,
        br.FIELD_SOLVER_MODEL: solver_cfg["model"],
        br.FIELD_SOLVER_EFFORT: solver_cfg["effort"],
        br.FIELD_JUDGE_MODEL: judge_cfg["model"],
        br.FIELD_JUDGE_EFFORT: judge_cfg["effort"],
        br.FIELD_VERSION_ID: version_id,
        br.FIELD_BEHAVIOR_LIVE_SQL: behavior_live_sql,
        "questions": {
            "total": len(loaded.questions),
            "dropped": loaded.dropped,
            "check_counts": loaded.check_counts,
        },
    }

    questions_by_id = {q.q_id: q for q in loaded.questions}
    report_doc, traces_doc = asyncio.run(
        execute_report(
            report_id=report_id,
            checks=checks,
            runs=runs,
            questions=loaded.questions,
            make_solve=make_solve,
            grader=grader,
            judge=judge,
            grade_behavior=grade_behavior,
            review_behavior=review_behavior,
            config_recap=config_recap,
            progress=progress,
            before_judge=lambda attempts: _write_judge_traces(
                judge_dir, questions_by_id, attempts
            ),
        )
    )
    report_doc["completed_at"] = _now_iso()
    return report_doc, traces_doc


# --------------------------------------------------------------------------- #
# aggregate_annotations
# --------------------------------------------------------------------------- #

AGGREGATOR_SYSTEM_PROMPT = """\
You are consolidating verified wiki-gap annotations from a benchmark report \
into the final set a human will review and apply to the data wiki. The \
candidates below were written by a judge, one per failed benchmark question — \
many often imply the SAME doc fix.

Rules:
- MERGE candidates that share a root cause into ONE final annotation (several \
questions tripping on one undocumented join → one annotation).
- Each final annotation states a concrete, dataset-level doc fix as guidance \
to the wiki author ("state that `status` is an int code, 1=active", "document \
that pit-stop durations are not tracked"). NEVER benchmark questions, expected \
answers, or SQL to memorize — only what the wiki should say.
- You have read-only wiki tools (`read_file`, `glob`, `grep`, `ls`). When a \
fix targets a specific page, VERIFY the page exists (e.g. read or glob \
`tables/results.md`) and pass its concept id (e.g. `tables/results`); if you \
can't confirm a target, leave the concept id empty — the annotation then \
applies dataset-wide.
- Emit each final annotation by calling `write_final_annotation(note, \
concept_id)` — one call per final annotation. Do NOT list them in prose; only \
tool calls count.

When every final annotation is written, reply with a one-line summary."""

_AGG_RECURSION_LIMIT = 60

_AGG_NUDGE = (
    "You have not written any final annotation yet — the candidates above still "
    "need consolidating. Call `write_final_annotation(note, concept_id)` once "
    "per final annotation now; only tool calls count."
)


def validate_aggregate_payload(payload: dict) -> str | None:
    for key in ("data_domain", "dataset", "report_id"):
        if not payload.get(key):
            return f"aggregate_annotations mode requires '{key}'"
    if not br.is_valid_report_id(payload["report_id"]):
        return f"invalid report_id: {payload['report_id']!r}"
    return None


def run_aggregate_annotations(payload: dict, session_id: str | None = None) -> None:
    """Aggregate a report's judge annotations into the final set (thread entry).

    Reads ``report.json``, runs the aggregator agent, writes ``annotations.
    final`` back onto the report and mirrors the status onto the REPORT# row
    (``agg_status`` + ``annotation_final_count``) for the polling UI.
    """
    data_domain = payload["data_domain"]
    dataset = payload["dataset"]
    report_id = payload["report_id"]
    registry = build_registry_client()

    def row(attrs: dict[str, Any]) -> None:
        update_report_row(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            report_id=report_id,
            attrs=attrs,
        )

    try:
        # agg_detail cleared so a retry doesn't show the previous failure.
        row({"agg_status": br.AGG_RUNNING, "agg_detail": ""})
        bucket = default_bucket()
        s3 = _s3_client()
        report_key = br.report_key(data_domain, dataset, report_id)
        report_doc = json.loads(_get_text(s3, bucket, report_key))
        candidates = list(
            (report_doc.get("annotations") or {}).get("candidates") or []
        )

        final = _aggregate(payload, report_doc, candidates) if candidates else []

        report_doc.setdefault("annotations", {})
        report_doc["annotations"]["final"] = final
        report_doc["annotations"]["status"] = br.AGG_COMPLETE
        s3.put_object(
            Bucket=bucket,
            Key=report_key,
            Body=json.dumps(report_doc).encode("utf-8"),
            ContentType="application/json",
        )
        row({"agg_status": br.AGG_COMPLETE, "annotation_final_count": len(final)})
        log.info(
            "Aggregated %d candidate(s) → %d final annotation(s) for report %s.",
            len(candidates),
            len(final),
            report_id,
        )
    except Exception as e:  # noqa: BLE001 - loud failure on the row
        # Its OWN attr — the shared `detail` belongs to the RUN lifecycle, and
        # an agg failure writing there clobbered the run's failure reason.
        row(
            {
                "agg_status": br.AGG_FAILED,
                "agg_detail": f"{type(e).__name__}: {e}"[:1024],
            }
        )
        raise


def _aggregate(
    payload: dict, report_doc: dict, candidates: list[dict]
) -> list[dict[str, str]]:
    """Run the aggregator ReAct agent; return the final [{note, concept_id}]."""
    import shutil

    from langchain_core.tools import tool

    from harvest.agent import _build_model, resolve_model_config
    from harvest.benchmark.solver import make_readonly_file_tools

    data_domain = payload["data_domain"]
    dataset = payload["dataset"]

    # The aggregator runs on the report's configured JUDGE model + effort —
    # read from the stored run config, not the aggregate payload, so the report
    # is self-describing (decided: no third model picker).
    config = report_doc.get("config") or {}
    model_cfg = resolve_model_config(
        config.get(br.FIELD_JUDGE_MODEL) or None,
        config.get(br.FIELD_JUDGE_EFFORT) or None,
    )
    model = _build_model(
        model_cfg["model"], model_cfg["effort"], model_cfg["max_tokens"]
    )

    docs_dir = tempfile.mkdtemp(prefix="okf-agg-docs-")
    try:
        # The CURRENT wiki (the fixes will be applied to it, so targets are
        # verified against what exists now, not the possibly-pinned version the
        # report measured). No judge tree: the aggregator only needs the doc
        # tree (a throwaway judge dir used to leak per aggregation).
        materialize_snapshots(
            _s3_client(),
            bucket=default_bucket(),
            data_domain=data_domain,
            dataset=dataset,
            version_id="",
            solver_dir=docs_dir,
            judge_dir=None,
        )

        store: list[dict[str, str]] = []
        docs_root = Path(docs_dir)

        @tool
        def write_final_annotation(note: str, concept_id: str = "") -> str:
            """Record one FINAL consolidated annotation. `note` is the doc fix as
            dataset-level guidance; `concept_id` optionally targets a specific
            wiki page (e.g. 'tables/results') and must name an existing doc."""
            note = (note or "").strip()
            if not note:
                return "rejected: the note is empty"
            concept_id = (concept_id or "").strip().strip("/")
            if concept_id:
                # Structural validation FIRST (parse_concept_id refuses '..',
                # '.', '#', …) — a bare exists() check let traversal-shaped ids
                # through, and downstream create_annotation 400s on them AFTER
                # earlier annotations already persisted.
                try:
                    parse_concept_id(concept_id)
                except ValueError:
                    store.append({"note": note, "concept_id": ""})
                    return (
                        f"recorded (dataset-wide): {concept_id!r} is not a "
                        "valid concept id, so the target was dropped"
                    )
                if not (docs_root / f"{concept_id}.md").exists():
                    # Fall back to dataset-wide rather than pointing at a ghost page.
                    store.append({"note": note, "concept_id": ""})
                    return (
                        f"recorded (dataset-wide): no doc found for {concept_id!r}, "
                        "so the target was dropped"
                    )
            store.append({"note": note, "concept_id": concept_id})
            return f"recorded ({concept_id or 'dataset-wide'}); total={len(store)}"

        from harvest.benchmark.react import is_recursion_limit, make_react_agent

        agent = make_react_agent(
            model,
            [*make_readonly_file_tools(docs_dir), write_final_annotation],
            AGGREGATOR_SYSTEM_PROMPT,
        )

        lines = [
            f"- [{c.get('check', '?')}] {c.get('annotation', '')}"
            for c in candidates
            if c.get("annotation")
        ]
        user = "Candidate annotations to consolidate:\n" + "\n".join(lines)
        # create_agent RAISES GraphRecursionError at the step budget (no
        # apology message). Everything the aggregator produced up to that point
        # already sits in `store` (the tool calls executed), so a budget-blown
        # consolidation ships the partial final set rather than failing the
        # whole aggregation.
        out: dict[str, Any] = {}
        hit_step_budget = False
        try:
            out = agent.invoke(
                {"messages": [("user", user)]},
                config={"recursion_limit": _AGG_RECURSION_LIMIT},
            )
        except Exception as e:  # noqa: BLE001 - partial store beats a dead aggregation
            if not is_recursion_limit(e):
                raise
            hit_step_budget = True
            log.warning(
                "Annotation aggregator hit its step budget; shipping the %d "
                "annotation(s) recorded so far.",
                len(store),
            )
        if not store and hit_step_budget:
            # A budget-blown run that recorded NOTHING gets no nudge: pressing
            # an already-exhausted agent to produce output invites hallucinated
            # annotations. The empty final set is the honest outcome.
            return store
        if not store:
            # The empty-store nudge, once: continue the SAME conversation with a
            # steering message (no middleware hook for this — the nudge is a
            # follow-up turn, same semantics as the RI-era after_model
            # re-prompt).
            messages = list(out.get("messages") or [])
            messages.append(("user", _AGG_NUDGE))
            try:
                agent.invoke(
                    {"messages": messages},
                    config={"recursion_limit": _AGG_RECURSION_LIMIT},
                )
            except Exception as e:  # noqa: BLE001
                if not is_recursion_limit(e):
                    raise
        return store
    finally:
        shutil.rmtree(docs_dir, ignore_errors=True)
