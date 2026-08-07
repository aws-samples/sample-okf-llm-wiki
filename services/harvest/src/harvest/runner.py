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

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harvest.agent import build_harvest_agent, resolve_model_config
from harvest.annotations import (
    build_annotations_client,
    resolve_annotation,
    revert_to_open,
)
from harvest.code_interpreter import sandbox_session
from harvest.ar_build import (
    OUTCOME_LOCKED,
    build_enabled,
    maybe_build_policy,
    publish_rebuild_event,
)
from harvest.finalize import finalize_bundle, mark_in_progress
from harvest.fsutil import clean_authored_output, remove_tree, write_text
from harvest.metadata_export import export_metadata, export_target_metadata
from harvest.prompts import (
    build_annotation_supervisor_prompt,
    build_annotation_user_prompt,
    build_cross_run_prompt,
    build_maintenance_supervisor_prompt,
    guidance_block,
)


def _prompt_is_gpt(model: str | None) -> bool:
    """Whether ``model`` selects the GPT-family prompt addendum (see prompts)."""
    from harvest.agent import _is_openai_model

    return bool(model) and _is_openai_model(model)
from harvest.source_base import Source
from harvest.status import (
    build_registry_client,
    read_run_identity,
    report_status,
    stamp_guidance_applied,
)
from okf_core.paths import external_pair_prefix

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: How long the follow-on policy build waits for the bundle's S3 flush, and
#: the extra settle margin after the commit marker appears. The bundle is
#: written THROUGH the S3 Files mount, whose write-back can lag the terminal
#: status write by a minute (observed live 2026-08-04: the regenerated
#: index.md files + marker landed 62s after the runner moved on) — and the
#: flush is not strictly ordered (two index files landed AFTER the marker),
#: hence the margin on top of marker visibility.
_BUNDLE_FLUSH_TIMEOUT_S = 180.0
_POST_MARKER_SETTLE_S = 10.0


