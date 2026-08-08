"""Deterministic adversarial-review workflow: the supervisor's ``run_review`` tool.

Replaces the model-driven review fan-out (``cluster_concepts`` → eval JS →
``Promise.all`` → supervisor-applied fixes) with ONE tool call that owns the
whole workflow deterministically:

1. **Clusters are computed HERE** (the same link-graph clustering behind
   ``cluster_concepts``) and persisted with stable ids
   (``.harvest/review/clusters.json``), so coverage is guaranteed by
   construction instead of by asking the model to paste a list faithfully.
2. **One READ-ONLY ``reviewer`` per cluster** verifies the docs against live
   data. Dispatch goes through the existing deepagents ``task`` tool (found on
   the calling runtime), so models, guards, ToolErrorMiddleware, and usage
   metering are exactly the ones the review pass always had.
3. **A cluster with findings pipelines straight into a ``fix-author``** whose
   guard is bound — via :func:`current_fix_allowlist`, a contextvar set only
   for the duration of that dispatch — to EXACTLY that cluster's doc paths.
   Parallel fixers cannot touch each other's files by construction, and the
   binding FAILS CLOSED: with no allowlist bound (e.g. a manual ``task()``
   dispatch of ``fix-author``), its guard refuses every write. Corrections
   that belong outside the cluster come back as ``PROPAGATION NOTES`` for the
   supervisor to apply serially (single writer — no race).
4. **Every dispatch is a fleet square** on the live feed: the tool emits the
   same custom-stream ``subagent`` events the QuickJS fan-out does (start
   with the full brief, complete with the final answer), in two batches —
   the review wave and the fix wave — so the drill-in works unchanged.
5. **The full transcript** lands in ``.harvest/review/report-<id>.md`` (a
   unique name per call, nothing is overwritten) and the tool returns a
   BOUNDED summary: per-cluster status, failed clusters with their uncovered
   docs, propagation notes, report path.
6. **Context-fidelity phase — after every cluster fix has completed.** When
   the run recorded context-extractor digests (``.harvest/context/`` — see
   :mod:`harvest.context_digests`), they are paired two-per-reviewer into
   ``x1..xN`` groups (persisted next to the clusters, so the same
   ``cluster_ids`` retry contract covers them) and one read-only
   ``context-reviewer`` per pair audits whether the BUNDLE fairly represents
   each extracted fact — semantic loss, dropped codes, weakened caveats, a
   fact that never landed. Findings pipe into a ``fix-author`` whose
   allowlist is every authored doc EXCEPT the supervisor-owned hubs. The
   phase runs in TWO stages: all pair reviewers in parallel (each reads the
   same settled, post-cluster-fix bundle), THEN the fixes strictly one at a
   time — unlike cluster fixers' disjoint allowlists, context fixers share
   one editable set, so interleaving them (or running them beside a still-
   reading reviewer) would race. Skipped entirely when no extractor ran.

Failure semantics: a cluster whose review or fix dispatch fails (raises,
times out, or returns no text) is recorded and the OTHER clusters proceed —
the tool itself never raises. The supervisor retries ONLY the failed clusters
with ``run_review(cluster_ids=[...])``, which reuses the persisted clustering
so ids stay stable across retries (deliberately: fixes may have changed the
link graph, but a retry must mean "the same cluster again").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from harvest.context_digests import digest_paths
from harvest.fsutil import write_text
from harvest.subagent_io import _result_text
from okf_core.paths import GUARDRAILS_CONCEPT_ID, is_reserved_rel_segments

# Module-level (not factory-local) because the tool function's `runtime`
# annotation must resolve against this module's globals when langchain builds
# the tool schema. Guarded so the module stays importable without langchain
# (the factory itself needs it and says so clearly).
try:  # pragma: no cover - exercised only when langchain is absent
    from langchain.tools import ToolRuntime
except Exception:  # noqa: BLE001
    ToolRuntime = Any  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

# Review state under the preserved .harvest/ dir (dot-prefixed: invisible to
# the downstream reader, never wiped by clean_authored_output).
_REVIEW_DIR = ".harvest/review"
_CLUSTERS_FILE = "clusters.json"

# Wall-clock cap per DISPATCH (one reviewer or one fixer). A hung sub-agent
# must not hang the whole tool call; on timeout the dispatch is cancelled and
# the cluster is recorded as failed (retryable via cluster_ids).
_DEFAULT_DISPATCH_TIMEOUT_S = 1800.0

# Docs per review cluster (env-overridable: OKF_HARVEST_REVIEW_CLUSTER_SIZE).
# 7, not the old 5: the supervisor-owned hubs are excluded from clustering
# (below), and they were what made larger clusters incoherent — without them a
# cluster is a table and its own spokes, which one reviewer can hold.
_DEFAULT_CLUSTER_SIZE = 7

# Extractor digests per context-fidelity reviewer (the "1 agent per 2 context
# outputs" pairing): a digest can be long (full enum legends), so two per
# reviewer keeps the audit thorough without drowning one agent in every
# digest at once.
_DIGESTS_PER_REVIEWER = 2

# Supervisor-owned docs, EXCLUDED from review clusters (they'd also poison the
# clustering: these hubs link to everything, so they seed clusters of
# unrelated spokes). DELIBERATE trade-off: the dataset overview docs
# (datasets/*) and the usage-guardrails contract get no direct adversarial
# review of their own. The compensation chain is: their content is DERIVED
# from table facts the clusters do review; the reviewer prompt tells
# reviewers a contradiction between a cluster doc and a linked outside doc
# (these hubs included) IS a reportable finding; the fixer can't write the
# hub (guard) so it becomes a propagation note; and the supervisor — their
# author/owner — applies it.
def _is_supervisor_owned(concept_id: str) -> bool:
    return concept_id == GUARDRAILS_CONCEPT_ID or concept_id.startswith("datasets/")

# Bounds on the model-facing tool RESULT (the full text always lands in the
# report file): per-note/per-error snippet caps and a cap on how many
# propagation notes ride the result (the rest are counted, never silent).
# Notes are generous (2000): the supervisor applies each one VERBATIM, so a
# truncated note is a wrong edit — and when one does exceed the cap, the
# truncation is marked explicitly so the supervisor knows to read the full
# text in the report file first.
_NOTE_CHARS = 2000
_MAX_NOTES = 40
_ERROR_CHARS = 300

# The per-dispatch write allowlist for `fix-author` (root-relative .md paths).
# Set by _dispatch() around a fixer's task-tool call — each cluster pipeline
# runs as its own asyncio task, so the var is isolated per dispatch. The
# fixer's guard reads it through current_fix_allowlist(); None (the default)
# means "no cluster bound" and the guard refuses every write (fail closed).
_FIX_ALLOWLIST: ContextVar[frozenset[str] | None] = ContextVar(
    "okf_fix_allowlist", default=None
)


def current_fix_allowlist() -> frozenset[str] | None:
    """The write allowlist bound to the current ``fix-author`` dispatch.

    Wired into the fixer's ``OKFGuardMiddleware(write_allowlist=...)`` by the
    agent builder. Returns None outside a ``run_review`` fixer dispatch.
    """
    return _FIX_ALLOWLIST.get()


def _snip(value: Any, cap: int = _ERROR_CHARS) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _describe_error(e: BaseException, timeout: float) -> str:
    """A failure string that is NEVER blank.

    ``str(asyncio.TimeoutError())`` is "" — snipping it alone left the
    failed-cluster entry with an empty reason (and the report then dropped
    the failure line entirely). Name the type, and spell out the one case we
    manufacture ourselves.
    """
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return f"dispatch timed out after {timeout:g}s"
    text = _snip(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


def _reviewer_is_clean(text: str) -> bool:
    """True when the reviewer's verdict line says the cluster checked out.

    The reviewer prompt requires the FIRST line of the reply to be ``CLEAN``
    or ``FINDINGS``. The verdict must be the line's ONLY word (after markdown
    dressing) — a prefix match would classify a findings-bearing reply that
    opens "Cleanup required: ..." as clean and silently skip its fixer.
    Anything else is treated as findings: a fixer over a clean report costs
    one cheap dispatch; a skipped fixer over real findings loses the fix.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.strip("#*_`>:.! \t").upper() == "CLEAN"
    return False


