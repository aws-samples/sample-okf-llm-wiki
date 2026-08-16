"""Control API /memory routes: per-user long-term chat memory management.

Driven through the real router (route()) with a JWT-claims event, like the
chat-threads tests, so path matching (including PATCH), caller-sub extraction,
and the namespace derivation are all covered. AgentCore Memory is a fake
(FakeAgentCoreMemory); the settings row rides moto DynamoDB on the chat
threads table.
"""

from __future__ import annotations

import json

import pytest

from okf_core.memory_records import format_header

from control_api.app import route
from tests.conftest import CHAT_THREADS
from tests.fakes import FakeAgentCoreMemory

MEMORY_ID = "okf-chat-memory-1"

# Header fields of alice's binding record — reused to assert PATCH preserves
# the header verbatim.
BINDING_HEADER = {"type": "binding", "dataset": "sales/orders", "expires": "2030-01-01"}


def _raw_record(record_id, namespace, *, text, type="stated", dataset="", expires=""):
    """A raw AgentCore Memory record dict (header INSIDE the content text)."""
    return {
        "memoryRecordId": record_id,
        "content": {
            "text": format_header(type=type, dataset=dataset, expires=expires)
            + "\n"
            + text
        },
        "namespaces": [namespace],
    }


def _seed_records():
    return [
        _raw_record("rec-plain", "wiki/alice", text="prefers concise answers"),
        _raw_record(
            "rec-binding",
            "wiki/alice",
            text="monthly revenue means the revenue_by_month computation",
            **BINDING_HEADER,
        ),
        _raw_record(
            "rec-expired",
            "wiki/alice",
            text="Q1 focus is the EMEA region",
            expires="2020-01-01",
        ),
        _raw_record("rec-bob", "wiki/bob", text="bob's private preference"),
    ]


@pytest.fixture
def memory():
    return FakeAgentCoreMemory(_seed_records())


@pytest.fixture
def mcfg(cfg, memory):
    """The shared Config with the memory feature switched ON."""
    cfg.agentcore_memory = memory
    cfg.chat_memory_id = MEMORY_ID
    return cfg


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


def _body(resp):
    return json.loads(resp["body"])


def _settings_key(sub):
    return {"pk": {"S": f"CHAT#{sub}"}, "sk": {"S": "SETTINGS#memory"}}


# --- GET /memory (list) -------------------------------------------------------


def test_list_parses_headers_and_flags_expired(mcfg, memory):
    resp = route(_event("GET", "/memory", sub="alice"), mcfg)
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["enabled"] is True  # missing settings row = enabled

    records = {r["id"]: r for r in body["records"]}
    # only alice's namespace — bob's record never appears
    assert set(records) == {"rec-plain", "rec-binding", "rec-expired"}

    plain = records["rec-plain"]
    assert plain["type"] == "stated" and plain["dataset"] == "" and plain["expires"] == ""
    assert plain["text"] == "prefers concise answers"  # header stripped
    assert plain["expired"] is False

    binding = records["rec-binding"]
    assert binding["type"] == "binding"
    assert binding["dataset"] == "sales/orders"
    assert binding["expires"] == "2030-01-01"
    assert binding["expired"] is False

    assert records["rec-expired"]["expired"] is True

    # the namespace came from the caller's sub, with the documented page size
    call = memory.list_calls[0]
    assert call["memoryId"] == MEMORY_ID
    assert call["namespace"] == "wiki/alice"
    assert call["maxResults"] == 100


def test_list_never_auto_deletes_expired_records(mcfg, memory):
    # The page is the inspection surface; lazy TTL deletion belongs to recall.
    route(_event("GET", "/memory", sub="alice"), mcfg)
    assert memory.delete_calls == []
    assert "rec-expired" in memory.records


def test_list_paginates_to_exhaustion(mcfg):
    paged = FakeAgentCoreMemory(_seed_records(), page_size=1)
    mcfg.agentcore_memory = paged
    resp = route(_event("GET", "/memory", sub="alice"), mcfg)
    assert resp["statusCode"] == 200
    assert {r["id"] for r in _body(resp)["records"]} == {
        "rec-plain",
        "rec-binding",
        "rec-expired",
    }
    # three 1-record pages: no token, then the two integer-offset cursors
    assert [c.get("nextToken") for c in paged.list_calls] == [None, "1", "2"]


