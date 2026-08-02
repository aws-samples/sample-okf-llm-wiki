"""Post-turn Automated Reasoning policy check — async, advisory, on demand.

The third steering lane (after ``chat.sql_anomalies`` mid-turn and
``chat.steering`` mid-turn): AFTER a turn completes, its data claims are
validated against the touched datasets' documented rules by Bedrock Guardrails
Automated Reasoning (an SMT solver over rules built from the wiki), and the
findings render in an opt-in sidebar. It never blocks, gates, or feeds back
into the model — a trust surface for the human, not a control loop for the
agent. Findings NEVER enter model context (hard rule).

The pipeline, per checked turn:

1. **Eligibility** (pure, deterministic): a turn is checkable iff it executed
   SQL, completed an ``ask_human`` exchange, or ships a fenced SQL block in the
   answer prose. Everything else is "no data claims — nothing to check", at
   zero model/API cost.
2. **Deterministic extraction** (pure): executed SQL texts, result shapes
   (rows/truncated — measured, never model-derived), ask_human Q&A, and the
   datasets the turn touched (routing only — reads never become claims).
3. **The pre-pass** (ONE small-model call, reasoning off, temperature 0): a
   condense-question rewrite plus a process transcript written BLIND to the
   final answer, so premises are gathered without hindsight.
4. **The AR call**, per touched dataset with a USABLE policy: the user's words
   go in as premises (``query``), everything agent-side — transcript,
   assumptions, answer text, recommended-SQL mechanics — as ONE claim set
   (``guard_content``). Usable = ``ar_build_status ∈ {ready, degraded}`` AND
   the stored source fingerprint equals one freshly computed from the live
   wiki. A stale policy NEVER renders a verdict — the dataset reports
   "rebuild pending", the row is flagged, and a ``policy_rebuild`` event is
   published (both best-effort) so the click starts the repair.
5. **Persistence**: the report lands on the ``okf-chat`` table as a
   ``POLICY#<thread>#<turn_key>`` row (structural per-user isolation, same as
   threads) and is returned as-is on every later click — ``force`` re-runs.

Verdict semantics are "violation detector", not "correctness prover":
``INVALID``/``IMPOSSIBLE`` → violation (the money finding), ``SATISFIABLE`` /
``VALID`` → consistent (the normal good state), translation failures →
"couldn't check" (neutral, never alarming).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chat.steering import STEERING_MARKER

log = logging.getLogger("chat.policy_check")

#: Wiki tools whose args/results ROUTE the check (select which dataset policies
#: run). They never contribute transcript material — rules constrain outcomes,
#: not reading.
_ROUTING_TOOLS = frozenset(
    {"read_page", "list_directory", "glob", "grep", "get_backlinks", "semantic_search"}
)

#: Cap on rows echoed into the pre-pass per SQL result — the model needs the
#: shape and a taste of the values, not the payload.
_PREPASS_SAMPLE_ROWS = 5

#: Cap on any single tool-result echo in the pre-pass input (chars).
_PREPASS_RESULT_CHARS = 1500

#: ```sql fenced block — the recommended-query contract.
_SQL_FENCE_RE = re.compile(r"```sql\b(.*?)```", re.DOTALL | re.IGNORECASE)
#: Fallback: ANY fenced block whose body leads with SELECT/WITH.
_ANY_FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.DOTALL)
_SQL_LEAD_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE | re.MULTILINE)

_VIOLATION_TYPES = frozenset({"INVALID", "IMPOSSIBLE"})
_NOT_CHECKABLE_TYPES = frozenset(
    {"NO_TRANSLATIONS", "TOO_COMPLEX", "TRANSLATION_AMBIGUOUS"}
)
#: Findings that carry a real solver evaluation. NO_TRANSLATIONS is absent on
#: purpose: it only says "this content unit wasn't policy-relevant" — a chart
#: sentence, a pleasantry — which is NORMAL alongside substantive findings
#: (live-observed) and must not veto them.
_SUBSTANTIVE_TYPES = frozenset(
    {"VALID", "SATISFIABLE", "INVALID", "IMPOSSIBLE",
     "TRANSLATION_AMBIGUOUS", "TOO_COMPLEX"}
)


# --- turn anatomy (pure) ------------------------------------------------------


def _is_steering(msg: Any) -> bool:
    return bool((getattr(msg, "additional_kwargs", None) or {}).get(STEERING_MARKER))


def _text(content: Any) -> str:
    """User-facing TEXT of a message content — reasoning blocks are not text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _thinking(content: Any) -> str:
    """The reasoning text of an AIMessage content (Converse + GPT shapes)."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "reasoning_content":
            rc = b.get("reasoning_content") or {}
            if isinstance(rc, dict) and rc.get("text"):
                parts.append(str(rc["text"]))
        elif b.get("type") == "reasoning":
            summary = b.get("summary")
            if isinstance(summary, list):
                parts.extend(
                    str(s.get("text", "")) for s in summary if isinstance(s, dict)
                )
    return "\n".join(p for p in parts if p)


def turn_slices(messages: list[Any]) -> list[list[Any]]:
    """Split a thread's messages into per-turn slices, by ordinal.

    The boundary predicate is IDENTICAL to the server's ``_messages_to_turns``
    fold (a genuine — non-steering — HumanMessage opens a turn), so slice index
    N is the same turn the history/UI calls ``turn_N``. Pinned by a test.
    """
    slices: list[list[Any]] = []
    current: list[Any] | None = None
    for msg in messages:
        if isinstance(msg, HumanMessage) and not _is_steering(msg):
            if current is not None:
                slices.append(current)
            current = [msg]
        elif current is not None:
            current.append(msg)
    if current is not None:
        slices.append(current)
    return slices


def _is_error_result(msg: Any) -> bool:
    if getattr(msg, "status", None) == "error":
        return True
    content = getattr(msg, "content", None)
    return isinstance(content, str) and content.startswith("Error:")


def final_answer_text(turn: list[Any]) -> str:
    """Everything the user READ this turn: all assistant text, in order.

    Answer prose may be split across AIMessages (text between tool calls); all
    of it is claim material, and all of it is hidden from the pre-pass.
    """
    return "\n".join(
        t for m in turn if isinstance(m, AIMessage) if (t := _text(m.content))
    )


def detect_fenced_sql(answer_text: str) -> bool:
    """True when the answer prose ships a SQL statement.

    The contract is a ```` ```sql ```` fence; a plain fence whose body leads
    with SELECT/WITH is accepted best-effort. Reasoning content never reaches
    this function (``_text`` drops it), which is the thinking-block exclusion.
    """
    if _SQL_FENCE_RE.search(answer_text):
        return True
    return any(
        _SQL_LEAD_RE.search(body) for body in _ANY_FENCE_RE.findall(answer_text)
    )


def recommended_sql(answer_text: str) -> list[str]:
    """The fenced SQL texts in the answer prose (the ``detect`` counterpart)."""
    found = [body.strip() for body in _SQL_FENCE_RE.findall(answer_text)]
    if found:
        return found
    return [
        body.strip()
        for body in _ANY_FENCE_RE.findall(answer_text)
        if _SQL_LEAD_RE.search(body)
    ]


def eligibility(turn: list[Any]) -> dict[str, bool]:
    """The layered §4 predicates, all deterministic."""
    turn_ran_sql = False
    turn_asked_human = False
    for msg in turn:
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", None) or ""
        if name == "run_sql" and not _is_error_result(msg):
            turn_ran_sql = True
        elif name == "ask_human" and not _is_error_result(msg):
            parsed = _parse_json(msg.content)
            if isinstance(parsed, dict) and parsed.get("status") == "answered":
                turn_asked_human = True
    answer_ships_sql = detect_fenced_sql(final_answer_text(turn))
    transcript_eligible = turn_ran_sql or turn_asked_human
    return {
        "turn_ran_sql": turn_ran_sql,
        "turn_asked_human": turn_asked_human,
        "answer_ships_sql": answer_ships_sql,
        "transcript_eligible": transcript_eligible,
        "check_eligible": transcript_eligible or answer_ships_sql,
    }


# --- deterministic extraction (pure) -------------------------------------------


def _parse_json(content: Any) -> Any:
    """Parse a tool result body that may carry a trailing anomaly reminder.

    ``run_sql`` appends ``\\n\\n<system-reminder>…`` after the JSON payload when
    the anomaly detector fires — the reminder is model-facing framing, never
    data. Returns None when nothing parseable remains.
    """
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    body = content.split("\n\n<system-reminder>", 1)[0].strip()
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def executed_queries(turn: list[Any]) -> list[dict[str, Any]]:
    """Each SUCCESSFUL ``run_sql`` call paired with its measured result shape.

    ``row_count`` / ``truncated`` / ``columns`` come from the result payload
    verbatim — these are the facts the pre-pass is TOLD, never asked to derive.
    Failed attempts are omitted: they produced no data the answer rests on.
    """
    results: dict[str, Any] = {
        msg.tool_call_id: msg
        for msg in turn
        if isinstance(msg, ToolMessage) and (getattr(msg, "name", None) == "run_sql")
    }
    out: list[dict[str, Any]] = []
    for msg in turn:
        if not isinstance(msg, AIMessage):
            continue
        for tc in msg.tool_calls or []:
            if (tc.get("name") or "") != "run_sql":
                continue
            result = results.get(tc.get("id"))
            if result is None or _is_error_result(result):
                continue
            payload = _parse_json(result.content) or {}
            rows = payload.get("rows") or []
            out.append(
                {
                    "sql": str((tc.get("args") or {}).get("sql") or ""),
                    "columns": payload.get("columns") or [],
                    "row_count": int(payload.get("row_count") or 0),
                    "truncated": bool(payload.get("truncated")),
                    "sample_rows": rows[:_PREPASS_SAMPLE_ROWS],
                }
            )
    return out


def ask_human_qa(turn: list[Any]) -> list[dict[str, str]]:
    """The answered clarification exchange(s): ``[{prompt, answer}]``."""
    out: list[dict[str, str]] = []
    for msg in turn:
        if not isinstance(msg, ToolMessage) or getattr(msg, "name", None) != "ask_human":
            continue
        parsed = _parse_json(msg.content)
        if not isinstance(parsed, dict) or parsed.get("status") != "answered":
            continue
        for a in parsed.get("answers") or []:
            if isinstance(a, dict):
                out.append(
                    {
                        "prompt": str(a.get("prompt") or a.get("id") or ""),
                        "answer": str(a.get("answer") or ""),
                    }
                )
        note = parsed.get("note")
        if note:
            out.append({"prompt": "(free-form note)", "answer": str(note)})
    return out


def _dataset_from_path(path: Any) -> str | None:
    """``<domain>/<dataset>`` from a concept path like ``bird/formula_1/tables/x``."""
    if not isinstance(path, str):
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def datasets_touched(turn: list[Any]) -> list[str]:
    """The ``<domain>/<dataset>`` labels this turn's wiki activity names.

    Routing only (design decision 9). Sources, strongest first: the stored
    ``[Scope: …]`` preamble on the user message, explicit ``data_domain`` /
    ``dataset`` args on wiki tool calls (unscoped runs), and the concept paths
    inside ``semantic_search`` results.
    """
    from chat.server import parse_scope_prefix

    labels: list[str] = []

    def _add(label: str | None) -> None:
        if label and label not in labels:
            labels.append(label)

    opener = turn[0] if turn else None
    if isinstance(opener, HumanMessage):
        scope = parse_scope_prefix(_text(opener.content))
        if scope:
            _add(f"{scope['data_domain']}/{scope['dataset']}")

    for msg in turn:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                if (tc.get("name") or "") not in _ROUTING_TOOLS:
                    continue
                args = tc.get("args") or {}
                if args.get("data_domain") and args.get("dataset"):
                    _add(f"{args['data_domain']}/{args['dataset']}")
        elif isinstance(msg, ToolMessage):
            if getattr(msg, "name", None) != "semantic_search":
                continue
            parsed = _parse_json(msg.content)
            hits = parsed if isinstance(parsed, list) else []
            for hit in hits:
                if isinstance(hit, dict):
                    _add(_dataset_from_path(hit.get("path")))
    return labels


# --- the pre-pass (one small-model call) -----------------------------------------


_PREPASS_CONTRACT = """\
You are auditing an AI data analyst's COMPLETED turn for an automated reasoner.
You see the conversation so far and the turn's working chain (thinking, tool
calls, tool results). You have NOT been shown the analyst's final answer — do
not guess or reconstruct it.

Return ONE JSON object and nothing else:
{"standalone_question": string, "rewritten": boolean, "transcript": string,
 "assumptions": [string, ...]}

standalone_question — the user's effective question as a self-contained
sentence, resolving pronouns and context from prior turns ("and for 2019?" ->
the full question). If the raw question already stands alone, copy it verbatim
and set "rewritten": false.

transcript — sharp declarative prose, one fact per sentence, describing what
the analyst DID. Structure: one short paragraph per successful executed query
(AT MOST two sentences of mechanics — tables touched and the operation — plus
its measured result sentence given under MEASURED RESULT SHAPES, numbers
copied verbatim, never derived; a probe/validation query like a min/max or
count sanity-check gets ONE mechanics sentence), then ONE final paragraph
carrying THE TURN'S TERM BINDINGS. The downstream reasoner has a hard
processing budget — every sentence beyond the bindings spends it.

The bindings paragraph: phrase facts with these exact terms where they apply:
clarificationObtained, termDisambiguated, periodSpecified,
periodWithinHorizon, queryExecuted, dedupApplied, sentinelExcluded,
snapshotSummedOverTime, disjointMeasuresCombined, deprecatedObjectUsed,
recipeApplied. When a POLICY VOCABULARY section is present, also bind facts
to those exact names — write the term verbatim rather than paraphrasing its
meaning; leave terms the chain does not evidence unmentioned. Each bound term
gets its OWN short declarative sentence, in EITHER polarity —
"dedupApplied is true." / "dedupApplied is false." — never a prose negation
("no deduplication was applied"): the reasoner cannot tell a prose negation
from unbound narrative. Bind each term AT MOST ONCE for the WHOLE TURN: the
claim set is evaluated as one conjunction, so binding a term true for one
query and false for another is a formal self-contradiction that voids the
check. When queries genuinely differ, bind the polarity that describes what
the ANSWER rests on (any answer-bearing reliance on out-of-horizon data means
"periodWithinHorizon is false"; dedup applied everywhere it mattered means
"dedupApplied is true") and describe the per-query difference in the prose,
without re-binding the term. Never bundle several terms into one sentence,
and never phrase a term as a condition or a consequence of another sentence.
Attest ONLY what the chain evidences; "not determinable" is a legal value —
never fabricate a binding. Do not mention wiki pages read. Do not mention any
final answer.

assumptions — interpretive choices the chain evidences (a term read one way, a
period assumed, a scope narrowed), one short sentence each. Empty list if none.
"""


def _summarize_result(msg: ToolMessage) -> str:
    name = getattr(msg, "name", None) or "tool"
    if _is_error_result(msg):
        return f"[{name} result: ERROR] {str(msg.content)[:200]}"
    if name == "run_sql":
        payload = _parse_json(msg.content) or {}
        return (
            f"[run_sql result] columns={payload.get('columns')} "
            f"row_count={payload.get('row_count')} truncated={payload.get('truncated')} "
            f"first_rows={json.dumps((payload.get('rows') or [])[:_PREPASS_SAMPLE_ROWS], default=str)}"
        )
    body = msg.content if isinstance(msg.content, str) else json.dumps(msg.content, default=str)
    return f"[{name} result] {body[:_PREPASS_RESULT_CHARS]}"


#: Per-variable description cap in the vocabulary section, and a total entry
#: cap — a policy carries up to 200 variables (the AR quota), and the pre-pass
#: is a small extraction call whose prompt shouldn't dwarf the chain itself.
_VOCAB_DESC_CHARS = 180
_VOCAB_MAX_ENTRIES = 200


def gather_policy_vocabulary(
    s3, *, bucket: str, labels: list[str]
) -> list[dict[str, str]]:
    """The touched datasets' policy variables, deduped, core terms excluded.

    The core vocabulary is already spelled out in the contract; what the
    pre-pass can't know are the DATASET-SPECIFIC variables the build derived
    (``vocabulary.json``, written at completion/restore). Missing artifacts
    degrade to an empty list — the pre-pass then runs exactly as before.
    """
    from okf_aws import ar_policy as ap

    core = {v["name"] for v in ap.CORE_VARIABLES}
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for label in labels or []:
        data_domain, _, dataset = label.partition("/")
        try:
            entries = ap.read_vocabulary(
                s3, bucket=bucket, data_domain=data_domain, dataset=dataset
            )
        except Exception:  # noqa: BLE001 - degrade to the core-only contract
            continue
        for entry in entries:
            name = str((entry or {}).get("name") or "")
            if not name or name in core or name in seen:
                continue
            seen.add(name)
            out.append(
                {"name": name, "description": str(entry.get("description") or "")}
            )
            if len(out) >= _VOCAB_MAX_ENTRIES:
                return out
    return out


def build_prepass_input(
    prior_turns: list[list[Any]],
    turn: list[Any],
    vocabulary: list[dict[str, str]] | None = None,
) -> str:
    """Assemble the pre-pass user message: history text + the answer-blind chain.

    Prior turns contribute only their user/assistant TEXT (what the rewrite
    needs). The current turn contributes thinking, tool calls with args, and
    shape-summarized results — but NO assistant text: the transcript must be
    written blind to the answer (design decision 8). ``vocabulary`` lists the
    touched policies' dataset-specific variables so the transcript names them
    instead of paraphrasing them (paraphrase is what breeds
    TRANSLATION_AMBIGUOUS findings).
    """
    from chat.server import strip_scope_prefix

    parts: list[str] = [_PREPASS_CONTRACT]
    if vocabulary:
        parts.append(
            "\n== POLICY VOCABULARY (dataset-specific terms — phrase facts "
            "with these exact names where the chain evidences them; leave "
            "unevidenced terms unmentioned) =="
        )
        parts.extend(
            f"- {v['name']}: {v['description'][:_VOCAB_DESC_CHARS]}"
            for v in vocabulary
        )
    parts.append("\n== CONVERSATION SO FAR ==")
    for prior in prior_turns:
        opener = prior[0] if prior else None
        if isinstance(opener, HumanMessage):
            parts.append(f"User: {strip_scope_prefix(_text(opener.content))}")
        answer = final_answer_text(prior)
        if answer:
            parts.append(f"Assistant: {answer[:2000]}")

    opener = turn[0] if turn else None
    raw_question = strip_scope_prefix(_text(opener.content)) if opener else ""
    parts.append("\n== THE CURRENT QUESTION ==")
    parts.append(raw_question)

    parts.append("\n== THE TURN'S WORKING CHAIN (final answer withheld) ==")
    for msg in turn[1:]:
        if isinstance(msg, AIMessage):
            think = _thinking(msg.content)
            if think:
                parts.append(f"[thinking] {think[:_PREPASS_RESULT_CHARS]}")
            for tc in msg.tool_calls or []:
                name = tc.get("name") or ""
                args = tc.get("args") or {}
                if name == "run_sql":
                    parts.append(f"[tool call] run_sql: {args.get('sql', '')}")
                else:
                    parts.append(
                        f"[tool call] {name}: {json.dumps(args, default=str)[:400]}"
                    )
        elif isinstance(msg, ToolMessage) and not _is_steering(msg):
            parts.append(_summarize_result(msg))

    shapes = [
        f"Query {i + 1} returned {q['row_count']} rows"
        + (", truncated at the row cap." if q["truncated"] else ", not truncated.")
        for i, q in enumerate(executed_queries(turn))
    ]
    parts.append("\n== MEASURED RESULT SHAPES (copy verbatim) ==")
    parts.append("\n".join(shapes) if shapes else "No query was executed this turn.")
    return "\n".join(parts)


def parse_prepass(text: str) -> dict[str, Any] | None:
    """Validate the pre-pass reply into its four-field contract, else None."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[^\n]*\n|```$", "", body).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(body[start : end + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    question = parsed.get("standalone_question")
    transcript = parsed.get("transcript")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(transcript, str):
        return None
    assumptions = parsed.get("assumptions")
    if not isinstance(assumptions, list):
        assumptions = []
    return {
        "standalone_question": question.strip(),
        "rewritten": bool(parsed.get("rewritten")),
        "transcript": transcript.strip(),
        "assumptions": [str(a) for a in assumptions if str(a).strip()],
    }


