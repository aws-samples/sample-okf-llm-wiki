"""The cross-service AR contract bits: the rebuild event + the POLICY# sort key.

Three independent services describe the same event (control_api and chat
publish, incremental consumes) and two describe the same DynamoDB key (chat
writes the report, the Control API reads/purges it), so these tests pin the
exact strings and shapes rather than any behaviour: a drift here is an
integration bug nothing else catches offline.
"""

from __future__ import annotations

import json

import pytest

from okf_core.chat_threads import (
    POLICY_SK_PREFIX,
    parse_policy_report_sk,
    policy_report_sk,
    policy_report_sk_prefix,
    thread_pk,
)
from okf_core.policy_rebuild import (
    DETAIL_TYPE_POLICY_REBUILD,
    EVENT_SOURCE,
    build_detail,
    parse_detail,
)


# --- event constants -----------------------------------------------------------


def test_event_source_is_a_custom_non_aws_source():
    # PutEvents rejects any Source beginning with "aws." — and the consumer's
    # EventBridge rule pattern is written against these exact literals.
    assert EVENT_SOURCE == "okf.policy"
    assert not EVENT_SOURCE.startswith("aws.")
    assert DETAIL_TYPE_POLICY_REBUILD == "policy_rebuild"


# --- detail round-trip ---------------------------------------------------------


def test_detail_round_trips_through_json():
    # The wire form is a JSON string in the envelope's Detail; the consumer sees
    # the decoded mapping. Both halves must agree across that boundary.
    detail = build_detail("sales", "orders")
    assert detail == {"data_domain": "sales", "dataset": "orders"}
    assert parse_detail(json.loads(json.dumps(detail))) == ("sales", "orders")


def test_reason_is_carried_but_not_part_of_the_parsed_result():
    # A publisher breadcrumb for logs only — the consumer runs the same rebuild
    # whatever the reason, so it must not widen the parsed contract.
    detail = build_detail("sales", "orders", reason="repromote")
    assert detail["reason"] == "repromote"
    assert parse_detail(detail) == ("sales", "orders")


def test_blank_reason_is_omitted_entirely():
    assert "reason" not in build_detail("sales", "orders", reason="   ")


def test_detail_values_are_stripped():
    assert build_detail("  sales ", "orders\n") == {
        "data_domain": "sales",
        "dataset": "orders",
    }
    assert parse_detail({"data_domain": " sales ", "dataset": " orders "}) == (
        "sales",
        "orders",
    )


def test_unknown_detail_fields_are_ignored():
    # Forward compatibility: the event is a poke, so an older consumer must not
    # reject a payload a newer publisher extended.
    assert parse_detail(
        {"data_domain": "sales", "dataset": "orders", "future_field": 1}
    ) == ("sales", "orders")


# --- malformed input -----------------------------------------------------------


@pytest.mark.parametrize(
    "domain,dataset",
    [("", "orders"), ("sales", ""), ("  ", "orders"), ("sales", "\t")],
)
def test_build_detail_rejects_a_missing_identifier(domain, dataset):
    with pytest.raises(ValueError):
        build_detail(domain, dataset)


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {"dataset": "orders"},
        {"data_domain": "sales"},
        {"data_domain": "sales", "dataset": ""},
        {"data_domain": "sales", "dataset": "   "},
        {"data_domain": None, "dataset": "orders"},
        {"data_domain": "sales", "dataset": 7},  # a JSON number, not an id
    ],
)
def test_parse_detail_rejects_malformed_details(detail):
    with pytest.raises(ValueError):
        parse_detail(detail)


@pytest.mark.parametrize("detail", ['{"data_domain": "sales"}', None, [], 7])
def test_parse_detail_rejects_non_mappings(detail):
    # A raw JSON string (an un-decoded Detail) is the likely publisher mistake.
    with pytest.raises(ValueError):
        parse_detail(detail)


# --- POLICY# sort key ----------------------------------------------------------


def test_policy_report_sk_shape():
    assert policy_report_sk("c1", 0) == "POLICY#c1#0"
    assert policy_report_sk("c1", 12) == "POLICY#c1#12"
    assert policy_report_sk("c1", 3).startswith(POLICY_SK_PREFIX)


def test_policy_report_sk_shares_the_thread_partition():
    # Same partition as the conversation index (structural per-user isolation),
    # distinguished only by the sk prefix.
    assert thread_pk("sub-1") == "CHAT#sub-1"


def test_policy_report_sk_round_trips():
    for thread_id, turn in (("c1", 0), ("c10", 2), ("thread#with#hash", 7)):
        assert parse_policy_report_sk(policy_report_sk(thread_id, turn)) == (
            thread_id,
            turn,
        )


def test_policy_report_sk_accepts_an_int_like_turn_key():
    assert policy_report_sk("c1", True) == "POLICY#c1#1"  # noqa: FBT003 - bool is an int


@pytest.mark.parametrize("thread_id,turn", [("", 0), ("c1", -1)])
def test_policy_report_sk_rejects_bad_input(thread_id, turn):
    with pytest.raises(ValueError):
        policy_report_sk(thread_id, turn)


def test_policy_report_sk_prefix_does_not_leak_across_threads():
    # Without the trailing '#', thread "c1" would also select "c10"'s reports —
    # and a conversation delete would purge a sibling's history.
    prefix = policy_report_sk_prefix("c1")
    assert prefix == "POLICY#c1#"
    assert policy_report_sk("c1", 4).startswith(prefix)
    assert not policy_report_sk("c10", 4).startswith(prefix)


def test_policy_report_sk_prefix_requires_a_thread_id():
    with pytest.raises(ValueError):
        policy_report_sk_prefix("")


@pytest.mark.parametrize(
    "sk",
    [
        "THREAD#c1",  # a conversation row in the same partition
        "POLICY#c1",  # no turn ordinal
        "POLICY##3",  # no thread id
        "POLICY#c1#x",  # non-numeric ordinal
        "",
    ],
)
def test_parse_policy_report_sk_rejects_non_report_keys(sk):
    with pytest.raises(ValueError):
        parse_policy_report_sk(sk)


def test_build_detail_carries_force_only_when_set():
    from okf_core.policy_rebuild import is_forced

    # Omitted-when-false keeps the default payload at exactly the two
    # identifying fields (existing consumers see no shape change).
    assert "force" not in build_detail("d", "ds")
    detail = build_detail("d", "ds", reason="manual_sync", force=True)
    assert detail["force"] is True
    # parse_detail ignores it (an extra, like reason)…
    assert parse_detail(detail) == ("d", "ds")
    # …and is_forced is the accessor, tolerant of absence and junk.
    assert is_forced(detail) is True
    assert is_forced({"data_domain": "d", "dataset": "ds"}) is False
    assert is_forced("not-a-mapping") is False