def _wait_for_bundle_flush(
    *, data_domain: str, dataset: str, completed_at: str = ""
) -> None:
    """Block until the committed bundle is visible in S3 (bounded; never raises).

    Gathering before the mount flush settles fingerprints a PARTIAL wiki: the
    authored document then reads "out of date" the moment the flush lands
    (and checks pause) even though nothing really changed. The marker is
    written last, so marker-visible ≈ flush-settled; the margin covers the
    near-marker stragglers.

    ``completed_at`` pins the wait to THIS run's marker: on a re-harvest the
    PREVIOUS run's ``complete`` marker is still the visible object until the
    mount flushes (mark_in_progress's overwrite lags exactly like every other
    write), so a bare status check would return immediately and fingerprint
    the PRE-run wiki. Best-effort — the post-build freshness re-check and the
    rebuild event self-heal whatever slips through.
    """
    import time

    bucket = os.environ.get("OKF_BUNDLE_BUCKET", "")
    if not bucket:
        return
    try:
        import boto3

        from okf_aws import bundle_marker_state

        s3 = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        deadline = time.monotonic() + _BUNDLE_FLUSH_TIMEOUT_S
        while time.monotonic() < deadline:
            state = bundle_marker_state(s3, bucket, data_domain, dataset) or {}
            if state.get("status") == "complete" and (
                not completed_at or state.get("completed_at") == completed_at
            ):
                time.sleep(_POST_MARKER_SETTLE_S)
                return
            time.sleep(5)
        log.warning(
            "bundle flush for %s/%s not visible in S3 after %.0fs — "
            "authoring anyway (the freshness re-check self-heals)",
            data_domain,
            dataset,
            _BUNDLE_FLUSH_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - advisory wait, never fatal
        log.warning("bundle flush wait failed (non-fatal)", exc_info=True)


def _follow_on_policy_build(
    *,
    data_domain: str,
    dataset: str,
    completed: bool,
    marker_completed_at: str = "",
) -> None:
    """The post-terminal guardrails build (one shared tail for all run modes).

    Runs ONLY when the terminal `complete` write actually landed: a False
    from report_status means an operator cancel won the race, and authoring
    guardrails for a run the operator was just told is cancelled would hold
    the build lock (and 409 their next action) for a ghost. The feature flag
    is checked BEFORE the flush wait — with policy builds off,
    maybe_build_policy is a no-op and the (up to ~190s of billed runtime)
    wait would buy nothing. ``marker_completed_at`` is the timestamp
    finalize_bundle stamped into THIS run's commit marker (see
    ``_wait_for_bundle_flush``). Losing the flip race (another author still
    running — typically the previous harvest's) leaves a `policy_rebuild`
    trigger behind so the newer wiki still gets its re-author without waiting
    for the nightly reconcile. Never raises.
    """
    if not completed:
        log.info(
            "harvest row already terminal (cancelled?) — skipping the "
            "follow-on policy build for %s/%s",
            data_domain,
            dataset,
        )
        return
    if not build_enabled():
        return
    _wait_for_bundle_flush(
        data_domain=data_domain, dataset=dataset, completed_at=marker_completed_at
    )
    outcome = maybe_build_policy(data_domain=data_domain, dataset=dataset)
    if outcome == OUTCOME_LOCKED:
        publish_rebuild_event(
            data_domain, dataset, reason="post_harvest_build_locked"
        )


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


def _emit_profile_summary(emitter, snap: dict[str, Any] | None) -> None:
    """One live-feed line summarizing the column-profile phase.

    Silent when profiling didn't run at all (disabled, or a source without the
    SQL atoms) — a "0 profiled" line would read as a failure. Guarded like
    every feed emission: never raises into the harvest.
    """
    if emitter is None or not isinstance(snap, dict):
        return
    prof = snap.get("profiles") or {}
    profiled = prof.get("profiled", 0)
    cached = prof.get("cached", 0)
    skipped = prof.get("skipped", 0)
    if not (profiled or cached or skipped):
        return
    bits = [f"{profiled} profiled"]
    if cached:
        bits.append(f"{cached} reused from cache")
    if skipped:
        bits.append(f"{skipped} skipped")
    emitter.emit_status("Column profiles: " + ", ".join(bits))


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


def _sandbox_for(dataset_root: str | Path):
    """A started CodeSandbox with .context/ uploaded, or None if unavailable.

    The lifecycle contextmanager lives in harvest.code_interpreter
    (``sandbox_session``) — shared with Benchmark Studio's judge; this wrapper
    just brands the log lines for the crawl.
    """
    return sandbox_session(dataset_root, label="Harvest")


def _table_versions(source: Source) -> dict[str, str]:
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


def _guidance_preamble(dataset_guidance: str | None) -> str:
    """The operator-guidance prompt block, or "" when none.

    Thin alias over :func:`harvest.prompts.guidance_block` — the ONE canonical
    rendering shared by all three run modes (this used to be a second,
    near-identical copy that was drifting from the annotation mode's).
    """
    return guidance_block(dataset_guidance)


def run_full_harvest(
    *,
    source: Source,
    dataset_root: str | Path,
    data_domain: str,
    dataset: str,
    model_config: dict[str, Any] | None = None,
    subagent_model_config: dict[str, Any] | None = None,
    reviewer_model_config: dict[str, Any] | None = None,
    recursion_limit: int = 1000,
    domain_description: str | None = None,
    domain_context: str | None = None,
    dataset_guidance: str | None = None,
    dataset_guidance_version: str | None = None,
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
    # This run's row identity, captured before any work: the terminal writes
    # below must never land on a SUCCESSOR run's row, and on the incremental
    # path the session pin alone can't tell two runs apart (deterministic
    # session id — see status.read_run_identity).
    run_started_at = read_run_identity(
        registry, data_domain=data_domain, dataset=dataset
    )
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
        # Also reset the review workflow's state (.harvest/review/ — the
        # persisted clustering + past run_review reports). clean_authored_output
        # preserves .harvest/ wholesale for the commit marker, but a clustering
        # from a PREVIOUS harvest describes docs this run is about to rebuild —
        # a retry against it would review the wrong groups.
        # Same for the recorded context-extractor digests (.harvest/context/):
        # this run re-extracts from the CURRENT .context/ uploads, and a stale
        # digest would feed the fidelity phase facts nobody extracted this run.
        if remove_tree(Path(dataset_root) / ".harvest" / "context"):
            log.info(
                "Full harvest %s/%s: cleared prior context digests",
                data_domain,
                dataset,
            )
        if remove_tree(Path(dataset_root) / ".harvest" / "review"):
            log.info(
                "Full harvest %s/%s: cleared prior review state", data_domain, dataset
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
            subagent_model=(subagent_model_config or {}).get("model"),
            subagent_effort=(subagent_model_config or {}).get("effort"),
            reviewer_model=(reviewer_model_config or {}).get("model"),
            reviewer_effort=(reviewer_model_config or {}).get("effort"),
        )

        tables = source.table_names()
        # Built BEFORE the snapshot so the pre-agent phases (metadata export +
        # column profiling) can narrate into the live feed via emit_status —
        # profiling can run minutes on a wide dataset, and a silent feed reads
        # as a stuck run. (It also still rides build_harvest_agent below.)
        emitter = _build_emitter(
            data_domain=data_domain, dataset=dataset, session_id=session_id
        )
        # Snapshot ALL Glue metadata to the read-only .metadata/ dir BEFORE the
        # agent runs. The agent explores it with read_file/glob/grep (one grep
        # over .metadata/columns.tsv finds every table with a given column — the
        # join/near-synonym discovery move); live verification stays on
        # sample_rows/run_sql. Best-effort: a snapshot failure must not wedge the
        # harvest — the agent can still author from sample_rows/run_sql.
        try:
            if emitter is not None:
                emitter.emit_status(
                    f"Snapshotting catalog metadata and profiling columns "
                    f"({len(tables)} tables)…"
                )
            snap = export_metadata(source, dataset_root)
            _emit_profile_summary(emitter, snap)
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
        # Run facts only — the supervisor SYSTEM prompt owns the workflow
        # (fan-outs, reference ownership, review pass); restating it here drifted
        # once already (this message used to assign the supervisor work the
        # system prompt routes to sub-agents).
        prompt = (
            f"{domain_preamble}"
            f"{_guidance_preamble(dataset_guidance)}"
            f"Harvest the Glue database `{dataset}` (data domain `{data_domain}`) into "
            f"a complete OKF bundle. It has {len(tables)} table(s): "
            f"{', '.join(tables)}. Follow your supervisor workflow end to end, "
            f"including the review pass."
        )
        # Open the code-execution sandbox for the crawl and upload .context/ docs
        # into it so the agent can extract text from binary formats. Best-effort:
        # None when no interpreter is configured (local dev / tests) — the agent
        # then runs without run_code (text-only .context reading), never wedged.
        # The step emitter (built above, before the snapshot) rides into
        # build_harvest_agent so its usage-metering callback sits on the shared
        # model instance (catches QuickJS sub-agent turns too).
        with _sandbox_for(dataset_root) as sandbox:
            built = build_harvest_agent(
                source,
                dataset_root,
                sandbox=sandbox,
                step_emitter=emitter,
                subagent_config=subagent_model_config,
                reviewer_config=reviewer_model_config,
                **resolved_config,
            )
            config = _invoke_config(recursion_limit, emitter)
            _run_agent(built.agent, prompt, config, emitter)

        # MECHANICAL lint backstop. The supervisor prompt prescribes the
        # fix-to-zero lint gate, but a prompt is advice (same rationale that
        # made chat's guardrails gate mechanical): a truncated or
        # recursion-clipped run can skip step 8 and still reach finalize. The
        # offline half of the gate is pure and cheap, so measure the bundle
        # as shipped and surface the counts on the status row — a bundle
        # published with lint errors must be visible, never silent. It does
        # NOT block publish (the bundle is still better than no bundle);
        # best-effort, never fails the run.
        lint_detail = None
        try:
            from okf_core.lint import lint_bundle as _offline_lint

            _rep = _offline_lint(Path(dataset_root))
            _errs = sum(1 for f in _rep.findings if f.severity == "error")
            _warns = sum(1 for f in _rep.findings if f.severity == "warning")
            if _errs:
                lint_detail = (
                    f"published with lint findings: {_errs} error(s), "
                    f"{_warns} warning(s)"
                )
                log.warning(
                    "Post-run lint backstop for %s/%s: %d error(s), %d "
                    "warning(s) — the supervisor did not fix to zero",
                    data_domain,
                    dataset,
                    _errs,
                    _warns,
                )
        except Exception:  # noqa: BLE001 - the backstop must never fail the run
            log.warning("Post-run lint backstop failed", exc_info=True)

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
            session_id=session_id,
            run_started_at=run_started_at,
        )
        raise

    completed = report_status(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        status="complete",
        detail=lint_detail,
        only_if_active=True,
        session_id=session_id,
        run_started_at=run_started_at,
    )
    # The bundle now reflects this guidance version — clear its DIRTY state.
    stamp_guidance_applied(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        version=dataset_guidance_version,
    )
    log.info("Harvest complete: %s/%s (%d tables)", data_domain, dataset, len(tables))
    # The HARVEST is done — status is terminal and the bundle is consumable.
    # The policy document authors as its own follow-on step: its `building`
    # flip on the mapping row (okf_aws.ar_policy.build_lock_active) is what
    # keeps new bundle-writing work out until it lands. Never raises.
    _follow_on_policy_build(
        data_domain=data_domain,
        dataset=dataset,
        completed=completed,
        marker_completed_at=str(state.get("completed_at") or ""),
    )
    return state


def run_incremental_harvest(
    *,
    source: Source,
    dataset_root: str | Path,
    data_domain: str,
    dataset: str,
    changed_table: str,
    diff: dict[str, Any] | None = None,
    model_config: dict[str, Any] | None = None,
    subagent_model_config: dict[str, Any] | None = None,
    reviewer_model_config: dict[str, Any] | None = None,
    recursion_limit: int = 400,
    domain_description: str | None = None,
    domain_context: str | None = None,
    dataset_guidance: str | None = None,
    dataset_guidance_version: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Re-review one changed table and the docs that reference it."""
    dataset_root = Path(dataset_root)
    started = _now_iso()
    # Registry client first, then wrap the filesystem setup so a mount failure in
    # mark_in_progress reports `failed` and frees the lease instead of wedging the
    # dataset at `queued` (see run_full_harvest for the full rationale).
    registry = build_registry_client()
    # This run's row identity, captured before any work: the terminal writes
    # below must never land on a SUCCESSOR run's row, and on the incremental
    # path the session pin alone can't tell two runs apart (deterministic
    # session id — see status.read_run_identity).
    run_started_at = read_run_identity(
        registry, data_domain=data_domain, dataset=dataset
    )
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
            subagent_model=(subagent_model_config or {}).get("model"),
            subagent_effort=(subagent_model_config or {}).get("effort"),
            reviewer_model=(reviewer_model_config or {}).get("model"),
            reviewer_effort=(reviewer_model_config or {}).get("effort"),
        )

        # Built before the snapshot so the profiling refresh narrates into the
        # live feed (see run_full_harvest); reused for the agent run below.
        emitter = _build_emitter(
            data_domain=data_domain, dataset=dataset, session_id=session_id
        )
        # Refresh the read-only .metadata/ snapshot so the changed table's current
        # Glue metadata (and its siblings, for backlink propagation) is on disk for
        # read_file/grep. Best-effort — the agent can fall back to live tools.
        # profile_mode="incremental": only the changed table is re-profiled; the
        # other tables' column profiles are reused from the previous run's cache.
        try:
            if emitter is not None:
                emitter.emit_status(
                    f"Refreshing metadata snapshot; re-profiling `{changed_table}` "
                    "(unchanged tables reuse cached profiles)…"
                )
            snap = export_metadata(
                source,
                dataset_root,
                profile_mode="incremental",
                changed_tables={changed_table},
            )
            _emit_profile_summary(emitter, snap)
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
            f"{_guidance_preamble(dataset_guidance)}"
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
        with _sandbox_for(dataset_root) as sandbox:
            built = build_harvest_agent(
                source,
                dataset_root,
                sandbox=sandbox,
                step_emitter=emitter,
                subagent_config=subagent_model_config,
                reviewer_config=reviewer_model_config,
                # Scoped system prompt: the full-harvest supervisor body would
                # prescribe a per-table fan-out + whole-bundle review, which an
                # incremental run must NOT do.
                supervisor_prompt=build_maintenance_supervisor_prompt(
                    source.prompt_profile,
                    gpt=_prompt_is_gpt(resolved_config.get("model")),
                ),
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
            session_id=session_id,
            run_started_at=run_started_at,
        )
        raise

    # Clear the pending diff now that it's been applied.
    pend_file = dataset_root / ".harvest" / "pending.json"
    if pend_file.exists():
        pend_file.unlink()
    completed = report_status(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        status="complete",
        only_if_active=True,
        session_id=session_id,
        run_started_at=run_started_at,
    )
    stamp_guidance_applied(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        version=dataset_guidance_version,
    )
    log.info("Incremental harvest complete: %s.%s", dataset, changed_table)
    # Follow-on policy authoring under its own lock (see run_full_harvest).
    _follow_on_policy_build(
        data_domain=data_domain,
        dataset=dataset,
        completed=completed,
        marker_completed_at=str(state.get("completed_at") or ""),
    )
    return state


def _assert_target_ready(
    target_root: Path, target_data_domain: str, target_dataset: str
) -> None:
    """Raise unless the target bundle's commit marker says ``complete``.

    The Control API checked readiness at TRIGGER time, but no lease is held on
    the target — a full harvest of it may have started since (flipping the
    marker to ``in_progress`` before its clean wipe). Called before AND after
    the snapshot copy so a wipe racing the copy fails this run loudly instead
    of shipping pair docs authored against a half-wiped wiki.
    """
    marker = Path(target_root) / ".harvest" / "state.json"
    try:
        status = json.loads(marker.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        status = None
    if status != "complete":
        raise RuntimeError(
            f"target bundle {target_data_domain}/{target_dataset} is not "
            f"published (marker status={status!r}) — a harvest of it may be in "
            "flight; retry the cross run when it completes"
        )


def run_cross_harvest(
    *,
    source: Source,
    dataset_root: str | Path,
    data_domain: str,
    dataset: str,
    target_source: Source,
    target_root: str | Path,
    target_data_domain: str,
    target_dataset: str,
    model_config: dict[str, Any] | None = None,
    subagent_model_config: dict[str, Any] | None = None,
    reviewer_model_config: dict[str, Any] | None = None,
    recursion_limit: int = 600,
    domain_description: str | None = None,
    domain_context: str | None = None,
    target_domain_description: str | None = None,
    target_domain_context: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Cross-dataset references: explore the target, verify, author ``external/``.

    Roadmap §5's OSS (flat-trust) mode. The run holds THIS dataset's lease
    throughout (it is a harvest of this dataset, taken by the Control API) and
    reads the target ONLY via a start-time snapshot
    (``.metadata/external/<td>/<tds>/`` — catalog + published docs). The agent
    authors exclusively under ``external/<td>/<tds>/`` (guard-enforced).

    The pair docs live ONLY here — nothing is ever written into the target's
    bundle. That is deliberate: a mirrored copy would be a distributed fact
    spread over two independently versioned, independently restorable bundles
    (a full harvest or repromote of the target would silently break the pair
    state). Instead the target side gets a DERIVED discovery signal: the
    reindex worker maintains ``XREF#`` rows from this run's object events, and
    ``list_domains`` surfaces "cross-referenced by" on the target (see
    CONVENTIONS.md).

    Deliberately NOT threaded through: ``dataset_guidance`` (dataset-scoped
    authoring instructions do not apply to pair docs read from both sides).

    ORDERING IS LOAD-BEARING: all required inputs (the target-readiness checks
    and the target snapshot) run BEFORE the first destructive step — a failure
    there reports ``failed`` and leaves the bundle untouched and READY (prior
    pair docs intact, complete marker intact). Only once the inputs are on disk
    does the run flip the in-progress marker and clear the pair's prior output.
    """
    dataset_root = Path(dataset_root)
    target_root = Path(target_root)
    started = _now_iso()
    registry = build_registry_client()
    # This run's row identity, captured before any work: the terminal writes
    # below must never land on a SUCCESSOR run's row, and on the incremental
    # path the session pin alone can't tell two runs apart (deterministic
    # session id — see status.read_run_identity).
    run_started_at = read_run_identity(
        registry, data_domain=data_domain, dataset=dataset
    )
    # external_pair_prefix VALIDATES both target segments (no "/", "..", "#" can
    # reach a destructive path) — the runtime-side gate behind _validate.
    pair_dir = dataset_root / external_pair_prefix(target_data_domain, target_dataset)
    try:
        resolved_config = model_config or resolve_model_config()
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="running",
            model=resolved_config.get("model"),
            effort=resolved_config.get("effort"),
            subagent_model=(subagent_model_config or {}).get("model"),
            subagent_effort=(subagent_model_config or {}).get("effort"),
            reviewer_model=(reviewer_model_config or {}).get("model"),
            reviewer_effort=(reviewer_model_config or {}).get("effort"),
        )

        # Built before the snapshots so both narrate into the live feed (see
        # run_full_harvest); reused for the agent run below.
        emitter = _build_emitter(
            data_domain=data_domain, dataset=dataset, session_id=session_id
        )
        # This dataset's snapshot: best-effort, like every other mode.
        # profile_mode="cross": a cross run documents relationships, not this
        # dataset's own docs — cached column profiles are reused wholesale and
        # only fingerprint-mismatched/missing tables are re-profiled.
        try:
            if emitter is not None:
                emitter.emit_status(
                    "Snapshotting catalog metadata (cached column profiles "
                    "reused where unchanged)…"
                )
            snap = export_metadata(source, dataset_root, profile_mode="cross")
            _emit_profile_summary(emitter, snap)
        except Exception:  # noqa: BLE001 - snapshot is an accelerator, not a hard dep
            log.warning(
                "Metadata snapshot failed for %s/%s (cross); continuing",
                data_domain,
                dataset,
                exc_info=True,
            )
        # The TARGET's snapshot is REQUIRED: it is this mode's discovery surface
        # (grep both columns.tsv files) AND its read path to the target's wiki —
        # without it the run would author cross docs blind. Fail loud. The
        # trigger-time readiness check only covered trigger time — re-check the
        # target's commit marker around the copy so a full harvest of the target
        # that started since (its clean wipe racing our copy) fails THIS run
        # loudly instead of snapshotting a half-wiped wiki.
        _assert_target_ready(target_root, target_data_domain, target_dataset)
        snap = export_target_metadata(
            target_source,
            dataset_root,
            target_data_domain=target_data_domain,
            target_dataset=target_dataset,
            target_bundle_root=target_root,
        )
        _assert_target_ready(target_root, target_data_domain, target_dataset)
        log.info(
            "Cross target snapshot written for %s/%s -> %s/%s: %d tables, %d docs",
            data_domain,
            dataset,
            target_data_domain,
            target_dataset,
            snap["table_count"],
            snap["docs_copied"],
        )

        # All required inputs are on disk — only NOW go destructive: flip the
        # commit marker to in_progress and clear the pair's prior output (a
        # re-run replaces the pair's docs wholesale — the scoped analogue of
        # clean_authored_output: a join dropped since last time leaves no stale
        # doc, and its vector is pruned via the S3 write-through ->
        # ObjectRemoved -> reindex DeleteVectors. Other pairs' subtrees and the
        # rest of the bundle are untouched).
        mark_in_progress(
            dataset_root, data_domain=data_domain, dataset=dataset, timestamp=started
        )
        if remove_tree(pair_dir):
            log.info(
                "Cross harvest %s/%s: cleared prior pair output external/%s/%s",
                data_domain,
                dataset,
                target_data_domain,
                target_dataset,
            )

        prompt = build_cross_run_prompt(
            data_domain=data_domain,
            dataset=dataset,
            database=source.database,
            target_data_domain=target_data_domain,
            target_dataset=target_dataset,
            target_database=target_source.database,
            tables=source.table_names(),
            target_tables=target_source.table_names(),
            domain_description=domain_description,
            domain_context=domain_context,
            target_domain_description=target_domain_description,
            target_domain_context=target_domain_context,
        )
        with _sandbox_for(dataset_root) as sandbox:
            built = build_harvest_agent(
                source,
                dataset_root,
                sandbox=sandbox,
                step_emitter=emitter,
                cross_target={
                    "data_domain": target_data_domain,
                    "dataset": target_dataset,
                },
                subagent_config=subagent_model_config,
                reviewer_config=reviewer_model_config,
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
            extra={"cross_target": f"{target_data_domain}/{target_dataset}"},
        )

        # Count what the agent actually authored for the pair. Zero docs = the
        # run found no genuine convergence (a valid, common outcome — see the
        # skill's plausibility gate); the detail says so plainly. INSIDE the
        # try: this walks the NFS mount, and a transient ESTALE here must
        # report `failed` (releasing the lease) rather than skipping both
        # terminal status writes and wedging the row at `running`.
        authored = sorted(
            str(p.relative_to(pair_dir))
            for p in pair_dir.rglob("*.md")
            if p.name != "index.md"
        ) if pair_dir.is_dir() else []
    except Exception as e:  # noqa: BLE001 - report failure, then re-raise
        # only_if_active: don't clobber a `cancelled` row if a cancel raced ahead.
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="failed",
            detail=f"{type(e).__name__}: {e}",
            only_if_active=True,
            session_id=session_id,
            run_started_at=run_started_at,
        )
        raise

    detail = (
        f"cross-dataset references to {target_data_domain}/{target_dataset}: "
        f"{len(authored)} doc(s)"
        + ("" if authored else " — no genuine convergence found")
    )
    report_status(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        status="complete",
        detail=detail,
        only_if_active=True,
        session_id=session_id,
        run_started_at=run_started_at,
    )
    log.info("Cross harvest complete: %s/%s (%s)", data_domain, dataset, detail)
    return state


