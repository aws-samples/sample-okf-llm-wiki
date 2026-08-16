"""Write the per-user conversation index row (the `okf-chat` table).

On each conversation turn the chat runtime upserts a small metadata row so the
Control API can list the user's conversations for the sidebar. The item shape +
keys are owned by ``okf_core.chat_threads`` (shared with the Control API reader).

The same row also carries the policy checks' ROLLING CONTEXT (see
docs/CONVENTIONS.md and the design doc §13.4): ``policy_curated_question`` (the
latest turn's curated standalone question), ``policy_last_answer`` (that
turn's final answer, truncated), and ``policy_question_history`` (the last few
curated questions, most recent last — the ones before the previous question
feed the next REWRITE as earlier-questions resolution context; the judges
never see them). Thread rows have no TTL, so reloading a chat
days later still chains context; all attributes are optional — a pre-v3 or
never-opted-in thread simply lacks them and the checker falls back to the raw
question (turn-1 semantics).

Isolation is structural (pk = ``CHAT#<user_sub>``). Everything here is
BEST-EFFORT: a failed write must never break the actual chat run — the
conversation + its checkpoint are the source of truth; the index row is a
convenience (sidebar) plus advisory context (policy checks).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from okf_core import chat_threads as ct

log = logging.getLogger("chat.threads")


def _now_iso(clock) -> str:
    return clock().isoformat()


def touch_thread(
    ddb,
    *,
    threads_table: str,
    user_sub: str,
    thread_id: str,
    title: str,
    model: str,
    effort: str,
    dataset_scope: dict[str, str] | None,
    now_iso: str,
) -> None:
    """Upsert the conversation index row (create-on-first-turn, touch after).

    A single ``update_item`` does both: it sets ``updated_at``/``model``/``effort``/
    scope every turn, but writes ``created_at`` and ``title`` only if_not_exists so
    the first turn seeds them and later turns leave them (the user may have renamed
    the title). Best-effort — logs and swallows on failure.
    """
    pk = ct.thread_pk(user_sub)
    sk = ct.thread_sk(thread_id)
    expr_names = {
        "#t": "title",  # reserved word in DynamoDB
    }
    expr_values: dict[str, Any] = {
        ":ua": {"S": now_iso},
        ":ca": {"S": now_iso},
        ":ti": {"S": title[: ct.TITLE_MAX]},
        ":m": {"S": model},
        ":e": {"S": effort},
    }
    set_parts = [
        "updated_at = :ua",
        "created_at = if_not_exists(created_at, :ca)",
        "#t = if_not_exists(#t, :ti)",
        "model = :m",
        "effort = :e",
    ]
    if dataset_scope:
        expr_values[":dd"] = {"S": dataset_scope["data_domain"]}
        expr_values[":ds"] = {"S": dataset_scope["dataset"]}
        set_parts += ["data_domain = :dd", "dataset = :ds"]
    try:
        ddb.update_item(
            TableName=threads_table,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except Exception:  # noqa: BLE001 - index write must never break the chat run
        log.warning("chat thread-index write failed (non-fatal)", exc_info=True)


# --- the policy checks' rolling context (design §13.3/§13.4) -------------------

#: Attribute caps: the curated question is one sentence by contract (the cap is
#: a backstop); the answer is context for the NEXT turn's rewrite, not a
#: transcript — ~2k chars carries the conclusions without bloating the row.
POLICY_QUESTION_MAX = 2000
POLICY_ANSWER_MAX = 2000
#: How many curated questions the rolling history keeps (most recent last).
#: The REWRITE receives the ones before the previous question (at most 2) as
#: earlier-questions resolution context — the judges never see the history:
#: 3 stored questions (the previous + 2 before it) is the design cap.
POLICY_HISTORY_KEEP = 3


def read_policy_state(
    ddb, *, threads_table: str, user_sub: str, thread_id: str
) -> dict[str, Any] | None:
    """The thread's rolling policy context:
    ``{curated_question, last_answer, question_history}``, or ``None``.

    Absent attributes (pre-v3 thread, deleted row, failed prior write) come
    back as ``""`` / ``[]`` — the caller's raw-question fallback IS the turn-1
    semantics. A FAILED read returns ``None`` instead: unreadable is not
    absent, and the caller must not seed-write turn-1 state over a chain it
    merely could not read. Never raises.
    """
    try:
        item = (
            ddb.get_item(
                TableName=threads_table,
                Key={
                    "pk": {"S": ct.thread_pk(user_sub)},
                    "sk": {"S": ct.thread_sk(thread_id)},
                },
            ).get("Item")
            or {}
        )
        history = [
            str(e.get("S") or "")
            for e in (item.get("policy_question_history") or {}).get("L") or []
            if isinstance(e, dict)
        ]
        return {
            "curated_question": str(
                (item.get("policy_curated_question") or {}).get("S") or ""
            ),
            "last_answer": str(
                (item.get("policy_last_answer") or {}).get("S") or ""
            ),
            "question_history": [q for q in history if q],
            # The RAW question whose curated form is the history's last entry
            # — the durable "same turn" signal an ask_human fold needs to
            # REPLACE that entry instead of appending a second one. Cleared
            # ("") by the final-answer write so it never outlives its turn
            # (an identical question RE-ASKED later must append, not replace).
            "history_last_raw": str(
                (item.get("policy_history_last_raw") or {}).get("S") or ""
            ),
        }
    except Exception:  # noqa: BLE001 - unreadable ≠ absent; caller skips writes
        log.warning("policy state read failed (non-fatal)", exc_info=True)
        return None


def write_policy_state(
    ddb,
    *,
    threads_table: str,
    user_sub: str,
    thread_id: str,
    curated_question: str | None = None,
    last_answer: str | None = None,
    question_history: list[str] | None = None,
    history_last_raw: str | None = None,
) -> None:
    """Best-effort SET of the rolling policy attributes on the THREAD row.

    Only the given attributes are touched (the curated question lands when the
    rewrite does; the answer lands at stream end; the history rides along with
    the curated-question write — the caller builds the rolled list, this just
    trims and stores it). The thread row itself is seeded by
    :func:`touch_thread` at turn start; an update racing a missing row would
    just create a bare one, which the next turn's touch fills in.
    """
    sets: list[str] = []
    values: dict[str, Any] = {}
    if curated_question is not None:
        sets.append("policy_curated_question = :cq")
        values[":cq"] = {"S": curated_question[:POLICY_QUESTION_MAX]}
    if last_answer is not None:
        sets.append("policy_last_answer = :la")
        values[":la"] = {"S": last_answer[:POLICY_ANSWER_MAX]}
    if question_history is not None:
        sets.append("policy_question_history = :qh")
        values[":qh"] = {
            "L": [
                {"S": q[:POLICY_QUESTION_MAX]}
                for q in question_history[-POLICY_HISTORY_KEEP:]
                if q
            ]
        }
    if history_last_raw is not None:
        sets.append("policy_history_last_raw = :hr")
        values[":hr"] = {"S": history_last_raw[:POLICY_QUESTION_MAX]}
    if not sets:
        return
    try:
        ddb.update_item(
            TableName=threads_table,
            Key={
                "pk": {"S": ct.thread_pk(user_sub)},
                "sk": {"S": ct.thread_sk(thread_id)},
            },
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=values,
        )
    except Exception:  # noqa: BLE001 - advisory context, never fatal
        log.warning("policy state write failed (non-fatal)", exc_info=True)


# --------------------------------------------------------------------------- #
# Long-term memory: the ask_human pause blob
# --------------------------------------------------------------------------- #

#: Size cap on the serialized blob — the turn observation is a handful of
#: dataset ids + governed-call summaries and the Q&A pairs are excerpt-capped
#: upstream, so anything near this is malformed; drop rather than bloat the row.
MEMORY_PENDING_MAX = 32000


def write_memory_pending(
    ddb,
    *,
    threads_table: str,
    user_sub: str,
    thread_id: str,
    obs: dict[str, Any] | None,
    qa: list[dict[str, str]] | None,
) -> None:
    """Persist the turn's memory context across an ask_human pause.

    A paused turn's invocation dies with its generator, and the RESUME is a
    separate invocation (possibly on a different container) — without this
    blob, governed-tool observations made before the pause and every
    clarification round except the last would vanish from the turn's memory
    event. Written at EVERY pause (each pause overwrites the previous blob,
    so a stale blob from an abandoned pause can never leak into a later
    turn's resume — the read only ever follows the most recent pause).
    Best-effort like every memory touchpoint.
    """
    try:
        blob = json.dumps({"obs": obs or {}, "qa": qa or []})
        if len(blob) > MEMORY_PENDING_MAX:
            log.warning("memory pending blob oversized (dropped)")
            return
        ddb.update_item(
            TableName=threads_table,
            Key={
                "pk": {"S": ct.thread_pk(user_sub)},
                "sk": {"S": ct.thread_sk(thread_id)},
            },
            UpdateExpression="SET memory_pending = :mp",
            ExpressionAttributeValues={":mp": {"S": blob}},
        )
    except Exception:  # noqa: BLE001 - advisory context, never fatal
        log.warning("memory pending write failed (non-fatal)", exc_info=True)


#: Cap on the per-thread observed-datasets ledger (``memory_datasets``) — a
#: thread that has genuinely touched more than this many datasets keeps the
#: most recent; the ledger is a citation-validation set, not an audit log.
MEMORY_DATASETS_MAX = 32


def read_memory_datasets(
    ddb, *, threads_table: str, user_sub: str, thread_id: str
) -> list[str]:
    """The thread's cumulative observed-dataset ledger (``memory_datasets``).

    Every dataset the harness has OBSERVED a tool touch in this thread, across
    turns. Exists so a no-tool follow-up's answer citation can still be
    corroborated (chat.memory composes the turn annotation citation-first,
    but only citations the harness can back). Never raises.
    """
    try:
        item = (
            ddb.get_item(
                TableName=threads_table,
                Key={
                    "pk": {"S": ct.thread_pk(user_sub)},
                    "sk": {"S": ct.thread_sk(thread_id)},
                },
            ).get("Item")
            or {}
        )
        raw = (item.get("memory_datasets") or {}).get("S") or ""
        if not raw:
            return []
        data = json.loads(raw)
        return [str(d) for d in data if d] if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 - unreadable degrades to empty
        log.warning("memory datasets read failed (non-fatal)", exc_info=True)
        return []


def merge_memory_datasets(
    ddb,
    *,
    threads_table: str,
    user_sub: str,
    thread_id: str,
    datasets: list[str],
) -> None:
    """Union the turn's observed datasets into the thread ledger.

    Read-modify-write, advisory: a lost race costs at most one turn's
    additions to the validation set, and the next turn re-merges. Best-effort
    like every memory touchpoint.
    """
    try:
        current = read_memory_datasets(
            ddb, threads_table=threads_table, user_sub=user_sub, thread_id=thread_id
        )
        merged = current + [d for d in datasets if d and d not in current]
        if merged == current:
            return
        merged = merged[-MEMORY_DATASETS_MAX:]
        ddb.update_item(
            TableName=threads_table,
            Key={
                "pk": {"S": ct.thread_pk(user_sub)},
                "sk": {"S": ct.thread_sk(thread_id)},
            },
            UpdateExpression="SET memory_datasets = :md",
            ExpressionAttributeValues={":md": {"S": json.dumps(merged)}},
        )
    except Exception:  # noqa: BLE001 - advisory context, never fatal
        log.warning("memory datasets write failed (non-fatal)", exc_info=True)


def read_memory_pending(
    ddb, *, threads_table: str, user_sub: str, thread_id: str
) -> dict[str, Any] | None:
    """The pause blob for a resume: ``{obs, qa}``, or ``None``. Never raises."""
    try:
        item = (
            ddb.get_item(
                TableName=threads_table,
                Key={
                    "pk": {"S": ct.thread_pk(user_sub)},
                    "sk": {"S": ct.thread_sk(thread_id)},
                },
            ).get("Item")
            or {}
        )
        raw = (item.get("memory_pending") or {}).get("S") or ""
        if not raw:
            return None
        blob = json.loads(raw)
        return blob if isinstance(blob, dict) else None
    except Exception:  # noqa: BLE001 - unreadable degrades to absent
        log.warning("memory pending read failed (non-fatal)", exc_info=True)
        return None