def _reply_text(reply: Any) -> str:
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    return _text(content)


_MECHANICS_PROMPT = """\
Describe mechanically, in one short paragraph of declarative prose, what the
following SQL does: tables touched, period/filter bounds, aggregation and
grain, whether deduplication is applied, whether sentinel/placeholder values
are excluded. Use these exact terms where they apply: dedupApplied,
sentinelExcluded, snapshotSummedOverTime, disjointMeasuresCombined,
deprecatedObjectUsed, recipeApplied. Begin the paragraph exactly with:
"The agent recommended a query that". Output only the paragraph.

SQL:
"""


def run_prepass(model: Any, prepass_input: str) -> dict[str, Any] | None:
    """One extraction call, ONE retry on a malformed reply, else None (fail-open)."""
    for attempt in (1, 2):
        try:
            reply = model.invoke([("user", prepass_input)])
        except Exception:  # noqa: BLE001 - fail-open is the contract (§5)
            log.warning("policy_check pre-pass model call failed", exc_info=True)
            return None
        parsed = parse_prepass(_reply_text(reply))
        if parsed is not None:
            return parsed
        log.warning("policy_check pre-pass reply unparseable (attempt %d)", attempt)
        prepass_input = (
            prepass_input + "\n\nReturn ONLY the JSON object, with no prose around it."
        )
    return None


