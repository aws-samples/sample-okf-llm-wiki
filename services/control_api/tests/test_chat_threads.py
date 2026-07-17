"""Control API /chat/threads routes: per-user list, rename (PUT), delete (+
checkpoint purge). Driven through the real router (route()) with a JWT-claims
event, so path matching, method dispatch, and caller-sub extraction are covered.
"""

from __future__ import annotations

import json

from okf_core import chat_threads as ct

from control_api import handlers
from control_api.app import route
from tests.conftest import CHAT_CHECKPOINTS, CHAT_THREADS


def _event(method, path, *, sub, body=None):
    """An API GW v2 event with the JWT authorizer claims the router trusts."""
    ev = {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": sub, "email": f"{sub}@x.io"}}},
        },
        "rawPath": path,
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def _seed_thread(ddb, sub, thread_id, *, title, updated_at):
    ddb.put_item(
        TableName=CHAT_THREADS,
        Item={
            "pk": {"S": ct.thread_pk(sub)},
            "sk": {"S": ct.thread_sk(thread_id)},
            "title": {"S": title},
            "model": {"S": "us.anthropic.claude-opus-4-8"},
            "effort": {"S": "high"},
            "created_at": {"S": "2026-07-15T00:00:00+00:00"},
            "updated_at": {"S": updated_at},
        },
    )


def _seed_checkpoint(ddb, namespaced_thread_id):
    # A CHECKPOINT_ item + a WRITES_ item, mirroring DynamoDBSaver's PK prefixes.
    ddb.put_item(
        TableName=CHAT_CHECKPOINTS,
        Item={"PK": {"S": f"CHECKPOINT_{namespaced_thread_id}"}, "SK": {"S": "#ckpt1"}},
    )
    ddb.put_item(
        TableName=CHAT_CHECKPOINTS,
        Item={"PK": {"S": f"WRITES_{namespaced_thread_id}#ns#ckpt1"}, "SK": {"S": "task#0"}},
    )


def _body(resp):
    return json.loads(resp["body"])


# --- list -------------------------------------------------------------------


def test_list_returns_only_callers_threads_newest_first(cfg, aws):
    _seed_thread(aws["ddb"], "alice", "c1", title="older", updated_at="2026-07-15T01:00:00+00:00")
    _seed_thread(aws["ddb"], "alice", "c2", title="newer", updated_at="2026-07-15T02:00:00+00:00")
    _seed_thread(aws["ddb"], "bob", "c3", title="bob's", updated_at="2026-07-15T03:00:00+00:00")

    resp = route(_event("GET", "/chat/threads", sub="alice"), cfg)
    assert resp["statusCode"] == 200
    threads = _body(resp)["threads"]
    # only alice's, newest updated first
    assert [t["thread_id"] for t in threads] == ["c2", "c1"]
    assert threads[0]["title"] == "newer"
    assert all(t["thread_id"] != "c3" for t in threads)


def test_list_skips_soft_deleted_rows(cfg, aws):
    _seed_thread(aws["ddb"], "alice", "c1", title="live", updated_at="2026-07-15T01:00:00+00:00")
    # a row already marked for TTL deletion must not resurface before the sweep
    aws["ddb"].update_item(
        TableName=CHAT_THREADS,
        Key={"pk": {"S": ct.thread_pk("alice")}, "sk": {"S": ct.thread_sk("c1")}},
        UpdateExpression="SET expires_at = :e",
        ExpressionAttributeValues={":e": {"N": "1000000000"}},
    )
    resp = route(_event("GET", "/chat/threads", sub="alice"), cfg)
    assert _body(resp)["threads"] == []


def test_list_paginates_across_query_pages(cfg, aws):
    # A single Query returns at most 1 MB; a large history spans multiple pages.
    # moto won't truncate a handful of items, so drive the handler with a fake
    # ddb that hands back two pages and asserts the loop concatenates them AND
    # threads LastEvaluatedKey -> ExclusiveStartKey. Order still sorts newest-first.
    def _item(tid, updated_at):
        return {
            "pk": {"S": ct.thread_pk("alice")},
            "sk": {"S": ct.thread_sk(tid)},
            "title": {"S": tid},
            "updated_at": {"S": updated_at},
        }

    calls = []

    class _FakeDdb:
        def query(self, **kwargs):
            calls.append(kwargs.get("ExclusiveStartKey"))
            if "ExclusiveStartKey" not in kwargs:
                return {
                    "Items": [_item("c1", "2026-07-15T01:00:00+00:00")],
                    "LastEvaluatedKey": {"pk": {"S": "cursor"}},
                }
            return {"Items": [_item("c2", "2026-07-15T02:00:00+00:00")]}

    resp = handlers.list_chat_threads(
        _FakeDdb(), threads_table=CHAT_THREADS, user_sub="alice"
    )
    # both pages present, newest-updated first
    assert [t["thread_id"] for t in resp["threads"]] == ["c2", "c1"]
    # first call has no cursor, second resumes from the returned LastEvaluatedKey
    assert calls == [None, {"pk": {"S": "cursor"}}]


