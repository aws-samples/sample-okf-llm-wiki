"""Raw FastAPI app hosting the chat agent on AgentCore Runtime (HTTP protocol).

This is a **Sparky-style** chat runtime: the browser POSTs directly to this
runtime's ``/invocations`` URL (Cognito JWT bearer, no proxy), and we own the
whole wire contract instead of an AG-UI adapter. The request/response shapes are
Sparky's:

Request body — a ``type``-discriminated ``input`` envelope::

    {"input": {"type": "send",  "prompt": "...", "model_id": "...", "budget_level": 3,
               "dataset_scope": {"data_domain": "...", "dataset": "..."}}}
    {"input": {"type": "get_session_history"}}
    {"input": {"type": "delete_history"}}
    {"input": {"type": "prepare", ...}}    # keep-warm, no LLM call

Response — for ``send``, a ``text/event-stream`` of ``data: {json}\\n\\n`` frames
carrying Sparky's **typed chunks**, produced by consuming the LangGraph run's
``astream(stream_mode=["messages","updates"])``:

    {"type": "think",  "content": "..."}          reasoning (Converse reasoning_content / GPT summary)
    {"type": "tool", "id": ..., "tool_name": ..., "tool_start": true,  "content": <args>}
    {"type": "tool", "id": ..., "tool_name": ..., "tool_start": false, "content": <result>, "error": bool}
    {"type": "text",  "content": "..."}           assistant answer tokens
    {"type": "error", "error_code": ..., "message": ...}
    {"end": true, "token_stats": {...}, "checkpoint_id": "..."}

Why hand-rolled rather than the ``ag_ui_langgraph`` adapter: the front end renders
reasoning + tool calls + markdown tables itself (like Sparky), so it needs the raw
typed chunks, not AG-UI's ``TEXT_MESSAGE_*``/``TOOL_CALL_*``/``REASONING_*`` event
model. Owning both layers is what removes the whole class of adapter rendering-slot
bugs.

Per-user isolation: the checkpoint / history thread id is namespaced with the
Cognito ``sub`` (``f"{sub}:{client_thread_id}"``) so one user can never read or
resume another's conversation by sending their thread id. The session-id header
(== the client thread id) is what the browser sends; ``sub`` comes from the
(AgentCore-validated) JWT, decoded here WITHOUT re-verifying the signature.

Guarded imports keep this module importable in the unit venv without
fastapi/langgraph; only ``__main__`` (the container entrypoint) needs the full
stack. The pure request logic lives in helpers that tests drive directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, AsyncGenerator, Callable

# ``from __future__ import annotations`` turns every annotation into a string,
# and FastAPI resolves a route handler's annotations with ``get_type_hints``
# against the DEFINING MODULE's globals. The route handlers live inside
# ``build_app`` but their ``Request`` annotation is resolved here — so ``Request``
# must exist at module scope or FastAPI mistakes it for a query param (422).
try:  # pragma: no cover - import shape, not behaviour
    from fastapi import Request
except ImportError:  # pragma: no cover
    Request = None  # type: ignore[assignment,misc]

from chat import live_streams

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

# LangGraph recursion ceiling for one turn (matches Sparky). A chat turn that
# needs >200 model/tool hops is a bug, not the norm.
RECURSION_LIMIT = 200


class MissingHeader(Exception):
    """A required inbound header (session id / Authorization) was absent."""


# --- request/identity helpers ------------------------------------------------


def decode_sub(auth_header: str | None) -> str:
    """Extract the Cognito ``sub`` from a ``Bearer <jwt>`` header.

    The signature is NOT verified: AgentCore's JWT authorizer already validated
    the token before the request reached this container. We only need the stable
    per-user subject for checkpoint namespacing + conversation ownership.
    """
    if not auth_header:
        raise MissingHeader("missing Authorization header")
    import jwt  # PyJWT; deferred so the module imports without it

    token = auth_header.removeprefix("Bearer ").strip()
    claims = jwt.decode(token, options={"verify_signature": False})
    sub = claims.get("sub")
    if not sub:
        raise MissingHeader("token has no 'sub' claim")
    return sub


def namespaced_thread_id(user_sub: str, client_thread_id: str) -> str:
    """The internal thread id, namespaced by user so state can't be cross-read.

    ``f"{sub}:{thread_id}"`` — isolation lives in the KEY, not in a check that can
    be forgotten (mirrors the annotations table's structural per-user pk).
    """
    return f"{user_sub}:{client_thread_id}"


def extract_scope(input_data: dict[str, Any] | None) -> dict[str, str] | None:
    """Pull an optional dataset scope out of the input envelope.

    Accepts either ``dataset_scope`` (snake) or ``datasetScope`` (camel). Returns
    ``{"data_domain", "dataset"}`` or ``None`` (whole wiki). A partial/malformed
    scope is treated as no scope.
    """
    if not input_data:
        return None
    scope = input_data.get("dataset_scope") or input_data.get("datasetScope")
    if not isinstance(scope, dict):
        return None
    dd, ds = scope.get("data_domain"), scope.get("dataset")
    if dd and ds:
        return {"data_domain": str(dd), "dataset": str(ds)}
    return None


# The scope line is prefixed to the user message and separated by a blank line, so
# it can be recognised + stripped on history reload (the bubble shows the user's
# ORIGINAL text, not our injected preamble).
_SCOPE_PREFIX_RE = re.compile(r"^\[Scope:[^\]]*\]\n\n", re.DOTALL)


def scoped_prompt(prompt: str, scope: dict[str, str] | None) -> str:
    """Prefix the user's message with the active dataset scope, when set.

    Injected on the HUMAN message (not the system prompt) on purpose: the system
    prompt is a static, cacheable prefix — rewriting it per-turn to name the scope
    would invalidate the prompt cache every time the scope changed. A per-turn line
    on the (already-new) user message tells the model which dataset it's scoped to
    at zero cache cost, and naturally varies turn-to-turn if the user re-tags.

    The tool schemas are ALSO pre-bound to this scope (chat.tools), so this line is
    the model's explicit signal of the confinement its tools already enforce.
    """
    if not scope:
        return prompt
    dd, ds = scope["data_domain"], scope["dataset"]
    return (
        f"[Scope: the dataset {dd}/{ds}. Answer about this dataset; your wiki "
        f"tools are already restricted to it. Only look beyond it if I explicitly "
        f"ask to compare against another dataset.]\n\n{prompt}"
    )


def strip_scope_prefix(text: str) -> str:
    """Remove a leading ``[Scope: …]`` preamble (added by :func:`scoped_prompt`).

    Used on history reload so the user's message bubble shows what they typed, not
    the injected scope line.
    """
    return _SCOPE_PREFIX_RE.sub("", text) if isinstance(text, str) else text


# Recover ``{data_domain, dataset}`` from a stored user message's scope prefix
# (``scoped_prompt`` writes "[Scope: the dataset <dd>/<ds>. …]"). Used on history
# reload to re-fold the scope into that turn's tool events (see _with_scope).
_SCOPE_DATASET_RE = re.compile(r"^\[Scope: the dataset ([^/]+)/([^.\]]+)")


def parse_scope_prefix(text: str) -> dict[str, str] | None:
    """Extract the dataset scope from a stored user message's ``[Scope: …]`` line."""
    if not isinstance(text, str):
        return None
    m = _SCOPE_DATASET_RE.match(text)
    if not m:
        return None
    return {"data_domain": m.group(1).strip(), "dataset": m.group(2).strip()}


def _pending_interrupts(graph: Any, cfg: dict) -> list[Any]:
    """Every interrupt the graph is currently paused on (across all pending tasks).

    Reads the checkpoint state, not the stream: the stream emits one
    ``__interrupt__`` per interrupt with no stable id, but the state carries each
    ``Interrupt`` object WITH its ``id`` — which is what
    ``Command(resume={id: ...})`` needs (langgraph REQUIRES the id-keyed map form
    when more than one interrupt is pending; a bare resume value raises). Returns []
    when the graph isn't paused."""
    try:
        state = graph.get_state(cfg)
    except Exception:  # noqa: BLE001 - no state / transient read error → not paused
        return []
    out: list[Any] = []
    for task in getattr(state, "tasks", None) or []:
        out.extend(getattr(task, "interrupts", None) or [])
    return out


def _ask_human_chunk_from_state(graph: Any, cfg: dict) -> dict[str, Any] | None:
    """Build the ``ask_human`` chunk from the graph's pending interrupts, or None.

    Consolidates ALL pending ask_human interrupts into one chunk so the UI renders a
    single QA form even if the model (against instructions) emitted several
    ``ask_human`` calls in one turn. Each group carries its ``interrupt_id`` so the
    answer round can resume every interrupt by id. Non-ask_human interrupts (none
    expected) are ignored."""
    groups: list[dict[str, Any]] = []
    for intr in _pending_interrupts(graph, cfg):
        value = getattr(intr, "value", None)
        if isinstance(value, dict) and value.get("type") == "ask_human":
            groups.append(
                {
                    "interrupt_id": getattr(intr, "id", None),
                    "questions": value.get("questions") or [],
                }
            )
    if not groups:
        return None
    # Flat questions list (what the form renders) + the per-group id mapping the
    # answer round needs. Each question is tagged with its owning interrupt_id so
    # answers can be split back per interrupt on resume.
    questions: list[dict[str, Any]] = []
    for g in groups:
        for q in g["questions"]:
            questions.append({**q, "interrupt_id": g["interrupt_id"]})
    return {
        "type": "ask_human",
        "questions": questions,
        "interrupt_ids": [g["interrupt_id"] for g in groups],
    }


def _build_resume_map(graph: Any, cfg: dict, answers: Any) -> dict[str, Any] | None:
    """Map each pending interrupt id → the answers destined for it, or None.

    ``answers`` is the UI's list of ``{id, answer, interrupt_id}``. langgraph resumes
    multiple interrupts via ``Command(resume={interrupt_id: value})``; each value is
    what THAT interrupt's ``interrupt(...)`` returns (here, the answer records for its
    questions). We route each answer to its ``interrupt_id`` when present; answers
    with no interrupt_id (single-interrupt case, or an older client) all go to the
    sole pending interrupt. Returns None when the graph has NO pending interrupt (so
    the caller ends cleanly instead of injecting a phantom turn)."""
    pending_ids = [
        getattr(i, "id", None) for i in _pending_interrupts(graph, cfg)
    ]
    pending_ids = [i for i in pending_ids if i]
    if not pending_ids:
        return None

    answer_list = answers if isinstance(answers, (list, tuple)) else []
    by_interrupt: dict[str, list[Any]] = {iid: [] for iid in pending_ids}
    unrouted: list[Any] = []
    for a in answer_list:
        iid = a.get("interrupt_id") if isinstance(a, dict) else None
        if iid in by_interrupt:
            by_interrupt[iid].append(a)
        else:
            unrouted.append(a)
    # Answers with no (or unknown) interrupt_id go to the first pending interrupt —
    # the common single-interrupt path where the UI needn't tag them.
    if unrouted:
        by_interrupt[pending_ids[0]].extend(unrouted)
    return by_interrupt


# --- stream chunk translation (the crux) -------------------------------------


def _tool_call_id(tc: Any) -> str | None:
    if isinstance(tc, dict):
        return tc.get("id") or tc.get("tool_call_id")
    return getattr(tc, "id", None)


# The scope keys a scoped conversation injects server-side (mirrors chat.tools).
_SCOPE_ARG_KEYS = ("data_domain", "dataset")

# Consumption tools that ACCEPT a (data_domain, dataset) location — the ones from
# which chat.tools drops + injects the scope. The domain-discovery tools
# (list_domains / list_declared_domains / search_domains) do NOT take a location,
# so scope must NOT be stamped onto them (their labels would then read wrongly).
_LOCATION_TAKING_TOOLS = frozenset(
    {"list_directory", "read_page", "get_backlinks", "glob", "grep", "semantic_search"}
)


def _with_scope(tool_name: Any, args: Any, scope: dict[str, str] | None) -> Any:
    """Fold the conversation's dataset scope back into a location-taking tool's args.

    In a scoped conversation, ``data_domain``/``dataset`` are dropped from the
    model's tool schema and injected server-side (chat.tools), so the streamed args
    lack them and the UI rendered ``undefined/undefined``. Restore them here — only
    for tools that take a location, and only for keys the model didn't itself pass
    (never overwrite). No scope / non-dict args / non-location tool ⇒ unchanged.
    """
    if not scope or not isinstance(args, dict) or tool_name not in _LOCATION_TAKING_TOOLS:
        return args
    merged = dict(args)
    for k in _SCOPE_ARG_KEYS:
        if not merged.get(k):
            merged[k] = scope.get(k)
    return merged


def process_stream_data(
    mode: str, data: Any, scope: dict[str, str] | None = None
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Translate one LangGraph ``astream`` part into Sparky typed chunk(s).

    ``mode`` is one of the requested ``stream_mode`` strings; ``data`` is that
    mode's payload. Returns a chunk dict, a list of chunk dicts, or ``None`` to
    drop the part. Mirrors Sparky's ``_process_stream_data`` but trimmed to the
    chunk types the wiki chat emits (no canvas / browser / interrupts):

    - ``updates``/``model`` → an AIMessage whose ``tool_calls`` are the reliable,
      fully-assembled tool-START events (args parsed). This is where tool starts
      come from — the streamed ``tool_call_chunks`` are partial JSON.
    - ``messages`` → live tokens: ``AIMessageChunk`` string content is answer
      ``text``; ``reasoning_content`` blocks are ``think``; a ``ToolMessage`` is
      the tool RESULT.

    ``scope`` (the conversation's dataset scope, when set) is folded BACK into each
    tool-start's args: in a scoped conversation ``data_domain``/``dataset`` are
    dropped from the model's tool schema and injected server-side (see chat.tools),
    so the streamed args lack them and the UI rendered ``undefined/undefined``. We
    restore them here (without overwriting any the model did pass) so the emitted
    event — live and in stored history — names the real dataset.
    """
    from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

    if mode == "updates":
        if not isinstance(data, dict):
            return None
        # A LangGraph interrupt (AskHumanMiddleware raised one) surfaces as a
        # top-level ``__interrupt__`` in the updates payload — but ONE per interrupt,
        # and without a reliable interrupt-id here. We DROP it and instead build the
        # ``ask_human`` chunk once, AFTER the stream, from the graph's checkpoint
        # state (which carries every pending interrupt AND its id — required to
        # resume with Command(resume={id: ...}); see ``_ask_human_chunk_from_state``).
        if "__interrupt__" in data:
            return None
        node = data.get("model") or data.get("agent")
        if not isinstance(node, dict):
            return None
        out: list[dict[str, Any]] = []
        for msg in node.get("messages", []) or []:
            if isinstance(msg, AIMessage):
                for tc in msg.tool_calls or []:
                    name = (
                        tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                    )
                    args = (
                        tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
                    )
                    out.append(
                        {
                            "type": "tool",
                            "id": _tool_call_id(tc),
                            "tool_name": name,
                            "tool_start": True,
                            "content": _with_scope(name, args, scope),
                            "error": False,
                        }
                    )
        return out or None

    if mode == "messages":
        chunk = data[0] if isinstance(data, (list, tuple)) else data

        # Tool RESULT: a completed ToolMessage flowing back from a tool node.
        if isinstance(chunk, ToolMessage):
            content = chunk.content
            if isinstance(content, str) and content:
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    pass  # leave as raw string
            return {
                "type": "tool",
                "id": chunk.tool_call_id,
                "tool_name": chunk.name,
                "tool_start": False,
                "content": content,
                "error": getattr(chunk, "status", None) == "error",
            }

        if not isinstance(chunk, AIMessageChunk):
            return None

        content = chunk.content
        # String content = plain answer text.
        if isinstance(content, str):
            return {"type": "text", "content": content} if content else None

        # List content = structured blocks. Two provider shapes:
        #  - Converse (Claude): {"type":"reasoning_content","reasoning_content":{text}}
        #  - GPT/Responses v1:  {"type":"reasoning","summary":[{"type":"summary_text",
        #                        "text":…}, …]} (summary is a LIST; streaming deltas
        #                        may carry a single {"text":…} or an "index"+text).
        if isinstance(content, list):
            chunks: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        chunks.append({"type": "text", "content": text})
                elif btype in ("reasoning_content", "reasoning"):
                    text = _reasoning_text(block)
                    if text:
                        chunks.append({"type": "think", "content": text})
            if len(chunks) == 1:
                return chunks[0]
            return chunks or None

    return None


def _reasoning_text(block: dict[str, Any]) -> str:
    """Pull reasoning text out of a content block across provider shapes.

    Converse: ``reasoning_content`` is a dict/str with ``text``. GPT Responses v1:
    ``summary`` is a LIST of ``{text}`` items (concatenate them); a streaming delta
    may instead put the partial text under ``summary`` as a dict or a bare string,
    or directly on the block's ``text``.
    """
    # Converse
    rc = block.get("reasoning_content")
    if isinstance(rc, dict):
        return rc.get("text", "") or ""
    if isinstance(rc, str):
        return rc
    # GPT Responses v1: summary list / dict / str
    summ = block.get("summary")
    if isinstance(summ, list):
        return "".join(
            s.get("text", "")
            for s in summ
            if isinstance(s, dict) and s.get("text")
        )
    if isinstance(summ, dict):
        return summ.get("text", "") or ""
    if isinstance(summ, str):
        return summ
    # Fallback: some delta frames carry the partial reasoning on `text`.
    t = block.get("text")
    return t if isinstance(t, str) else ""


def _sse(obj: dict[str, Any]) -> str:
    """Frame one chunk dict as an SSE ``data:`` line (Sparky wire format)."""
    return f"data: {json.dumps(obj)}\n\n"


def _error_chunk(error_code: str, message: str, details: dict | None = None) -> dict:
    chunk = {"type": "error", "error_code": error_code, "message": message}
    if details is not None:
        chunk["details"] = details
    return chunk


# --- dependency construction (container vs test) -----------------------------


def build_deps(config: Any = None):
    """Construct the live boto3 clients + config for a deployment.

    Kept separate from the request logic so the container builds real clients from
    the execution role while tests inject fakes. Returns ``(chat_config,
    consumption_config, clients)``.
    """
    import boto3

    from chat.config import ChatConfig
    from consumption_mcp.tools import ConsumptionConfig

    chat_config = config or ChatConfig.from_env()
    region = chat_config.region
    consumption_config = ConsumptionConfig(
        bundle_bucket=chat_config.bundle_bucket,
        vector_bucket=chat_config.vector_bucket,
        vector_index=chat_config.vector_index,
        registry_table=chat_config.registry_table,
    )
    clients = {
        "s3": boto3.client("s3", region_name=region),
        "s3vectors": boto3.client("s3vectors", region_name=region),
        "bedrock_runtime": boto3.client("bedrock-runtime", region_name=region),
        "ddb": boto3.resource("dynamodb", region_name=region).Table(
            consumption_config.registry_table
        ),
    }
    # Athena client for the optional read-only SQL tool — only built when the
    # deploy flag is on (else the role has no Glue/Athena grants anyway).
    if chat_config.sql_enabled:
        clients["athena"] = boto3.client("athena", region_name=region)
    # Redshift Data API client for SQL on a Redshift-scoped conversation — needs
    # BOTH deploy flags (the role only carries redshift-data/secret grants when
    # var.enable_redshift AND var.enable_chat_sql are set).
    if chat_config.sql_enabled and chat_config.redshift_enabled:
        clients["redshift_data"] = boto3.client("redshift-data", region_name=region)
    return chat_config, consumption_config, clients


def make_index_writer(chat_config: Any, ddb_client: Any = None) -> Callable:
    """Return an ``index_writer(**kwargs)`` that upserts the conversation index row.

    Uses a LOW-LEVEL dynamodb client (the threads writer speaks the wire format,
    like the Control API). Best-effort inside; a failed write never breaks a run.
    """
    import datetime

    import boto3

    from chat.threads import touch_thread

    ddb_client = ddb_client or boto3.client("dynamodb", region_name=chat_config.region)

    def index_writer(*, user_sub, thread_id, title, model, effort, dataset_scope) -> None:
        touch_thread(
            ddb_client,
            threads_table=chat_config.threads_table,
            user_sub=user_sub,
            thread_id=thread_id,
            title=title or "",
            model=model,
            effort=effort,
            dataset_scope=dataset_scope,
            now_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

    return index_writer


def make_agent_factory(chat_config: Any, consumption_config: Any, clients: dict) -> Callable:
    """Return ``build_agent(model, effort, scope, checkpointer, features=…) -> CompiledStateGraph``.

    A factory (not a singleton) because the model is PINNED PER CONVERSATION: each
    run builds a graph for its resolved model. Cheap enough per run; the heavy
    state (checkpoints) lives in DynamoDB, not the graph object.

    ``features`` (default empty) is the per-run set of opted-in optional tools
    (e.g. ``{"sql"}`` from the composer's "+" menu). The read-only SQL tool is
    added only when the deploy flag is on AND the run opted in — see
    :func:`make_agent_tools_with_features`.

    NOTE: this returns the RAW compiled LangGraph graph (no AG-UI wrapper) —
    ``stream_run`` drives ``.astream`` on it directly.
    """
    from chat.ask_human import make_ask_human_tool
    from chat.ask_human_middleware import AskHumanMiddleware
    from chat.charts import make_chart_tool
    from chat.config import build_chat_model
    from chat.graph import (
        SYSTEM_PROMPT_WITH_SQL,
        SYSTEM_PROMPT_WITH_SQL_REDSHIFT,
        build_graph,
    )
    from chat.sql import AthenaSQL, RedshiftDataSQL, make_sql_tool
    from chat.tools import build_consumption_tools, make_agent_tools

    # The Athena / Redshift Data API clients are present only when their deploy
    # flags are on (build_deps).
    athena_client = clients.pop("athena", None)
    redshift_data_client = clients.pop("redshift_data", None)
    tools_impl = build_consumption_tools(config=consumption_config, **clients)

    def _sql_engine() -> AthenaSQL | None:
        if not (chat_config.sql_enabled and athena_client is not None):
            return None
        return AthenaSQL(
            athena=athena_client,
            catalog=chat_config.athena_catalog,
            output_location=chat_config.athena_output,
            workgroup=chat_config.athena_workgroup,
            max_rows=chat_config.sql_max_rows,
        )

    def _redshift_sql_engine(source: dict) -> RedshiftDataSQL | None:
        """A Data API engine pinned to the scoped mapping's connection, or None.

        None when Redshift SQL isn't deploy-enabled (no client) or the mapping's
        descriptor is incomplete (a legacy db-only row) — the caller then wires NO
        SQL tool for the run, never the Athena engine (wrong backend).
        """
        if not (chat_config.sql_enabled and redshift_data_client is not None):
            return None
        if not source.get("redshift_database"):
            return None
        try:
            return RedshiftDataSQL(
                data=redshift_data_client,
                database=source["redshift_database"],
                cluster_identifier=source.get("cluster_identifier") or None,
                workgroup_name=source.get("workgroup_name") or None,
                secret_arn=source.get("secret_arn") or None,
                max_rows=chat_config.sql_max_rows,
            )
        except ValueError:
            # No target/secret in the descriptor -> unconnectable mapping.
            return None

    def _sql_scope(scope: dict | None) -> dict | None:
        """Enrich a dataset scope with its registry mapping's source facts.

        The UI-sent scope carries only ``{data_domain, dataset}``. From the
        mapping row we add: ``glue_database`` (a dataset id need not equal its
        Glue database name — run_sql's default database for unqualified names
        must be the real Glue DB) and ``source`` (the ``{type, ...config}``
        descriptor, so the SQL tool dispatches to the right backend — a Redshift
        dataset must not be queried through Athena). Best-effort: on any lookup
        failure the scope passes through unenriched, which yields the historical
        Athena default (the model is told to qualify tables anyway).
        """
        if not scope:
            return scope
        try:
            resp = tools_impl.ddb.get_item(
                Key={
                    "pk": f"DOMAIN#{scope['data_domain']}",
                    "sk": f"DATASET#{scope['dataset']}",
                }
            )
            item = resp.get("Item") or {}
            enriched = dict(scope)
            if item.get("glue_database"):
                enriched["glue_database"] = item["glue_database"]
            # The resource-API Table returns the source map as a plain dict.
            if isinstance(item.get("source"), dict):
                enriched["source"] = item["source"]
            return enriched
        except Exception:  # noqa: BLE001 - fall back to the unenriched scope
            pass
        return scope

    def build_agent(
        model: str,
        effort: str,
        scope: dict | None,
        checkpointer: Any,
        features: set[str] | None = None,
    ):
        chat_model = build_chat_model(chat_config, model, effort)
        agent_tools = make_agent_tools(tools_impl, dataset_scope=scope)
        # render_chart is ALWAYS available (no deploy flag, no per-run opt-in): it
        # does no server work — the model writes chart "script code" that the UI
        # runs in a sandboxed frame — so there's nothing to gate. Its authoring
        # contract lives in the tool description; the SYSTEM_PROMPT's <charts> block
        # covers when to use it, so the base prompt (no SQL) already knows about it.
        agent_tools = [*agent_tools, make_chart_tool()]
        # ask_human is ALWAYS available (like render_chart, no deploy flag / opt-in):
        # the tool is inert and AskHumanMiddleware owns the interrupt. The <asking_the_user>
        # prompt block covers when to use it, so the base prompt already knows about it.
        agent_tools = [*agent_tools, make_ask_human_tool()]
        # Optional read-only SQL: both the deploy flag (engine present) and the
        # per-run opt-in (features) are required. system_prompt gains a block so
        # the model knows the tool exists this turn. The ENGINE is picked by the
        # @-scoped dataset's source descriptor: a Redshift-backed dataset gets a
        # Data API engine pinned to its mapping's connection; anything else (no
        # scope, glue scope, legacy row) gets the catalog-wide Athena engine. A
        # Redshift scope on a deployment without Redshift enabled gets NO SQL
        # tool — running the dataset's queries through Athena would silently hit
        # the wrong backend/dialect.
        prompt = None
        if features and "sql" in features:
            scoped = _sql_scope(scope)
            scoped_source = (scoped or {}).get("source") or {}
            if scoped_source.get("type") == "redshift":
                engine = _redshift_sql_engine(scoped_source)
                if engine is not None:
                    agent_tools = [
                        *agent_tools,
                        make_sql_tool(engine, dataset_scope=scoped),
                    ]
                    prompt = SYSTEM_PROMPT_WITH_SQL_REDSHIFT
            else:
                engine = _sql_engine()
                if engine is not None:
                    agent_tools = [
                        *agent_tools,
                        make_sql_tool(engine, dataset_scope=scoped),
                    ]
                    prompt = SYSTEM_PROMPT_WITH_SQL
        # AskHumanMiddleware owns the human-in-the-loop interrupt for ask_human.
        middleware = [AskHumanMiddleware()]
        if prompt is not None:
            return build_graph(
                chat_model, agent_tools, checkpointer,
                system_prompt=prompt, middleware=middleware,
            )
        return build_graph(
            chat_model, agent_tools, checkpointer, middleware=middleware
        )

    return build_agent


def make_checkpointer(chat_config: Any):
    """Build the DynamoDBSaver checkpointer from config (PK/SK schema, optional TTL)."""
    from langgraph_checkpoint_aws import DynamoDBSaver

    return DynamoDBSaver(
        table_name=chat_config.checkpoint_table,
        region_name=chat_config.region,
        ttl_seconds=chat_config.checkpoint_ttl_seconds,
    )


# --- history read / delete (Sparky-style, over the checkpointer) -------------


def _messages_to_turns(messages: list[Any]) -> list[dict[str, Any]]:
    """Fold a LangGraph message list into the front end's chatTurns shape.

    Each user message opens a turn; assistant text + tool messages fill its
    ``aiMessage`` event list (the same typed-chunk shape the live stream emits),
    capped with an ``{"end": true}`` marker so the renderer treats it as complete.
    Restored turns carry text + tool blocks only — enough to re-read a past
    conversation; live turns get the full reasoning stream.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    def _ai_events(content: Any) -> list[dict[str, Any]]:
        """Turn a persisted AIMessage's content into ordered think/text events.

        The checkpoint stores reasoning in the message content (Converse's
        ``reasoning_content`` or GPT's ``reasoning`` summary blocks), so a reloaded
        turn rebuilds the SAME think/text stream the live run emitted — otherwise
        reasoning silently vanishes on history load. Reuses ``_reasoning_text`` so
        both provider shapes are handled identically to the live path.
        """
        if isinstance(content, str):
            return [{"type": "text", "content": content}] if content else []
        events: list[dict[str, Any]] = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text" and b.get("text"):
                    events.append({"type": "text", "content": b["text"]})
                elif btype in ("reasoning_content", "reasoning"):
                    txt = _reasoning_text(b)
                    if txt:
                        events.append({"type": "think", "content": txt})
        return events

    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    turn_scope: dict[str, str] | None = None
    for msg in messages:
        if isinstance(msg, HumanMessage):
            if current:
                current["aiMessage"].append({"end": True})
                turns.append(current)
            # Recover the turn's dataset scope from the stored [Scope: …] preamble
            # BEFORE stripping it, so this turn's tool events can name the dataset
            # (the stored tool_call args lack it — see _with_scope).
            turn_scope = parse_scope_prefix(_text(msg.content))
            current = {
                "id": f"turn_{len(turns)}",
                # Strip the injected [Scope: …] preamble so the bubble shows the
                # user's original text on reload.
                "userMessage": strip_scope_prefix(_text(msg.content)),
                "aiMessage": [],
            }
        elif isinstance(msg, AIMessage) and current is not None:
            # Reasoning + answer text in content order (reasoning first), then the
            # tool starts this message issued — mirrors the live stream order so a
            # reloaded turn renders identically (reasoning included).
            current["aiMessage"].extend(_ai_events(msg.content))
            for tc in msg.tool_calls or []:
                name = (
                    tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                )
                args = (
                    tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
                )
                current["aiMessage"].append(
                    {
                        "type": "tool",
                        "id": _tool_call_id(tc),
                        "tool_name": name,
                        "tool_start": True,
                        "content": _with_scope(name, args, turn_scope),
                    }
                )
        elif isinstance(msg, ToolMessage) and current is not None:
            current["aiMessage"].append(
                {
                    "type": "tool",
                    "id": msg.tool_call_id,
                    "tool_name": msg.name,
                    "tool_start": False,
                    "content": msg.content,
                    "error": getattr(msg, "status", None) == "error",
                }
            )
    if current:
        current["aiMessage"].append({"end": True})
        turns.append(current)
    return turns


def read_history(
    build_agent: Callable,
    checkpointer: Any,
    internal_thread_id: str,
    *,
    drop_inflight: bool = False,
) -> dict[str, Any]:
    """Return ``{"history": [chatTurns]}`` for a conversation (empty if none).

    Builds a throwaway graph bound to the checkpointer and reads its persisted
    state. Model/effort don't matter for a read, so use safe defaults.

    ``drop_inflight`` (set when a LIVE run is active for this thread) drops the
    trailing in-flight turn so the ``resume`` path can render it fresh from the
    live buffer instead of showing it from the checkpoint AND replaying it (which
    would duplicate). The in-flight turn is one that has NOT committed a final
    answer yet — it may already carry checkpointed reasoning or a tool call
    (LangGraph checkpoints at each node boundary, so a turn stopped mid-tool has
    an AIMessage(tool_calls) with no text), but no assistant TEXT. A turn that DID
    produce answer text is a completed turn and is always kept — this also avoids
    dropping a prior completed turn when the active run's own human message hasn't
    been checkpointed yet.
    """
    graph = build_agent("us.anthropic.claude-opus-4-8", "high", None, checkpointer)
    cfg = {"configurable": {"thread_id": internal_thread_id}}
    state = graph.get_state(cfg)
    messages = (state.values or {}).get("messages", []) if state else []
    turns = _messages_to_turns(messages)
    if drop_inflight and turns:
        last = turns[-1]
        # No committed answer text → this is the turn the live run is still
        # producing (empty, reasoning-only, or stopped mid-tool). resume() rebuilds
        # it in full from the buffer, so drop it here to avoid a duplicate.
        has_answer_text = any(
            e.get("type") == "text" for e in last.get("aiMessage", [])
        )
        if not has_answer_text:
            turns = turns[:-1]
    out: dict[str, Any] = {"history": turns}
    # If the graph is PAUSED at an ask_human interrupt, surface it so a page reload
    # re-renders the QA form and the user can still answer (the interrupt is durable
    # in the checkpoint; nothing was lost). Not dropped by drop_inflight: the paused
    # turn IS the one awaiting input, and answer_human resumes it in place.
    pending_ask = _ask_human_chunk_from_state(graph, cfg)
    if pending_ask is not None:
        out["pending_ask"] = pending_ask
    return out


def delete_history(checkpointer: Any, internal_thread_id: str) -> dict[str, Any]:
    """Purge a conversation's checkpoints (used by the ``delete_history`` type)."""
    checkpointer.delete_thread(internal_thread_id)
    return {"type": "delete_history", "deleted": True}


# --- the streaming run -------------------------------------------------------


async def _produce_run_chunks(
    input_data: dict[str, Any],
    internal_id: str,
    *,
    chat_config: Any,
    build_agent: Callable,
    checkpointer: Any,
    index_writer: Callable | None,
    user_sub: str,
    client_thread_id: str,
    prompt: str,
    on_graph: Callable[[Any], None],
    resume_answers: Any = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Drive one agent turn and YIELD raw typed chunk dicts (no SSE framing).

    This is the run BODY: resolve/validate the model, upsert the index row, drive
    ``graph.astream``, translate parts to typed chunks, and always finish with a
    terminal ``{"end": …}`` (with token_stats + checkpoint_id) — or an ``error``
    chunk then ``end`` on failure. It does NOT handle cancellation: the live-stream
    runner catches that and invokes the checkpoint reconcile (via ``on_cancel``).
    ``on_graph(graph)`` is called once the graph is built, so the caller can wire the
    reconcile against it.

    ``resume_answers`` (set on the ``answer_human`` path) makes this RESUME a graph
    paused at an ``ask_human`` interrupt instead of starting a fresh turn: the graph
    input becomes ``Command(resume={interrupt_id: answers})`` (built by
    :func:`_build_resume_map` from the pending interrupts) rather than a new user
    message, and the run continues from the checkpoint. If the graph is NOT paused on
    any interrupt, the run ends cleanly instead (no phantom turn). The prompt is
    ignored in that case.
    """
    from langchain_core.messages import AIMessageChunk

    from chat.sql import normalize_features

    token_stats: dict[str, int] = {}
    cfg = {
        "configurable": {"thread_id": internal_id},
        "recursion_limit": RECURSION_LIMIT,
    }
    graph = None
    try:
        model, effort = chat_config.resolve_model_effort(
            input_data.get("model_id") or input_data.get("model"),
            input_data.get("effort"),
        )
        scope = extract_scope(input_data)
        # Per-run opted-in optional tools (composer "+" menu). Normalized to the
        # server-recognized subset; the factory further gates each on its deploy
        # flag, so an unknown/forbidden feature string is simply ignored here.
        features = normalize_features(input_data.get("features"))

        # Best-effort conversation-index upsert (sidebar). Only on a fresh turn — a
        # RESUME must NOT touch the index: its (model, effort) are the defaults (the
        # answer_human request doesn't carry them), and touch_thread writes those
        # every turn (SET, not if_not_exists), so writing here would overwrite the
        # conversation's real pinned model/effort with defaults. After validation so
        # a rejected (model, effort) never seeds an index row; never fatal.
        if index_writer is not None and resume_answers is None:
            index_writer(
                user_sub=user_sub,
                thread_id=client_thread_id,
                title=prompt,
                model=model,
                effort=effort,
                dataset_scope=scope,
            )

        graph = build_agent(model, effort, scope, checkpointer, features=features)
        on_graph(graph)

        # Resume a paused ask_human interrupt with the user's answers, OR start a
        # fresh turn from the user message. Command(resume=…) continues the graph
        # from its checkpoint; a message dict starts a new turn (scope injected on
        # the user message, keeping the system prompt a cacheable prefix).
        if resume_answers is not None:
            from langgraph.types import Command

            # Build the id-keyed resume map from the graph's CURRENTLY pending
            # interrupts. langgraph REQUIRES the {interrupt_id: value} map form when
            # >1 interrupt is pending (a bare value raises RuntimeError); the map
            # form also works for the single case. If the graph is NOT paused on any
            # interrupt (double-submit, stale UI, already-resumed run), do NOT feed a
            # resume — that would inject a phantom fresh turn — just end cleanly.
            resume_map = _build_resume_map(graph, cfg, resume_answers)
            if resume_map is None:
                yield {"end": True}
                return
            graph_input: Any = Command(resume=resume_map)
        else:
            graph_input = {
                "messages": [
                    {"role": "user", "content": scoped_prompt(prompt, scope)}
                ]
            }

        seen_text = False
        async for mode, data in graph.astream(
            graph_input,
            cfg,
            stream_mode=["messages", "updates"],
        ):
            # Accumulate token usage from streamed chunks that carry it.
            if mode == "messages":
                chunk = data[0] if isinstance(data, (list, tuple)) else data
                if isinstance(chunk, AIMessageChunk) and chunk.usage_metadata:
                    u = chunk.usage_metadata
                    details = u.get("input_token_details", {}) or {}
                    token_stats["input_tokens"] = token_stats.get("input_tokens", 0) + (
                        u.get("input_tokens") or 0
                    )
                    token_stats["output_tokens"] = token_stats.get("output_tokens", 0) + (
                        u.get("output_tokens") or 0
                    )
                    token_stats["cache_read_input_tokens"] = token_stats.get(
                        "cache_read_input_tokens", 0
                    ) + (details.get("cache_read") or 0)
                    token_stats["cache_creation_input_tokens"] = token_stats.get(
                        "cache_creation_input_tokens", 0
                    ) + (details.get("cache_creation") or 0)

            produced = process_stream_data(mode, data, scope)
            if not produced:
                continue
            for chunk in produced if isinstance(produced, list) else [produced]:
                # Strip leading newlines some models emit before real text.
                if not seen_text and chunk.get("type") == "text":
                    stripped = chunk["content"].lstrip("\n")
                    if not stripped:
                        continue
                    chunk = {**chunk, "content": stripped}
                    seen_text = True
                elif chunk.get("type") in ("text", "think"):
                    seen_text = True
                yield chunk

        # If the stream ended because the graph PAUSED on ask_human interrupt(s),
        # emit the consolidated QA chunk (built from checkpoint state so it carries
        # every pending interrupt + its id). Done here — after the loop, before the
        # end marker — so the UI gets the form and the run cleanly ends (a later
        # answer_human resumes from the checkpoint).
        if graph is not None:
            ask = _ask_human_chunk_from_state(graph, cfg)
            if ask is not None:
                yield ask
    except asyncio.CancelledError:
        # Explicit stop — let the live-stream runner's on_cancel do the checkpoint
        # repair + cancelled end marker; just propagate.
        raise
    except Exception as exc:  # noqa: BLE001 - surface ANY failure as an error chunk
        yield _error_chunk("agent_error", str(exc))

    # Terminal end marker (always), with token stats + the checkpoint id.
    end_marker: dict[str, Any] = {"end": True}
    if token_stats:
        end_marker["token_stats"] = token_stats
    try:
        if graph is not None:
            state = graph.get_state(cfg)
            cp_id = (
                state.config.get("configurable", {}).get("checkpoint_id")
                if state and state.config
                else None
            )
            if cp_id:
                end_marker["checkpoint_id"] = cp_id
    except Exception:  # noqa: BLE001 - the end marker is best-effort metadata
        pass
    yield end_marker


def _cancel_end_marker() -> dict[str, Any]:
    return {"end": True, "cancelled": True}


async def stream_run(
    input_data: dict[str, Any],
    user_sub: str,
    client_thread_id: str,
    *,
    chat_config: Any,
    build_agent: Callable,
    checkpointer: Any,
    index_writer: Callable | None = None,
) -> AsyncGenerator[str, None]:
    """SSE frames for a ``send`` turn — as a DETACHED run the client subscribes to.

    The run itself is a background task in the live-stream registry, NOT this HTTP
    generator: the turn keeps going if the browser disconnects, and a later
    ``resume`` re-subscribes (replay + live). This generator starts (or joins) that
    run and relays its chunks as SSE. On cancellation the registry runner does the
    checkpoint repair (chat.cancellation) — a dropped connection no longer cancels;
    only an explicit ``stop`` (see :func:`stop_run`) does.
    """
    from chat.cancellation import reconcile_cancelled_turn

    internal_id = namespaced_thread_id(user_sub, client_thread_id)
    prompt = input_data.get("prompt", "")

    # HARD GUARD: never invoke the model on an empty prompt. An empty/whitespace
    # prompt reaching the send path (e.g. a resume/stop request that fell through
    # to the default branch, or a stray client send) must NOT start a turn — that's
    # what produced phantom "accidental send" replies. Emit a clean end and stop.
    if not prompt.strip():
        yield _sse({"end": True})
        return

    # If a live run already exists for this thread (e.g. a double-send), subscribe
    # to it rather than starting a second — start() returns the existing stream.
    graph_holder: dict[str, Any] = {"graph": None}
    cfg = {"configurable": {"thread_id": internal_id}, "recursion_limit": RECURSION_LIMIT}

    def _on_cancel() -> list[dict[str, Any]]:
        # Repair the checkpoint's dangling tool calls (detached write) and emit the
        # cancelled tool chunks + a cancelled end marker into the buffer.
        graph = graph_holder["graph"]
        out: list[dict[str, Any]] = []
        if graph is not None:
            out.extend(reconcile_cancelled_turn(graph, cfg))
        out.append(_cancel_end_marker())
        return out

    source = _produce_run_chunks(
        input_data,
        internal_id,
        chat_config=chat_config,
        build_agent=build_agent,
        checkpointer=checkpointer,
        index_writer=index_writer,
        user_sub=user_sub,
        client_thread_id=client_thread_id,
        prompt=prompt,
        on_graph=lambda g: graph_holder.__setitem__("graph", g),
    )
    live_streams.start(internal_id, source, user_message=prompt, on_cancel=_on_cancel)

    async for chunk in live_streams.subscribe(internal_id):
        yield _sse(chunk)


async def answer_run(
    input_data: dict[str, Any],
    user_sub: str,
    client_thread_id: str,
    *,
    chat_config: Any,
    build_agent: Callable,
    checkpointer: Any,
    index_writer: Callable | None = None,
) -> AsyncGenerator[str, None]:
    """SSE frames for an ``answer_human`` turn — resume a paused ask_human interrupt.

    The client sends the user's answers (``{answers: [...], model_id, effort,
    features, dataset_scope}``); this drives the graph forward with
    ``Command(resume={interrupt_id: answers})`` from its checkpoint. It carries the
    conversation's model/effort/features/scope so ``build_agent`` rebuilds the SAME
    graph the interrupt paused on (the model is pinned + its checkpoint isn't
    portable across models). Runs as a detached live-stream run exactly like
    :func:`stream_run` (same subscribe / stop / disconnect semantics), differing only
    in that it feeds resume answers instead of a fresh user message. The ``answers``
    are validated + normalized downstream (chat.ask_human.normalize_answers).
    """
    from chat.cancellation import reconcile_cancelled_turn

    internal_id = namespaced_thread_id(user_sub, client_thread_id)
    answers = input_data.get("answers")
    # Nothing to resume with → clean end (mirrors the empty-prompt guard on send).
    if answers is None:
        yield _sse({"end": True})
        return

    graph_holder: dict[str, Any] = {"graph": None}
    cfg = {"configurable": {"thread_id": internal_id}, "recursion_limit": RECURSION_LIMIT}

    def _on_cancel() -> list[dict[str, Any]]:
        graph = graph_holder["graph"]
        out: list[dict[str, Any]] = []
        if graph is not None:
            out.extend(reconcile_cancelled_turn(graph, cfg))
        out.append(_cancel_end_marker())
        return out

    source = _produce_run_chunks(
        input_data,
        internal_id,
        chat_config=chat_config,
        build_agent=build_agent,
        checkpointer=checkpointer,
        index_writer=index_writer,
        user_sub=user_sub,
        client_thread_id=client_thread_id,
        prompt="",
        on_graph=lambda g: graph_holder.__setitem__("graph", g),
        resume_answers=answers,
    )
    # user_message stays empty: this turn's question bubble already rendered on the
    # original send; resume only continues the assistant's answer.
    live_streams.start(internal_id, source, user_message="", on_cancel=_on_cancel)

    async for chunk in live_streams.subscribe(internal_id):
        yield _sse(chunk)


async def resume_run(user_sub: str, client_thread_id: str) -> AsyncGenerator[str, None]:
    """SSE frames for a ``resume``: re-subscribe to an in-flight run (replay + live).

    Yields the buffered backlog (what the client missed) then live chunks until the
    run ends. If no run is active for the thread, emits a single ``no_active_stream``
    marker so the client falls back to loading history from the checkpointer.
    """
    internal_id = namespaced_thread_id(user_sub, client_thread_id)
    if not live_streams.is_active(internal_id):
        yield _sse({"type": "no_active_stream"})
        yield _sse({"end": True})
        return
    # Lead with the in-flight turn's user message so the client renders the whole
    # turn (question + streaming answer) from the buffer — read_history dropped this
    # turn (drop_inflight) precisely so it's rendered here, not duplicated.
    stream = live_streams.get(internal_id)
    if stream is not None and stream.user_message:
        yield _sse({"type": "user_message", "content": stream.user_message})
    async for chunk in live_streams.subscribe(internal_id):
        yield _sse(chunk)


async def stop_run(user_sub: str, client_thread_id: str) -> dict[str, Any]:
    """Explicit stop: cancel the thread's live run (triggers the checkpoint repair)."""
    internal_id = namespaced_thread_id(user_sub, client_thread_id)
    cancelled = await live_streams.cancel(internal_id)
    return {"type": "stop", "stopped": bool(cancelled)}


async def stream_prepare() -> AsyncGenerator[str, None]:
    """Keep-warm short-circuit: emit only the ``end`` marker, no LLM call.

    A real (minimal) invocation to the same runtimeSessionId resets the AgentCore
    idle timer without spending a model call. The client fires this optionally;
    it must not create a turn or an index row.
    """
    yield _sse({"end": True})


# --- app ---------------------------------------------------------------------


def build_app(
    chat_config: Any = None,
    build_agent: Callable | None = None,
    index_writer: Callable | None = None,
):
    """Build the FastAPI app wired to live deps. Requires fastapi + the stack.

    ``chat_config`` / ``build_agent`` / ``index_writer`` are injectable for tests;
    in the container they default to env-resolved live clients.
    """
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse

    if build_agent is None or chat_config is None:
        chat_config, consumption_config, clients = build_deps(chat_config)
        build_agent = make_agent_factory(chat_config, consumption_config, clients)
    if index_writer is None:
        index_writer = make_index_writer(chat_config)

    checkpointer = make_checkpointer(chat_config)

    app = FastAPI(title="OKF Chat Agent")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        **CORS_HEADERS,
    }

    @app.options("/invocations")
    async def _preflight():
        return JSONResponse({"ok": True}, headers=CORS_HEADERS)

    @app.get("/ping")
    async def _ping():
        return {"status": "Healthy"}

    def _stream_error(message: str, code: str = "bad_request"):
        async def gen() -> AsyncGenerator[str, None]:
            yield _sse(_error_chunk(code, message))
            yield _sse({"end": True})

        return StreamingResponse(gen(), media_type="text/event-stream", headers=sse_headers)

    @app.post("/invocations")
    async def _invocations(request: Request):
        session_id = request.headers.get(SESSION_HEADER)
        auth = request.headers.get("Authorization") or request.headers.get("authorization")
        if not session_id or not auth:
            return _stream_error("missing session id or Authorization header", "auth_error")
        try:
            user_sub = decode_sub(auth)
            body = await request.json()
        except (MissingHeader, ValueError, json.JSONDecodeError) as exc:
            return _stream_error(f"bad request: {exc}", "auth_error")

        input_data = body.get("input") if isinstance(body, dict) else None
        if not isinstance(input_data, dict):
            return _stream_error("missing 'input' object")
        req_type = input_data.get("type", "send")

        # The session-id header IS the client thread id (browser sets both equal).
        client_thread_id = session_id
        internal_id = namespaced_thread_id(user_sub, client_thread_id)

        # Non-streaming control types return a JSON envelope (Sparky pattern).
        if req_type == "get_session_history":
            try:
                # When a live run is in flight, drop its half-turn from history —
                # the client's follow-up resume() renders it fresh from the buffer.
                data = read_history(
                    build_agent,
                    checkpointer,
                    internal_id,
                    drop_inflight=live_streams.is_active(internal_id),
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _error_chunk("internal_error", str(exc)), headers=CORS_HEADERS
                )
            return JSONResponse(data, headers=CORS_HEADERS)

        if req_type == "delete_history":
            try:
                data = delete_history(checkpointer, internal_id)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _error_chunk("internal_error", str(exc)), headers=CORS_HEADERS
                )
            return JSONResponse(data, headers=CORS_HEADERS)

        if req_type == "prepare":
            return StreamingResponse(
                stream_prepare(), media_type="text/event-stream", headers=sse_headers
            )

        # Resume: re-subscribe to an in-flight run (replay buffered chunks + live).
        # Used when the client returns to a thread whose turn is still streaming.
        if req_type == "resume":
            return StreamingResponse(
                resume_run(user_sub, client_thread_id),
                media_type="text/event-stream",
                headers=sse_headers,
            )

        # Answer a paused ask_human interrupt: resume the graph with the user's
        # answers (Command(resume=…)). Distinct from ``resume`` above, which merely
        # re-subscribes to a still-streaming run — this ADVANCES a paused graph.
        if req_type == "answer_human":
            gen = answer_run(
                input_data,
                user_sub,
                client_thread_id,
                chat_config=chat_config,
                build_agent=build_agent,
                checkpointer=checkpointer,
                index_writer=index_writer,
            )
            return StreamingResponse(gen, media_type="text/event-stream", headers=sse_headers)

        # Stop: explicitly cancel the thread's in-flight run (the ONLY thing that
        # cancels — a dropped connection no longer does). Triggers the checkpoint
        # repair. JSON envelope, not a stream.
        if req_type == "stop":
            try:
                data = await stop_run(user_sub, client_thread_id)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    _error_chunk("internal_error", str(exc)), headers=CORS_HEADERS
                )
            return JSONResponse(data, headers=CORS_HEADERS)

        # A streamed chat turn — ONLY for an explicit `send`. An unknown type must
        # NOT fall through to the model (that's how a resume/stop against an
        # out-of-sync backend produced phantom empty-prompt replies); reject it.
        if req_type != "send":
            return _stream_error(f"unknown request type: {req_type}", "bad_request")

        gen = stream_run(
            input_data,
            user_sub,
            client_thread_id,
            chat_config=chat_config,
            build_agent=build_agent,
            checkpointer=checkpointer,
            index_writer=index_writer,
        )
        return StreamingResponse(gen, media_type="text/event-stream", headers=sse_headers)

    return app


if __name__ == "__main__":  # pragma: no cover - exercised only in the container
    import uvicorn

    uvicorn.run(build_app(), host="0.0.0.0", port=8080)  # nosec B104