def test_list_reflects_disabled_settings_row(mcfg, aws):
    aws["ddb"].put_item(
        TableName=CHAT_THREADS,
        Item={**_settings_key("alice"), "memory_enabled": {"BOOL": False}},
    )
    body = _body(route(_event("GET", "/memory", sub="alice"), mcfg))
    assert body["enabled"] is False
    # the switch hides nothing — records stay inspectable while disabled
    assert len(body["records"]) == 3


def test_list_requires_auth(mcfg):
    ev = {
        "requestContext": {
            "http": {"method": "GET", "path": "/memory"},
            "authorizer": {"jwt": {"claims": {}}},
        },
        "rawPath": "/memory",
    }
    assert route(ev, mcfg)["statusCode"] == 401


# --- /memory/settings ---------------------------------------------------------


def test_settings_get_defaults_to_enabled(mcfg):
    resp = route(_event("GET", "/memory/settings", sub="alice"), mcfg)
    assert resp["statusCode"] == 200
    assert _body(resp) == {"enabled": True}


def test_settings_put_get_round_trip(mcfg, aws):
    resp = route(
        _event("PUT", "/memory/settings", sub="alice", body={"enabled": False}), mcfg
    )
    assert resp["statusCode"] == 200
    assert _body(resp) == {"enabled": False}

    # persisted with the documented typed attrs on the chat threads table
    item = aws["ddb"].get_item(TableName=CHAT_THREADS, Key=_settings_key("alice"))[
        "Item"
    ]
    assert item["memory_enabled"] == {"BOOL": False}

    got = route(_event("GET", "/memory/settings", sub="alice"), mcfg)
    assert _body(got) == {"enabled": False}

    # and back on again
    route(_event("PUT", "/memory/settings", sub="alice", body={"enabled": True}), mcfg)
    got = route(_event("GET", "/memory/settings", sub="alice"), mcfg)
    assert _body(got) == {"enabled": True}


def test_settings_put_is_per_user(mcfg, aws):
    route(_event("PUT", "/memory/settings", sub="alice", body={"enabled": False}), mcfg)
    # bob's switch is untouched (his row doesn't even exist)
    got = route(_event("GET", "/memory/settings", sub="bob"), mcfg)
    assert _body(got) == {"enabled": True}


@pytest.mark.parametrize("bad", ["false", 1, 0, None, [True]])
def test_settings_put_rejects_non_boolean(mcfg, bad):
    resp = route(
        _event("PUT", "/memory/settings", sub="alice", body={"enabled": bad}), mcfg
    )
    assert resp["statusCode"] == 400


# --- PATCH /memory/{record_id} -------------------------------------------------


def test_patch_rewrites_text_and_preserves_header(mcfg, memory):
    resp = route(
        _event(
            "PATCH", "/memory/rec-binding", sub="alice", body={"text": "new text"}
        ),
        mcfg,
    )
    assert resp["statusCode"] == 200
    body = _body(resp)
    assert body["id"] == "rec-binding"
    assert body["text"] == "new text"
    # header fields survive the edit
    assert body["type"] == "binding"
    assert body["dataset"] == "sales/orders"
    assert body["expires"] == "2030-01-01"

    call = memory.update_calls[-1]
    assert call["memoryId"] == MEMORY_ID
    (record,) = call["records"]
    assert record["memoryRecordId"] == "rec-binding"
    # the stored content is the CANONICAL header line + the new text
    assert record["content"]["text"] == format_header(**BINDING_HEADER) + "\nnew text"
    assert record["namespaces"] == ["wiki/alice"]
    from datetime import datetime

    assert isinstance(record["timestamp"], datetime)


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_patch_rejects_empty_text(mcfg, memory, bad):
    resp = route(
        _event("PATCH", "/memory/rec-plain", sub="alice", body={"text": bad}), mcfg
    )
    assert resp["statusCode"] == 400
    assert memory.update_calls == []


def test_patch_rejects_oversized_text(mcfg, memory):
    resp = route(
        _event("PATCH", "/memory/rec-plain", sub="alice", body={"text": "x" * 8001}),
        mcfg,
    )
    assert resp["statusCode"] == 400
    assert memory.update_calls == []