def _propagation_notes(fixer_text: str) -> list[str]:
    """Extract the fixer's ``PROPAGATION NOTES`` section (list items only).

    The fixer prompt requires the section LAST in the reply, one ``- `` item
    per out-of-cluster correction (or ``- none``). The section opener must be
    a HEADING-like line that IS the section title (markdown dressing aside) —
    matching any line merely containing the word "propagation" harvested
    unrelated summary bullets when the fixer mentioned the section in prose
    first. When several lines qualify, the LAST one wins (the prompt puts the
    section last). If the model deviates entirely, the notes are still in the
    report file verbatim.
    """
    lines = fixer_text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip().strip("#*_`>: \t").upper() == "PROPAGATION NOTES":
            start = i
    if start is None:
        return []
    notes: list[str] = []
    open_note = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            item = stripped[2:].strip()
            open_note = bool(item) and item.lower() not in ("none", "none.", "n/a")
            if open_note:
                notes.append(item)
        elif stripped and open_note and not stripped.startswith("#"):
            # A wrapped bullet: the prompt invites multi-line "exact change
            # needed" text, and the supervisor applies notes VERBATIM — a
            # note silently cut at its first physical line becomes a wrong
            # edit. Fold continuation lines into the open note.
            notes[-1] += f" {stripped}"
        elif not stripped:
            # A blank line ends the bullet (markdown paragraph break) — a
            # trailing sign-off after the list must not glue onto a note.
            open_note = False
    return notes