def sql_mechanics(model: Any, sql_texts: list[str]) -> str:
    """The recommended-SQL mechanics paragraph (design §5), or "" on any failure.

    Interprets ONLY the fenced SQL text — never the surrounding prose — which
    preserves the pipeline's answer-blindness of interpretation.
    """
    if not sql_texts:
        return ""
    try:
        reply = model.invoke([("user", _MECHANICS_PROMPT + "\n\n".join(sql_texts))])
        text = _reply_text(reply).strip()
        return text if text.startswith("The agent recommended") else ""
    except Exception:  # noqa: BLE001 - the block is optional claim material
        log.warning("policy_check sql-mechanics call failed", exc_info=True)
        return ""


# --- verdicts & report ------------------------------------------------------------


def dataset_verdict(findings: list[dict[str, Any]]) -> str:
    """Worst-of mapping over the SUBSTANTIVE findings.

    ``violation > not_checkable > consistent`` — but a ``NO_TRANSLATIONS``
    finding only weighs in when it is all there is: the reasoner emits one per
    content unit it couldn't bind to the policy AT ALL (a rendered chart, a
    courtesy sentence), and that is expected noise next to real findings, not
    a reason to call a checked turn unchecked.
    """
    types = {f.get("type") for f in findings}
    if types & _VIOLATION_TYPES:
        return "violation"
    substantive = types & _SUBSTANTIVE_TYPES
    if substantive & _NOT_CHECKABLE_TYPES:
        return "not_checkable"
    if substantive:
        return "consistent"
    # Nothing substantive at all: only NO_TRANSLATIONS (or nothing).
    return "not_checkable" if types else "consistent"


