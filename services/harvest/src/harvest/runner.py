"""Drive a harvest: build the agent, run the crawl, finalize the bundle.

Two entry paths:
- ``run_full_harvest`` — author the whole dataset (one sub-agent per table).
- ``run_incremental_harvest`` — re-review a single table plus, via backlinks,
  the docs that reference it (the incremental path from the design).

The crawl talks to the compiled deepagents graph with a single ``invoke`` (the
supervisor plans and fans out sub-agents internally). Kept import-light at
module load; deepagents/boto3 are pulled in by ``agent.build_harvest_agent`` and
``clients``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harvest.agent import build_harvest_agent, resolve_model_config
from harvest.code_interpreter import build_sandbox
from harvest.finalize import finalize_bundle, mark_in_progress
from harvest.fsutil import clean_authored_output, write_text
from harvest.glue_source import GlueAthenaSource
from harvest.metadata_export import export_metadata
from harvest.status import build_registry_client, report_status

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_emitter(*, data_domain: str, dataset: str, session_id: str | None):
    """Build the :class:`~harvest.steps.StepEmitter` for the live feed, or None.

    Created BEFORE the agent so its usage-metering callback can be attached to the
    shared model instance (see ``build_harvest_agent(step_emitter=...)``). Best-
    effort: if langchain/steps can't be set up, returns None so the harvest still
    runs without a feed.
    """
    try:
        from harvest.steps import StepEmitter, make_log_sink

        sink = make_log_sink(
            data_domain=data_domain, dataset=dataset, session_id=session_id
        )
        return StepEmitter(sink)
    except Exception:  # noqa: BLE001 - the step feed is an enhancement, never a hard dep
        log.warning("Could not build harvest step emitter (continuing).", exc_info=True)
        return None


def _invoke_config(recursion_limit: int, emitter):
    """Build the agent-invoke config for an already-built ``emitter``.

    The :class:`~harvest.steps.StepEmitter` rides on ``config["callbacks"]`` so it
    observes the supervisor AND every sub-agent dispatched as a LangGraph subgraph
    (parent callbacks propagate into those). It is ALSO handed the sub-agent fleet
    events by the streaming drain loop (which ride LangGraph's custom stream, NOT
    the callback surface). NOTE: token usage is metered separately on the model
    instance (UsageForwarder), because QuickJS ``task()`` sub-agents never reach
    this run-config callback — see ``build_harvest_agent``.
    """
    config: dict[str, Any] = {"recursion_limit": recursion_limit}
    if emitter is not None:
        config["callbacks"] = [emitter]
    return config


def _run_agent(agent, prompt: str, config: dict[str, Any], emitter) -> None:
    """Drive the agent and drain its stream, surfacing the sub-agent fleet.

    We use ``.stream(stream_mode=["custom"], subgraphs=True)`` instead of
    ``.invoke()`` for ONE added capability: LangGraph's *custom* stream carries
    the QuickJS sub-agent lifecycle events (``langchain_quickjs`` emits
    ``{type:'subagent', phase:'start'|'complete'|'error', id, eval_id, ...}``),
    which the callback surface never sees. The existing ``StepEmitter`` callback
    still fires identically under ``.stream()`` (``invoke`` is built on ``stream``),
    so the fine-grained step feed is unchanged — this only ADDS the fleet view.
    The UI grows a squares row as sub-agents actually START (there is no reliable
    pre-start count: the model builds the fan-out list dynamically at runtime).

    CRITICAL: the generator MUST be drained to exhaustion (never ``break``) or the
    graph stalls mid-run. ``finalize_bundle`` runs after this returns, as before.

    stream_mode MUST be a *list* (not a tuple) + ``subgraphs=True`` for the
    3-tuple ``(namespace, mode, chunk)`` shape; a tuple silently changes it.
    """
    inputs = {"messages": [{"role": "user", "content": prompt}]}

    # No emitter (steps unavailable) → the fleet view has nowhere to go; a plain
    # invoke is simplest and preserves the prior behavior exactly.
    if emitter is None:
        agent.invoke(inputs, config)
        return

    for _ns, mode, chunk in agent.stream(
        inputs, config, stream_mode=["custom"], subgraphs=True
    ):
        # QuickJS sub-agent lifecycle event → a fleet square update. (The step
        # feed rides config['callbacks'], which fire as a side effect during
        # iteration — no per-mode handling needed here.) A streaming failure is
        # allowed to propagate so the caller reports the harvest failed, exactly
        # as the old invoke() path did.
        if (
            mode == "custom"
            and isinstance(chunk, dict)
            and chunk.get("type") == "subagent"
        ):
            emitter.emit_subagent_event(chunk)


@contextlib.contextmanager
def _sandbox_for(dataset_root: str | Path):
    """Yield a started CodeSandbox with .context/ uploaded, or None if unavailable.

    Owns the sandbox lifecycle for one crawl: start the session, upload the
    dataset's ``.context/`` docs so the agent's ``run_code`` can read them, and
    ALWAYS stop the session on exit. Best-effort — a build/start/upload failure
    degrades the harvest to running WITHOUT the sandbox (yields None) rather than
    failing it, so the offline path and any CI-unavailable environment still work.
    """
    sandbox = build_sandbox()
    if sandbox is None:
        yield None
        return
    try:
        sandbox.start()
        uploaded = sandbox.upload_context(dataset_root)
        log.info("Harvest sandbox ready (%d context doc(s) uploaded)", len(uploaded))
    except Exception:  # noqa: BLE001 - sandbox is an enhancement, never a hard dep
        log.warning(
            "Sandbox start/upload failed; running without run_code.", exc_info=True
        )
        sandbox.stop()
        yield None
        return
    try:
        yield sandbox
    finally:
        sandbox.stop()


def _table_versions(source: GlueAthenaSource) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in source.table_names():
        ref = source.find(("tables", name))
        if ref is None:
            continue
        try:
            meta = source.read_concept(ref)
        except Exception:  # noqa: BLE001
            continue
        vid = meta.get("version_id")
        if vid is not None:
            versions[name] = str(vid)
    return versions


def run_full_harvest(
    *,
    source: GlueAthenaSource,
    dataset_root: str | Path,
    data_domain: str,
    dataset: str,
    model_config: dict[str, Any] | None = None,
    recursion_limit: int = 1000,
    domain_description: str | None = None,
    domain_context: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Author (or refresh) the entire dataset bundle end to end."""
    dataset_root = Path(dataset_root)
    started = _now_iso()
    # Build the registry client FIRST so the whole run (including the filesystem
    # setup below) is covered by failure reporting. Otherwise a crash before the
    # status flip (e.g. an EACCES from the S3 Files mount inside mark_in_progress)
    # would leave the registry row stuck at `queued` forever, holding the harvest
    # lease and wedging the dataset — which is exactly what happened when the
    # incremental path polluted the mount tree with raw put_object writes.
    registry = build_registry_client()
    try:
        # Mark in-progress FIRST (creates .harvest/, flips consumers to mid-write),
        # then wipe prior authored output so a "full" harvest truly starts from
        # scratch: stale docs for dropped tables don't linger, and their vectors
        # are pruned via the S3 write-through -> ObjectRemoved -> reindex
        # DeleteVectors. User-uploaded .context/ and the .harvest/ marker are kept.
        mark_in_progress(
            dataset_root, data_domain=data_domain, dataset=dataset, timestamp=started
        )
        removed = clean_authored_output(dataset_root)
        if removed:
            log.info(
                "Full harvest %s/%s: cleared prior output before rebuild: %s",
                data_domain,
                dataset,
                ", ".join(removed),
            )

        # Resolve the effective model config up front so we can both build the
        # agent with it AND record the resolved model/effort on the status row.
        resolved_config = model_config or resolve_model_config()

        # The Control API wrote the registry row as `queued`; now that the agent
        # has actually picked the job up, advance it — stamping the resolved
        # model/effort so the UI can show what this run is using.
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="running",
            model=resolved_config.get("model"),
            effort=resolved_config.get("effort"),
        )

        tables = source.table_names()
        # Snapshot ALL Glue metadata to the read-only .metadata/ dir BEFORE the
        # agent runs. The agent explores it with read_file/glob/grep (one grep
        # over .metadata/columns.tsv finds every table with a given column — the
        # join/near-synonym discovery move); live verification stays on
        # sample_rows/run_sql. Best-effort: a snapshot failure must not wedge the
        # harvest — the agent can still author from sample_rows/run_sql.
        try:
            snap = export_metadata(source, dataset_root)
            log.info(
                "Metadata snapshot written for %s/%s: %d tables, %d files",
                data_domain,
                dataset,
                snap["table_count"],
                snap["files_written"],
            )
        except Exception:  # noqa: BLE001 - snapshot is an accelerator, not a hard dep
            log.warning(
                "Metadata snapshot failed for %s/%s; agent runs without .metadata/",
                data_domain,
                dataset,
                exc_info=True,
            )
        # Build the domain context preamble if the Control API enriched the payload.
        domain_preamble = ""
        if domain_description or domain_context:
            domain_preamble = (
                f"**Domain context** (provided by the domain administrator):\n"
                f"- Description: {domain_description or '(none)'}\n"
                f"- Context: {domain_context or '(none)'}\n\n"
                "Use this domain information to inform your authoring — reference "
                "it in the dataset overview and use it to frame table descriptions "
                "and known issues.\n\n"
            )
        prompt = (
            f"{domain_preamble}"
            f"Harvest the Glue database `{dataset}` (data domain `{data_domain}`) into "
            f"a complete OKF bundle. It has {len(tables)} table(s): "
            f"{', '.join(tables)}.\n\n"
            f"Plan the work with write_todos, dispatch one `table-author` sub-agent "
            f"per table, then author the dataset overview, known_issues, joins, and "
            f"metrics. Validate query patterns with run_sql. Use get_backlinks when "
            f"you change a referenced doc."
        )
        # Open the code-execution sandbox for the crawl and upload .context/ docs
        # into it so the agent can extract text from binary formats. Best-effort:
        # None when no interpreter is configured (local dev / tests) — the agent
        # then runs without run_code (text-only .context reading), never wedged.
        # Build the step emitter FIRST so its usage-metering callback can ride on
        # the shared model instance (catches QuickJS sub-agent turns too).
        emitter = _build_emitter(
            data_domain=data_domain, dataset=dataset, session_id=session_id
        )
        with _sandbox_for(dataset_root) as sandbox:
            built = build_harvest_agent(
                source,
                dataset_root,
                sandbox=sandbox,
                step_emitter=emitter,
                **resolved_config,
            )
            config = _invoke_config(recursion_limit, emitter)
            _run_agent(built.agent, prompt, config, emitter)

        state = finalize_bundle(
            dataset_root,
            data_domain=data_domain,
            dataset=dataset,
            tables=tables,
            timestamp=_now_iso(),
            table_versions=_table_versions(source),
        )
    except Exception as e:  # noqa: BLE001 - report failure, then re-raise
        # only_if_active: a cancel may have raced ahead (StopRuntimeSession tears
        # down the crawl, which then throws) — don't clobber the `cancelled` row.
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="failed",
            detail=f"{type(e).__name__}: {e}",
            only_if_active=True,
        )
        raise

    report_status(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        status="complete",
        only_if_active=True,
    )
    log.info("Harvest complete: %s/%s (%d tables)", data_domain, dataset, len(tables))
    return state


