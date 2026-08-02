"""Mid-turn policy checks — two tracks, per-run opt-in, advisory (v3).

The dataset's authored policy document (``policies.yaml`` — see
``okf_core.policy_doc``) tags every policy ``computational`` or
``behavioural``, and each type has its own check track, both running INSIDE
the turn so a genuine flag can still change the answer (the post-turn panel
of v2 could only report a flawed one — removed):

* **Computational** — violations visible in a SQL query itself (additivity,
  grain/collapse, fan-out joins, sentinel decoding). Judged by the
  query-time race: ``run_sql`` submits the check BEFORE dispatching the
  query, the judge fleet runs concurrently with the engine, and any
  violations ride back inside the tool result as a hedged
  ``<system-reminder>`` — the model decides the correction. Structural
  exploration gate (:func:`is_analytical_sql`), dedup per SQL, per-turn
  budget.
* **Behavioural** — process rules (ask-before-committing, refuse
  out-of-domain, require explicit scope), judged against THE STEPS the agent
  has taken so far, not any single query. Trigger point is a
  ``before_model`` middleware hook (:class:`BehaviouralPolicyMiddleware`) —
  it runs exactly once after ALL parallel tool results return, which batches
  two parallel analytical queries into ONE evaluation for free. The hook
  then WAITS (bounded, same budget as the computational wait) for the
  verdict and injects it as a steering-style ``<system-reminder>`` before
  that very model call — the step right after an analytical batch is
  usually the one that writes the answer, so a fire-and-forget eval would
  lose the race by seconds and never deliver (observed live 2026-08-02).
  The note surfaces in the UI as the same shield timeline step. Only a
  verdict slower than the budget rolls to the next hook (or drops at turn
  end — advisory, logged).

Both tracks judge against the thread's **rolling curated question**
(§13.3): turn 1 uses the raw question at zero model cost; later turns run
ONE minimal-effort rewrite — ``(previous curated question, previous final
answer, current raw question) → curated question`` — kicked on the turn's
first submitted query so it races the query's own execution. An answered
``ask_human`` clarification IS folded in, inline (deliberate v3 reversal:
the ask-first evidence lives in the behavioural STEPS track now, and the
computational judges genuinely need the clarified intent). The state
survives reload on the THREAD row (``policy_curated_question`` +
``policy_last_answer`` — ``chat.threads``), fail-open to raw-question
semantics whenever absent.

Both tracks are armed PER RUN from the composer's Policy feature
(``features: ["sql", "policy:computational" | "policy:behavioural" |
"policy:strict"]`` — ``chat.sql.policy_tracks``), underneath the deploy-time
master gate (``OKF_CHAT_POLICY_CHECK_ENABLED``). One turn-scoped
:class:`PolicyChecker` serves both tracks: constructed by the server, handed
to the SQL tool (computational) and the middleware (behavioural); shared
state = curated question, policy caches, budgets, a single worker thread.

Soft contract throughout: every failure mode (unusable policies, judge
error, timeout, state read/write failure) returns the results untouched —
no reminder, never an error into the run.

A dataset is judged only when it is enrolled, its document status is
``ready``, AND the stored source fingerprint equals one freshly computed
from the live wiki. A stale or invalid document NEVER renders a verdict —
the row is flagged and a ``policy_rebuild`` event is published (best-effort)
so the check self-heals. A pre-v3 document without ``type`` fields fails the
parse, which rides the same self-heal to a re-authored, typed document.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic as _monotonic
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

try:  # langchain is present in the runtime image + the unit venv
    from langchain.agents.middleware import AgentMiddleware
except Exception:  # pragma: no cover - only when langchain.agents is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]

log = logging.getLogger("chat.policy_check")

#: additional_kwargs key marking a harness-injected BEHAVIOURAL policy note.
#: Like the steering marker: keeps the message out of user bubbles on history
#: reload and lets the server emit it as a typed ``policy`` chunk.
POLICY_MARKER = "okf_policy"

#: Concurrent fleet rounds per turn-checker. More than one so the checks of
#: parallel tool-called queries — and a behavioural eval queued alongside —
#: run side by side instead of serializing behind a single worker (each
#: waiting site's budget burns while its check sits in the queue). 8 is
#: comfortably above the worst case (3 budgeted query checks + 1 behavioural
#: eval + the warm-up/answer-write chores). Shared checker state is guarded
#: by an RLock; the fleets themselves run outside it.
_POOL_WORKERS = 8

#: Cap on rows echoed into steps evidence per SQL result — judges need the
#: shape and a taste of the values, not the payload.
_RESULT_SAMPLE_ROWS = 5

#: Cap on any single tool-result echo in the evidence (chars).
_RESULT_CHARS = 1500


# --- message anatomy (pure) ---------------------------------------------------


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


def _is_injected(msg: Any) -> bool:
    """A harness-injected message (steering nudge or policy note) — never evidence."""
    from chat.steering import STEERING_MARKER

    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return bool(kwargs.get(STEERING_MARKER) or kwargs.get(POLICY_MARKER))


def _is_error_result(msg: Any) -> bool:
    if getattr(msg, "status", None) == "error":
        return True
    content = getattr(msg, "content", None)
    return isinstance(content, str) and content.startswith("Error:")


def _parse_json(content: Any) -> Any:
    """Parse a tool result body that may carry trailing ``<system-reminder>`` blocks.

    ``run_sql`` appends ``\\n\\n<system-reminder>…`` after the JSON payload for
    anomaly and policy reminders — model-facing framing, never data. Returns
    None when nothing parseable remains.
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


def ask_human_qa(turn: list[Any]) -> list[dict[str, str]]:
    """The answered clarification exchange(s) in a slice: ``[{prompt, answer}]``."""
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


def _summarize_result(msg: ToolMessage) -> str:
    name = getattr(msg, "name", None) or "tool"
    if _is_error_result(msg):
        return f"[{name} result: ERROR] {str(msg.content)[:200]}"
    if name == "run_sql":
        payload = _parse_json(msg.content) or {}
        return (
            f"[run_sql result] columns={payload.get('columns')} "
            f"row_count={payload.get('row_count')} truncated={payload.get('truncated')} "
            f"first_rows={json.dumps((payload.get('rows') or [])[:_RESULT_SAMPLE_ROWS], default=str)}"
        )
    body = msg.content if isinstance(msg.content, str) else json.dumps(msg.content, default=str)
    return f"[{name} result] {body[:_RESULT_CHARS]}"