def render_findings(
    findings: list[dict[str, Any]], grounding: dict[str, Any]
) -> list[dict[str, Any]]:
    """Attach quoted rule text + source page to each finding, from grounding."""
    out: list[dict[str, Any]] = []
    for f in findings:
        rule_ids = f.get("rule_ids") or []
        rule = next(
            (grounding[r] for r in rule_ids if isinstance(grounding.get(r), dict)),
            {},
        )
        out.append(
            {
                "type": f.get("type"),
                "claim": f.get("claim") or "",
                "rule_text": rule.get("rule_text") or "",
                "rule_source_page": rule.get("rule_source_page") or "",
                "scenario": f.get("scenario") or [],
                "confidence": f.get("confidence"),
            }
        )
    return out


# --- persistence -----------------------------------------------------------------


def _report_key(user_sub: str, client_thread_id: str, turn_key: int) -> dict:
    from okf_core import chat_threads as ct

    return {
        "pk": {"S": ct.thread_pk(user_sub)},
        "sk": {"S": ct.policy_report_sk(client_thread_id, turn_key)},
    }


def read_report(ddb, table: str, user_sub: str, client_thread_id: str, turn_key: int):
    """The stored report envelope, or None. Terminal statuses only are stored."""
    item = ddb.get_item(
        TableName=table, Key=_report_key(user_sub, client_thread_id, turn_key)
    ).get("Item")
    if not item:
        return None
    raw = (item.get("report_json") or {}).get("S") or ""
    try:
        return json.loads(raw)
    except ValueError:
        return None