def run_incremental_harvest(
    *,
    source: GlueAthenaSource,
    dataset_root: str | Path,
    data_domain: str,
    dataset: str,
    changed_table: str,
    diff: dict[str, Any] | None = None,
    model_config: dict[str, Any] | None = None,
    recursion_limit: int = 400,
    domain_description: str | None = None,
    domain_context: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Re-review one changed table and the docs that reference it."""
    dataset_root = Path(dataset_root)
    started = _now_iso()
    # Registry client first, then wrap the filesystem setup so a mount failure in
    # mark_in_progress reports `failed` and frees the lease instead of wedging the
    # dataset at `queued` (see run_full_harvest for the full rationale).
    registry = build_registry_client()
    try:
        mark_in_progress(
            dataset_root, data_domain=data_domain, dataset=dataset, timestamp=started
        )

        # Persist the diff so the agent can read exactly what changed.
        if diff is not None:
            pend = dataset_root / ".harvest"
            write_text(pend / "pending.json", json.dumps(diff, indent=2) + "\n")

        resolved_config = model_config or resolve_model_config()
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="running",
            model=resolved_config.get("model"),
            effort=resolved_config.get("effort"),
        )

        # Refresh the read-only .metadata/ snapshot so the changed table's current
        # Glue metadata (and its siblings, for backlink propagation) is on disk for
        # read_file/grep. Best-effort — the agent can fall back to live tools.
        try:
            export_metadata(source, dataset_root)
        except Exception:  # noqa: BLE001 - snapshot is an accelerator, not a hard dep
            log.warning(
                "Metadata snapshot failed for %s/%s (incremental); continuing",
                data_domain,
                dataset,
                exc_info=True,
            )

        diff_note = ""
        if diff:
            diff_note = (
                f"\n\nThe change diff is in `.harvest/pending.json`: {json.dumps(diff)}"
            )
        domain_preamble = ""
        if domain_description or domain_context:
            domain_preamble = (
                f"**Domain context**: {domain_description or ''} "
                f"{domain_context or ''}\n\n"
            )
        prompt = (
            f"{domain_preamble}"
            f"The Glue table `{changed_table}` in database `{dataset}` changed. "  # nosec B608 - a natural-language instruction to the harvest agent, not a SQL query; no SQL is constructed or executed here.
            f"Review its OKF doc `tables/{changed_table}` against the current Glue "
            f"metadata (`.metadata/tables/{changed_table}.md`) and a fresh sample "
            f"(sample_rows), and "
            f"update it. Then call get_backlinks('tables/{changed_table}') to find "
            f"every doc that references it — join docs, metrics, the dataset "
            f"overview, sibling tables — and update those so the change propagates "
            f"and nothing goes stale. Preserve existing schema fields and citations "
            f"(augmentation guard).{diff_note}"
        )
        emitter = _build_emitter(
            data_domain=data_domain, dataset=dataset, session_id=session_id
        )
        with _sandbox_for(dataset_root) as sandbox:
            built = build_harvest_agent(
                source,
                dataset_root,
                sandbox=sandbox,
                step_emitter=emitter,
                **resolved_config,
            )
            config = _invoke_config(recursion_limit, emitter)
            _run_agent(built.agent, prompt, config, emitter)

        state = finalize_bundle(
            dataset_root,
            data_domain=data_domain,
            dataset=dataset,
            tables=source.table_names(),
            timestamp=_now_iso(),
            table_versions=_table_versions(source),
        )
    except Exception as e:  # noqa: BLE001 - report failure, then re-raise
        # only_if_active: don't clobber a `cancelled` row if a cancel raced ahead.
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="failed",
            detail=f"{type(e).__name__}: {e}",
            only_if_active=True,
        )
        raise

    # Clear the pending diff now that it's been applied.
    pend_file = dataset_root / ".harvest" / "pending.json"
    if pend_file.exists():
        pend_file.unlink()
    report_status(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        status="complete",
        only_if_active=True,
    )
    log.info("Incremental harvest complete: %s.%s", dataset, changed_table)
    return state
