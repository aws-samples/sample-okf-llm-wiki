"""Long-term chat memory — AgentCore Memory recall/write for the chat runtime.

Design (Roadmap §10): memory stores facts about the USER — stated preferences
(including meanings the user assigns to terms), personal context (name, role,
team), and question→computation bindings — never facts about the data (tables,
joins, metrics belong to the wiki). The managed service owns extraction and
consolidation (one CUSTOM user-preference strategy, configured with prompts +
the per-record ``metadata_schema`` in ``infra/durable/agent_memory.tf``); this
module owns the two deterministic touchpoints:

- **turn start** — :meth:`ChatMemory.recall`: semantic retrieval over the
  user's namespace, client-side TTL check (expired records are lazy-DELETED on
  recall — the provider cannot express per-record TTL natively), dataset
  scoping (pinned conversation → generic + that dataset's records, retrieved
  server-side via metadataFilters and INTERLEAVED so neither pool starves the
  other; unpinned → everything, semantic relevance scopes), then an injected
  marker-carrying HumanMessage (the steering/policy machinery keeps marked
  messages out of user bubbles and turn slices). Per-turn injections are
  REPLACED each turn — the server strips the previous turn's marker-carrying
  message from the checkpointed state before appending the fresh one, so
  deleting a record on the Memory page actually retracts it from ongoing
  conversations. Personal context (:meth:`recall_personal`) is the exception:
  fetched once, injected on the thread's first turn, carried by history.
- **turn end** — :meth:`ChatMemory.write_turn`: one ``create_event`` carrying
  the turn's user/assistant text plus a ``[[okf-harness]]`` annotation of what
  the harness OBSERVED (datasets touched, which governed tool resolved the
  turn) — so the extractor never has to infer the factual core, only judge
  acceptance and phrasing.

Structured record fields (``type``/``dataset``/``expires``) come from REAL
record metadata; ``okf_core.memory_records`` also parses the two drift
fallbacks (content-embedded metadata JSON, the legacy header line) and owns
the namespace derivation both services share.

A recalled binding is a HINT: the injection text tells the model to re-verify
any computation/metric against the wiki before relying on it (the
validation-at-use guard — memory must degrade to the status quo, never to a
wrong answer).

Per-user switch: a settings row on the chat threads table
(``pk=CHAT#<sub>, sk=SETTINGS#memory``, ``memory_enabled`` BOOL). A missing
row means the DEPLOY DEFAULT (``OKF_CHAT_MEMORY_DEFAULT_ON``): opt-out when
true (memory on until the user switches it off — the default), opt-in when
false (off until the user explicitly enables it). The row is written by the
Control API (the Memory page); this module only reads it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

from okf_core import chat_threads as ct
from okf_core.memory_records import (
    is_expired,
    memory_namespace,
    parse_record,
    safe_actor_id as _safe_id,
)

__all__ = [
    "ANNOTATION_PREFIX",
    "MEMORY_MARKER",
    "ChatMemory",
    "TurnObservation",
    "is_expired",
    "make_chat_memory",
    "parse_record",
]

log = logging.getLogger(__name__)

#: additional_kwargs key marking the injected recall message. Mirrored in
#: chat.steering._INJECTED_MARKER_KEYS (never opens a turn slice) and skipped
#: by the server's history rebuild (never renders as a user bubble).
MEMORY_MARKER = "okf_memory"

#: Prefix of the harness observation block appended to the turn event — the
#: extraction prompt (infra/durable/agent_memory.tf) treats double-bracket
#: blocks as trusted observation, not conversation.
ANNOTATION_PREFIX = "[[okf-harness]]"

#: The governed tools whose successful use makes a turn binding-eligible.
GOVERNED_TOOLS = frozenset({"run_computation", "query_metric"})

#: Retrieval breadth vs injection budget: retrieve wide (client-side dataset +
#: TTL filtering happens AFTER retrieval — the provider can't pre-filter on
#: header fields), inject narrow (the model needs the relevant few, not a dump).
TOP_K = 25
INJECT_MAX = 8

#: Query text cap (the API allows 10k chars; stay comfortably under).
QUERY_MAX_CHARS = 4000

#: Per-message text cap in the turn event — extraction feedstock, not a
#: transcript (the checkpointer owns transcripts). Applies to the USER text
#: and clarification answers too: one oversized paste must truncate, not blow
#: the CreateEvent payload limit and silently drop the whole event.
ANSWER_EXCERPT_CHARS = 4000

#: Answer citations: ``<c src="dd/ds/concept[,dd/ds/concept…]"></c>`` — the tag
#: contract lives in chat/graph.py's <citations> prompt block (the UI renders
#: the same shape). Only a fully-qualified wiki address carries a dataset; web
#: URLs and bare concept ids are skipped.
_CITE_SRC_RE = re.compile(r'<c\s+src="([^"]*)"')


def _cited_datasets(answer_text: str) -> list[str]:
    """``domain/dataset`` pairs the answer CITES, in first-citation order."""
    out: list[str] = []
    for m in _CITE_SRC_RE.finditer(answer_text or ""):
        for src in m.group(1).split(","):
            src = src.strip()
            if not src or src.startswith(("http://", "https://")):
                continue
            parts = src.split("/")
            if len(parts) < 3:
                continue
            key = f"{parts[0]}/{parts[1]}"
            if key not in out:
                out.append(key)
    return out


#: Cap on the annotation's curated-question line (curated questions are one
#: standalone sentence; anything near this is malformed).
CURATED_MAX_CHARS = 1000


def _render_annotation(
    datasets: list[str],
    governed: list[dict[str, Any]],
    cited: list[str] | None = None,
    curated: str = "",
) -> str:
    """The ``[[okf-harness]]`` block; ``''`` = nothing to annotate.

    Dataset resolution is two-level and EITHER/OR: a non-empty ``cited`` list
    (the answer's validated citations) replaces the touched list entirely —
    attribution beats exploration, a turn that grepped three datasets but
    answered from one must not offer the extractor three. The observed list
    is the fallback when no citation validates. ``resolved-by`` lines ride
    regardless: they are the binding evidence, and citations are docs-only.

    ``curated`` is the harness's context-resolved form of the user's question
    (the policy machinery's rolling rewrite) — extraction is asynchronous and
    its window over past events is the service's business, so an elliptical
    turn ("and last month?") may reach the extractor without its antecedent;
    this line is what makes the event self-contained. The user's own words
    stay in the USER message — meanings are extracted from those, never from
    the rewrite.
    """
    lines = [f"{ANNOTATION_PREFIX} harness observation (trusted, not conversation):"]
    if curated:
        lines.append(f"curated-question: {curated[:CURATED_MAX_CHARS]}")
    if cited:
        lines.append(f"datasets-cited: {json.dumps(cited)}")
    elif datasets:
        lines.append(f"datasets-touched: {json.dumps(datasets)}")
    for g in governed:
        tool, slug = g.get("tool") or "", g.get("slug") or ""
        ds, params = g.get("dataset") or "", g.get("params") or {}
        lines.append(
            f"resolved-by: {tool} slug={slug}"
            + (f" dataset={ds}" if ds else "")
            + (f" params={json.dumps(params, default=str)}" if params else "")
        )
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


class TurnObservation:
    """Collect what the harness OBSERVES during one turn's chunk stream.

    Fed every typed chunk from ``_produce_run_chunks``; remembers (a) every
    (domain, dataset) any tool call touched and (b) each governed tool call
    (run_computation / query_metric) whose result did not error — the factual
    core of a binding. Values come from tool args the server already folded
    the conversation scope into, so they are canonical ids, never conversation
    wording.
    """

    def __init__(self) -> None:
        self.datasets: list[str] = []
        self._pending: dict[str, dict[str, Any]] = {}
        self.governed: list[dict[str, Any]] = []

    def observe(self, chunk: dict[str, Any]) -> None:
        if chunk.get("type") != "tool":
            return
        if chunk.get("tool_start"):
            args = chunk.get("content")
            args = args if isinstance(args, dict) else {}
            dd, ds = args.get("data_domain"), args.get("dataset")
            if dd and ds:
                key = f"{dd}/{ds}"
                if key not in self.datasets:
                    self.datasets.append(key)
            name = chunk.get("tool_name") or ""
            if name in GOVERNED_TOOLS and chunk.get("id"):
                self._pending[chunk["id"]] = {
                    "tool": name,
                    "slug": str(args.get("name") or args.get("slug") or ""),
                    "params": {
                        k: v
                        for k, v in args.items()
                        if k not in ("data_domain", "dataset")
                    },
                    "dataset": f"{dd}/{ds}" if dd and ds else "",
                }
        else:
            pending = self._pending.pop(chunk.get("id") or "", None)
            if pending is not None and not chunk.get("error"):
                self.governed.append(pending)

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe state for the ask_human pause blob (``chat.threads``).

        A paused turn's invocation dies with its generator; without persisting
        the observation, governed calls made BEFORE the pause vanish from the
        resumed turn's annotation — and the extractor only trusts a binding
        when the annotation shows the governed tool resolving the turn.
        In-flight calls (``_pending``) are deliberately dropped: un-finished
        at pause time means no observed success to carry.
        """
        return {
            "datasets": list(self.datasets),
            "governed": [dict(g) for g in self.governed],
        }

    def restore(self, snapshot: dict[str, Any] | None) -> None:
        """Seed from a pause blob written by the paused invocation."""
        if not isinstance(snapshot, dict):
            return
        for key in snapshot.get("datasets") or []:
            if key and str(key) not in self.datasets:
                self.datasets.append(str(key))
        for g in snapshot.get("governed") or []:
            if isinstance(g, dict):
                self.governed.append(dict(g))

    def annotation(self) -> str:
        """The tools-only annotation (no citation level) — the write path
        composes through :meth:`ChatMemory.write_turn`, which layers the
        answer's validated citations on top; this stays for round-trip
        checks against the pause blob."""
        return _render_annotation(self.datasets, self.governed)


class ChatMemory:
    """The runtime's memory client — recall at turn start, write at turn end.

    ``client`` is a ``bedrock-agentcore`` data-plane client (tests inject a
    fake); ``ddb`` is a LOW-LEVEL DynamoDB client for the per-user settings
    row. Every public method is best-effort: memory must never fail a turn.
    """

    def __init__(
        self,
        client: Any,
        *,
        memory_id: str,
        ddb: Any = None,
        threads_table: str = "",
        namespace_prefix: str = "wiki",
        default_enabled: bool = True,
    ) -> None:
        self._client = client
        self._memory_id = memory_id
        self._ddb = ddb
        self._threads_table = threads_table
        self._prefix = namespace_prefix
        self._default_enabled = default_enabled

    def _namespace(self, user_sub: str) -> str:
        # The shared derivation (okf_core.memory_records) — same sanitized id
        # the events are written under; the Control API calls the same helper.
        return memory_namespace(user_sub, prefix=self._prefix)

    # -- per-user switch --------------------------------------------------

    def user_enabled(self, user_sub: str) -> bool:
        """The Memory page's per-user switch; missing row/attr = the DEPLOY
        DEFAULT (``default_enabled`` — opt-out when True, opt-in when False).

        A FAILED read also returns the default: an unreadable settings row
        must not silently flip a user's memory in either direction — it
        degrades to the deployment's stated policy, never its opposite.
        """
        if self._ddb is None or not self._threads_table:
            return self._default_enabled
        try:
            item = (
                self._ddb.get_item(
                    TableName=self._threads_table,
                    Key={
                        "pk": {"S": ct.thread_pk(user_sub)},
                        "sk": {"S": ct.MEMORY_SETTINGS_SK},
                    },
                ).get("Item")
                or {}
            )
            flag = item.get("memory_enabled") or {}
            if "BOOL" in flag:
                return bool(flag["BOOL"])
            return self._default_enabled
        except Exception:  # noqa: BLE001 - never fail the turn on settings
            log.warning("memory settings read failed (deploy default)", exc_info=True)
            return self._default_enabled

    # -- turn start: recall -------------------------------------------------

    def recall(
        self,
        *,
        user_sub: str,
        query: str,
        dataset_scope: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve → parse → lazy-expire → dataset-filter → top N records.

        ``dataset_scope`` pinned: TWO retrieval calls — generic records
        (``dataset NOT_EXISTS``) + that dataset's (``EQUALS_TO``) — because
        the filters AND together and "unset OR equals-pin" is an OR. Unpinned:
        one unfiltered call (semantic relevance scopes, and two datasets'
        bindings both matching is a disambiguation the model resolves with
        the user). The two pinned pools INTERLEAVE (dataset records first) up
        to ``INJECT_MAX``, so a pile of generic preferences can't starve the
        pin's records. Expired records are DELETED here — the lazy-TTL
        contract.
        """
        pin = (
            f"{dataset_scope['data_domain']}/{dataset_scope['dataset']}"
            if dataset_scope
            else ""
        )
        criteria: dict[str, Any] = {
            "searchQuery": (query or "")[:QUERY_MAX_CHARS],
            "topK": TOP_K,
        }
        calls: list[dict[str, Any]] = [criteria]
        if pin:
            calls = [
                {
                    **criteria,
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "dataset"},
                            "operator": "NOT_EXISTS",
                        }
                    ],
                },
                {
                    **criteria,
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "dataset"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": pin}},
                        }
                    ],
                },
            ]
        raws_per_call: list[list[dict[str, Any]]] = []
        seen_ids: set[str] = set()
        for search_criteria in calls:
            raws: list[dict[str, Any]] = []
            try:
                resp = self._client.retrieve_memory_records(
                    memoryId=self._memory_id,
                    namespace=self._namespace(user_sub),
                    searchCriteria=search_criteria,
                    # Explicit page size: the API's default page is 20, so
                    # topK=25 without maxResults silently retrieved only 20.
                    maxResults=TOP_K,
                )
            except Exception:  # noqa: BLE001 - recall is best-effort
                log.warning("memory recall failed (turn continues)", exc_info=True)
            else:
                for raw in resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []:
                    rid = raw.get("memoryRecordId") or ""
                    if rid and rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    raws.append(raw)
            raws_per_call.append(raws)

        kept_per_call: list[list[dict[str, Any]]] = []
        for raws in raws_per_call:
            kept: list[dict[str, Any]] = []
            for raw in raws:
                parsed = parse_record(raw)
                if not parsed["text"]:
                    continue
                if is_expired(parsed):
                    self._delete_record(parsed["id"])
                    continue
                # Personal context is injected ONCE per session
                # (recall_personal, first turn) — exclude it from per-turn
                # semantic recall, but ONLY when its type lives in REAL
                # metadata: a fallback-typed personal record can never match
                # recall_personal's server-side type filter, so skipping it
                # here too would make it invisible to EVERY recall path. It
                # degrades to per-turn injection instead. Client-side because
                # the filter operators have no NOT_EQUALS.
                if parsed["type"] == "personal" and parsed["type_from_metadata"]:
                    continue
                # Belt-and-braces for fallback records (no real metadata, so
                # the server-side dataset filter couldn't exclude them).
                if pin and parsed["dataset"] and parsed["dataset"] != pin:
                    continue
                kept.append(parsed)
            kept_per_call.append(kept)

        if len(kept_per_call) == 1:
            return kept_per_call[0][:INJECT_MAX]
        # Pinned: INTERLEAVE the two pools, dataset records first — a user
        # with >= INJECT_MAX generic preferences must not starve the pin
        # call's records (retrieved specifically for THIS conversation).
        from itertools import zip_longest

        generic, pinned = kept_per_call
        merged = [
            r
            for pair in zip_longest(pinned, generic)
            for r in pair
            if r is not None
        ]
        return merged[:INJECT_MAX]

    def recall_personal(self, *, user_sub: str) -> list[dict[str, Any]]:
        """The user's personal-context records (name, role, team) — fetched
        ONCE per session and injected on the FIRST turn only: they don't vary
        with the question, and the thread's checkpointed history carries the
        injection forward. Server-side ``type EQUALS_TO personal`` filter; the
        searchQuery is a fixed probe (retrieval requires one, relevance is
        irrelevant for a handful of identity records)."""
        try:
            resp = self._client.retrieve_memory_records(
                memoryId=self._memory_id,
                namespace=self._namespace(user_sub),
                searchCriteria={
                    "searchQuery": "who the user is: name, role, team",
                    "topK": INJECT_MAX,
                    "metadataFilters": [
                        {
                            "left": {"metadataKey": "type"},
                            "operator": "EQUALS_TO",
                            "right": {"metadataValue": {"stringValue": "personal"}},
                        }
                    ],
                },
                maxResults=INJECT_MAX,
            )
        except Exception:  # noqa: BLE001 - best-effort
            log.warning("personal-context recall failed (turn continues)", exc_info=True)
            return []
        kept: list[dict[str, Any]] = []
        for raw in resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []:
            parsed = parse_record(raw)
            if not parsed["text"]:
                continue
            if is_expired(parsed):
                # Same lazy-TTL contract as recall() — this IS a recall path.
                self._delete_record(parsed["id"])
                continue
            kept.append(parsed)
        return kept

    def _delete_record(self, record_id: str) -> None:
        if not record_id:
            return
        try:
            self._client.delete_memory_record(
                memoryId=self._memory_id, memoryRecordId=record_id
            )
        except Exception:  # noqa: BLE001 - hygiene, never fatal
            log.warning("expired-memory delete failed", exc_info=True)

    @staticmethod
    def injection_message(records: list[dict[str, Any]], *, marker: str = "recall") -> Any:
        """The marker-carrying HumanMessage the turn input appends.

        Framed as background the model must re-validate — a binding names a
        computation/metric the model still has to confirm against the wiki
        (describe it; only a VERIFIED artifact may answer). The marker keeps
        it out of user bubbles (server history rebuild) and out of steering's
        turn-slice accounting (chat.steering). The marker VALUE is the
        injection's lifecycle: ``"recall"`` messages are stripped from the
        checkpointed state and re-injected fresh each turn (so an edited or
        deleted record actually changes ongoing conversations); ``"personal"``
        is injected once on the first turn and carried by history.
        """
        from langchain_core.messages import HumanMessage

        lines = [
            "<system-reminder>Long-term memory about THIS user (preferences and "
            "previously accepted interpretations). Use it to shape defaults and "
            "shortcuts, and say so when you do (e.g. 'your usual view'). A "
            "remembered computation/metric is a HINT: describe it first and use "
            "it only if it is still available and VERIFIED — otherwise explore "
            "normally. Memory never overrides the wiki, the docs, or anything "
            "the user says in this conversation."
        ]
        for r in records:
            tag = (
                "binding"
                if r["type"] == "binding"
                else "about the user" if r["type"] == "personal" else "preference"
            )
            scope = f" [dataset {r['dataset']}]" if r["dataset"] else ""
            window = f" [valid until {r['expires']}]" if r["expires"] else ""
            lines.append(f"- ({tag}{scope}{window}) {r['text']}")
        lines.append("</system-reminder>")
        return HumanMessage(
            content="\n".join(lines),
            additional_kwargs={MEMORY_MARKER: marker},
        )

    # -- turn end: write ------------------------------------------------------

    def write_turn(
        self,
        *,
        user_sub: str,
        session_id: str,
        user_text: str,
        answer_text: str,
        observation: dict[str, Any] | None = None,
        thread_id: str = "",
        pin: str = "",
        curated_question: str = "",
        clarifications: list[dict[str, str]] | None = None,
    ) -> None:
        """One ``create_event`` per finished turn — the extraction feedstock.

        Payload = the user's message, any mid-turn clarification Q&A (an
        ask_human exchange — the richest preference/intent evidence a turn
        can carry: the user EXPLICITLY disambiguating what they meant), an
        assistant-answer excerpt, and the harness annotation (when anything
        was observed) as a separate assistant message the extraction prompt
        knows to trust.

        ``observation`` is a :meth:`TurnObservation.snapshot`. The annotation
        it becomes resolves datasets in two levels: the answer's citations —
        VALIDATED against what the harness can corroborate (this turn's
        observation, the thread's cumulative ledger, the conversation pin) —
        replace the touched list when any survive; the raw observed list is
        the fallback. A citation to a never-observed dataset is a model claim
        the trusted block must not launder into fact. The turn's observed
        datasets then merge into the thread ledger, which is what lets a
        LATER no-tool follow-up's citation still resolve its dataset.
        """
        # The curated question rides the annotation only when it actually
        # adds context — identical to the raw text (the rewrite hadn't
        # landed, or the turn was already standalone) is noise.
        curated = (curated_question or "").strip()
        if curated == (user_text or "").strip():
            curated = ""
        annotation = self._compose_annotation(
            observation,
            answer_text or "",
            user_sub=user_sub,
            thread_id=thread_id,
            pin=pin,
            curated=curated,
        )
        # Every piece is excerpt-capped, not just the answer: an oversized
        # user paste would otherwise blow the CreateEvent payload limit and
        # silently drop the WHOLE event, clarifications included.
        messages = [("USER", (user_text or "")[:ANSWER_EXCERPT_CHARS])]
        for pair in clarifications or []:
            prompt_q = str(pair.get("prompt") or "")[:ANSWER_EXCERPT_CHARS]
            answer_a = str(pair.get("answer") or "")[:ANSWER_EXCERPT_CHARS]
            if prompt_q:
                messages.append(("ASSISTANT", f"(clarifying question) {prompt_q}"))
            if answer_a:
                messages.append(("USER", answer_a))
        messages += [
            ("ASSISTANT", (answer_text or "")[:ANSWER_EXCERPT_CHARS]),
        ]
        if annotation:
            messages.append(("ASSISTANT", annotation))
        try:
            self._client.create_event(
                memoryId=self._memory_id,
                actorId=_safe_id(user_sub),
                sessionId=_safe_id(session_id),
                eventTimestamp=datetime.now(timezone.utc),
                payload=[
                    {"conversational": {"role": role, "content": {"text": text}}}
                    for role, text in messages
                    if text
                ],
            )
        except Exception:  # noqa: BLE001 - memory writes are best-effort
            log.warning("memory event write failed", exc_info=True)
        # Ledger update rides the same daemon thread, independent of the event
        # write's fate — it records what the harness OBSERVED, not what the
        # extractor was told.
        self._update_datasets_ledger(user_sub, thread_id, observation)

    def _compose_annotation(
        self,
        observation: dict[str, Any] | None,
        answer_text: str,
        *,
        user_sub: str,
        thread_id: str,
        pin: str,
        curated: str = "",
    ) -> str:
        if not isinstance(observation, dict):
            return ""
        datasets = [str(d) for d in observation.get("datasets") or [] if d]
        governed = [g for g in observation.get("governed") or [] if isinstance(g, dict)]
        cited = _cited_datasets(answer_text)
        if cited:
            # Level 1 counts only what the harness can corroborate: observed
            # this turn, observed earlier in the thread (the ledger), or the
            # conversation's pin (server-enforced, so harness-known).
            valid = set(datasets) | ({pin} if pin else set())
            if thread_id:
                valid |= set(self._read_datasets_ledger(user_sub, thread_id))
            cited = [d for d in cited if d in valid]
        return _render_annotation(
            datasets, governed, cited=cited or None, curated=curated
        )

    def _read_datasets_ledger(self, user_sub: str, thread_id: str) -> list[str]:
        if self._ddb is None or not self._threads_table or not thread_id:
            return []
        from chat.threads import read_memory_datasets

        return read_memory_datasets(
            self._ddb,
            threads_table=self._threads_table,
            user_sub=user_sub,
            thread_id=thread_id,
        )

    def _update_datasets_ledger(
        self, user_sub: str, thread_id: str, observation: dict[str, Any] | None
    ) -> None:
        datasets = (
            [str(d) for d in observation.get("datasets") or [] if d]
            if isinstance(observation, dict)
            else []
        )
        if (
            not datasets
            or self._ddb is None
            or not self._threads_table
            or not thread_id
        ):
            return
        from chat.threads import merge_memory_datasets

        merge_memory_datasets(
            self._ddb,
            threads_table=self._threads_table,
            user_sub=user_sub,
            thread_id=thread_id,
            datasets=datasets,
        )

    def write_turn_async(self, **kwargs: Any) -> None:
        """Fire-and-forget :meth:`write_turn` — the stream must never wait."""
        threading.Thread(
            target=self.write_turn, kwargs=kwargs, daemon=True
        ).start()


def make_chat_memory(chat_config: Any, clients: dict[str, Any]) -> ChatMemory | None:
    """The runtime's memory singleton, or None when the deploy gate is off."""
    memory_id = getattr(chat_config, "memory_id", "") or ""
    client = clients.get("agentcore_memory")
    if not memory_id or client is None:
        return None
    return ChatMemory(
        client,
        memory_id=memory_id,
        ddb=clients.get("dynamodb"),
        threads_table=getattr(chat_config, "threads_table", "") or "",
        default_enabled=bool(getattr(chat_config, "memory_default_on", True)),
    )