def test_patch_missing_record_is_404(mcfg):
    resp = route(
        _event("PATCH", "/memory/ghost", sub="alice", body={"text": "x"}), mcfg
    )
    assert resp["statusCode"] == 404


def test_patch_another_users_record_is_404_and_leaks_nothing(mcfg, memory):
    # the fake RETURNS bob's record on get; the handler must refuse on namespace
    resp = route(
        _event("PATCH", "/memory/rec-bob", sub="alice", body={"text": "hax"}), mcfg
    )
    assert resp["statusCode"] == 404
    assert memory.update_calls == []
    assert "bob's private preference" not in resp["body"]
    # indistinguishable from a record that doesn't exist at all
    ghost = route(_event("PATCH", "/memory/ghost", sub="alice", body={"text": "x"}), mcfg)
    assert _body(resp)["error"].replace("rec-bob", "ghost") == _body(ghost)["error"]


def test_patch_resupplies_real_metadata_verbatim(mcfg, memory):
    # Replace-vs-merge on BatchUpdateMemoryRecords is undocumented — the fake
    # models the pessimistic REPLACE, so a handler that failed to re-supply
    # metadata would strip type/dataset/expires here (and a personal record
    # would fall out of every recall path while still rendering on the page).
    meta = {
        "type": {"stringValue": "personal"},
        "dataset": {"stringValue": "sales/orders"},
    }
    memory.records["rec-plain"]["metadata"] = dict(meta)
    resp = route(
        _event("PATCH", "/memory/rec-plain", sub="alice", body={"text": "renamed"}),
        mcfg,
    )
    assert resp["statusCode"] == 200
    assert _body(resp)["type"] == "personal"
    (record,) = memory.update_calls[-1]["records"]
    assert record["metadata"] == meta
    assert memory.records["rec-plain"]["metadata"] == meta  # survived the replace


def test_patch_surfaces_failed_records_as_409(mcfg, memory):
    # The record vanishes between the ownership GET and the update (the chat
    # runtime's lazy-TTL delete, async consolidation): BatchUpdate reports it
    # in failedRecords inside a 200 response — returning the edited record
    # anyway would fabricate a success the store never saw.
    stored = memory.records.pop("rec-plain")
    memory.get_memory_record = lambda **kw: {"memoryRecord": dict(stored)}
    resp = route(
        _event("PATCH", "/memory/rec-plain", sub="alice", body={"text": "renamed"}),
        mcfg,
    )
    assert resp["statusCode"] == 409
    assert "update failed" in _body(resp)["error"]


# --- DELETE /memory/{record_id} -------------------------------------------------


def test_delete_removes_callers_record(mcfg, memory):
    resp = route(_event("DELETE", "/memory/rec-plain", sub="alice"), mcfg)
    assert resp["statusCode"] == 200
    assert _body(resp) == {"deleted": "rec-plain"}
    assert memory.delete_calls[-1] == {
        "memoryId": MEMORY_ID,
        "memoryRecordId": "rec-plain",
    }
    assert "rec-plain" not in memory.records


def test_delete_missing_record_is_404(mcfg):
    resp = route(_event("DELETE", "/memory/ghost", sub="alice"), mcfg)
    assert resp["statusCode"] == 404


def test_delete_another_users_record_is_404(mcfg, memory):
    resp = route(_event("DELETE", "/memory/rec-bob", sub="alice"), mcfg)
    assert resp["statusCode"] == 404
    assert memory.delete_calls == []  # never reached the provider delete
    assert "rec-bob" in memory.records
    assert "bob's private preference" not in resp["body"]


# --- feature off (OKF_CHAT_MEMORY_ID empty) --------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/memory", None),
        ("GET", "/memory/settings", None),
        ("PUT", "/memory/settings", {"enabled": False}),
        ("PATCH", "/memory/rec-plain", {"text": "x"}),
        ("DELETE", "/memory/rec-plain", None),
    ],
)
def test_all_memory_routes_404_when_feature_off(cfg, method, path, body):
    # the shared cfg has no memory wiring: chat_memory_id="" / client None
    assert cfg.chat_memory_id == "" and cfg.agentcore_memory is None
    resp = route(_event(method, path, sub="alice", body=body), cfg)
    assert resp["statusCode"] == 404
