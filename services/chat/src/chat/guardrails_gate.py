"""``GuardrailsGateMiddleware`` — no page reads before the dataset's guardrails.

The wiki's contract is that ``references/usage_guardrails.md`` is the ONE
behavioural document a consumer must read before using a dataset (the system
prompt says so, the dataset overview links it first) — but a prompt is advice.
This middleware makes it MECHANICAL: a ``read_page`` on any page of a dataset
whose guardrails this thread has not yet read is DENIED at the tool boundary
with an instruction to read the guardrails first; the model then does exactly
that and retries. Enforcement is deliberately narrow:

* **Only ``read_page`` is gated.** Browsing and searching (``list_directory``,
  ``glob``, ``grep``, ``semantic_search``, ``get_backlinks``, ``list_domains``,
  …) stay free — the agent must be able to FIND the guardrails doc, and
  orientation is not consumption.
* **The guardrails read itself always passes** (else the gate would deadlock),
  and completing it marks the dataset satisfied — on success OR error: the
  gate exists to force the ATTEMPT, and a legacy bundle whose guardrails page
  is missing must not become permanently unreadable.
* **Tracking is per dataset** (``"<data_domain>/<dataset>"`` keys), because one
  conversation can wander across datasets, and it lives in CHECKPOINTED agent
  state (``state_schema`` below) — the graph is rebuilt every turn/resume, so
  middleware instance attributes would forget; the DynamoDB checkpointer
  remembers for the thread's lifetime.
* **The state channel has a merge reducer.** Two guardrails reads for
  DIFFERENT datasets can land in the same parallel tool batch; each returns a
  ``Command`` updating this channel in the same superstep, and a plain
  last-value channel would raise on the second update.

Scoped (``@``-mention) conversations drop ``data_domain``/``dataset`` from the
model-facing tool schemas and inject them inside the tool wrappers — so the
middleware receives the scope at construction (like ``server._with_scope``)
and falls back to it when the args don't carry a location. A call whose
dataset cannot be determined passes through: the tool itself rejects the
malformed call, and gating on a guess would deny the wrong thing.

Deploy-time kill switch: ``OKF_CHAT_GUARDRAILS_GATE_ENABLED`` (default on).
Deferred/guarded langchain imports keep the module importable in the unit venv.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, NotRequired

from okf_core.paths import GUARDRAILS_CONCEPT_ID as _GUARDRAILS_CONCEPT_ID

try:  # langchain/langgraph are present in the runtime image + the unit venv
    from langchain.agents.middleware import AgentMiddleware, AgentState
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    AgentState = dict  # type: ignore[assignment,misc]
    ToolMessage = None  # type: ignore[assignment]
    Command = None  # type: ignore[assignment]
    _HAVE_LANGCHAIN = False

_READ_TOOL = "read_page"

#: The one concept id the gate is about (no .md — read_page's arg convention).
#: The value's ONE owner is okf_core.paths — shared with the harvest (which
#: authors and lint-requires the doc) so the two services can't drift.
GUARDRAILS_CONCEPT_ID = _GUARDRAILS_CONCEPT_ID

#: The checkpointed state channel: {"<domain>/<dataset>": True, ...}.
STATE_KEY = "guardrails_read"


def guardrails_gate_enabled(env: dict[str, str] | None = None) -> bool:
    """Deploy-time kill switch (``OKF_CHAT_GUARDRAILS_GATE_ENABLED``, default on)."""
    raw = (env if env is not None else os.environ).get(
        "OKF_CHAT_GUARDRAILS_GATE_ENABLED", "true"
    )
    return str(raw).strip().lower() not in ("false", "0", "no", "off")


def _merge_read(
    left: dict[str, bool] | None, right: dict[str, bool] | None
) -> dict[str, bool]:
    """Channel reducer: union of the datasets marked so far (see module doc —
    parallel guardrails reads update this channel in one superstep)."""
    return {**(left or {}), **(right or {})}


if _HAVE_LANGCHAIN:

    class GuardrailsGateState(AgentState):  # type: ignore[misc]
        guardrails_read: NotRequired[Annotated[dict[str, bool], _merge_read]]

else:  # pragma: no cover - only when langchain is absent
    GuardrailsGateState = dict  # type: ignore[assignment,misc]


class GuardrailsGateMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Deny ``read_page`` until the dataset's usage guardrails have been read.

    Attach to the chat agent's middleware list (see ``server.build_agent``),
    passing the conversation's ``@``-scope when there is one. Only
    ``read_page`` is touched; every other tool passes straight through.
    """

    state_schema = GuardrailsGateState

    def __init__(self, scope: dict | None = None):
        super().__init__()
        scope = scope or {}
        self._scope_domain = str(scope.get("data_domain") or "").strip()
        self._scope_dataset = str(scope.get("dataset") or "").strip()

    def wrap_tool_call(self, request, handler):  # type: ignore[override]
        """Sync path (invoke/tests) — same decision logic as the async path."""
        verdict = self._classify(request)
        if verdict is None:
            return handler(request)
        kind, key = verdict
        if kind == "deny":
            return self._deny(request, key)
        return self._mark(handler(request), key)

    async def awrap_tool_call(self, request, handler):  # type: ignore[override]
        """Async path (astream) — the chat supervisor's real path."""
        verdict = self._classify(request)
        if verdict is None:
            return await handler(request)
        kind, key = verdict
        if kind == "deny":
            return self._deny(request, key)
        return self._mark(await handler(request), key)

    def _classify(self, request) -> tuple[str, str] | None:
        """(\"guardrails\"|\"deny\", dataset key), or None to pass through."""
        tool_call = request.tool_call
        if tool_call.get("name") != _READ_TOOL:
            return None
        args = tool_call.get("args") or {}
        domain = str(args.get("data_domain") or self._scope_domain).strip()
        dataset = str(args.get("dataset") or self._scope_dataset).strip()
        if not domain or not dataset:
            return None  # unattributable — the tool rejects the call itself
        key = f"{domain}/{dataset}"
        concept = str(args.get("concept_id") or "").strip().strip("/")
        if concept.endswith(".md"):
            concept = concept[: -len(".md")]
        if concept == GUARDRAILS_CONCEPT_ID:
            return ("guardrails", key)
        already = (getattr(request, "state", None) or {}).get(STATE_KEY) or {}
        if key in already:
            return None
        return ("deny", key)

    def _deny(self, request, key: str):
        """Short-circuit: the tool never runs; the model reads why and how."""
        domain, dataset = key.split("/", 1)
        locator = (
            ""
            if self._scope_dataset
            else f", data_domain '{domain}', dataset '{dataset}'"
        )
        payload = {
            "status": "denied",
            "error": (
                f"Read denied: before reading any page of dataset '{key}' you "
                f"must read its usage guardrails. Call read_page with "
                f"concept_id '{GUARDRAILS_CONCEPT_ID}'{locator} first, then "
                f"retry this read. Browsing/search tools (list_directory, "
                f"glob, grep, semantic_search, get_backlinks) are not "
                f"restricted."
            ),
        }
        return ToolMessage(
            content=json.dumps(payload),
            tool_call_id=request.tool_call["id"],
            name=_READ_TOOL,
            status="error",
        )

    def _mark(self, result: Any, key: str):
        """Fold a completed guardrails read into checkpointed state.

        Marks on ANY completed attempt — the tool reports a missing page as an
        ``"Error: ..."`` result, and treating that as unsatisfied would leave a
        guardrails-less dataset permanently unreadable. A ``Command`` from an
        inner middleware passes through unmarked (nothing composes inside this
        gate today; losing the mark just means one more guardrails read)."""
        if Command is None or not isinstance(result, ToolMessage):
            return result
        return Command(update={"messages": [result], STATE_KEY: {key: True}})