def _find_task_tool(tools: Any) -> Any:
    """The deepagents ``task`` tool off the calling runtime's tool list."""
    for tool in tools:
        if getattr(tool, "name", None) == "task":
            return tool
    return None


def _emit(writer: Any, event: dict[str, Any]) -> None:
    """Best-effort custom-stream emission (a lost square never breaks a review)."""
    if writer is None:
        return
    try:
        writer(event)
    except Exception:  # noqa: BLE001 — observability must not break the pass
        log.debug("Failed to emit review fleet event", exc_info=True)


def _load_clusters(
    root: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]]] | None:
    """``(clusters, context_groups)`` from the persisted state, or None.

    ``context_groups`` (``x1..xN`` → digest file paths) rides the same file as
    the ``c*`` clusters so ONE retry contract (``cluster_ids``) covers both;
    state written before the context phase existed simply loads with no
    groups.
    """
    path = root / _REVIEW_DIR / _CLUSTERS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        clusters = {c["id"]: list(c["docs"]) for c in data["clusters"]}
        context = {c["id"]: list(c["docs"]) for c in data.get("context", [])}
        return clusters, context
    except Exception:  # noqa: BLE001 — missing/corrupt = no prior clustering
        return None


def _persist_clusters(
    root: Path,
    clusters: dict[str, list[str]],
    context: dict[str, list[str]],
) -> None:
    payload: dict[str, Any] = {
        "clusters": [{"id": cid, "docs": docs} for cid, docs in clusters.items()]
    }
    if context:
        payload["context"] = [
            {"id": xid, "docs": docs} for xid, docs in context.items()
        ]
    write_text(root / _REVIEW_DIR / _CLUSTERS_FILE, json.dumps(payload, indent=1))


def _fixable_docs(root: Path) -> frozenset[str]:
    """Every authored doc a CONTEXT fixer may edit (root-relative ``.md`` paths).

    The whole bundle minus generated files, reserved dirs, and the
    supervisor-owned hubs — a context finding can land anywhere the digest
    routed a fact, so the cluster fixers' per-cluster confinement doesn't fit;
    hub corrections still travel as propagation notes (same rule as always).
    Enumerated fresh per dispatch: docs created since the clustering are
    editable too.
    """
    out: set[str] = set()
    if root.is_dir():
        for path in root.rglob("*.md"):
            rel = path.relative_to(root)
            if path.name in ("index.md", "log.md"):
                continue
            if is_reserved_rel_segments(rel.parts):
                continue
            rel_posix = rel.as_posix()
            if _is_supervisor_owned(rel_posix[: -len(".md")]):
                continue
            out.add(rel_posix)
    return frozenset(out)


