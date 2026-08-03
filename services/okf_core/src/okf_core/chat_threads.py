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

The same partition also holds the per-turn **policy-check reports** (the
Automated Reasoning sidebar) under a ``POLICY#`` sort key — same table, same
user-scoped partition, so one module owns the whole key space and a
conversation delete knows what else to sweep.
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


# --------------------------------------------------------------------------- #
# Policy-check reports (the Automated Reasoning sidebar)
# --------------------------------------------------------------------------- #

# Sort-key prefix for a per-turn policy-check report, on the SAME
# ``CHAT#<user_sub>`` partition as the conversation index. Consequence for every
# reader of that partition: a Query for conversations MUST constrain
# ``begins_with(sk, "THREAD#")`` — an unconstrained Query now also returns these
# rows.
POLICY_SK_PREFIX = "POLICY#"


def policy_report_sk(thread_id: str, turn_key: int) -> str:
    """Sort key: ``POLICY#<thread_id>#<turn_key>`` — one turn's report.

    ``turn_key`` is the turn's ORDINAL in the history rebuilt server-side from
    the checkpoint (threads are append-only, so an ordinal is stable and needs
    no id of its own). It is written as a PLAIN integer, not zero-padded, so
    reports read by exact key are trivially addressable from the client's turn
    index — the cost is that a ``begins_with`` Query returns ``#10`` before
    ``#2``, so a caller that wants turn order must sort on the parsed
    ``turn_key`` (:func:`parse_policy_report_sk`) rather than on the sk.
    """
    if not thread_id:
        raise ValueError("policy_report_sk requires a non-empty thread_id")
    turn = int(turn_key)
    if turn < 0:
        raise ValueError(f"turn_key must be >= 0, got {turn_key!r}")
    return f"{POLICY_SK_PREFIX}{thread_id}#{turn}"


def policy_report_sk_prefix(thread_id: str) -> str:
    """The ``begins_with`` prefix for one conversation's report rows.

    Note the TRAILING ``#``: thread ids are client-supplied, so without it
    thread ``c1`` also selects ``c10``'s reports — which would make a
    conversation delete purge a sibling conversation's history.
    """
    if not thread_id:
        raise ValueError("policy_report_sk_prefix requires a non-empty thread_id")
    return f"{POLICY_SK_PREFIX}{thread_id}#"


def parse_policy_report_sk(sk: str) -> tuple[str, int]:
    """``(thread_id, turn_key)`` from a report sort key, or ``ValueError``.

    Splits the turn ordinal off the RIGHT, so a thread id that itself contains
    ``#`` still round-trips with :func:`policy_report_sk`. Raises on anything
    that is not a report key — callers Query a whole partition, so "is this row
    a report?" must be answered by :data:`POLICY_SK_PREFIX`, never by a parse
    that quietly returns a garbage ordinal.
    """
    if not sk.startswith(POLICY_SK_PREFIX):
        raise ValueError(f"not a policy report sk: {sk!r}")
    thread_id, sep, turn = sk[len(POLICY_SK_PREFIX) :].rpartition("#")
    if not sep or not thread_id or not turn.isdigit():
        raise ValueError(f"malformed policy report sk: {sk!r}")
    return thread_id, int(turn)
