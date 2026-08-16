"""The conversation-index writer (chat/threads.py) against moto DynamoDB.

Verifies the upsert semantics: first turn seeds created_at + title, later turns
touch updated_at/model/effort/scope but preserve created_at + title (so a UI
rename survives), and that a write failure is swallowed (best-effort).
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from okf_core import chat_threads as ct

from chat.threads import touch_thread

REGION = "us-east-1"
THREADS_TABLE = "okf-chat"


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=THREADS_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _get(ddb, user_sub, thread_id):
    resp = ddb.get_item(
        TableName=THREADS_TABLE,
        Key={"pk": {"S": ct.thread_pk(user_sub)}, "sk": {"S": ct.thread_sk(thread_id)}},
    )
    return resp.get("Item")


def test_first_turn_seeds_row(ddb):
    touch_thread(
        ddb,
        threads_table=THREADS_TABLE,
        user_sub="alice",
        thread_id="conv1",
        title="What tables track orders?",
        model="us.anthropic.claude-opus-4-8",
        effort="high",
        dataset_scope={"data_domain": "sales", "dataset": "orders"},
        now_iso="2026-07-15T00:00:00+00:00",
    )
    item = _get(ddb, "alice", "conv1")
    assert item["pk"]["S"] == "CHAT#alice"
    assert item["sk"]["S"] == "THREAD#conv1"
    assert item["title"]["S"] == "What tables track orders?"
    assert item["created_at"]["S"] == "2026-07-15T00:00:00+00:00"
    assert item["updated_at"]["S"] == "2026-07-15T00:00:00+00:00"
    assert item["model"]["S"] == "us.anthropic.claude-opus-4-8"
    assert item["effort"]["S"] == "high"
    assert item["data_domain"]["S"] == "sales"
    assert item["dataset"]["S"] == "orders"


def test_later_turn_preserves_created_at_and_title(ddb):
    touch_thread(
        ddb, threads_table=THREADS_TABLE, user_sub="alice", thread_id="conv1",
        title="original title", model="us.anthropic.claude-opus-4-8", effort="high",
        dataset_scope=None, now_iso="2026-07-15T00:00:00+00:00",
    )
    # second turn: different title text (from a later message) + newer timestamp
    touch_thread(
        ddb, threads_table=THREADS_TABLE, user_sub="alice", thread_id="conv1",
        title="a later message that must NOT overwrite the title",
        model="us.anthropic.claude-opus-4-8", effort="xhigh",
        dataset_scope=None, now_iso="2026-07-15T01:00:00+00:00",
    )
    item = _get(ddb, "alice", "conv1")
    # created_at + title are seeded once (if_not_exists) ...
    assert item["created_at"]["S"] == "2026-07-15T00:00:00+00:00"
    assert item["title"]["S"] == "original title"
    # ... updated_at + effort are refreshed every turn
    assert item["updated_at"]["S"] == "2026-07-15T01:00:00+00:00"
    assert item["effort"]["S"] == "xhigh"


def test_users_are_isolated_by_pk(ddb):
    for sub in ("alice", "bob"):
        touch_thread(
            ddb, threads_table=THREADS_TABLE, user_sub=sub, thread_id="shared",
            title=f"{sub}'s convo", model="m", effort="high",
            dataset_scope=None, now_iso="2026-07-15T00:00:00+00:00",
        )
    assert _get(ddb, "alice", "shared")["title"]["S"] == "alice's convo"
    assert _get(ddb, "bob", "shared")["title"]["S"] == "bob's convo"


def test_write_failure_is_swallowed():
    class _BoomDDB:
        def update_item(self, **kwargs):
            raise RuntimeError("dynamo down")

    # Must NOT raise — the index write is best-effort and can't break the chat run.
    touch_thread(
        _BoomDDB(), threads_table=THREADS_TABLE, user_sub="alice", thread_id="conv1",
        title="t", model="m", effort="high", dataset_scope=None,
        now_iso="2026-07-15T00:00:00+00:00",
    )


def test_title_truncated_to_max(ddb):
    long = "x" * (ct.TITLE_MAX + 50)
    touch_thread(
        ddb, threads_table=THREADS_TABLE, user_sub="alice", thread_id="conv1",
        title=long, model="m", effort="high", dataset_scope=None,
        now_iso="2026-07-15T00:00:00+00:00",
    )
    assert len(_get(ddb, "alice", "conv1")["title"]["S"]) == ct.TITLE_MAX