def _write_report(
    root: Path, review_id: str, entries: list[dict[str, Any]]
) -> str:
    """Dump the full per-cluster transcript; returns the root-relative path."""
    lines = [f"# Review report `{review_id}`", ""]
    for e in entries:
        is_context = e.get("kind") == "context"
        title = "Context pair" if is_context else "Cluster"
        lines.append(f"## {title} `{e['cluster']}` — {e['status'].upper()}")
        lines.append(f"{'Digests' if is_context else 'Docs'}: {', '.join(e['docs'])}")
        # Keyed on the STATUS, not the error text: a failure whose reason
        # came back blank must still print its line and the NOT-covered
        # warning (the old `if e.get("error")` guard dropped both).
        if e.get("status") == "failed":
            reason = e.get("error") or "no failure reason captured"
            lines.append(f"Failed during **{e.get('stage', '?')}**: {reason}")
            lines.append("These docs were NOT covered by this call.")
        if e.get("review"):
            lines.append("")
            lines.append("### Reviewer findings")
            lines.append(e["review"])
        if e.get("fix"):
            lines.append("")
            lines.append("### Fixer report")
            lines.append(e["fix"])
        lines.append("")
    rel = f"{_REVIEW_DIR}/report-{review_id}.md"
    write_text(root / rel, "\n".join(lines))
    return rel