# --- the rolling curated question (§13.3) ---------------------------------------

_CURATE_CONTRACT = """\
Produce ONE self-contained question that captures what the user currently
wants, by merging the pieces below. Resolve pronouns and references to the
earlier exchange ("and for 2019?" -> the full question), and fold any answered
clarifications into the question itself (a clarified "championship points"
replaces a bare "points"). Change NOTHING else: do not interpret terms nobody
clarified, do not add definitions, do not answer. If the latest message
already stands alone and nothing was clarified, return it verbatim.

Return ONE JSON object and nothing else:
{"question": string}
"""


def build_curate_input(
    *,
    prev_question: str = "",
    prev_answer: str = "",
    raw_question: str = "",
    qa: list[dict[str, str]] | None = None,
) -> str:
    """The rewrite prompt for :func:`curate_question` (pure, testable)."""
    parts: list[str] = [_CURATE_CONTRACT]
    if prev_question:
        parts += ["\n== THE PREVIOUS QUESTION (already self-contained) ==", prev_question]
    if prev_answer:
        parts += ["\n== THE ANSWER THE USER GOT TO IT ==", prev_answer[:2000]]
    if raw_question:
        parts += ["\n== THE USER'S LATEST MESSAGE ==", raw_question]
    if qa:
        parts.append("\n== CLARIFICATIONS THE USER JUST ANSWERED ==")
        parts.extend(f"Q: {x.get('prompt', '')}\nA: {x.get('answer', '')}" for x in qa)
    return "\n".join(parts)


def _reply_text(reply: Any) -> str:
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    return _text(content)


def curate_question(
    model: Any,
    *,
    prev_question: str = "",
    prev_answer: str = "",
    raw_question: str = "",
    qa: list[dict[str, str]] | None = None,
) -> str:
    """One minimal-effort rewrite call → the curated question ("" = failed).

    Fail-open: the caller falls back to the raw question (or the previous
    curated one) — a missing rewrite degrades context, never the run.
    """
    try:
        reply = model.invoke(
            [
                (
                    "user",
                    build_curate_input(
                        prev_question=prev_question,
                        prev_answer=prev_answer,
                        raw_question=raw_question,
                        qa=qa,
                    ),
                )
            ]
        )
        body = _reply_text(reply).strip()
        start, end = body.find("{"), body.rfind("}")
        parsed = json.loads(body[start : end + 1]) if start >= 0 < end else {}
        return str(parsed.get("question") or "").strip()
    except Exception:  # noqa: BLE001 - the raw question is always usable
        log.warning("policy curated-question rewrite failed", exc_info=True)
    return ""


# --- the judge fleet (map) -----------------------------------------------------


def _judge_tools() -> list[Any]:
    # IDS ONLY, deliberately: the reminder the main agent receives is built
    # from the AUTHORED policy text (condition/action/source), so judge prose
    # would only spend output tokens and leak model-generated wording into
    # the main agent's context. A judge has nothing to say beyond which
    # policies fired.
    from pydantic import BaseModel, Field

    class report_violations(BaseModel):
        """Report the VIOLATED policy ids in your shard; empty list = all clean."""

        violations: list[str] = Field(
            default_factory=list,
            description='ids of the violated policies, e.g. ["P012"] — ids only',
        )

    return [report_violations]


def _forced_tool_call(
    model: Any, tools: list[Any], prompt: str, *, tool_name: str
) -> dict[str, Any] | None:
    """Invoke with ONE required tool; retry once on a missing call; None = fail.

    The retry rebuilds the message list from scratch (never echoing the
    tool-less reply — some providers reject dangling assistant turns). A judge
    that still won't call the tool fails open as a missing shard, never a
    fabricated verdict.
    """
    bound = model.bind_tools(tools)
    demand = ""
    for _attempt in (1, 2):
        try:
            reply = bound.invoke([("user", prompt + demand)])
        except Exception:  # noqa: BLE001 - fail-open is the contract
            log.warning("policy judge call failed", exc_info=True)
            return None
        for call in getattr(reply, "tool_calls", None) or []:
            if call.get("name") == tool_name:
                args = call.get("args") or {}
                return args if isinstance(args, dict) else None
        demand = (
            f"\n\nRespond ONLY by calling the {tool_name} tool — no prose, "
            "no other tools."
        )
    return None


def judge_shard(
    model: Any, *, shard_text: str, evidence: str, prompt: str
) -> list[str] | None:
    """One mini-judge over one policy shard → violated ids. None = unjudged."""
    args = _forced_tool_call(
        model,
        _judge_tools(),
        prompt.format(shard=shard_text, evidence=evidence),
        tool_name="report_violations",
    )
    if args is None:
        return None
    raw = args.get("violations")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def judge_policies(
    model: Any,
    policies: list[dict[str, str]],
    evidence: str,
    *,
    shard_size: int,
    prompt: str,
) -> tuple[list[str], int, int]:
    """Map step: ``(flagged_policy_ids, failed_shards, total_shards)``.

    Shards run in parallel; a flagged id must belong to the judged set or it
    is dropped (a judge can only fire policies it was shown). ``prompt``
    picks the track framing (:data:`_QUERY_JUDGE_PROMPT` /
    :data:`_STEPS_JUDGE_PROMPT`).
    """
    from okf_core.policy_doc import render_policies_for_judge, shard_policies

    shards = shard_policies(policies, shard_size)
    known_ids = {p["id"] for p in policies}
    flagged: list[str] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=min(4, len(shards))) as pool:
        results = list(
            pool.map(
                lambda shard: judge_shard(
                    model,
                    shard_text=render_policies_for_judge(shard),
                    evidence=evidence,
                    prompt=prompt,
                ),
                shards,
            )
        )
    for result in results:
        if result is None:
            failed += 1
            continue
        for pid in result:
            if pid in known_ids and pid not in flagged:
                flagged.append(pid)
    return flagged, failed, len(shards)


