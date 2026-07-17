"""Write the per-user conversation index row (the `okf-chat` table).

On each conversation turn the chat runtime upserts a small metadata row so the
Control API can list the user's conversations for the sidebar. The item shape +
keys are owned by ``okf_core.chat_threads`` (shared with the Control API reader).

Isolation is structural (pk = ``CHAT#<user_sub>``). This is BEST-EFFORT: a failed
index write must never break the actual chat run — the conversation + its
checkpoint are the source of truth; the index is a convenience for the sidebar.
"""

from __future__ import annotations

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