def make_run_review_tool(
    *,
    link_graph: Any,
    dataset_root: Path,
    concurrency: int,
    max_cluster_size: int | None = None,
    timeout_s: float | None = None,
) -> Any:
    """Build the supervisor's ``run_review`` tool (full harvests only).

    ``link_graph`` provides the deterministic clustering; ``concurrency``
    bounds in-flight cluster pipelines (one dispatch at a time per cluster,
    so it is the same "concurrent sub-agents" knob the QuickJS fan-out had).
    The reviewer/fixer dispatches ride the deepagents ``task`` tool found on
    the calling runtime, so this factory needs no models or guards of its own.
    """
    from langchain_core.tools import tool

    root = Path(dataset_root)
    timeout = timeout_s
    if timeout is None:
        try:
            timeout = float(
                os.environ.get(
                    "OKF_HARVEST_REVIEW_DISPATCH_TIMEOUT_S",
                    _DEFAULT_DISPATCH_TIMEOUT_S,
                )
            )
        except ValueError:
            timeout = _DEFAULT_DISPATCH_TIMEOUT_S
    cluster_size = max_cluster_size
    if cluster_size is None:
        try:
            cluster_size = int(
                os.environ.get(
                    "OKF_HARVEST_REVIEW_CLUSTER_SIZE", _DEFAULT_CLUSTER_SIZE
                )
            )
        except ValueError:
            cluster_size = _DEFAULT_CLUSTER_SIZE
    cluster_size = max(1, cluster_size)

    async def _review_workflow(
        cluster_ids: list[str] | None, runtime: Any
    ) -> dict[str, Any]:
        writer = getattr(runtime, "stream_writer", None)
        task_tool = _find_task_tool(getattr(runtime, "tools", ()) or ())
        if task_tool is None:
            return {
                "ok": False,
                "error": "internal: the sub-agent dispatch tool is unavailable "
                "on this runtime; report this failure in your summary.",
            }

        # Fresh call: recompute + persist the clustering (and pair up any
        # recorded context digests). Retry call: reuse the persisted state so
        # the ids the supervisor quotes stay meaningful — c* and x* alike.
        # `is not None`, NOT truthiness: an empty list is a natural literalism
        # when the result's `failed` list is empty, and falling into the
        # fresh-call branch would re-cluster and re-dispatch the WHOLE review.
        if cluster_ids is not None and len(cluster_ids) == 0:
            return {
                "ok": True,
                "note": "cluster_ids was an empty list — nothing to retry, "
                "nothing was run. Omit the argument entirely to start a fresh "
                "full review.",
            }
        if cluster_ids is not None:
            state = _load_clusters(root)
            if state is None:
                return {
                    "ok": False,
                    "error": "cluster_ids given but no prior clustering exists — "
                    "call run_review with no arguments first.",
                }
            clusters, context_groups = state
            known = {**clusters, **context_groups}
            unknown = [c for c in cluster_ids if c not in known]
            if unknown:
                return {
                    "ok": False,
                    "error": f"unknown cluster ids {unknown}; "
                    f"known ids: {sorted(known)}",
                }
            wanted = dict.fromkeys(cluster_ids)
            targets = {cid: clusters[cid] for cid in wanted if cid in clusters}
            context_targets = {
                cid: context_groups[cid] for cid in wanted if cid in context_groups
            }
            persist_error = None  # retries never rewrite the state file
        else:
            raw = link_graph.cluster(
                max_size=cluster_size, exclude=_is_supervisor_owned
            )
            clusters = {f"c{i + 1}": list(ids) for i, ids in enumerate(raw)}
            # The context-fidelity phase's inputs: every extractor digest the
            # run recorded, paired _DIGESTS_PER_REVIEWER per reviewer. No
            # digests = no extractor ran = the phase is skipped.
            digests = digest_paths(root)
            context_targets = {
                f"x{i + 1}": digests[
                    i * _DIGESTS_PER_REVIEWER : (i + 1) * _DIGESTS_PER_REVIEWER
                ]
                for i in range(
                    (len(digests) + _DIGESTS_PER_REVIEWER - 1)
                    // _DIGESTS_PER_REVIEWER
                )
            }
            # Persist failure degrades (retries won't resolve ids) but must
            # not abort the review itself — the never-raises contract.
            try:
                _persist_clusters(root, clusters, context_targets)
                persist_error = None
            except Exception as e:  # noqa: BLE001 — reviews outrank the state file
                persist_error = _describe_error(e, _DEFAULT_DISPATCH_TIMEOUT_S)
                log.warning(
                    "run_review could not persist its clustering: %s", persist_error
                )
            targets = clusters

        review_id = uuid.uuid4().hex[:8]
        call_id = getattr(runtime, "tool_call_id", None) or f"review-{review_id}"
        review_batch = f"{call_id}:review"
        fix_batch = f"{call_id}:fix"
        context_batch = f"{call_id}:context"
        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _dispatch(
            *,
            sub_id: str,
            batch: str,
            subagent_type: str,
            label: str,
            brief: str,
            allowlist: frozenset[str] | None,
        ) -> str:
            """One sub-agent dispatch: fleet events + timeout + allowlist binding.

            Runs inside the cluster's own asyncio task, so setting the fixer
            allowlist contextvar here scopes it to exactly this dispatch.
            """
            _emit(
                writer,
                {
                    "type": "subagent",
                    "phase": "start",
                    "id": sub_id,
                    "batch": batch,
                    "subagent_type": subagent_type,
                    "label": label,
                    "description": brief,
                },
            )
            token = _FIX_ALLOWLIST.set(allowlist)
            try:
                from dataclasses import replace

                dispatch_runtime = replace(runtime, tool_call_id=sub_id)
                result = await asyncio.wait_for(
                    task_tool.arun(
                        {
                            "description": brief,
                            "subagent_type": subagent_type,
                            "runtime": dispatch_runtime,
                        }
                    ),
                    timeout,
                )
            except Exception as e:
                _emit(
                    writer,
                    {
                        "type": "subagent",
                        "phase": "error",
                        "id": sub_id,
                        "batch": batch,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
                raise
            finally:
                _FIX_ALLOWLIST.reset(token)
            text = _result_text(result)
            if not text.strip():
                # An empty answer IS a failure (the caller records the entry
                # as failed) — emit the error square, not a green `complete`:
                # a done square for a cluster the result reports failed would
                # contradict the feed.
                _emit(
                    writer,
                    {
                        "type": "subagent",
                        "phase": "error",
                        "id": sub_id,
                        "batch": batch,
                        "error": f"{subagent_type} returned no text",
                    },
                )
                raise RuntimeError(f"{subagent_type} returned no text")
            _emit(
                writer,
                {
                    "type": "subagent",
                    "phase": "complete",
                    "id": sub_id,
                    "batch": batch,
                    "result": text,
                },
            )
            return text

        async def _run_cluster(cid: str, ids: list[str]) -> dict[str, Any]:
            entry: dict[str, Any] = {"cluster": cid, "docs": list(ids)}
            docs = ", ".join(ids)
            async with sem:
                try:
                    review_text = await _dispatch(
                        sub_id=f"rev-{cid}-{review_id}",
                        batch=review_batch,
                        subagent_type="reviewer",
                        label=f"{cid} · {len(ids)} docs",
                        brief=(
                            "Adversarially verify these related docs against "
                            f"live data: {docs}"
                        ),
                        allowlist=None,
                    )
                except Exception as e:  # noqa: BLE001 — recorded, never propagated
                    entry.update(
                        status="failed",
                        stage="review",
                        error=_describe_error(e, timeout),
                    )
                    return entry
                entry["review"] = review_text
                if _reviewer_is_clean(review_text):
                    entry["status"] = "clean"
                    return entry
                try:
                    fix_text = await _dispatch(
                        sub_id=f"fix-{cid}-{review_id}",
                        batch=fix_batch,
                        subagent_type="fix-author",
                        label=f"{cid} · fix",
                        brief=(
                            "Apply the reviewer's confirmed findings to these "
                            f"docs — your cluster, the ONLY files you may edit: "
                            f"{docs}\n\nReviewer findings:\n{review_text}"
                        ),
                        allowlist=frozenset(f"{i}.md" for i in ids),
                    )
                except Exception as e:  # noqa: BLE001 — recorded, never propagated
                    entry.update(
                        status="failed",
                        stage="fix",
                        error=_describe_error(e, timeout),
                    )
                    return entry
                entry["fix"] = fix_text
                entry["notes"] = _propagation_notes(fix_text)
                entry["status"] = "fixed"
                return entry

        async def _review_pair(xid: str, digests: list[str]) -> dict[str, Any]:
            """Stage 1 of a digest pair: the fidelity review (read-only).
            Returns the entry; a missing `status` means findings await a fix."""
            entry: dict[str, Any] = {
                "cluster": xid,
                "docs": list(digests),
                "kind": "context",
            }
            paths = ", ".join(digests)
            async with sem:
                try:
                    review_text = await _dispatch(
                        sub_id=f"ctx-{xid}-{review_id}",
                        batch=context_batch,
                        subagent_type="context-reviewer",
                        label=f"{xid} · {len(digests)} digest(s)",
                        brief=(
                            "Audit how faithfully the bundle represents the "
                            "extracted context facts. Read these digest files "
                            f"IN FULL first: {paths}"
                        ),
                        allowlist=None,
                    )
                except Exception as e:  # noqa: BLE001 — recorded, never propagated
                    entry.update(
                        status="failed",
                        stage="context-review",
                        error=_describe_error(e, timeout),
                    )
                    return entry
            entry["review"] = review_text
            if _reviewer_is_clean(review_text):
                entry["status"] = "clean"
            return entry

        async def _fix_pair(entry: dict[str, Any]) -> None:
            """Stage 2: apply one pair's findings. Called SEQUENTIALLY (see
            below) — context fixers share one bundle-wide editable set, so
            unlike the disjoint cluster fixers they may not run in parallel."""
            xid = entry["cluster"]
            try:
                async with sem:
                    fix_text = await _dispatch(
                        sub_id=f"ctxfix-{xid}-{review_id}",
                        batch=context_batch,
                        subagent_type="fix-author",
                        label=f"{xid} · fix",
                        brief=(
                            "Apply the context-fidelity reviewer's "
                            "confirmed findings. Your editable file set is "
                            "EVERY authored bundle doc EXCEPT the dataset "
                            "overview docs (datasets/*) and "
                            "references/usage_guardrails.md — corrections "
                            "for those two go under PROPAGATION NOTES. "
                            "Edit ONLY the docs the findings name.\n\n"
                            f"Reviewer findings:\n{entry['review']}"
                        ),
                        allowlist=_fixable_docs(root),
                    )
            except Exception as e:  # noqa: BLE001 — recorded, never propagated
                entry.update(
                    status="failed",
                    stage="context-fix",
                    error=_describe_error(e, timeout),
                )
                return
            entry["fix"] = fix_text
            entry["notes"] = _propagation_notes(fix_text)
            entry["status"] = "fixed"

        entries = list(
            await asyncio.gather(*(_run_cluster(c, ids) for c, ids in targets.items()))
        )
        # The context-fidelity phase starts strictly AFTER every cluster fix
        # has landed (the gather above is the barrier), and runs in TWO
        # stages: every pair reviewer in parallel first — they all read the
        # same settled, post-cluster-fix bundle — then the fixes strictly one
        # at a time. Interleaving them (the old per-pair pipeline) let pair
        # A's fixer edit a doc pair B's reviewer was mid-reading (read skew),
        # and a fix waiting on a shared lock while holding its semaphore slot
        # could starve later reviews outright.
        context_entries = list(
            await asyncio.gather(
                *(_review_pair(x, d) for x, d in context_targets.items())
            )
        )
        for entry in context_entries:
            if "status" not in entry:
                await _fix_pair(entry)
        all_entries = entries + context_entries
        # One persistent write error here must not discard 30+ minutes of
        # completed dispatches (the module contract says the tool never
        # raises): degrade to a result without a report file.
        try:
            report_path = _write_report(root, review_id, all_entries)
            report_error = None
        except Exception as e:  # noqa: BLE001 — results outrank the transcript
            report_path = None
            report_error = _describe_error(e, timeout)
            log.warning("run_review could not write its report file: %s", report_error)

        failed = [
            {
                "cluster": e["cluster"],
                "docs": e["docs"],
                "stage": e.get("stage", "?"),
                "error": e.get("error", ""),
            }
            for e in all_entries
            if e["status"] == "failed"
        ]
        # A truncated note must SAY so (the supervisor applies notes
        # verbatim; a silent mid-sentence cut becomes a wrong edit).
        def _note_entry(cluster: str, note: str) -> dict[str, Any]:
            entry = {"cluster": cluster, "note": _snip(note, _NOTE_CHARS)}
            if len(" ".join(note.split())) > _NOTE_CHARS:
                entry["truncated"] = True
                where = (
                    f"read the full note in {report_path} before applying"
                    if report_path
                    else "the report file could not be written, so the full "
                    "text is unavailable — re-run this cluster id instead of "
                    "applying an incomplete note"
                )
                entry["note"] += f" [TRUNCATED — {where}]"
            return entry

        notes = [
            _note_entry(e["cluster"], n)
            for e in all_entries
            for n in e.get("notes", [])
        ]
        hidden_notes = max(0, len(notes) - _MAX_NOTES)
        result: dict[str, Any] = {
            "ok": not failed,
            "review_id": review_id,
            "clusters": len(targets),
            "docs": sum(len(ids) for ids in targets.values()),
            "clean": [e["cluster"] for e in entries if e["status"] == "clean"],
            "fixed": [e["cluster"] for e in entries if e["status"] == "fixed"],
            "failed": failed,
            "propagation_notes": notes[:_MAX_NOTES],
            "report_path": report_path,
        }
        if report_error:
            result["report_error"] = (
                f"the report file could not be written ({report_error}); the "
                "statuses and notes above are complete — do not re-run for "
                "the report's sake."
            )
        if persist_error:
            result["persist_error"] = (
                f"the clustering could not be persisted ({persist_error}) — "
                "cluster_ids retries will NOT resolve for this run; if a "
                "retry is needed, call run_review with no arguments."
            )
        if context_targets:
            result["context"] = {
                "pairs": len(context_targets),
                "clean": [
                    e["cluster"] for e in context_entries if e["status"] == "clean"
                ],
                "fixed": [
                    e["cluster"] for e in context_entries if e["status"] == "fixed"
                ],
            }
        elif not cluster_ids:
            result["context"] = {
                "skipped": "no context-extractor ran this harvest — nothing to audit"
            }
        if hidden_notes:
            result["hidden_propagation_notes"] = hidden_notes
            result["note"] = (
                f"{hidden_notes} propagation note(s) beyond the first "
                f"{_MAX_NOTES} are in the report file only."
            )
        if failed:
            result["retry_hint"] = (
                "Re-run ONLY the failed clusters: call run_review with "
                f"cluster_ids={[f['cluster'] for f in failed]}."
            )
        return result

    @tool
    def run_review(
        cluster_ids: list[str] | None = None,
        runtime: ToolRuntime = None,  # noqa: RUF013 — injected, hidden from the model
    ) -> dict[str, Any]:
        """Run the whole adversarial review + fix pass as one deterministic workflow.

        With NO arguments: clusters every non-reserved doc by link relations
        (full coverage; small clusters), EXCEPT the docs YOU own — the dataset
        overview docs (datasets/*) and references/usage_guardrails, which are
        never clustered or fixer-editable: issues with them come back as
        propagation notes. Dispatches one read-only `reviewer` sub-agent per
        cluster against live data IN PARALLEL, and pipes each cluster's
        confirmed findings into a `fix-author` sub-agent that may edit ONLY
        that cluster's files. Returns a bounded summary: per-cluster status
        (clean / fixed / failed), propagation notes YOU must apply (fixes for
        docs outside the finding's cluster, including your overview/guardrails
        docs), and the report file path with every reviewer/fixer transcript.

        When context-extractors ran this harvest, a CONTEXT-FIDELITY phase
        follows automatically after all cluster fixes: their recorded digests
        are audited (`x1..xN` ids in the result's `context` section) against
        the bundle for semantic loss — facts dropped, weakened, or
        mis-routed — and confirmed losses are fixed the same way. You do NOT
        dispatch anything for this; it is skipped when no extractor ran.

        If the result lists failed ids (clusters or `x*` context pairs), call
        this again with `cluster_ids` set to EXACTLY those ids — only they are
        re-run, on the same persisted state. Do not review or fix docs
        yourself beyond applying the returned propagation notes.
        """
        # SYNC tool, deliberately: the harvest drives the agent through the
        # sync `agent.stream(...)`, where an async-only StructuredTool raises
        # NotImplementedError (live incident — the model retried twice and
        # gave up). The orchestration itself is async (parallel dispatches),
        # so it runs on ONE persistent background event loop shared by every
        # run_review call in the process — NOT a fresh asyncio.run per call:
        # the dispatches ride long-lived model clients whose async connection
        # pools bind to the first loop they run on, so a per-call loop leaves
        # the documented cluster_ids RETRY call with pools bound to a closed
        # loop ("Event loop is closed" on the very call meant to recover a
        # failure). Blocking on the future is safe from the sync tool path
        # (this thread hosts no loop; under an async driver ToolNode runs
        # sync tools in an executor thread).
        # Last-resort catch: the module contract is "the tool itself never
        # raises" — anything escaping the workflow (a clustering crash, a
        # loop failure) becomes a reportable error, not a lost tool call.
        try:
            return asyncio.run_coroutine_threadsafe(
                _review_workflow(cluster_ids, runtime), _review_loop()
            ).result()
        except Exception as e:  # noqa: BLE001 — report, never raise
            log.exception("run_review workflow crashed")
            return {
                "ok": False,
                "error": f"run_review crashed: {type(e).__name__}: "
                f"{_snip(e)} — report this failure in your summary.",
            }

    return run_review


# One background event loop for ALL run_review calls in this process (see the
# comment at the call site). Created lazily; the daemon thread dies with the
# process — a harvest process runs one supervisor, so this is one thread.
_LOOP_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None


def _review_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever, name="okf-review-loop", daemon=True
            ).start()
            _LOOP = loop
        return _LOOP
