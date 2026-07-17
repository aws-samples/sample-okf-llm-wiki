"""Chat conversation index — the per-user list of conversations the UI shows.

Pure-Python primitives shared by the chat runtime (which CREATES/TOUCHES a row
on each conversation turn) and the Control API (which LISTS/RENAMES/DELETES them
for the sidebar). No AWS deps — the boto3 calls live in the services; this module
owns the *invariants*: the DynamoDB key shape, the item fields, and the terminal
TTL. Mirrors ``okf_core.annotations``.

## The item (``okf-chat`` table, CONVENTIONS.md)

    pk = "CHAT#<user_sub>"        # user isolation is STRUCTURAL
    sk = "THREAD#<thread_id>"

The caller's Cognito ``sub`` (immutable, opaque, ``#``-free) is baked into the
partition key, so a user's Query can only ever return their OWN conversations —
there is no cross-user read path to forget an ``if`` on. This is separate from
the LangGraph ``DynamoDBSaver`` checkpoint table (keyed by the sub-namespaced
thread id); this index only carries the metadata the sidebar needs.

``thread_id`` here is the CLIENT-FACING conversation id the browser sends as the
AG-UI ``threadId`` (NOT the ``<sub>:<thread_id>`` checkpoint-namespaced form).
Deleting a conversation removes this row AND purges the checkpoint via the
saver's ``delete_thread`` (see the Control API).
"""

from __future__ import annotations

from typing import Any

# Terminal (deleted) rows linger this long before DynamoDB's TTL (``expires_at``,
# epoch seconds) reaps them; an ACTIVE conversation carries no ``expires_at`` and
# never expires — the attribute is set ONLY on delete. A short window keeps a
# just-deleted conversation recoverable/consistent without lingering forever.
DELETED_TTL_SECONDS = 24 * 60 * 60  # 1 day

# Bounds on stored free-text so a hostile/oversized title can't bloat the row.
TITLE_MAX = 200


def thread_pk(user_sub: str) -> str:
    """Partition key that scopes conversations to one user.

    ``user_sub`` is the caller's immutable Cognito ``sub``. It must be non-empty
    (a missing subject would collapse everyone into one shared partition), so the
    caller is responsible for rejecting an unauthenticated request before here.
    """
    if not user_sub:
        raise ValueError("thread_pk requires a non-empty user_sub")
    return f"CHAT#{user_sub}"


def thread_sk(thread_id: str) -> str:
    """Sort key: ``THREAD#<thread_id>`` (the client-facing conversation id)."""
    if not thread_id:
        raise ValueError("thread_sk requires a non-empty thread_id")
    return f"THREAD#{thread_id}"


def derive_title(first_message: str | None, *, fallback: str = "New conversation") -> str:
    """A default conversation title from the first user message.

    The UI can rename later; this is just the initial label so the sidebar isn't
    full of untitled rows. Collapses whitespace and truncates to ``TITLE_MAX``.
    """
    text = " ".join((first_message or "").split()).strip()
    if not text:
        return fallback
    return text[:TITLE_MAX]