def write_report(
    ddb,
    table: str,
    user_sub: str,
    client_thread_id: str,
    turn_key: int,
    envelope: dict[str, Any],
    *,
    expires_at: int | None = None,
) -> None:
    """Persist the report row. Best-effort: a failed write only costs a re-run."""
    item: dict[str, Any] = {
        **_report_key(user_sub, client_thread_id, turn_key),
        "status": {"S": str(envelope.get("status") or "")},
        "created_at": {"S": str(envelope.get("created_at") or "")},
        "model": {"S": str(envelope.get("model") or "")},
        "eligible": {"BOOL": bool(envelope.get("eligible"))},
        "standalone_question": {"S": str(envelope.get("standalone_question") or "")},
        "report_json": {"S": json.dumps(envelope, default=str)},
    }
    if expires_at is not None:
        item["expires_at"] = {"N": str(expires_at)}
    try:
        ddb.put_item(TableName=table, Item=item)
    except Exception:  # noqa: BLE001 - the report is re-derivable on the next click
        log.warning("policy_check report write failed (non-fatal)", exc_info=True)


def _thread_expiry(ddb, table: str, user_sub: str, client_thread_id: str) -> int | None:
    """Mirror the thread row's TTL onto the report row, when one exists."""
    from okf_core import chat_threads as ct

    try:
        item = ddb.get_item(
            TableName=table,
            Key={
                "pk": {"S": ct.thread_pk(user_sub)},
                "sk": {"S": ct.thread_sk(client_thread_id)},
            },
        ).get("Item")
        raw = ((item or {}).get("expires_at") or {}).get("N")
        return int(raw) if raw else None
    except Exception:  # noqa: BLE001 - TTL mirroring is cosmetic
        return None


