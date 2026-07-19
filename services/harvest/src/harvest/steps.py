"""Human-readable harvest step feed — a LangChain callback that narrates a run.

The harvest agent runs blocking (``runner.py`` calls ``built.agent.invoke``), so
the only way to observe what it's doing WHILE it runs is to hook the LangChain
callback surface. This module provides:

* :func:`shape_step` — a pure, table-driven mapper from a raw tool name + args to
  a short human label ("Reading ``tables/races``", "Running SQL", "Started
  ``table-author`` for ``tables/races``"). It's pure so it's unit-testable with
  no framework installed, and it owns the ONE place tool-name → phrasing lives.
* :class:`StepEmitter` — a ``BaseCallbackHandler`` that turns the agent's
  intermediate messages into step events and hands each to a sink. Passed via
  ``config={"callbacks": [emitter]}`` on the agent call, it ALSO observes every
  sub-agent's steps for free: LangGraph seeds each sub-agent run from the ambient
  parent config, so callbacks propagate down without re-attaching per sub-agent
  (this is why we use a callback rather than middleware — sub-agent middleware
  REPLACES rather than inherits; see ``agent.py`` / CLAUDE.md footgun).

Design constraints (from the investigation):

* **Status, not content.** We emit tool NAMES shaped into labels and tool-call
  success/failure — never tool response bodies (they run to ~60KB) and only a
  short summary of AIMessage text. Keeps the event payload tiny.
* **Best-effort.** Like ``report_status``, an emission failure must NEVER break a
  harvest — the sink is wrapped so any exception is swallowed + logged.
* **Tool failure is a message field, not an exception.** The agent's ToolNode
  catches tool errors and returns a ``ToolMessage(status="error")`` rather than
  raising, so ``on_tool_end`` (not ``on_tool_error``) fires for a failed tool and
  we read ``ToolMessage.status`` / the output to classify ok vs error.

Framework imports are deferred (mirrors ``okf_guard.py``) so this module imports
cleanly for unit tests without langchain installed.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

# The Control API greps the harvest runtime's CloudWatch log group for this exact
# token to reconstruct a run's feed, so it is a FROZEN contract (mirror it in
# control_api and docs/CONVENTIONS.md). Each step is one line: ``OKF_STEP <json>``.
STEP_MARKER = "OKF_STEP"

# A dedicated logger for step lines so they're formatted as raw JSON we control,
# independent of the root logging.basicConfig format — AgentCore ships stdout to
# /aws/bedrock-agentcore/runtimes/*, which is where the Control API reads them.
_step_log = logging.getLogger("okf.harvest.steps")

try:  # langchain is only present in the runtime image
    from langchain_core.callbacks import BaseCallbackHandler

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - exercised only when langchain is absent
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _HAVE_LANGCHAIN = False


# The event kinds the UI knows how to render. Kept small and stable — the UI's
# icon/badge map keys off these.
KIND_AGENT = "agent"  # an AIMessage (the model said/decided something)
KIND_TOOL_CALL = "tool_call"  # the agent invoked a tool (shaped into a label)
KIND_TOOL_RESULT = "tool_result"  # a tool returned; carries ok=True/False only
# A sub-agent fan-out lifecycle event (the "fleet squares"). Carries a `phase`
# (start|complete|error), a `batch` (the top-level `eval` tool-call id that groups
# one fan-out wave) and a per-dispatch `id`. Sourced from langchain_quickjs's
# custom stream — the
# UI grows a row of squares as sub-agents actually start (there is no reliable
# pre-start "planned" count: the model builds the fan-out list dynamically).
KIND_SUBAGENT = "subagent"
# A running token-usage snapshot. Carries a `usage` object with the CUMULATIVE
# counts across every model turn so far — the whole run, INCLUDING sub-agent
# turns (which dominate the spend but emit no feed row). Emitted on each model
# turn that reports usage; the UI shows the latest snapshot as a running total.
# It renders no feed row (it's a metric, not a step); the fields mirror
# LangChain's normalized `usage_metadata` (same names sparky's stream uses):
# {input, output, cache_read, cache_write, total}. `cache_write` is LangChain's
# `cache_creation` (Anthropic prompt-cache WRITE); `cache_read` is a cache HIT.
KIND_USAGE = "usage"
# Recursive-improvement benchmark events. KIND_BENCHMARK_PROGRESS is a live
# in-round update (phase + an N/M counter) the UI renders as a progress row that
# updates in place, keyed by (iteration, phase). KIND_BENCHMARK is the per-round
# KPI summary emitted when a round finishes. Both carry benchmark-specific fields
# (phase/iteration/current/total + the KPI fields) that the Control API
# _parse_step_line must whitelist to reach the UI.
KIND_BENCHMARK = "benchmark"
KIND_BENCHMARK_PROGRESS = "benchmark_progress"

# Benchmark phases (the label the progress row shows).
BENCH_PHASE_SOLVE = "solving"
BENCH_PHASE_GRADE = "grading"
BENCH_PHASE_ADJUDICATE = "reviewing"
BENCH_PHASE_DONE = "done"

# Sub-agent lifecycle phases (mirror langchain_quickjs SubagentStreamEvent).
PHASE_START = "start"  # dispatched and running
PHASE_COMPLETE = "complete"
PHASE_ERROR = "error"

# Cap the AIMessage one-line summary (the feed `label`). A short teaser only.
_AGENT_SUMMARY_MAX = 200

# Cap the FULL agent text carried alongside the summary (the `full` field the UI
# renders as markdown in a modal when the row is expanded). Bounded so one log
# line can't blow up the CloudWatch event size, but generous enough for a normal
# authoring/decision message. Structure (newlines, lists) is PRESERVED here —
# unlike `label`, which collapses whitespace for the one-liner.
_AGENT_FULL_MAX = 8000


def _first_arg(args: dict[str, Any], *keys: str) -> str | None:
    """Return the first present, non-empty string among ``keys`` in ``args``."""
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _basename(path: str) -> str:
    """A short, readable tail of a virtual file path (last 1-2 segments)."""
    parts = [p for p in str(path).lstrip("/").split("/") if p]
    if len(parts) <= 2:
        return "/".join(parts) or path
    return "/".join(parts[-2:])


def shape_step(tool_name: str, args: dict[str, Any] | None) -> dict[str, str]:
    """Map a raw ``(tool_name, args)`` to a human step label.

    Returns ``{"tool": <raw name>, "label": <human phrase>}``. Pure and total: an
    unknown tool degrades to a title-cased name so a newly-added tool still shows
    something sensible instead of nothing. The label set mirrors the harvest
    agent's ACTUAL tools (deepagents filesystem built-ins + source/graph/run_code
    tools + the ``task`` sub-agent dispatcher) — see agent.py.
    """
    args = args or {}
    name = tool_name or ""

    # deepagents filesystem built-ins (arg key: file_path; ls/glob/grep use path).
    if name == "ls":
        p = _first_arg(args, "path", "file_path")
        # deepagents' ls takes an absolute `path`; "/" is the dataset root.
        if p and p != "/":
            return {"tool": name, "label": f"Listing {_basename(p)}"}
        return {"tool": name, "label": "Listing files"}
    if name == "read_file":
        fp = _first_arg(args, "file_path", "path")
        return {
            "tool": name,
            "label": f"Reading {_basename(fp)}" if fp else "Reading a file",
        }
    if name == "write_file":
        fp = _first_arg(args, "file_path", "path")
        return {
            "tool": name,
            "label": f"Writing {_basename(fp)}" if fp else "Writing a file",
        }
    if name == "edit_file":
        fp = _first_arg(args, "file_path", "path")
        return {
            "tool": name,
            "label": f"Editing {_basename(fp)}" if fp else "Editing a file",
        }
    if name in ("glob", "grep"):
        pat = _first_arg(args, "pattern", "query")
        return {
            "tool": name,
            "label": f"Searching for {pat}" if pat else "Searching files",
        }
    if name == "write_todos":
        return {"tool": name, "label": "Planning the work"}

    # Live source tools (source_tools.py; arg key: concept_id / query). Static
    # metadata is now read from .metadata/ via read_file/grep (labeled above).
    if name == "sample_rows":
        cid = _first_arg(args, "concept_id")
        return {
            "tool": name,
            "label": f"Sampling rows from {cid}" if cid else "Sampling table rows",
        }
    if name == "run_sql":
        return {"tool": name, "label": "Running an Athena query"}

    # LinkGraph tools (graph_tools.py).
    if name in ("get_backlinks", "get_links"):
        cid = _first_arg(args, "concept_id")
        return {
            "tool": name,
            "label": f"Checking links for {cid}" if cid else "Checking doc links",
        }

    # Code sandbox (code_interpreter.py).
    if name == "run_code":
        return {"tool": name, "label": "Running code in the sandbox"}

    # Sub-agent dispatch (deepagents task tool; args: subagent_type, description).
    if name == "task":
        sub = _first_arg(args, "subagent_type") or "sub-agent"
        target = _first_arg(args, "description")
        # The description is a full instruction; show a short lead-in only.
        if target:
            lead = target.split("\n", 1)[0][:60]
            return {"tool": name, "label": f"Started {sub}: {lead}"}
        return {"tool": name, "label": f"Started {sub}"}

    # Unknown tool: readable fallback so nothing is silently dropped.
    return {
        "tool": name,
        "label": name.replace("_", " ").strip().capitalize() or "Working",
    }


def shape_subagent_label(subagent_type: str | None, label: str | None) -> str:
    """A short row label for a fleet square, from the event's type + label."""
    sub = (subagent_type or "sub-agent").strip()
    lbl = (label or "").strip()
    if lbl:
        return f"{sub}: {lbl}"[:80]
    return sub