# The agent writes its per-annotation verdicts here (through the mount); the
# runner reads it back and reconciles to DynamoDB. Under .harvest/ so it's an
# input/scratch path the guard leaves alone and finalize never publishes.
ANNOTATION_RESULTS_REL = ".harvest/annotation_results.json"


def _reconcile_annotation_results(
    client_table,
    dataset_root: Path,
    *,
    data_domain: str,
    dataset: str,
    user_sub: str,
    survivors: list[dict[str, Any]],
) -> dict[str, int]:
    """Read the agent's verdict file and flip each annotation's DDB row.

    ``client_table`` is the shared (client, table) tuple (or None → no-op) so the
    caller builds one boto3 client for the whole run. ``survivors`` is the payload
    list the run was dispatched with. We resolve each annotation the agent ruled
    on (applied/rejected + comment) and REVERT any it left unaddressed back to
    ``open`` so that feedback isn't silently lost. Returns a
    ``{applied, rejected, reverted}`` tally so the caller can report what actually
    happened (a run that applied nothing must not read as a plain success). All
    best-effort: the S3 bundle is already the durable result; a write-back hiccup
    must not fail the harvest.
    """
    tally = {"applied": 0, "rejected": 0, "reverted": 0}
    if client_table is None:
        return tally

    verdicts: dict[str, dict[str, Any]] = {}
    results_path = dataset_root / ANNOTATION_RESULTS_REL
    if results_path.exists():
        try:
            raw = json.loads(results_path.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else raw.get("results", [])
            for entry in entries:
                if isinstance(entry, dict) and entry.get("annotation_id"):
                    verdicts[entry["annotation_id"]] = entry
        except Exception:  # noqa: BLE001 - a malformed file -> revert everything
            log.warning(
                "Could not parse %s; reverting all in-review annotations",
                ANNOTATION_RESULTS_REL,
                exc_info=True,
            )

    for ann in survivors:
        aid = ann.get("annotation_id")
        concept_id = ann.get("concept_id")
        if not aid or not concept_id:
            continue
        verdict = verdicts.get(aid)
        if verdict is None:
            # The agent didn't rule on this one — return it to the open pool.
            revert_to_open(
                client_table,
                data_domain=data_domain,
                dataset=dataset,
                user_sub=user_sub,
                concept_id=concept_id,
                annotation_id=aid,
            )
            tally["reverted"] += 1
            continue
        outcome = verdict.get("outcome", "")
        resolve_annotation(
            client_table,
            data_domain=data_domain,
            dataset=dataset,
            user_sub=user_sub,
            concept_id=concept_id,
            annotation_id=aid,
            outcome=outcome,
            comment=verdict.get("comment", ""),
        )
        # resolve_annotation coerces any non-"applied" outcome to rejected.
        tally["applied" if outcome == "applied" else "rejected"] += 1
    return tally


def run_annotation_harvest(
    *,
    source: Source,
    dataset_root: str | Path,
    data_domain: str,
    dataset: str,
    user_sub: str,
    annotations: list[dict[str, Any]],
    model_config: dict[str, Any] | None = None,
    subagent_model_config: dict[str, Any] | None = None,
    reviewer_model_config: dict[str, Any] | None = None,
    recursion_limit: int = 400,
    domain_description: str | None = None,
    domain_context: str | None = None,
    dataset_guidance: str | None = None,
    dataset_guidance_version: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Apply a user's wiki annotations to the bundle, then reconcile verdicts.

    Scoped/in-place like the incremental path (no ``clean_authored_output``): the
    agent assesses each annotation against LIVE data, edits the affected doc when
    the feedback is factually grounded (augmentation guard applies), and writes a
    per-annotation verdict + comment to ``.harvest/annotation_results.json``. The
    runner then reconciles that file to DynamoDB (resolve applied/rejected; revert
    any the agent skipped back to open).

    The Control API already ran the orphan pre-flight, so every annotation here is
    expected to still anchor to the live doc.
    """
    dataset_root = Path(dataset_root)
    started = _now_iso()
    registry = build_registry_client()
    # This run's row identity, captured before any work: the terminal writes
    # below must never land on a SUCCESSOR run's row, and on the incremental
    # path the session pin alone can't tell two runs apart (deterministic
    # session id — see status.read_run_identity).
    run_started_at = read_run_identity(
        registry, data_domain=data_domain, dataset=dataset
    )
    # One annotations client for the whole run (reconcile + any failure revert),
    # instead of rebuilding boto3 clients per path.
    anno_client = build_annotations_client()

    def _clear_scratch() -> None:
        # Remove the on-mount scratch files on EVERY exit (success or failure), so
        # a partial results file can't leak into a later run's reconcile.
        for rel in ("annotations.json", "annotation_results.json"):
            f = dataset_root / ".harvest" / rel
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                log.warning("Could not remove scratch %s (continuing)", rel)

    try:
        mark_in_progress(
            dataset_root, data_domain=data_domain, dataset=dataset, timestamp=started
        )

        # Persist the annotations through the mount so the agent reads them with
        # its file tools (the pending.json precedent). A stale results file from a
        # prior run must not leak into this one's reconcile — clear it up front.
        write_text(
            dataset_root / ".harvest" / "annotations.json",
            json.dumps(annotations, indent=2) + "\n",
        )
        stale_results = dataset_root / ANNOTATION_RESULTS_REL
        if stale_results.exists():
            stale_results.unlink()

        resolved_config = model_config or resolve_model_config()
        report_status(
            registry,
            data_domain=data_domain,
            dataset=dataset,
            status="running",
            model=resolved_config.get("model"),
            effort=resolved_config.get("effort"),
            subagent_model=(subagent_model_config or {}).get("model"),
            subagent_effort=(subagent_model_config or {}).get("effort"),
            reviewer_model=(reviewer_model_config or {}).get("model"),
            reviewer_effort=(reviewer_model_config or {}).get("effort"),
        )

        # Refresh the read-only .metadata/ snapshot so the agent verifies each
        # annotation against current Glue metadata. Best-effort accelerator.
        try:
            export_metadata(source, dataset_root)
        except Exception:  # noqa: BLE001 - snapshot is an accelerator, not a hard dep
            log.warning(
                "Metadata snapshot failed for %s/%s (annotated); continuing",
                data_domain,
                dataset,
                exc_info=True,
            )

        # Job spec = the annotation SUPERVISOR system prompt; the user message
        # carries only the run facts. This used to be one combined user prompt,
        # which shipped the runtime preamble TWICE (the system prompt was the
        # full-harvest supervisor's) and instructed a scoped-edit run to fan out
        # per table and review the whole bundle.
        prompt = build_annotation_user_prompt(
            dataset=dataset,
            annotations=annotations,
            results_rel=ANNOTATION_RESULTS_REL,
            domain_description=domain_description,
            domain_context=domain_context,
            dataset_guidance=dataset_guidance,
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
                subagent_config=subagent_model_config,
                reviewer_config=reviewer_model_config,
                supervisor_prompt=build_annotation_supervisor_prompt(
                    results_rel=ANNOTATION_RESULTS_REL,
                    profile=source.prompt_profile,
                    # Keyed to the SUPERVISOR's resolved model.
                    gpt=_prompt_is_gpt(resolved_config.get("model")),
                ),
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
            session_id=session_id,
            run_started_at=run_started_at,
        )
        # A failed run leaves the survivors stuck in_review — return them to open
        # so the feedback survives (mirrors the Control API's invoke-failure revert).
        for ann in annotations:
            revert_to_open(
                anno_client,
                data_domain=data_domain,
                dataset=dataset,
                user_sub=user_sub,
                concept_id=ann.get("concept_id"),
                annotation_id=ann.get("annotation_id"),
            )
        _clear_scratch()
        raise

    # Reconcile the agent's verdicts back to DynamoDB (resolve/revert per note).
    # Best-effort: a reconcile hiccup must not fail an already-finalized bundle nor
    # skip the terminal status write below (which would wedge the row at `running`).
    tally = {"applied": 0, "rejected": 0, "reverted": 0}
    try:
        tally = _reconcile_annotation_results(
            anno_client,
            dataset_root,
            data_domain=data_domain,
            dataset=dataset,
            user_sub=user_sub,
            survivors=annotations,
        )
    except Exception:  # noqa: BLE001 - never let write-back break a finished harvest
        log.warning(
            "Annotation write-back failed for %s/%s (bundle already finalized)",
            data_domain,
            dataset,
            exc_info=True,
        )
    _clear_scratch()

    # Report the outcome in the status detail so a run that APPLIED NOTHING (agent
    # wrote no/garbage verdicts -> all reverted) doesn't read as a plain success:
    # the reverted notes are open again, and the detail says so.
    detail = (
        f"annotations: {tally['applied']} applied, "
        f"{tally['rejected']} rejected, {tally['reverted']} returned to open"
    )
    completed = report_status(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        status="complete",
        detail=detail,
        only_if_active=True,
        session_id=session_id,
        run_started_at=run_started_at,
    )
    # The bundle now reflects this guidance version — clear its DIRTY state. (A
    # zero-annotation run that ran ONLY because guidance was dirty still lands here.)
    stamp_guidance_applied(
        registry,
        data_domain=data_domain,
        dataset=dataset,
        version=dataset_guidance_version,
    )
    log.info("Annotation harvest complete: %s/%s (%s)", data_domain, dataset, detail)
    # Follow-on policy authoring under its own lock (see run_full_harvest).
    _follow_on_policy_build(
        data_domain=data_domain,
        dataset=dataset,
        completed=completed,
        marker_completed_at=str(state.get("completed_at") or ""),
    )
    return state