def render_judged_findings(
    flagged_ids: list[str], policies_by_id: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    """Join flagged ids back to their AUTHORED policy text for the reminder.

    Everything the main agent will read comes from the policy document
    itself — condition, action, source — never from judge output (ids only),
    so a flag can restate the dataset's rules but never inject judge prose.
    """
    out: list[dict[str, Any]] = []
    for pid in flagged_ids:
        policy = policies_by_id.get(pid) or {}
        out.append(
            {
                "policy_id": pid,
                "condition": policy.get("condition") or "",
                "action": policy.get("action") or "",
                "source": policy.get("source") or "",
            }
        )
    return out


# --- the computational track: gate, resolution, framing ------------------------
#
# Structural exploration gate: only queries that COMPUTE something the policies
# govern are worth a judge pass. Aggregation, windowing, joins, and unions are
# the analytical tells; bare row-peeks (SELECT … LIMIT n), DISTINCT value
# enumeration, SHOW/EXPLAIN/DESCRIBE, and information_schema probes are
# exploration. A single-table aggregate still counts — `SUM(points)` over a
# standings table is the canonical additivity violation.
_ANALYTICAL_RE = re.compile(
    r"""
    \b(?:join|group\s+by|union)\b
    | \bover\s*\(
    | \b(?:count|sum|avg|min|max|approx_distinct|stddev(?:_pop|_samp)?
         |var(?:iance|_pop|_samp)|corr)\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_analytical_sql(sql: str) -> bool:
    """True when the query computes something worth judging (see gate above)."""
    s = (sql or "").strip().rstrip(";").strip()
    head = s.split(None, 1)[0].upper() if s else ""
    if head not in ("SELECT", "WITH"):
        return False
    if "information_schema" in s.lower():
        return False
    return bool(_ANALYTICAL_RE.search(s))


# Dataset resolution for UNSCOPED runs: with no @-scope there is no default
# database, so the model MUST schema-qualify every table (Athena errors
# otherwise) — the dataset is readable from the SQL itself. Only identifiers
# in FROM/JOIN position count (elsewhere, `alias."col"` looks identical to a
# qualified name); string literals and comments are stripped first so neither
# can fake a table reference. CTE names never match (they are unqualified).
# Redshift runs never reach this path: they are @-scoped by construction (the
# engine can only be built from the scope's source descriptor), so the pinned
# scope answers directly — see docs/CONVENTIONS.md.
_STRING_LIT_RE = re.compile(r"'(?:[^']|'')*'")
_QUALIFIED_REF_RE = re.compile(
    r"\b(?:from|join)\s+"
    r"((?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z_][\w$]*)){1,2})",
    re.IGNORECASE,
)


def extract_sql_schemas(sql: str) -> list[str]:
    """Distinct schema/database names the query reads, in first-use order.

    ``"db"."table"`` → ``db``; ``"catalog"."db"."table"`` → ``db`` (the
    second-from-last part). Lowercased for the registry match.
    """
    from chat.sql import strip_sql_comments

    s = _STRING_LIT_RE.sub("''", strip_sql_comments(sql or ""))
    out: list[str] = []
    for m in _QUALIFIED_REF_RE.finditer(s):
        parts = [p.strip().strip('"').lower() for p in m.group(1).split(".")]
        schema = parts[-2]
        if schema and schema not in out:
            out.append(schema)
    return out


def _enrolled_glue_map(ddb, table: str) -> dict[str, tuple[str, str]]:
    """``{glue_database (lower) -> (data_domain, dataset)}`` over ENROLLED rows.

    The mapping row's ``glue_database`` wins (a dataset id need not equal its
    Glue DB name — same contract as the server's scope enrichment); the
    dataset id is the fallback. Enrolled datasets are few, so one filtered
    scan per turn is cheap.
    """
    out: dict[str, tuple[str, str]] = {}
    kwargs: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": (
            "ar_enrolled = :t AND begins_with(pk, :p) AND begins_with(sk, :s)"
        ),
        "ExpressionAttributeValues": {
            ":t": {"BOOL": True},
            ":p": {"S": "DOMAIN#"},
            ":s": {"S": "DATASET#"},
        },
    }
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items") or []:
            domain = _row_s(item, "pk").removeprefix("DOMAIN#")
            dataset = _row_s(item, "sk").removeprefix("DATASET#")
            glue = _row_s(item, "glue_database") or dataset
            if domain and dataset:
                out[glue.lower()] = (domain, dataset)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return out
        kwargs["ExclusiveStartKey"] = lek


_QUERY_JUDGE_PROMPT = """\
You are one judge in a fleet auditing ONE SQL query that an AI data analyst is
running MID-TURN against a dataset's documented COMPUTATIONAL policies. You
received a SHARD of the policies — judge ONLY these, ONLY against the material
below. You are not a general fact-checker, and you cannot see the query's
results.

The turn is IN PROGRESS: the final answer does not exist yet, so obligations
about the eventual answer (disclosures, caveats, how results get presented)
are NOT violations here. Flag ONLY what this query itself already commits: an
invalid computation, an ungoverned collapse or DISTINCT, a forbidden mapping
or join, treating absence as zero, and the like. The user's request may be
absent or abbreviated and earlier exploration is not shown — when a violation
depends on material you cannot see, prefer clean.

Judge conduct, not keywords, with common sense:
- A policy applies only when its condition genuinely occurs in this query.
  A condition gated on the user asking something is about the user's request,
  never about the agent's SQL choices.
- These policies are distilled from the dataset's own documentation; SQL that
  follows the documented handling (excluding flagged marker rows,
  pre-aggregating a child side, using the documented keys) is compliance.
- Peeking at rows, ranges, or distinct values is legitimate exploration; only
  what the query computes can violate a policy.

Respond ONLY by calling report_violations, with the IDS of the policies this
query violates (e.g. ["P012"]) — ids only, no explanations, no other fields;
all-clean is an empty list. The recipient reads each flagged policy's own
text, so your reasoning stays private. Ids outside your shard are discarded.

== THE POLICIES IN YOUR SHARD ==
{shard}

== THE QUERY MATERIAL ==
{evidence}
"""


def build_query_evidence(question: str, sql: str) -> str:
    """The computational judge material: the curated request (when known) + SQL."""
    parts: list[str] = []
    if question:
        parts += ["== THE USER'S REQUEST (verbatim) ==", question]
    parts += ["== THE SQL QUERY THE AGENT IS RUNNING ==", sql]
    return "\n".join(parts)


# --- the behavioural track: steps evidence + framing ----------------------------

_STEPS_JUDGE_PROMPT = """\
You are one judge in a fleet auditing the steps an AI data analyst has taken
SO FAR in an IN-PROGRESS turn, against a dataset's documented BEHAVIOURAL
policies (process rules: asking before committing, refusing out-of-domain
requests, requiring explicit scope). You received a SHARD of the policies —
judge ONLY these, ONLY against the material below. You are not a general
fact-checker.

The turn is IN PROGRESS: the final answer does not exist yet. Flag ONLY
conduct the agent has already COMMITTED — computing results on top of an
unresolved ambiguity a policy says to clarify first IS committed conduct;
not-having-asked-YET, or not-having-disclosed-yet, is not (the agent can
still ask or disclose before answering). Obligations about the eventual
answer's wording are premature here.

The material lays out the steps in order: the user's request, any
clarifications the agent requested through its ask_human tool (ask_human
pauses the turn, puts a question to the user, and resumes with their answer —
it is the agent's prescribed way to resolve ambiguity, never a fault; the
exchange being present is evidence the agent ASKED), and the tool calls made
with their results.

Judge conduct, not keywords — read the steps as one narrative and use common
sense about what actually happened:
- A policy applies only when its condition genuinely occurred in THESE steps.
  Read conditions literally about WHO acts: a condition gated on the user
  asking something is about the user's request, not the agent's choices.
- These policies are distilled from the dataset's own documentation. An agent
  that follows, repeats, or cites that documentation's handling and
  vocabulary is applying the docs, not inventing knowledge.
- The policies work as a set: conduct one policy prescribes (asking first,
  excluding flagged rows, refusing) is compliance, even when its wording
  superficially resembles another policy's violation.

Respond ONLY by calling report_violations, with the IDS of the policies
these steps violate (e.g. ["P012"]) — ids only, no explanations, no other
fields; all-clean is an empty list. The recipient reads each flagged
policy's own text, so your reasoning stays private. Ids outside your shard
are discarded.

== THE POLICIES IN YOUR SHARD ==
{shard}

== THE STEPS SO FAR ==
{evidence}
"""


def analytical_sqls(turn: list[Any]) -> list[str]:
    """SQL texts of successful ANALYTICAL ``run_sql`` calls in a slice, in order.

    The SQL lives in the AIMessage tool_calls (matched to results by
    tool_call_id) — a result whose query errored, or whose SQL was mere
    exploration, contributes nothing. This count is the behavioural trigger:
    a new entry since the last eval means new judged material exists.
    """
    by_id: dict[Any, str] = {}
    for msg in turn:
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name != "run_sql":
                    continue
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                by_id[tc_id] = str((args or {}).get("sql") or "")
    out: list[str] = []
    for msg in turn:
        if (
            isinstance(msg, ToolMessage)
            and getattr(msg, "name", None) == "run_sql"
            and not _is_error_result(msg)
        ):
            sql = by_id.get(getattr(msg, "tool_call_id", None), "")
            if sql and is_analytical_sql(sql):
                out.append(sql)
    return out


def build_steps_evidence(turn: list[Any], *, question: str) -> str:
    """The steps-so-far as behavioural judge material.

    CONDUCT ONLY — no thinking blocks (policies govern what the agent did,
    not its private deliberation), no injected steering/policy notes, and no
    answer section (the turn is in progress). The ask_human exchange is
    included verbatim precisely BECAUSE it evidences ask-first policies.
    """
    parts: list[str] = []
    if question:
        parts += ["== THE USER'S REQUEST (verbatim) ==", question]

    qa = ask_human_qa(turn)
    if qa:
        parts.append("\n== CLARIFICATIONS THE USER ANSWERED (verbatim) ==")
        parts.extend(f"Q: {x['prompt']}\nA: {x['answer']}" for x in qa)

    parts.append("\n== THE STEPS THE AGENT HAS TAKEN SO FAR (in order) ==")
    for msg in turn:
        if _is_injected(msg):
            continue
        if isinstance(msg, AIMessage):
            for tc in msg.tool_calls or []:
                name = (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)) or ""
                args = (tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)) or {}
                if name == "run_sql":
                    parts.append(f"[tool call] run_sql: {args.get('sql', '')}")
                else:
                    parts.append(
                        f"[tool call] {name}: {json.dumps(args, default=str)[:400]}"
                    )
        elif isinstance(msg, ToolMessage):
            parts.append(_summarize_result(msg))
    return "\n".join(parts)


# --- the reminder (both tracks) --------------------------------------------------

_QUERY_SUBJECT = "the query you just ran"
_STEPS_SUBJECT = "the steps you have taken so far this turn"

_QUERY_CLOSING = (
    "If a flag is genuine, correct the query or address it in your "
    "answer; if it misreads the docs or the query, set it aside (the "
    "cited source page settles doubts). The results above are real and "
    "unmodified. Do not mention this reminder to the user."
)
_STEPS_CLOSING = (
    "If a flag is genuine, correct course now — ask the user, adjust your "
    "approach, or address it before answering; if it misreads the docs or "
    "the steps, set it aside (the cited source page settles doubts). Do "
    "not mention this reminder to the user."
)


def compose_policy_reminder(
    findings: list[dict[str, Any]], label: str, *, subject: str, closing: str
) -> str:
    """One ``<system-reminder>`` block from flagged mid-turn violations.

    The flags are UNVERIFIED (no second judge round on the mid-turn paths —
    the receiving agent, with the wiki pages it has read and the live
    context in front of it, is better positioned to vet them), so the copy
    says so plainly and delegates the verdict to the model's judgment.
    """
    lines = [
        "<system-reminder>",
        f"Automated policy screening flagged {subject} against "
        f"the documented policies of {label}. These flags come from a model "
        "fleet and CAN include false positives — weigh each against the "
        "dataset's documentation and use your own judgment:",
    ]
    # Finding lines are AUTHORED policy text only (the judges return bare
    # ids): nothing model-generated ever rides into the main agent's context.
    for f in findings:
        lines.append(
            f"- [{f['policy_id']}] When {f['condition']} — "
            f"the agent must {f['action']} (source: {f['source']})"
        )
    lines.append(closing)
    lines.append("</system-reminder>")
    return "\n".join(lines)


def policy_display(text: str) -> str:
    """The user-visible slice of a policy reminder: the finding lines only.

    The framing sentences ("use your own judgment", "do not mention this
    reminder") are model-facing instructions, not user reading material.
    """
    findings = [line for line in (text or "").splitlines() if line.startswith("- [")]
    return "\n".join(findings) or "A documented policy was flagged."


# The reminder block, as embedded in a run_sql ToolMessage. The server splits
# it back out of the tool content so the UI can render the flag as its own
# timeline step (shield marker, like the steering bulbs) instead of burying it
# inside the result payload. The MODEL always sees the full string — this
# extraction shapes only the UI chunks. (Behavioural notes never need this:
# they are their own POLICY_MARKER messages, not embedded in tool results.)
_POLICY_REMINDER_RE = re.compile(
    r"\n*<system-reminder>\nAutomated policy screening.*?</system-reminder>",
    re.DOTALL,
)


def split_policy_reminder(content: str) -> tuple[str, str]:
    """``(content without the reminder block, the flag display text or "")``."""
    if not content or "Automated policy screening" not in content:
        return content, ""
    matches = list(_POLICY_REMINDER_RE.finditer(content))
    if not matches:
        return content, ""
    # A cross-dataset query appends one block per dataset — join their lines.
    display = policy_display("\n".join(m.group(0) for m in matches))
    return _POLICY_REMINDER_RE.sub("", content).strip(), display


# --- the turn-scoped checker (both tracks) ---------------------------------------


class PolicyChecker:
    """Per-turn policy checker serving both tracks (one instance per run).

    Constructed by the server when the deploy master gate is on AND the run
    opted into at least one track; handed to the SQL tool (computational) and
    the behavioural middleware. Heavy work runs on a small worker pool
    (:data:`_POOL_WORKERS`) so concurrent checks — parallel tool-called
    queries, a behavioural eval alongside — don't serialize behind each
    other while their callers' wait budgets burn. Cold-start state (boto3
    clients, the judge + rewrite models, the freshness gate, the
    curated-question rewrite, the loaded policy sets) is built lazily on the
    first submitted work and cached for the turn; every shared cache is
    guarded by one RLock, held for the bookkeeping and the one-time gates but
    NEVER across a judge fleet call.

    Computational: ``submit(sql)`` returns a Future the tool resolves AFTER
    the query comes back, bounded by ``wait_budget_s`` — the judges run
    concurrently with the engine. Behavioural: ``submit_behavioural(window)``
    returns a Future the middleware waits on (same budget). Soft contract:
    any failure returns "" (no reminder), never an error into the run.

    Which dataset's policies apply: an @-scoped run pins it via
    ``data_domain``/``dataset`` (Redshift runs are ALWAYS pinned — scoped by
    construction). UNSCOPED Athena runs read the dataset from the SQL itself
    (qualified names in FROM/JOIN position → the enrolled-dataset Glue map);
    a query touching several enrolled datasets is judged against each, and
    one touching only un-enrolled data is skipped for free.
    """

    def __init__(
        self,
        *,
        chat_config: Any,
        tracks: Any = (),
        data_domain: str = "",
        dataset: str = "",
        question: str = "",
        user_sub: str = "",
        thread_id: str = "",
        clients: dict[str, Any] | None = None,
        judge_model: Any = None,
        rewrite_model: Any = None,
    ) -> None:
        self._cfg = chat_config
        self.tracks = frozenset(tracks)
        self._domain = data_domain
        self._dataset = dataset
        self._question = question
        self._user_sub = user_sub
        self._thread_id = thread_id
        self._clients = clients
        self._judge = judge_model
        self._rewriter = rewrite_model
        # (domain, dataset) -> usable policy list, or None (gated: unusable).
        self._policies_by_ds: dict[tuple[str, str], list[dict[str, str]] | None] = {}
        self._glue_map: dict[str, tuple[str, str]] | None = None
        self._notes: dict[str, str] = {}  # normalized SQL -> reminder ("" = clean)
        self._checks = 0
        self._warmed = False
        self._curated_q: str | None = None
        self._ask_qa: list[dict[str, str]] = []
        # Behavioural flags already reported this turn (never nag twice).
        self._behaviour_flagged: set[tuple[str, str, str]] = set()
        # Re-entrant because the one-time gates nest (warm-up -> curated
        # question -> policy state read) while holding it.
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=_POOL_WORKERS, thread_name_prefix="policy-check"
        )
        self.wait_budget_s: float = float(
            getattr(chat_config, "policy_query_timeout_s", 60)
        )

    def wants(self, track: str) -> bool:
        """Whether the run opted this track in ("computational"/"behavioural")."""
        return track in self.tracks

    # -- caller side ---------------------------------------------------------------

    def submit(self, sql: str):
        """Queue the computational check; cheap for exploration/duplicate SQL."""
        return self._pool.submit(self._check, sql)

    def should_wait(self, sql: str) -> bool:
        """Whether run_sql should block on this query's verdict (pure, no I/O).

        Only analytical queries can produce a reminder — waiting on an
        exploration query's future would stall it behind whatever the worker
        is doing (e.g. the warm-up), for a guaranteed-empty verdict.
        """
        return is_analytical_sql(sql)

    def submit_behavioural(self, messages: list[Any]):
        """Queue ONE behavioural eval over the steps-so-far (middleware calls).

        The slice is copied here (caller thread) so the worker never races the
        graph's own state updates.
        """
        return self._pool.submit(self._check_behavioural, list(messages))

    def prewarm(self) -> None:
        """Queue the cold-start work without a query (middleware calls, once).

        A behavioural-only run has no ``submit(sql)`` to piggyback the warm-up
        on, so its first eval would pay model build + curated rewrite + gates
        INSIDE the wait window. Kicked on the middleware's first hook instead
        — the turn's exploration phase absorbs it. Idempotent (``_warmed``).
        """
        try:
            self._pool.submit(self._warm_once)
        except Exception:  # noqa: BLE001 - warm-up is best-effort
            log.warning("policy prewarm submit failed (non-fatal)", exc_info=True)

    def fold_clarifications(self, raw_question: str, qa: list[dict[str, str]]) -> None:
        """Fold an answered ask_human exchange into the curated question.

        Called by the server on the ``answer_human`` resume, BEFORE any query
        runs on the rebuilt agent: the fresh checker re-runs the rewrite with
        the clarification Q&A included (the deliberate v3 reversal — the
        ask-first evidence lives in the behavioural steps track, and the
        computational judges need the clarified intent).
        """
        if raw_question and not self._question:
            self._question = raw_question
        if qa:
            self._ask_qa = list(qa)
        self._curated_q = None  # recompute with the fold on next use

    def record_final_answer(self, answer_text: str) -> None:
        """Persist the turn's final answer for the NEXT turn's rewrite (async).

        Queued on the worker so stream teardown never blocks on DynamoDB;
        best-effort like every policy-state write.
        """
        if not (self._user_sub and self._thread_id and answer_text):
            return
        from chat.threads import write_policy_state

        try:
            self._pool.submit(
                write_policy_state,
                self._ddb(),
                threads_table=self._cfg.threads_table,
                user_sub=self._user_sub,
                thread_id=self._thread_id,
                last_answer=answer_text,
            )
        except Exception:  # noqa: BLE001 - advisory context, never fatal
            log.warning("policy answer write submit failed (non-fatal)", exc_info=True)

    # -- worker side ----------------------------------------------------------------

    def _check(self, sql: str) -> str:
        try:
            self._warm_once()
            return self._check_inner(sql)
        except Exception:  # noqa: BLE001 - soft check, results must flow
            log.warning("query policy check failed (non-fatal)", exc_info=True)
            return ""

    def _warm_once(self) -> None:
        """Front-load the cold start into the turn's FIRST work item (usually
        an exploration probe or the middleware's prewarm — nobody waits on
        it): judge-model build, the curated question rewrite (racing the
        query's own execution — the first analytical check then waits on a
        warm cache), and the freshness gate / enrolled-dataset map. Without
        this, the first analytical query pays it all inside its own verdict
        window — the observed cause of dropped verdicts. Runs under the lock:
        concurrent workers block here until the caches they need exist.
        """
        with self._lock:
            if self._warmed:
                return
            self._warmed = True
            try:
                self._judge_model()
                self._curated()
                if self._domain and self._dataset:
                    self._load_policies(self._domain, self._dataset)
                elif self._glue_map is None:
                    self._glue_map = _enrolled_glue_map(
                        self._ddb(), self._cfg.registry_table
                    )
            except Exception:  # noqa: BLE001 - warm-up is best-effort
                log.warning("policy warm-up failed (non-fatal)", exc_info=True)

    def _check_inner(self, sql: str) -> str:
        if not is_analytical_sql(sql):
            return ""
        key = " ".join(sql.split())
        max_checks = int(getattr(self._cfg, "policy_query_max_per_turn", 3))
        with self._lock:
            if key in self._notes:
                return self._notes[key]
            if self._checks >= max_checks:
                log.info(
                    "query policy check budget (%d) exhausted this turn", max_checks
                )
                return ""
        from okf_core.policy_doc import policies_of_type

        evidence = build_query_evidence(self._curated(), sql)
        notes: list[str] = []
        judged_query = False
        for domain, dataset in self._resolve_datasets([sql]):
            policies = policies_of_type(
                self._load_policies(domain, dataset) or [], "computational"
            )
            if not policies:
                continue
            if not judged_query:
                # The budget counts QUERIES, not datasets — a rare
                # cross-dataset join costs two fleets but one budget unit.
                # Re-checked under the lock: concurrent checks race the
                # pre-check above, and the cap must hold across them.
                with self._lock:
                    if self._checks >= max_checks:
                        log.info(
                            "query policy check budget (%d) exhausted this turn",
                            max_checks,
                        )
                        return ""
                    self._checks += 1
                judged_query = True
            findings = self._run_fleet(
                domain, dataset, policies, evidence, _QUERY_JUDGE_PROMPT
            )
            if findings:
                notes.append(
                    compose_policy_reminder(
                        findings,
                        f"{domain}/{dataset}",
                        subject=_QUERY_SUBJECT,
                        closing=_QUERY_CLOSING,
                    )
                )
        note = "\n\n".join(notes)
        with self._lock:
            self._notes[key] = note
        return note

    def _check_behavioural(self, window: list[Any]) -> str:
        try:
            self._warm_once()
            return self._check_behavioural_inner(window)
        except Exception:  # noqa: BLE001 - soft check, the turn must flow
            log.warning("behavioural policy check failed (non-fatal)", exc_info=True)
            return ""

    def _check_behavioural_inner(self, window: list[Any]) -> str:
        from okf_core.policy_doc import policies_of_type

        sqls = analytical_sqls(window)
        evidence = build_steps_evidence(window, question=self._curated())
        notes: list[str] = []
        for domain, dataset in self._resolve_datasets(sqls):
            policies = policies_of_type(
                self._load_policies(domain, dataset) or [], "behavioural"
            )
            if not policies:
                continue
            findings = self._run_fleet(
                domain, dataset, policies, evidence, _STEPS_JUDGE_PROMPT
            )
            # Never nag: a behavioural policy flagged once this turn stays
            # flagged — the model was told; repeating it on every later eval
            # of the (growing) same window is noise.
            fresh = []
            with self._lock:
                for f in findings:
                    fkey = (domain, dataset, f["policy_id"])
                    if fkey in self._behaviour_flagged:
                        continue
                    self._behaviour_flagged.add(fkey)
                    fresh.append(f)
            if fresh:
                notes.append(
                    compose_policy_reminder(
                        fresh,
                        f"{domain}/{dataset}",
                        subject=_STEPS_SUBJECT,
                        closing=_STEPS_CLOSING,
                    )
                )
        return "\n\n".join(notes)

    def _run_fleet(
        self,
        domain: str,
        dataset: str,
        policies: list[dict[str, str]],
        evidence: str,
        prompt: str,
    ) -> list[dict[str, Any]]:
        """Run one track's fleet over one dataset; flagged findings.

        The judges return violated IDS only — unverified by design; the
        receiving agent is the verifier (the reminder copy says so), and the
        reminder text itself is authored policy text, never judge prose.
        """
        t0 = _monotonic()
        flagged, _failed, _total = judge_policies(
            self._judge_model(),
            policies,
            evidence,
            shard_size=self._cfg.policy_shard_size,
            prompt=prompt,
        )
        log.info(
            "policy fleet for %s/%s: %d policies, %d flagged, %.1fs",
            domain, dataset, len(policies), len(flagged), _monotonic() - t0,
        )
        return render_judged_findings(flagged, {p["id"]: p for p in policies})

    # -- the rolling curated question -------------------------------------------------

    def _curated(self) -> str:
        with self._lock:
            if self._curated_q is None:
                self._curated_q = self._curate()
            return self._curated_q

    def _curate(self) -> str:
        """Resolve the turn's curated question (worker-side, once per turn).

        Turn 1 (no stored state, no fold) is the raw question at zero model
        cost. Otherwise: ONE minimal-effort rewrite over (previous curated
        question, previous answer, current raw question, any folded
        clarifications), persisted to the THREAD row when it lands so the
        next turn — days later, after a reload — picks up the chain.
        """
        from chat.threads import read_policy_state, write_policy_state

        raw = self._question
        prev_q = prev_a = ""
        if self._user_sub and self._thread_id:
            state = read_policy_state(
                self._ddb(),
                threads_table=self._cfg.threads_table,
                user_sub=self._user_sub,
                thread_id=self._thread_id,
            )
            prev_q = state.get("curated_question") or ""
            prev_a = state.get("last_answer") or ""
        if not prev_q and not prev_a and not self._ask_qa:
            # Turn-1 semantics — a fresh thread, a pre-v3 thread, or the turn
            # where policy was first enabled MID-conversation (no state was
            # ever written because no earlier run was armed; the checker
            # deliberately never re-reads the whole history). The raw question
            # IS the curated question, zero model calls — but it must still be
            # PERSISTED, or the next turn would find nothing to chain from and
            # the rolling rewrite could never start.
            if raw and self._user_sub and self._thread_id:
                write_policy_state(
                    self._ddb(),
                    threads_table=self._cfg.threads_table,
                    user_sub=self._user_sub,
                    thread_id=self._thread_id,
                    curated_question=raw,
                )
            return raw
        # prev_q may still be empty here (e.g. the previous armed turn ran no
        # SQL, so only its ANSWER was recorded) — the answer alone still gives
        # the rewrite the context to resolve a fragment, so it runs.
        curated = curate_question(
            self._rewrite_model(),
            prev_question=prev_q,
            prev_answer=prev_a,
            raw_question=raw,
            qa=self._ask_qa,
        )
        curated = curated or raw or prev_q
        if curated and curated != prev_q and self._user_sub and self._thread_id:
            write_policy_state(
                self._ddb(),
                threads_table=self._cfg.threads_table,
                user_sub=self._user_sub,
                thread_id=self._thread_id,
                curated_question=curated,
            )
        return curated

    # -- dataset resolution + the usability gate ---------------------------------------

    def _resolve_datasets(self, sqls: list[str]) -> list[tuple[str, str]]:
        """The dataset(s) whose policies govern this material.

        A pinned scope answers directly (Redshift runs always land here —
        scoped by construction). Otherwise the schemas the SQL reads
        (FROM/JOIN qualified names — mandatory without a default database)
        are matched against the enrolled datasets' Glue map, built once per
        turn. No match = nothing enrolled to judge against = skip for free.
        """
        if self._domain and self._dataset:
            return [(self._domain, self._dataset)]
        schemas: list[str] = []
        for sql in sqls:
            for schema in extract_sql_schemas(sql):
                if schema not in schemas:
                    schemas.append(schema)
        if not schemas:
            return []
        with self._lock:
            if self._glue_map is None:
                self._glue_map = _enrolled_glue_map(
                    self._ddb(), self._cfg.registry_table
                )
        seen: list[tuple[str, str]] = []
        for schema in schemas:
            pair = self._glue_map.get(schema)
            if pair and pair not in seen:
                seen.append(pair)
        return seen

    def _load_policies(
        self, domain: str, dataset: str
    ) -> list[dict[str, str]] | None:
        """The dataset's usable policy set (BOTH types), gated ONCE per turn.

        Unusable states (not enrolled, building, failed, stale fingerprint,
        missing/invalid document — including a pre-v3 document without
        ``type`` fields) yield None for the rest of the turn; a stale or
        document-less READY row additionally publishes the rebuild event —
        the soft check self-heals. Runs under the lock: without it, a
        concurrent check racing the gate would read the ``None`` sentinel
        mid-load and silently skip a dataset that is actually usable.
        """
        with self._lock:
            return self._load_policies_locked(domain, dataset)

    def _load_policies_locked(
        self, domain: str, dataset: str
    ) -> list[dict[str, str]] | None:
        from okf_aws import ar_policy as ap
        from okf_core import policy_doc as pdoc

        cache_key = (domain, dataset)
        if cache_key in self._policies_by_ds:
            return self._policies_by_ds[cache_key]
        self._policies_by_ds[cache_key] = None  # gate ran; overwrite on success

        ddb, s3 = self._ddb(), self._s3()
        row = _registry_row(ddb, self._cfg.registry_table, domain, dataset)
        if not ap.is_enrolled(row):
            return None
        status = _row_s(row, ap.ATTR_BUILD_STATUS)
        if status not in ap.USABLE_BUILD_STATUSES:
            return None
        fresh = ap.source_hash(s3, self._cfg.bundle_bucket, domain, dataset)
        if not fresh or fresh != _row_s(row, ap.ATTR_SOURCE_HASH):
            try:
                ap.flag_stale(
                    ddb,
                    self._cfg.registry_table,
                    data_domain=domain,
                    dataset=dataset,
                )
            except Exception:  # noqa: BLE001 - reconcile re-derives this
                log.warning("flag_stale failed (non-fatal)", exc_info=True)
            _publish_rebuild(self._events(), domain, dataset)
            return None
        doc = ap.read_policy_doc(
            s3, bucket=self._cfg.bundle_bucket,
            data_domain=domain, dataset=dataset,
        )
        policies: list[dict[str, str]] = []
        if doc is not None:
            try:
                policies = pdoc.parse_policies(doc)
            except pdoc.PolicyDocError:
                log.warning(
                    "stored policy doc for %s/%s is invalid", domain, dataset
                )
        if not policies:
            _publish_rebuild(self._events(), domain, dataset)
            return None
        self._policies_by_ds[cache_key] = policies
        return policies

    # -- lazy seams (tests inject via the constructor) ---------------------------------

    def _boto(self, name: str):
        with self._lock:
            if self._clients is None:
                self._clients = {}
            if name not in self._clients:
                import boto3

                self._clients[name] = boto3.client(
                    name if name != "ddb" else "dynamodb",
                    region_name=self._cfg.region,
                )
            return self._clients[name]

    def _ddb(self):
        return self._boto("ddb")

    def _s3(self):
        return self._boto("s3")

    def _events(self):
        return self._boto("events")

    def _judge_model(self):
        with self._lock:
            if self._judge is None:
                from chat.config import build_policy_judge_model

                self._judge = build_policy_judge_model(self._cfg)
            return self._judge

    def _rewrite_model(self):
        with self._lock:
            if self._rewriter is None:
                from chat.config import build_policy_check_model

                self._rewriter = build_policy_check_model(self._cfg)
            return self._rewriter


def make_policy_checker(
    chat_config: Any,
    *,
    tracks: Any,
    scope: dict[str, str] | None,
    question: str,
    user_sub: str,
    thread_id: str,
) -> PolicyChecker | None:
    """The run's checker, or None (deploy gate off / no track opted in).

    Construction is cheap — clients, both models, the freshness gate, and
    the curated question are all lazy inside the checker's worker thread.
    """
    tracks = frozenset(tracks or ())
    if not tracks or not getattr(chat_config, "policy_check_enabled", False):
        return None
    return PolicyChecker(
        chat_config=chat_config,
        tracks=tracks,
        data_domain=(scope or {}).get("data_domain") or "",
        dataset=(scope or {}).get("dataset") or "",
        question=question,
        user_sub=user_sub,
        thread_id=thread_id,
    )


# --- the behavioural middleware ------------------------------------------------


class BehaviouralPolicyMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Batch-evaluate behavioural policies over the steps-so-far (before_model).

    ``before_model`` runs exactly once after ALL parallel tool results return
    — two analytical queries fired in parallel produce ONE evaluation
    covering everything up to the last one, with no debounce logic. The hook
    kicks the eval when successful analytical results have appeared since the
    last one, then WAITS for the verdict (bounded by the checker's
    ``wait_budget_s``) and injects it as a marker ``HumanMessage`` before
    THIS model call (the Converse adapter merges it into the tool-result user
    message, like steering). The wait is deliberate: the model step right
    after an analytical batch is usually the one that writes the FINAL
    answer, so a fire-and-forget eval loses that race by seconds and its
    verdict never delivers at all (observed live) — the whole point of the
    mid-turn check is changing the answer, which needs the flag IN CONTEXT
    before the answer is written. A verdict slower than the budget rolls to
    the next hook (if any); everything stays fail-open.

    The trigger is NEW analytical queries only: the first hook of a run
    baselines the count already in the window (an ask_human resume rebuilds
    this middleware, and without the baseline the pre-ask queries — already
    evaluated by the run that issued them — would re-trigger an eval on
    every resume). The first hook also pre-warms the checker so the eventual
    eval spends its wait on the fleet only, not on cold-start. One eval in
    flight at a time. Both sync and async hooks are implemented because the
    framework's defaults are no-ops and the server runs ``astream`` — the
    async variant waits via ``asyncio.to_thread`` so the event loop (and the
    SSE stream / health checks riding it) never blocks.
    """

    def __init__(self, checker: PolicyChecker) -> None:
        super().__init__()
        self._checker = checker
        self._future: Any = None
        # Analytical results already covered by a kicked eval. None until the
        # first hook BASELINES it to whatever the window already holds: zero
        # on a fresh send, the pre-ask count on an ask_human RESUME (the
        # middleware is rebuilt there, and without the baseline the old
        # queries would read as new and re-trigger an eval on every resume —
        # the trigger contract is NEW complex queries only, never the resume
        # itself; observed live 2026-08-02).
        self._covered: int | None = None
        self._prewarmed = False

    def before_model(self, state, runtime=None):  # type: ignore[override]
        self._maybe_kick(state)
        if self._future is None:
            return None
        return self._inject(self._await_note())

    async def abefore_model(self, state, runtime=None):  # type: ignore[override]
        import asyncio

        self._maybe_kick(state)
        if self._future is None:
            return None
        return self._inject(await asyncio.to_thread(self._await_note))

    def _maybe_kick(self, state) -> None:
        from chat.steering import turn_slice

        try:
            messages = list(state.get("messages") or [])
        except AttributeError:  # a state object without .get — nothing to do
            return
        if not self._prewarmed:
            self._prewarmed = True
            self._checker.prewarm()
        window = turn_slice(messages)
        count = len(analytical_sqls(window))
        if self._covered is None:
            # First sight of the window: material already here was either
            # evaluated by the run that produced it (pre-ask) or is a fresh
            # turn's empty slate — either way it is not a trigger.
            self._covered = count
            return
        if count > self._covered and self._future is None:
            self._covered = count
            try:
                self._future = self._checker.submit_behavioural(window)
            except Exception:  # noqa: BLE001 - advisory, never fatal
                log.warning(
                    "behavioural policy submit failed (non-fatal)", exc_info=True
                )

    def _await_note(self) -> str:
        """Resolve the pending eval within the budget; "" = nothing to inject.

        On a timeout the future is KEPT — a later hook (if the turn continues)
        gets another window; a turn that ends first drops it (advisory). Any
        error consumes the future and fails open.
        """
        from concurrent.futures import TimeoutError as FuturesTimeout

        try:
            note = self._future.result(
                timeout=getattr(self._checker, "wait_budget_s", 60)
            )
            self._future = None
            return note or ""
        except FuturesTimeout:
            log.info(
                "behavioural policy verdict not ready in time; "
                "deferred to the next model step (advisory)"
            )
            return ""
        except Exception:  # noqa: BLE001 - advisory, never fatal
            log.warning("behavioural policy eval failed (non-fatal)", exc_info=True)
            self._future = None
            return ""

    def _inject(self, note: str) -> dict[str, Any] | None:
        if not note:
            return None
        return {
            "messages": [
                HumanMessage(
                    content=note,
                    additional_kwargs={POLICY_MARKER: "behavioural"},
                )
            ]
        }


# --- shared plumbing ---------------------------------------------------------------


def _publish_rebuild(events, data_domain: str, dataset: str) -> None:
    """Best-effort ``policy_rebuild`` publish — a stale check STARTS the repair."""
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