def _summarize_ai_text(text: str) -> str:
    """One short line summarizing an AIMessage (no full content is streamed)."""
    line = " ".join(str(text).split())  # collapse whitespace/newlines
    if len(line) > _AGENT_SUMMARY_MAX:
        return line[: _AGENT_SUMMARY_MAX - 1].rstrip() + "…"
    return line


def _extract_ai_text(message: Any) -> str:
    """Best-effort plain text of an AIMessage across content shapes.

    LangChain content is either a string or a list of blocks (text/reasoning/
    tool_use). We keep only text blocks and drop reasoning/tool_use so the
    summary is the model's actual prose, not its chain-of-thought.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _message_of(response: Any) -> Any:
    """Best-effort ``response.generations[0][0].message`` off an LLMResult."""
    try:
        gens = getattr(response, "generations", None)
        if gens and gens[0]:
            return getattr(gens[0][0], "message", None)
    except Exception:  # noqa: BLE001
        pass
    return None


def _usage_from_message(message: Any) -> dict[str, int] | None:
    """Pull the per-turn token counts off an AIMessage's ``usage_metadata``.

    LangChain normalizes every provider's usage onto ``usage_metadata`` (a dict
    on the message): ``input_tokens`` / ``output_tokens`` plus an optional
    ``input_token_details`` carrying Anthropic's prompt-cache split
    (``cache_read`` = a cache HIT, ``cache_creation`` = a cache WRITE). This is
    the same shape sparky's stream reads. Returns a per-turn delta dict with our
    field names (``input``/``output``/``cache_read``/``cache_write``), or None if
    the turn reported no usage (some providers omit it on streamed chunks).

    Pure/total: any missing key defaults to 0, and a non-int value is coerced
    away so one odd turn can't corrupt the cumulative total.
    """
    um = getattr(message, "usage_metadata", None)
    if not isinstance(um, dict):
        return None
    details = um.get("input_token_details")
    details = details if isinstance(details, dict) else {}

    def _int(v: Any) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    delta = {
        "input": _int(um.get("input_tokens")),
        "output": _int(um.get("output_tokens")),
        "cache_read": _int(details.get("cache_read")),
        "cache_write": _int(details.get("cache_creation")),
    }
    if not any(delta.values()):
        return None
    return delta


class StepEmitter(BaseCallbackHandler):  # type: ignore[misc]
    """Turn a harvest run's intermediate messages into human step events.

    ``sink(event: dict)`` receives each event; the caller supplies a sink that
    persists it (e.g. a structured log line the Control API reads back). The
    sink is called defensively — any exception is swallowed so observation never
    breaks the crawl. ``agent_label`` distinguishes the main supervisor from a
    sub-agent (the parent stamps ``ls_agent_type=subagent`` into the sub-agent's
    config, surfaced here via ``metadata``).

    Events are ``{seq, kind, tool?, label, ok?, agent}`` — seq is a monotonic
    per-emitter counter so a consumer can page with ``?since=<seq>``; the wall
    time is added by the sink (which has the clock).
    """

    # BaseCallbackHandler opts a subclass into being invoked for nested runs.
    ignore_agent = False

    def __init__(self, sink: Callable[[dict[str, Any]], None]):
        super().__init__()
        self._sink = sink
        # seq is 1-based: a consumer polls with ``?since=<last seq>`` and the
        # "seen nothing yet" default is 0, so ``seq > since`` returns the first
        # event (seq 1). A 0-based seq would make the first event unreachable.
        self._seq = 1
        # Sub-agents fan out across threads (LangChain runs sync callbacks in a
        # thread-pool for async runs), so seq assignment must be atomic — a race
        # would mint duplicate seqs that the consumer's dedup then drops.
        self._lock = threading.Lock()
        # Track the call_ids we EMITTED a tool_call for, so a tool_result is only
        # emitted when its call was. LangChain only passes `metadata` to the START
        # callbacks (on_tool_start / on_chat_model_start) — the END callbacks
        # (on_tool_end / on_llm_end) receive an EMPTY metadata dict (verified
        # against the installed langchain_core). So the subagent discriminator can
        # only be evaluated at start; the end must be paired to it by run_id.
        # Pairing by call_id is authoritative: no emitted call => drop the result.
        self._emitted_calls: set[str] = set()
        # run_ids of MODEL turns that started inside a sub-agent (nested langgraph
        # namespace at on_chat_model_start). on_llm_end carries NO metadata, so it
        # can't re-classify itself — it looks the run_id up here. Same reason as
        # the tool pairing above. Bounded: only sub-agent runs are added (top-level
        # runs never are), and each is discarded when its turn ends/errors.
        self._subagent_llm_runs: set[str] = set()
        # Fleet-batch correlation. langchain_quickjs's per-event ``eval_id`` is a
        # REPL-LOCAL counter that resets to ``call_0`` on EVERY ``eval()`` call, so
        # it does NOT distinguish one fan-out from the next — every wave would share
        # ``call_0`` and the UI (which keys its fleet row by batch) would fold the
        # reviewer fan-out into the table-author row created at the first wave's
        # position. Instead we group by the TOP-LEVEL ``eval`` tool-call id, which
        # IS globally unique per fan-out (and is what CONVENTIONS documents ``batch``
        # to be). Top-level evals never overlap — a tool call blocks the agent turn
        # until it returns — so the most-recent eval call_id is the current wave.
        # ``_fleet_batch_of`` pins each sub-agent's batch at START so a late
        # complete/error still lands in the right row even if a new eval has since
        # begun. Guarded by the same lock (subagent events arrive on the drain loop;
        # eval tool_starts fire on the callback surface).
        self._current_eval_batch = ""
        self._fleet_batch_of: dict[str, str] = {}
        # Cumulative token usage across EVERY model turn in the run — top-level AND
        # sub-agent (sub-agents emit no feed row but dominate the spend, so they
        # must count). Guarded by the same lock as _seq since sub-agent turns end
        # on pool threads. Snapshotted into a KIND_USAGE event on each metered turn.
        self._usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    # -- emission ----------------------------------------------------------- #

    def _emit(self, event: dict[str, Any]) -> None:
        """Stamp a monotonic seq and hand the event to the sink (never raises)."""
        try:
            with self._lock:
                event["seq"] = self._seq
                self._seq += 1
            self._sink(event)
        except Exception:  # noqa: BLE001 - a feed emission must never break a harvest
            log.debug("step emit failed (continuing)", exc_info=True)

    @staticmethod
    def _is_subagent(metadata: dict[str, Any] | None) -> bool:
        """True iff this callback fired INSIDE a sub-agent's graph (not the
        top-level supervisor). Such events are the fan-out's internal model turns
        and tool calls — they FLOOD the feed, so the step feed drops them and the
        fleet squares (from the custom stream) represent sub-agents instead.

        Discriminator (verified empirically against the installed
        langchain/langgraph/deepagents — see tests/test_steps.py): the ONLY
        reliable signal is a NESTED langgraph checkpoint namespace. A sub-agent
        runs *under* the parent's ``tools`` node (whether dispatched via the
        static ``task`` tool or the QuickJS ``task()`` global — both go through the
        deepagents task tool), so its ``langgraph_checkpoint_ns`` is
        ``tools:<uuid>|<child-node>:<uuid>`` (note the ``|`` separating levels).
        A TOP-LEVEL node's namespace is a single ``node:uuid`` segment, no ``|``.

        CRITICAL — this can ONLY be evaluated on a START callback. LangChain
        passes ``metadata`` to ``on_chat_model_start`` / ``on_tool_start`` but
        NOT to ``on_llm_end`` / ``on_tool_end`` (they receive an EMPTY metadata
        dict). So the END hooks must pair back to the start's classification by
        ``run_id`` (``_subagent_llm_runs`` for model turns, ``_emitted_calls`` for
        tools) — calling ``_is_subagent`` on an end hook's metadata always returns
        False and would leak every sub-agent event. This asymmetry is exactly the
        bug that kept resurfacing.

        Do NOT also test ``checkpoint_ns`` non-empty: a top-level tool's
        ``on_tool_start`` legitimately carries a non-empty ``checkpoint_ns``
        (the ``tools`` node's own namespace, e.g. ``tools:<uuid>``), so that
        clause wrongly dropped every top-level tool CALL. ``ls_agent_type`` is
        also unusable — the deepagents task tool stamps it into the sub-agent's
        ``configurable`` (not ``metadata``), and langchain_core's ``ensure_config``
        only promotes ``model``/``checkpoint_ns`` from configurable to metadata,
        so it arrives None on the callback surface."""
        lg_ns = (metadata or {}).get("langgraph_checkpoint_ns")
        return isinstance(lg_ns, str) and "|" in lg_ns

    # -- LangChain callback hooks ------------------------------------------- #

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: Any,
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """A model turn began — classify top-level vs sub-agent NOW and remember
        it by ``run_id``. This is the ONLY point where ``metadata`` (carrying the
        nested langgraph namespace) is available for a model turn; the matching
        ``on_llm_end`` gets no metadata, so it pairs back to this record. We record
        ONLY sub-agent runs (the set stays small — one entry per in-flight
        sub-agent turn, cleared when it ends)."""
        self._note_model_start(run_id, kwargs.get("metadata"))

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: Any,
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        """Completion-model / fallback path. LangChain calls ``on_chat_model_start``
        for chat models (our ChatBedrockConverse) and ``on_llm_start`` for plain
        completion models; implementing both makes the sub-agent classification
        robust to either. Same body — record sub-agent runs by ``run_id``."""
        self._note_model_start(run_id, kwargs.get("metadata"))

    def _note_model_start(self, run_id: Any, metadata: dict[str, Any] | None) -> None:
        """Record (by run_id) that a model turn started inside a sub-agent, so the
        metadata-less ``on_llm_end`` can drop it."""
        if self._is_subagent(metadata):
            with self._lock:
                self._subagent_llm_runs.add(self._call_id(run_id))

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        """A model turn finished — meter its tokens, then emit an 'agent' step if
        it produced text.

        Emits both a short one-line ``label`` (for the feed row) and the FULL
        markdown ``full`` (whitespace/structure preserved, bounded) which the UI
        renders in a modal when the row is expanded. Skips empty/thinking-only
        turns so the feed shows decisions, not silent tool-planning turns.
        """
        # Pair to the classification made at on_chat_model_start via run_id, and
        # discard the record either way. on_llm_end carries NO metadata, so we
        # CANNOT re-check _is_subagent here (it would always say "top-level" and
        # leak every sub-agent turn — the bug that kept coming back).
        cid = self._call_id(run_id)
        with self._lock:
            was_subagent = cid in self._subagent_llm_runs
            self._subagent_llm_runs.discard(cid)
        # NOTE: token metering is NOT done here. This run-config callback fires
        # only for turns on the parent graph — QuickJS `task()` sub-agents run on
        # their own asyncio tasks and never reach it, so metering here would
        # UNDERCOUNT (exactly the bug: a flat total while sub-agents work). Usage
        # is metered on the shared MODEL instance instead (see UsageForwarder /
        # record_usage), which fires for every turn on every dispatch path.
        # Drop sub-agent-internal turns from the narrative feed — they flood it;
        # the fleet squares represent the fan-out instead. Only the top-level
        # supervisor narrates.
        if was_subagent:
            return
        message = _message_of(response)
        raw = _extract_ai_text(message) if message is not None else ""
        label = _summarize_ai_text(raw)
        if not label:
            return
        event: dict[str, Any] = {
            "kind": KIND_AGENT,
            "label": label,
        }
        # Carry the full text only when it adds something beyond the one-liner
        # (multi-line, or longer than the collapsed label) — so the UI knows when
        # to offer "expand". Preserve structure; only bound the size.
        full = raw.strip()
        if full and (len(full) > len(label) or "\n" in full):
            event["full"] = full[:_AGENT_FULL_MAX]
        self._emit(event)

    def record_usage(self, message: Any) -> None:
        """Fold one model turn's ``usage_metadata`` into the running total and emit
        a cumulative ``KIND_USAGE`` snapshot.

        Called from the MODEL-instance callback (UsageForwarder), NOT the
        run-config callback — that is the whole point: it fires for EVERY turn on
        EVERY dispatch path (supervisor, static-`task` sub-agents, AND QuickJS
        `task()` sub-agents that never reach the parent run's callbacks), so the
        total reflects the real spend. No-op for turns with no usage (thinking-
        only / provider omission). The snapshot carries absolute cumulative
        counts — the UI renders the latest one, so a missed/out-of-order poll
        can't corrupt a client-side running sum. Thread-safe: sub-agent turns
        end on pool threads, so the accumulate + snapshot is under the lock."""
        delta = _usage_from_message(message)
        if delta is None:
            return
        with self._lock:
            for k, v in delta.items():
                self._usage[k] += v
            snapshot = dict(self._usage)
        snapshot["total"] = snapshot["input"] + snapshot["output"]
        self._emit({"kind": KIND_USAGE, "usage": snapshot})

    @staticmethod
    def _call_id(run_id: Any) -> str:
        """Stringify LangChain's ``run_id`` — the correlation key that ties a
        tool's start event to its end/error event (identical for both). The UI
        folds the ``tool_call`` and ``tool_result`` sharing a ``call_id`` into one
        row. Parallel sub-agents interleave, so this pairing MUST be by id, not
        by adjacency."""
        return str(run_id) if run_id is not None else ""

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: Any = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """A tool began — shape (name, args) into a human label and emit it.

        The ``task`` tool (the static sub-agent dispatcher) IS a top-level tool
        call, so it's kept — but the sub-agent's OWN internal tool calls (which
        fire under a nested namespace) are dropped so they don't flood the feed.
        """
        if self._is_subagent(kwargs.get("metadata")):
            return
        name = (serialized or {}).get("name") or ""
        shaped = shape_step(name, inputs)
        cid = self._call_id(run_id)
        with self._lock:
            self._emitted_calls.add(cid)
            # An ``eval`` (the QuickJS fan-out dispatcher) opens a NEW fleet batch:
            # its globally-unique call_id groups the sub-agents it spawns. Recorded
            # here (top-level, so it's not filtered) and read by emit_subagent_event
            # to give each wave its own row instead of all sharing REPL ``call_0``.
            if shaped["tool"] == "eval":
                self._current_eval_batch = cid
        self._emit(
            {
                "kind": KIND_TOOL_CALL,
                "tool": shaped["tool"],
                "label": shaped["label"],
                "call_id": cid,
            }
        )

    def _emitted_call(self, run_id: Any) -> bool:
        """Did we emit the tool_call for this run? A result whose call we dropped
        (a sub-agent's) must be dropped too — else it renders as a label-less row.
        Authoritative pairing by call_id, robust to the subagent filter seeing a
        tool's start/end metadata asymmetrically."""
        cid = self._call_id(run_id)
        with self._lock:
            return cid in self._emitted_calls

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        """A tool returned — emit success/failure ONLY (no response body).

        Tool errors are surfaced as ``ToolMessage(status="error")`` rather than
        raised, so we classify from the output's ``status`` when present. Carries
        the same ``call_id`` as its ``on_tool_start`` so the UI pairs them. Emitted
        ONLY if we emitted the matching tool_call (drops sub-agent-internal
        results, whose call was filtered out).
        """
        if not self._emitted_call(run_id):
            return
        ok = True
        status = getattr(output, "status", None)
        if status == "error":
            ok = False
        self._emit(
            {
                "kind": KIND_TOOL_RESULT,
                "ok": ok,
                "call_id": self._call_id(run_id),
            }
        )

    def on_tool_error(
        self, error: BaseException, *, run_id: Any = None, **kwargs: Any
    ) -> None:
        """A tool raised (error handling disabled) — emit a failure result (only
        if we emitted the matching tool_call)."""
        if not self._emitted_call(run_id):
            return
        self._emit(
            {
                "kind": KIND_TOOL_RESULT,
                "ok": False,
                "call_id": self._call_id(run_id),
            }
        )

    # -- sub-agent fleet (driven from the custom stream, not callbacks) ------ #
    #
    # Called directly by the runner's stream-drain loop (NOT a LangChain callback
    # hook): the QuickJS sub-agent lifecycle rides LangGraph's custom stream,
    # which callbacks don't see. The runner passes each custom event here. There
    # is no pre-start "planned" event — the model builds the fan-out list
    # dynamically, so a reliable count isn't statically knowable; the UI grows the
    # squares row as sub-agents actually start.

    def emit_subagent_event(self, event: dict[str, Any]) -> None:
        """Emit one real sub-agent lifecycle event from the custom stream.

        ``event`` is a langchain_quickjs ``SubagentStreamEvent``
        (``{type:'subagent', phase, id, eval_id?, subagent_type?, label?, ...}``).
        We forward only the fields the fleet view needs, keyed by ``batch``
        (the fan-out group) and the per-dispatch ``sub_id`` (the event ``id``).

        ``batch`` is NOT the event's own ``eval_id`` — that's a REPL-local counter
        that resets to ``call_0`` on every ``eval()``, so distinct fan-outs would
        collide into one row. We use the top-level ``eval`` tool-call id instead
        (``_current_eval_batch``), which is globally unique per wave. A sub-agent's
        batch is PINNED at its ``start`` and reused on its ``complete``/``error`` so
        a late terminal event lands in the right row even after a new eval opened.
        """
        phase = event.get("phase")
        if phase not in (PHASE_START, PHASE_COMPLETE, PHASE_ERROR):
            return
        sub_id = event.get("id") or ""
        with self._lock:
            if phase == PHASE_START:
                # Pin this sub-agent to the wave that's currently dispatching. Fall
                # back to the raw eval_id if no top-level eval was seen (defensive:
                # e.g. the static `task` path), so a batch is never empty.
                batch = self._current_eval_batch or event.get("eval_id") or ""
                if sub_id:
                    self._fleet_batch_of[sub_id] = batch
            else:
                # Terminal: reuse the batch pinned at start; fall back to current.
                batch = self._fleet_batch_of.pop(sub_id, None)
                if batch is None:
                    batch = self._current_eval_batch or event.get("eval_id") or ""
        out: dict[str, Any] = {
            "kind": KIND_SUBAGENT,
            "phase": phase,
            "batch": batch,
            "sub_id": sub_id,
        }
        if phase == PHASE_START:
            out["label"] = shape_subagent_label(
                event.get("subagent_type"), event.get("label")
            )
            if event.get("subagent_type"):
                out["subagent_type"] = event.get("subagent_type")
        self._emit(out)


class UsageForwarder(BaseCallbackHandler):  # type: ignore[misc]
    """A model-instance callback that meters token usage on EVERY model turn.

    Attached to the shared chat-model instance — ``ChatBedrockConverse`` (Claude)
    or ``ChatOpenAI`` on Bedrock Mantle (GPT), whichever the model id selected —
    which all sub-agents inherit, NOT to the run config. That distinction is the
    fix: LangChain normalizes ``usage_metadata`` across both providers, and fires
    a model's *local* (instance) callbacks on every invocation of that model
    object regardless of which graph/thread drives it — including the QuickJS
    ``task()`` sub-agents that run on their own asyncio tasks and never reach the
    parent run's ``config["callbacks"]``. So this sees the supervisor's turns AND
    every sub-agent's, giving a complete running total. It only forwards usage to
    ``StepEmitter.record_usage``; the narrative feed still comes from the
    StepEmitter on the run config. Best-effort: never raises into the model call.
    """

    ignore_agent = False

    def __init__(self, emitter: "StepEmitter"):
        super().__init__()
        self._emitter = emitter

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        try:
            self._emitter.record_usage(_message_of(response))
        except Exception:  # noqa: BLE001 - metering must never break a model call
            log.debug("usage forward failed (continuing)", exc_info=True)


def make_log_sink(
    *, data_domain: str, dataset: str, session_id: str | None
) -> Callable[[dict[str, Any]], None]:
    """A sink that writes each step as one ``OKF_STEP <json>`` stdout line.

    Reuses the EXISTING harvest-runtime CloudWatch log group (no new storage): the
    Control API reads these lines back with FilterLogEvents, keyed by
    ``session_id`` (== the run's ``runtime_session_id``, already on the DynamoDB
    STATUS row) so a poll only sees THIS run's steps. Best-effort: the emitter
    already guards the call, and logging itself never raises in normal operation.

    Each line's payload adds a server-side ``ts`` (ISO-8601) and the correlation
    keys to whatever the emitter produced (seq/kind/label/…).
    """

    def sink(event: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "data_domain": data_domain,
            "dataset": dataset,
            "session_id": session_id or "",
            **event,
        }
        # separators keep the line compact; the marker is a leading token so a
        # CloudWatch filter pattern (?"OKF_STEP") matches cheaply.
        _step_log.info("%s %s", STEP_MARKER, json.dumps(record, separators=(",", ":")))

    return sink