# --- clients ----------------------------------------------------------------------


def build_policy_clients(chat_config: Any) -> dict[str, Any]:
    """The live boto3 seams for :func:`run_policy_check` (tests inject fakes).

    ``model`` stays None here — it is built per run from config so a test can
    swap it without touching boto3.
    """
    import boto3

    region = chat_config.region
    return {
        "ddb": boto3.client("dynamodb", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "bedrock_runtime": boto3.client("bedrock-runtime", region_name=region),
        "events": boto3.client("events", region_name=region),
        "model": None,
    }


def _publish_rebuild(events, data_domain: str, dataset: str) -> None:
    """Best-effort ``policy_rebuild`` publish — a stale click STARTS the repair."""
    from okf_core import policy_rebuild

    try:
        events.put_events(
            Entries=[
                {
                    "Source": policy_rebuild.EVENT_SOURCE,
                    "DetailType": policy_rebuild.DETAIL_TYPE_POLICY_REBUILD,
                    "Detail": json.dumps(
                        policy_rebuild.build_detail(
                            data_domain, dataset, reason="stale_policy_check"
                        )
                    ),
                }
            ]
        )
    except Exception:  # noqa: BLE001 - the nightly reconcile is the safety net
        log.warning("policy_rebuild publish failed (non-fatal)", exc_info=True)


# --- the orchestrator ---------------------------------------------------------------


def run_policy_check(
    input_data: dict[str, Any],
    *,
    user_sub: str,
    client_thread_id: str,
    internal_thread_id: str,
    chat_config: Any,
    build_agent: Any,
    checkpointer: Any,
    clients: dict[str, Any],
) -> dict[str, Any]:
    """§8.1 steps 1–8. Returns the JSON envelope (never raises to the caller's user).

    The caller (the server branch) wraps unexpected exceptions into the standard
    error envelope; expected conditions (bad turn_key, in-flight turn) come back
    as typed envelopes from here.
    """
    from chat import live_streams
    from chat.config import build_policy_check_model
    from chat.server import _error_chunk
    from okf_aws import ar_policy as ap

    try:
        turn_key = int(input_data.get("turn_key"))
    except (TypeError, ValueError):
        return _error_chunk("bad_request", "policy_check requires an integer turn_key")
    force = bool(input_data.get("force"))
    ddb = clients["ddb"]
    table = chat_config.threads_table

    # 2. The stored report is the answer for every later click (idempotent).
    if not force:
        stored = read_report(ddb, table, user_sub, client_thread_id, turn_key)
        if stored is not None:
            return stored

    # 1. Rebuild history WITHOUT the in-flight drop, so ordinals are the
    # persisted truth; reject the live turn explicitly instead.
    graph = build_agent("global.anthropic.claude-opus-5", "high", None, checkpointer)
    state = graph.get_state({"configurable": {"thread_id": internal_thread_id}})
    messages = (state.values or {}).get("messages", []) if state else []
    slices = turn_slices(messages)
    if turn_key < 0 or turn_key >= len(slices):
        live = live_streams.get(internal_thread_id)
        if live is not None and live_streams.is_active(internal_thread_id):
            return {"type": "policy_check", "turn_key": turn_key, "status": "running"}
        return _error_chunk("not_found", f"no such turn: {turn_key}")
    live = live_streams.get(internal_thread_id)
    if (
        turn_key == len(slices) - 1
        and live is not None
        and live_streams.is_active(internal_thread_id)
    ):
        return {"type": "policy_check", "turn_key": turn_key, "status": "running"}

    turn = slices[turn_key]
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    model_id = chat_config.policy_check_model
    base = {
        "type": "policy_check",
        "turn_key": turn_key,
        "model": model_id,
        "created_at": now,
    }
    expires_at = _thread_expiry(ddb, table, user_sub, client_thread_id)

    # 3. Eligibility — ineligible turns are persisted too (the predicates are
    # deterministic; re-running them on every click buys nothing).
    flags = eligibility(turn)
    if not flags["check_eligible"]:
        envelope = {
            **base,
            "status": "not_eligible",
            "eligible": False,
            "reason": "no data claims this turn",
        }
        write_report(
            ddb, table, user_sub, client_thread_id, turn_key, envelope,
            expires_at=expires_at,
        )
        return envelope

    # 4–5. Deterministic facts + the answer-blind pre-pass (fail-open). The
    # touched datasets are resolved FIRST so the pre-pass can be handed their
    # policies' own vocabulary: a transcript that merely paraphrases a
    # dataset-specific variable yields TRANSLATION_AMBIGUOUS (live-observed —
    # two candidate readings differing on whether the variable binds), while
    # naming it translates deterministically.
    answer_text = final_answer_text(turn)
    s3 = clients["s3"]
    labels = datasets_touched(turn) or _thread_fallback(slices, turn_key)
    vocabulary = gather_policy_vocabulary(
        s3, bucket=chat_config.bundle_bucket, labels=labels
    )
    model = clients.get("model") or build_policy_check_model(chat_config)
    prepass = run_prepass(
        model,
        build_prepass_input(slices[:turn_key], turn, vocabulary=vocabulary),
    )
    if prepass is None:
        # NOT persisted: unavailable is transient — the next click retries.
        return {
            **base,
            "status": "unavailable",
            "eligible": True,
            "reason": "the transcript pass failed",
        }
    mechanics = (
        sql_mechanics(model, recommended_sql(answer_text))
        if flags["answer_ships_sql"]
        else ""
    )

    # 6–7. Per touched dataset: the fingerprint gate, then the AR call.
    qa = ask_human_qa(turn)
    qa_text = "\n".join(f"Q: {x['prompt']}\nA: {x['answer']}" for x in qa)
    datasets: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    for label in labels:
        data_domain, _, dataset = label.partition("/")
        entry: dict[str, Any] = {
            "data_domain": data_domain,
            "dataset": dataset,
            "findings": [],
        }
        datasets.append(entry)
        row = _registry_row(ddb, chat_config.registry_table, data_domain, dataset)
        # Enrollment is the user's per-dataset opt-in (the Reasoning page).
        # Its own verdict rather than no_policy: the sidebar can say HOW to
        # turn the check on, instead of implying a build that never comes.
        if not ap.is_enrolled(row):
            entry["verdict"] = "not_enrolled"
            continue
        status = _row_s(row, ap.ATTR_BUILD_STATUS)
        if status == ap.BUILD_BUILDING:
            entry["verdict"] = "building"
            continue
        if status not in ap.USABLE_BUILD_STATUSES:
            entry["verdict"] = "no_policy"
            continue
        fresh = ap.source_hash(s3, chat_config.bundle_bucket, data_domain, dataset)
        if not fresh or fresh != _row_s(row, ap.ATTR_SOURCE_HASH):
            # The hard gate: only the latest wiki state is truth. Flag + repair.
            entry["verdict"] = "stale"
            try:
                ap.flag_stale(
                    ddb,
                    chat_config.registry_table,
                    data_domain=data_domain,
                    dataset=dataset,
                )
            except Exception:  # noqa: BLE001 - reconcile re-derives this
                log.warning("flag_stale failed (non-fatal)", exc_info=True)
            _publish_rebuild(clients["events"], data_domain, dataset)
            continue

        content = [
            {"text": {"text": prepass["standalone_question"], "qualifiers": ["query"]}}
        ]
        if qa_text:
            content.append({"text": {"text": qa_text, "qualifiers": ["query"]}})
        claims: list[str] = []
        if flags["transcript_eligible"] and prepass["transcript"]:
            claims.append(prepass["transcript"])
        if prepass["assumptions"]:
            claims.append(
                "Declared assumptions: " + "; ".join(prepass["assumptions"])
            )
        if answer_text:
            claims.append(answer_text)
        if mechanics:
            claims.append(mechanics)
        content.extend(
            {"text": {"text": c, "qualifiers": ["guard_content"]}} for c in claims
        )
        try:
            response = clients["bedrock_runtime"].apply_guardrail(
                guardrailIdentifier=_row_s(row, "ar_guardrail_id"),
                guardrailVersion=_row_s(row, "ar_guardrail_version") or "DRAFT",
                source="OUTPUT",
                content=content,
                outputScope="FULL",
            )
        except Exception:  # noqa: BLE001 - one dataset failing must not kill the rest
            log.warning("apply_guardrail failed for %s", label, exc_info=True)
            entry["verdict"] = "not_checkable"
            continue
        findings = ap.parse_ar_findings(response)
        grounding = ap.read_grounding(
            s3, bucket=chat_config.bundle_bucket,
            data_domain=data_domain, dataset=dataset,
        )
        entry["findings"] = render_findings(findings, grounding)
        entry["verdict"] = dataset_verdict(findings)
        versions[label] = _row_s(row, "ar_policy_version")

    # 8. Persist + return.
    envelope = {
        **base,
        "status": "complete",
        "eligible": True,
        "standalone_question": prepass["standalone_question"],
        "rewritten": prepass["rewritten"],
        "transcript": prepass["transcript"],
        "assumptions": prepass["assumptions"],
        "datasets": datasets,
        "policy_versions_used": versions,
    }
    write_report(
        ddb, table, user_sub, client_thread_id, turn_key, envelope,
        expires_at=expires_at,
    )
    return envelope


def _thread_fallback(slices: list[list[Any]], turn_key: int) -> list[str]:
    """Datasets from EARLIER turns, nearest first — used when this turn read nothing.

    A follow-up ("and for 2019?") often re-queries without re-reading the wiki;
    the conversation's earlier routing is the best available signal.
    """
    for i in range(turn_key - 1, -1, -1):
        labels = datasets_touched(slices[i])
        if labels:
            return labels
    return []


def _registry_row(ddb, table: str, data_domain: str, dataset: str) -> dict[str, Any]:
    from okf_aws.ar_policy import registry_key

    try:
        return (
            ddb.get_item(
                TableName=table, Key=registry_key(data_domain, dataset)
            ).get("Item")
            or {}
        )
    except Exception:  # noqa: BLE001 - treated as "no policy"
        log.warning("registry read failed (non-fatal)", exc_info=True)
        return {}


def _row_s(item: dict[str, Any], name: str) -> str:
    return str((item.get(name) or {}).get("S") or "")
