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
  short summary of AIMessage text. Keeps the event payload tiny. THREE
  exceptions, each bounded: a FAILED tool call / errored sub-agent carries an
  ``error`` snippet, because without it a transient provider failure (e.g. a
  Mantle 400 killing a reviewer) is undiagnosable from the feed — the body
  never reaches the logs; a successful ``lint_bundle`` result carries a
  ``lint`` report (already structured and self-capped by the tool), because
  the lint gate's findings ARE the content the UI must surface on the feed
  row; and a sub-agent DISPATCH carries its I/O — the full brief (``full``)
  and the final answer (``result``), on the start/result events of a static
  ``task`` call or as ``phase:"update"`` patches for a QuickJS ``task()``
  square (whose library events carry neither; see ``harvest.subagent_io``) —
  because those two texts are the whole story of a fleet square (the
  sub-agent's internal steps stay filtered; see ``_is_subagent``).
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

import ast
import json
import logging
import threading
import time
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
# The snapshot ALSO carries `by`: the same counters split per METERING SCOPE —
# `supervisor` (the top-level agent's own turns), `subagents` (the dispatched
# authors/extractors), and `reviewer` (the
# adversarial review fan-out, which can run on its own model for cross-model
# coverage). Attribution is by MODEL INSTANCE: each scope runs on a separate
# instance carrying its own scope-tagged UsageForwarder, so no per-callback
# discrimination is needed. The top-level counters stay the sum of the scopes
# (older UIs keep working; new UIs render the drill-down from `by`).
KIND_USAGE = "usage"
#: A coalescing progress tick for the pre-agent snapshot passes — the UI
#: renders ONE live bar per `phase` and updates it in place.
KIND_PROGRESS = "progress"

# Usage metering scopes (the keys of the KIND_USAGE snapshot's `by` object).
SCOPE_SUPERVISOR = "supervisor"
SCOPE_SUBAGENTS = "subagents"
SCOPE_REVIEWER = "reviewer"
# Sub-agent lifecycle phases (mirror langchain_quickjs SubagentStreamEvent).
PHASE_START = "start"  # dispatched and running
PHASE_COMPLETE = "complete"
PHASE_ERROR = "error"
# I/O enrichment events from OUR shim (harvest.subagent_io), not the library:
# `input` carries the full dispatch brief (the library's start truncates it),
# `result` the final answer (the library's complete never carries it). Both
# are forwarded to the feed as ONE outbound phase, `update`, that patches the
# existing square — the UI folds `full`/`result` into it without touching the
# start/complete/error state machine.
PHASE_INPUT = "input"
PHASE_RESULT = "result"
PHASE_UPDATE = "update"

# Cap the AIMessage one-line summary (the feed `label`). A short teaser only.
_AGENT_SUMMARY_MAX = 200

# Cap the `error` snippet carried by a failed tool_result / errored subagent
# event. Long enough for a provider error body (an openai BadRequestError's
# str() with its JSON payload fits), short enough to keep the line tiny.
_ERROR_SNIPPET_MAX = 500

# Cap the FULL agent text carried alongside the summary (the `full` field the UI
# renders as markdown in a modal when the row is expanded). Bounded so one log
# line can't blow up the CloudWatch event size, but generous enough for a normal
# authoring/decision message. Structure (newlines, lists) is PRESERVED here —
# unlike `label`, which collapses whitespace for the one-liner.
_AGENT_FULL_MAX = 8000

# The one tool whose SUCCESS body rides the feed: the lint gate's structured
# report, carried as a `lint` field on its tool_result so the UI can badge the
# row and open the findings in a modal. Same size philosophy as _AGENT_FULL_MAX
# — findings past the budget are dropped from the tail (the tool orders errors
# first) and counted in `hidden`, never silently.
_LINT_TOOL = "lint_bundle"
_LINT_EVENT_BUDGET = 8000

# Sub-agent dispatch I/O for the fleet drill-in: a `task` dispatch's START
# carries the FULL brief (`full` — the same field the agent modal uses) and
# its RESULT carries the sub-agent's final answer (`result`). NEVER the
# sub-agent's internal steps — that firehose stays filtered (see
# _is_subagent); this is two bounded texts per dispatch. QuickJS-fanned
# sub-agents get the same two texts via the harvest.subagent_io shim's
# input/result enrichment events (forwarded as a `phase:"update"` feed event),
# because the library's own lifecycle events carry neither.
# 64KB, not the 8KB the other bounded texts use: a table-author brief carries
# its context-digest slice and an extractor's returned digest carries full
# enum legends — both routinely exceed the smaller caps, and a truncated
# brief/answer defeats the drill-in's purpose. Each event is one CloudWatch
# log line (hard limit 256KB) and one field rides per event, so 64KB stays
# comfortably bounded (and FilterLogEvents' 1MB pages still hold ~15 events).
_TASK_TOOL = "task"
_SUBAGENT_IO_MAX = 64000


def _bounded_text(value: Any, cap: int = _SUBAGENT_IO_MAX) -> str:
    text = value if isinstance(value, str) else str(value or "")
    text = text.strip()
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


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
    if name == "delete":
        fp = _first_arg(args, "file_path", "path")
        # The FULL root-relative path, not the 2-segment tail the other file
        # ops show: a removal is the one op an operator audits from the feed,
        # so the label must name the doc unambiguously.
        return {
            "tool": name,
            "label": f"Deleting {fp.lstrip('/')}" if fp else "Deleting a file",
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
        return {"tool": name, "label": "Running a SQL query"}

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


def _error_snippet(text: Any) -> str:
    """A short single-line snippet of an error, for a failed step event."""
    line = " ".join(str(text or "").split())
    if len(line) > _ERROR_SNIPPET_MAX:
        return line[: _ERROR_SNIPPET_MAX - 1].rstrip() + "…"
    return line


def _tool_error_snippet(output: Any) -> str:
    """The error text off a failed ToolMessage, bounded to a snippet.

    ToolNode puts the error string in ``content`` (usually a plain string;
    some providers wrap it in text blocks). Falls back to ``str`` of whatever
    arrived so a failure is never silently label-less.
    """
    text = _extract_ai_text(output)
    if not text:
        content = getattr(output, "content", output)
        text = content if isinstance(content, str) else str(content or "")
    return _error_snippet(text)


def _lint_event_payload(output: Any) -> dict[str, Any] | None:
    """The lint report off a successful ``lint_bundle`` ToolMessage, bounded.

    The tool returns a dict, but what reaches ``on_tool_end`` depends on the
    framework's ToolMessage formatting: a dict, a JSON string, or a Python
    ``str(dict)`` repr. Parse best-effort (json first, ``ast.literal_eval``
    fallback — the report is all literals) and return None when the shape
    isn't a lint report; the event then simply carries no ``lint`` field.
    Error/warning totals come from the per-step counters, so they stay
    accurate even when the findings list is truncated to the budget.
    """
    content = getattr(output, "content", output)
    if isinstance(content, list):  # provider text-block form
        content = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    data: Any = content
    if isinstance(content, str):
        text = content.strip()
        if not text.startswith("{"):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            try:
                data = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return None
    if not isinstance(data, dict):
        return None
    steps = data.get("steps")
    findings = data.get("findings")
    if not isinstance(steps, list) or not isinstance(findings, list):
        return None

    def _count(key: str) -> int:
        total = 0
        for s in steps:
            if isinstance(s, dict):
                try:
                    total += int(s.get(key) or 0)
                except (TypeError, ValueError):
                    pass
        return total

    def _capped_note(value: Any) -> str:
        note = str(value)
        return note if len(note) <= 300 else note[:299] + "…"

    # Notes ride the event too, so they get their own bound — a failed step's
    # note embeds an exception string, and a multi-KB boto error body would
    # bust the budget the findings are trimmed to fit.
    slim_steps = [
        {**s, "note": _capped_note(s["note"])}
        if isinstance(s, dict) and s.get("note")
        else s
        for s in steps
    ]
    payload: dict[str, Any] = {
        "ok": bool(data.get("ok")),
        "errors": _count("errors"),
        "warnings": _count("warnings"),
        "steps": slim_steps,
        "findings": [],
    }
    if data.get("note"):
        payload["note"] = _capped_note(data["note"])
    used = len(json.dumps(payload, separators=(",", ":"), default=str))
    kept = 0
    for f in findings:
        used += len(json.dumps(f, separators=(",", ":"), default=str)) + 1
        if used > _LINT_EVENT_BUDGET:
            break
        payload["findings"].append(f)
        kept += 1
    hidden = len(findings) - kept
    if hidden > 0:
        payload["hidden"] = hidden
    return payload


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

    # Cache WRITES: normally input_token_details.cache_creation — but
    # langchain_aws's Converse path ZEROES cache_creation whenever Bedrock
    # returns a per-TTL cacheDetails breakdown (Anthropic models do) and
    # reports the writes only as ephemeral_{5m,1h}_input_tokens buckets, which
    # made Anthropic runs show no cache writes at all. langchain_anthropic
    # (native) emits BOTH (cache_creation = the total, the buckets = its
    # per-TTL split), so SUMMING them would double-count there — max() reads
    # the true write count from every shape.
    cache_write = max(
        _int(details.get("cache_creation")),
        _int(details.get("ephemeral_5m_input_tokens"))
        + _int(details.get("ephemeral_1h_input_tokens")),
    )

    delta = {
        "input": _int(um.get("input_tokens")),
        "output": _int(um.get("output_tokens")),
        "cache_read": _int(details.get("cache_read")),
        "cache_write": cache_write,
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
        # Per-phase timestamp of the last progress emission (throttling state
        # for emit_progress — see there).
        self._progress_last: dict[str, float] = {}
        # Track the call_ids we EMITTED a tool_call for, so a tool_result is only
        # emitted when its call was. LangChain only passes `metadata` to the START
        # callbacks (on_tool_start / on_chat_model_start) — the END callbacks
        # (on_tool_end / on_llm_end) receive an EMPTY metadata dict (verified
        # against the installed langchain_core). So the subagent discriminator can
        # only be evaluated at start; the end must be paired to it by run_id.
        # Pairing by call_id is authoritative: no emitted call => drop the result.
        # call_id -> tool name. Membership answers "did we emit this call?";
        # the name lets on_tool_end recognise the lint gate's result.
        self._emitted_calls: dict[str, str] = {}
        # call_id -> (subagent_type, raw brief) for static `task` dispatches.
        # on_tool_end uses it to persist a context-extractor's digest verbatim
        # (harvest.context_digests) — the type/brief only ride the START.
        self._task_meta: dict[str, tuple[Any, Any]] = {}
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
        # `_usage` is the run total; `_usage_by` splits it per metering scope
        # (supervisor vs subagents — see the KIND_USAGE comment).
        self._usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._usage_by = {
            SCOPE_SUPERVISOR: dict(self._usage),
            SCOPE_SUBAGENTS: dict(self._usage),
            SCOPE_REVIEWER: dict(self._usage),
        }

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

    def record_usage(self, message: Any, scope: str = SCOPE_SUPERVISOR) -> None:
        """Fold one model turn's ``usage_metadata`` into the running totals and
        emit a cumulative ``KIND_USAGE`` snapshot.

        Called from the MODEL-instance callback (UsageForwarder), NOT the
        run-config callback — that is the whole point: it fires for EVERY turn on
        EVERY dispatch path (supervisor, static-`task` sub-agents, AND QuickJS
        `task()` sub-agents that never reach the parent run's callbacks), so the
        total reflects the real spend. ``scope`` names which metering bucket the
        turn belongs to (the forwarder's tag — supervisor vs subagents; each
        model instance carries its own tagged forwarder, so attribution is by
        instance). No-op for turns with no usage (thinking-only / provider
        omission). The snapshot carries absolute cumulative counts — the UI
        renders the latest one, so a missed/out-of-order poll can't corrupt a
        client-side running sum. Thread-safe: sub-agent turns end on pool
        threads, so the accumulate + snapshot is under the lock."""
        delta = _usage_from_message(message)
        if delta is None:
            return
        bucket_key = scope if scope in self._usage_by else SCOPE_SUPERVISOR
        with self._lock:
            bucket = self._usage_by[bucket_key]
            for k, v in delta.items():
                self._usage[k] += v
                bucket[k] += v
            snapshot = dict(self._usage)
            by = {s: dict(counts) for s, counts in self._usage_by.items()}
        snapshot["total"] = snapshot["input"] + snapshot["output"]
        for counts in by.values():
            counts["total"] = counts["input"] + counts["output"]
        snapshot["by"] = by
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
            self._emitted_calls[cid] = shaped["tool"]
            # An ``eval`` (the QuickJS fan-out dispatcher) opens a NEW fleet batch:
            # its globally-unique call_id groups the sub-agents it spawns. Recorded
            # here (top-level, so it's not filtered) and read by emit_subagent_event
            # to give each wave its own row instead of all sharing REPL ``call_0``.
            if shaped["tool"] == "eval":
                self._current_eval_batch = cid
        event: dict[str, Any] = {
            "kind": KIND_TOOL_CALL,
            "tool": shaped["tool"],
            "label": shaped["label"],
            "call_id": cid,
        }
        # A task dispatch's full brief rides the start event (the label keeps
        # only the description's first line) — the UI's fleet drill-in shows
        # it in the square's Input tab.
        if shaped["tool"] == _TASK_TOOL:
            brief = _bounded_text((inputs or {}).get("description"))
            if brief:
                event["full"] = brief
            with self._lock:
                self._task_meta[cid] = (
                    (inputs or {}).get("subagent_type"),
                    (inputs or {}).get("description"),
                )
        self._emit(event)

    def _emitted_call(self, run_id: Any) -> bool:
        """Did we emit the tool_call for this run? A result whose call we dropped
        (a sub-agent's) must be dropped too — else it renders as a label-less row.
        Authoritative pairing by call_id, robust to the subagent filter seeing a
        tool's start/end metadata asymmetrically."""
        cid = self._call_id(run_id)
        with self._lock:
            return cid in self._emitted_calls

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        """A tool returned — emit success/failure (no response body on success).

        Tool errors are surfaced as ``ToolMessage(status="error")`` rather than
        raised, so we classify from the output's ``status`` when present. A
        failure ALSO carries a bounded ``error`` snippet of the message content —
        that text exists nowhere else (it goes back to the model, not the logs),
        so without it a failed call is undiagnosable after the run. Carries
        the same ``call_id`` as its ``on_tool_start`` so the UI pairs them. Emitted
        ONLY if we emitted the matching tool_call (drops sub-agent-internal
        results, whose call was filtered out).
        """
        if not self._emitted_call(run_id):
            return
        cid = self._call_id(run_id)
        event: dict[str, Any] = {
            "kind": KIND_TOOL_RESULT,
            "ok": True,
            "call_id": cid,
        }
        if getattr(output, "status", None) == "error":
            event["ok"] = False
            snippet = _tool_error_snippet(output)
            if snippet:
                event["error"] = snippet
        else:
            with self._lock:
                tool = self._emitted_calls.get(cid, "")
            if tool == _LINT_TOOL:
                # The lint gate's report rides its result event (bounded) so
                # the UI can badge the row and open the findings in a modal.
                payload = _lint_event_payload(output)
                if payload is not None:
                    event["lint"] = payload
            elif tool == _TASK_TOOL:
                # A task dispatch's final answer (the sub-agent's return text,
                # never its internal steps) — the fleet drill-in's Output tab.
                # _result_text handles EVERY shape the task tool returns:
                # deepagents' Command(update={"messages": [ToolMessage(...)]})
                # (which reaches on_tool_end unchanged), a bare message, or a
                # plain string — _extract_ai_text alone misses the Command
                # envelope and left static-path dispatches with no result.
                from harvest.subagent_io import _result_text

                text = _result_text(output) or _extract_ai_text(output)
                # A context-extractor's digest is persisted VERBATIM for the
                # review's fidelity phase (record() filters by type, fail-soft;
                # the feed keeps its bounded copy below).
                with self._lock:
                    sub_type, raw_brief = self._task_meta.get(cid, (None, None))
                try:
                    from harvest.context_digests import record

                    record(
                        sub_type if isinstance(sub_type, str) else None,
                        raw_brief if isinstance(raw_brief, str) else None,
                        text,
                    )
                except Exception:  # noqa: BLE001 — observability only
                    log.debug("Failed to record extractor digest", exc_info=True)
                if text:
                    event["result"] = _bounded_text(text)
        self._emit(event)

    def on_tool_error(
        self, error: BaseException, *, run_id: Any = None, **kwargs: Any
    ) -> None:
        """A tool raised (error handling disabled) — emit a failure result (only
        if we emitted the matching tool_call), with a bounded error snippet."""
        if not self._emitted_call(run_id):
            return
        event: dict[str, Any] = {
            "kind": KIND_TOOL_RESULT,
            "ok": False,
            "call_id": self._call_id(run_id),
        }
        snippet = _error_snippet(error)
        if snippet:
            event["error"] = snippet
        self._emit(event)

    # -- sub-agent fleet (driven from the custom stream, not callbacks) ------ #
    #
    # Called directly by the runner's stream-drain loop (NOT a LangChain callback
    # hook): the QuickJS sub-agent lifecycle rides LangGraph's custom stream,
    # which callbacks don't see. The runner passes each custom event here. There
    # is no pre-start "planned" event — the model builds the fan-out list
    # dynamically, so a reliable count isn't statically knowable; the UI grows the
    # squares row as sub-agents actually start.

    def emit_status(self, label: str) -> None:
        """A runner-authored narration line for PRE-AGENT phases.

        The callback surface only observes the agent, so work that happens
        before the first model turn — the metadata snapshot and the column
        profiling pass — was invisible in the live feed. The runner calls this
        directly around those phases; the event is a plain ``kind="agent"``
        one-liner, so the UI renders it like any other narration row with no
        changes.
        """
        self._emit({"kind": KIND_AGENT, "label": str(label)})

    def emit_progress(
        self, phase: str, done: int, total: int, label: str = ""
    ) -> None:
        """A coalescing progress tick for the PRE-AGENT snapshot passes
        (column profiles, relationship evidence): ``kind="progress"`` with a
        stable ``phase`` key, so the UI renders ONE live bar per phase and
        updates it in place — not 237 feed lines for a 237-table pass.

        Throttled per phase (and, like every emission, never raises): the
        FIRST tick and any FINAL tick (done >= total) always emit;
        intermediate ticks emit at most every ~2s — the UI polls the feed
        every few seconds, so finer granularity is invisible and would only
        bloat CloudWatch.
        """
        try:
            now = time.monotonic()
            with self._lock:
                last = self._progress_last.get(phase)
                final = total > 0 and done >= total
                if last is not None and not final and now - last < 2.0:
                    return
                self._progress_last[phase] = now
        except Exception:  # noqa: BLE001 - a progress tick must never break a harvest
            return
        self._emit(
            {
                "kind": KIND_PROGRESS,
                "phase": str(phase),
                "done": int(done),
                "total": int(total),
                "label": str(label or ""),
            }
        )

    def emit_subagent_event(self, event: dict[str, Any]) -> None:
        """Emit one real sub-agent lifecycle event from the custom stream.

        ``event`` is a langchain_quickjs ``SubagentStreamEvent``
        (``{type:'subagent', phase, id, eval_id?, subagent_type?, label?, ...}``)
        OR one of OUR shim's I/O enrichment events (``phase: input|result`` —
        see :mod:`harvest.subagent_io`), which forward as ``phase: update``.
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
        if phase not in (
            PHASE_START,
            PHASE_COMPLETE,
            PHASE_ERROR,
            PHASE_INPUT,
            PHASE_RESULT,
        ):
            return
        sub_id = event.get("id") or ""
        # An event carrying an explicit `batch` names its own fleet row — the
        # run_review tool emits its dispatches this way (":review" / ":fix"
        # waves keyed by ITS tool_call_id), because the eval-batch heuristic
        # below would fold them into whatever eval() ran last. Library events
        # never carry the key, so their grouping is unchanged.
        explicit_batch = event.get("batch") or ""
        with self._lock:
            if phase == PHASE_START:
                # Pin this sub-agent to the wave that's currently dispatching. Fall
                # back to the raw eval_id if no top-level eval was seen (defensive:
                # e.g. the static `task` path), so a batch is never empty.
                batch = (
                    explicit_batch
                    or self._current_eval_batch
                    or event.get("eval_id")
                    or ""
                )
                if sub_id:
                    self._fleet_batch_of[sub_id] = batch
            elif phase in (PHASE_INPUT, PHASE_RESULT):
                # Mid-flight enrichment: the square is still running, so read the
                # pinned batch WITHOUT popping it (complete/error still needs it).
                batch = self._fleet_batch_of.get(sub_id)
                if batch is None:
                    batch = (
                        explicit_batch
                        or self._current_eval_batch
                        or event.get("eval_id")
                        or ""
                    )
            else:
                # Terminal: reuse the batch pinned at start; fall back to current.
                batch = self._fleet_batch_of.pop(sub_id, None)
                if batch is None:
                    batch = (
                        explicit_batch
                        or self._current_eval_batch
                        or event.get("eval_id")
                        or ""
                    )
        out: dict[str, Any] = {
            "kind": KIND_SUBAGENT,
            "phase": phase,
            "batch": batch,
            "sub_id": sub_id,
        }
        if phase in (PHASE_INPUT, PHASE_RESULT):
            # Enrichment from OUR shim (harvest.subagent_io): the full dispatch
            # brief right after start, the final answer right before complete.
            # Forwarded as ONE outbound phase — `update` — carrying the same
            # `full`/`result` fields the static-`task` path uses; the UI patches
            # the existing square. Dropped when the text is missing (nothing to
            # patch) so the feed never grows an empty event.
            out["phase"] = PHASE_UPDATE
            key = "full" if phase == PHASE_INPUT else "result"
            value = event.get("input") if phase == PHASE_INPUT else event.get("result")
            if not (isinstance(value, str) and value.strip()):
                return
            out[key] = _bounded_text(value)
        elif phase == PHASE_START:
            out["label"] = shape_subagent_label(
                event.get("subagent_type"), event.get("label")
            )
            if event.get("subagent_type"):
                out["subagent_type"] = event.get("subagent_type")
            # Until the shim's `input` enrichment lands (milliseconds later),
            # the best start-time brief is the library's own `description`
            # teaser (200 chars), then the raw label when it says more than
            # the shaped one. Bounded either way.
            raw = event.get("description") or event.get("label")
            if isinstance(raw, str) and raw.strip() and raw.strip() != out["label"]:
                out["full"] = _bounded_text(raw)
        elif phase == PHASE_COMPLETE:
            # Forward the sub-agent's final answer when the stream event
            # carries one (field name probed defensively — older
            # langchain_quickjs versions may omit it, and the UI copes).
            for key in ("result", "output", "text", "content"):
                value = event.get(key)
                if isinstance(value, str) and value.strip():
                    out["result"] = _bounded_text(value)
                    break
        elif phase == PHASE_ERROR:
            # langchain_quickjs's SubagentErrorEvent carries the failure string
            # (str() of the raised exception — e.g. the provider's 400 body).
            # Forward a bounded snippet: this is the ONLY place the failure text
            # surfaces (the exception is consumed by the task() promise, so it
            # never reaches the logs), and without it an errored square is
            # undiagnosable after the run.
            snippet = _error_snippet(event.get("error"))
            if snippet:
                out["error"] = snippet
        self._emit(out)


class UsageForwarder(BaseCallbackHandler):  # type: ignore[misc]
    """A model-instance callback that meters token usage on EVERY model turn.

    Attached to a chat-model INSTANCE — ``ChatBedrockConverse`` (Claude) or
    ``ChatOpenAI`` on Bedrock Mantle (GPT), whichever the model id selected —
    NOT to the run config. That distinction is the fix: LangChain normalizes
    ``usage_metadata`` across both providers, and fires a model's *local*
    (instance) callbacks on every invocation of that model object regardless of
    which graph/thread drives it — including the QuickJS ``task()`` sub-agents
    that run on their own asyncio tasks and never reach the parent run's
    ``config["callbacks"]``.

    ``scope`` tags every turn this instance's forwarder sees into one metering
    bucket (supervisor vs subagents). The harvest builds TWO model instances —
    the supervisor's and the sub-agents' (also used by the benchmark
    solver/adjudicator) — each with its own scoped forwarder, so the run total
    stays complete AND splits cleanly per scope. It only forwards usage to
    ``StepEmitter.record_usage``; the narrative feed still comes from the
    StepEmitter on the run config. Best-effort: never raises into the model call.
    """

    ignore_agent = False

    def __init__(self, emitter: "StepEmitter", scope: str = SCOPE_SUPERVISOR):
        super().__init__()
        self._emitter = emitter
        self._scope = scope

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        try:
            self._emitter.record_usage(_message_of(response), scope=self._scope)
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