def test_list_requires_auth(cfg, aws):
    # No sub in claims -> 401 (never an unscoped scan).
    ev = {"requestContext": {"http": {"method": "GET", "path": "/chat/threads"},
                             "authorizer": {"jwt": {"claims": {}}}}, "rawPath": "/chat/threads"}
    resp = route(ev, cfg)
    assert resp["statusCode"] == 401


# --- rename (PUT) -----------------------------------------------------------


def test_rename_updates_title(cfg, aws):
    _seed_thread(aws["ddb"], "alice", "c1", title="old", updated_at="2026-07-15T01:00:00+00:00")
    resp = route(_event("PUT", "/chat/threads/c1", sub="alice", body={"title": "new title"}), cfg)
    assert resp["statusCode"] == 200
    assert _body(resp)["title"] == "new title"
    # persisted
    got = aws["ddb"].get_item(
        TableName=CHAT_THREADS,
        Key={"pk": {"S": ct.thread_pk("alice")}, "sk": {"S": ct.thread_sk("c1")}},
    )["Item"]
    assert got["title"]["S"] == "new title"


def test_rename_missing_thread_is_404(cfg, aws):
    resp = route(_event("PUT", "/chat/threads/ghost", sub="alice", body={"title": "x"}), cfg)
    assert resp["statusCode"] == 404


def test_rename_empty_title_is_400(cfg, aws):
    _seed_thread(aws["ddb"], "alice", "c1", title="old", updated_at="2026-07-15T01:00:00+00:00")
    resp = route(_event("PUT", "/chat/threads/c1", sub="alice", body={"title": "   "}), cfg)
    assert resp["statusCode"] == 400


def test_rename_another_users_thread_is_404(cfg, aws):
    # bob's thread is in bob's partition; alice's rename can't reach it -> 404.
    _seed_thread(aws["ddb"], "bob", "c1", title="bob", updated_at="2026-07-15T01:00:00+00:00")
    resp = route(_event("PUT", "/chat/threads/c1", sub="alice", body={"title": "hax"}), cfg)
    assert resp["statusCode"] == 404
    # bob's row is untouched
    got = aws["ddb"].get_item(
        TableName=CHAT_THREADS,
        Key={"pk": {"S": ct.thread_pk("bob")}, "sk": {"S": ct.thread_sk("c1")}},
    )["Item"]
    assert got["title"]["S"] == "bob"


# --- delete (+ checkpoint purge) --------------------------------------------


def test_delete_removes_index_row_and_purges_checkpoints(cfg, aws):
    _seed_thread(aws["ddb"], "alice", "c1", title="doomed", updated_at="2026-07-15T01:00:00+00:00")
    _seed_checkpoint(aws["ddb"], "alice:c1")

    resp = route(_event("DELETE", "/chat/threads/c1", sub="alice"), cfg)
    assert resp["statusCode"] == 200
    assert _body(resp)["deleted"] is True

    # index row gone
    got = aws["ddb"].get_item(
        TableName=CHAT_THREADS,
        Key={"pk": {"S": ct.thread_pk("alice")}, "sk": {"S": ct.thread_sk("c1")}},
    )
    assert "Item" not in got
    # checkpoint items purged (both CHECKPOINT_ and WRITES_)
    scan = aws["ddb"].scan(TableName=CHAT_CHECKPOINTS)
    assert scan["Count"] == 0


def test_delete_missing_thread_is_404(cfg, aws):
    resp = route(_event("DELETE", "/chat/threads/ghost", sub="alice"), cfg)
    assert resp["statusCode"] == 404


def test_delete_purges_only_that_conversations_checkpoints(cfg, aws):
    _seed_thread(aws["ddb"], "alice", "c1", title="a", updated_at="2026-07-15T01:00:00+00:00")
    _seed_checkpoint(aws["ddb"], "alice:c1")
    _seed_checkpoint(aws["ddb"], "alice:c2")  # a different conversation, must survive

    route(_event("DELETE", "/chat/threads/c1", sub="alice"), cfg)

    scan = aws["ddb"].scan(TableName=CHAT_CHECKPOINTS)
    remaining = {it["PK"]["S"] for it in scan["Items"]}
    assert remaining == {"CHECKPOINT_alice:c2", "WRITES_alice:c2#ns#ckpt1"}


def test_delete_does_not_purge_a_prefix_sibling_conversation(cfg, aws):
    # Thread ids are client-supplied, so one can be a PREFIX of another ("c1" of
    # "c10"). The WRITES purge must include the "#" delimiter so deleting "c1"
    # leaves "c10"'s pending writes intact (the bug: begins_with("WRITES_alice:c1")
    # also matches "WRITES_alice:c10#…").
    _seed_thread(aws["ddb"], "alice", "c1", title="a", updated_at="2026-07-15T01:00:00+00:00")
    _seed_checkpoint(aws["ddb"], "alice:c1")
    _seed_checkpoint(aws["ddb"], "alice:c10")  # prefix sibling — must survive

    route(_event("DELETE", "/chat/threads/c1", sub="alice"), cfg)

    scan = aws["ddb"].scan(TableName=CHAT_CHECKPOINTS)
    remaining = {it["PK"]["S"] for it in scan["Items"]}
    assert remaining == {"CHECKPOINT_alice:c10", "WRITES_alice:c10#ns#ckpt1"}
