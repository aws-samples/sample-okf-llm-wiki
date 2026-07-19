"""The agent reports its own harvest lifecycle to the DynamoDB registry.

report_status must use UpdateItem (preserving mode/started_at/session_id that
the Control API set at `queued` time) and be strictly best-effort — a registry
failure must never propagate.
"""

from __future__ import annotations

import harvest.status as status


class _FakeDDB:
    def __init__(self, raise_on_update: bool = False):
        self.calls: list[dict] = []
        self._raise = raise_on_update

    def update_item(self, **kwargs):
        if self._raise:
            raise RuntimeError("ddb down")
        self.calls.append(kwargs)


def test_report_status_uses_update_item_preserving_fields():
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "okf-registry"),
        data_domain="sport",
        dataset="formula_1",
        status="complete",
    )
    assert len(ddb.calls) == 1
    call = ddb.calls[0]
    assert call["TableName"] == "okf-registry"
    assert call["Key"] == {
        "pk": {"S": "HARVEST#sport#formula_1"},
        "sk": {"S": "STATUS"},
    }
    # Only status + updated_at are touched — NOT mode/started_at/runtime_session_id.
    assert "SET" in call["UpdateExpression"]
    assert "updated_at" in call["UpdateExpression"]
    lowered = call["UpdateExpression"].lower()
    assert "mode" not in lowered
    assert "started_at" not in lowered
    assert "session" not in lowered
    # "status" is a reserved word — must go through an expression-attribute-name.
    assert call["ExpressionAttributeNames"]["#s"] == "status"
    assert call["ExpressionAttributeValues"][":s"] == {"S": "complete"}


def test_report_status_includes_detail_when_given():
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="failed",
        detail="ValueError: boom",
    )
    call = ddb.calls[0]
    assert "detail" in call["UpdateExpression"]
    assert call["ExpressionAttributeValues"][":d"] == {"S": "ValueError: boom"}


def test_report_status_records_model_and_effort():
    # The `running` transition stamps the resolved model/effort so the UI can
    # show what the run used. Both aliased via ExpressionAttributeNames.
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="running",
        model="openai.gpt-5.6-sol",
        effort="xhigh",
    )
    call = ddb.calls[0]
    assert call["ExpressionAttributeNames"]["#m"] == "model"
    assert call["ExpressionAttributeNames"]["#e"] == "effort"
    assert call["ExpressionAttributeValues"][":m"] == {"S": "openai.gpt-5.6-sol"}
    assert call["ExpressionAttributeValues"][":e"] == {"S": "xhigh"}


def test_report_status_omits_model_effort_when_absent():
    # Not passed (e.g. terminal transitions) -> not written, so a `running`-stamped
    # model/effort is preserved rather than blanked.
    ddb = _FakeDDB()
    status.report_status((ddb, "t"), data_domain="d", dataset="ds", status="complete")
    call = ddb.calls[0]
    assert ":m" not in call["ExpressionAttributeValues"]
    assert ":e" not in call["ExpressionAttributeValues"]


def test_report_status_truncates_long_detail():
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="failed",
        detail="x" * 5000,
    )
    assert (
        len(ddb.calls[0]["ExpressionAttributeValues"][":d"]["S"]) == status._DETAIL_MAX
    )


def test_report_status_is_best_effort_on_error():
    # A DynamoDB failure must be swallowed, never raised.
    ddb = _FakeDDB(raise_on_update=True)
    status.report_status(
        (ddb, "t"), data_domain="d", dataset="ds", status="running"
    )  # no exception


def test_report_status_noop_when_registry_none():
    # No registry configured -> silent no-op (no crash).
    status.report_status(None, data_domain="d", dataset="ds", status="running")


def test_report_status_only_if_active_adds_condition():
    # Terminal writes guard on the row still being queued/running so they can't
    # clobber a `cancelled` row (post-cancel the crawl throws and reports failed).
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="failed",
        detail="RuntimeError: cannot schedule new futures after shutdown",
        only_if_active=True,
    )
    call = ddb.calls[0]
    assert "ConditionExpression" in call
    cond = call["ConditionExpression"]
    assert ":queued" in cond and ":running" in cond
    assert call["ExpressionAttributeValues"][":queued"] == {"S": "queued"}
    assert call["ExpressionAttributeValues"][":running"] == {"S": "running"}


def test_report_status_swallows_conditional_check_failure():
    # A rejected conditional write (row already terminal, e.g. cancelled) is
    # expected — swallowed, not raised, and the terminal status is left intact.
    class _CondFailDDB:
        def update_item(self, **kwargs):
            err = RuntimeError("conditional check failed")
            err.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise err

    status.report_status(
        (_CondFailDDB(), "t"),
        data_domain="d",
        dataset="ds",
        status="failed",
        only_if_active=True,
    )  # no exception


def test_build_registry_client_none_without_env(monkeypatch):
    monkeypatch.delenv("OKF_REGISTRY_TABLE", raising=False)
    assert status.build_registry_client() is None


# --- stamp_guidance_applied (clears guidance dirty on a successful harvest) --


def test_stamp_guidance_applied_writes_version_to_mapping_row():
    ddb = _FakeDDB()
    status.stamp_guidance_applied(
        (ddb, "okf-registry"),
        data_domain="sport",
        dataset="formula_1",
        version="v-123",
    )
    assert len(ddb.calls) == 1
    call = ddb.calls[0]
    # Targets the DATASET# mapping row, NOT the HARVEST# status row.
    assert call["Key"] == {
        "pk": {"S": "DOMAIN#sport"},
        "sk": {"S": "DATASET#formula_1"},
    }
    assert "guidance_applied_version" in call["UpdateExpression"]
    assert call["ExpressionAttributeValues"][":v"] == {"S": "v-123"}


def test_stamp_guidance_applied_noop_without_version():
    # A run carrying no guidance passes version=None → nothing written.
    ddb = _FakeDDB()
    status.stamp_guidance_applied(
        (ddb, "t"), data_domain="d", dataset="ds", version=None
    )
    assert ddb.calls == []


def test_stamp_guidance_applied_swallows_ddb_error():
    # Best-effort: a write failure must never break a finalized bundle.
    status.stamp_guidance_applied(
        (_FakeDDB(raise_on_update=True), "t"),
        data_domain="d",
        dataset="ds",
        version="v1",
    )  # no exception


def test_stamp_guidance_applied_noop_when_registry_none():
    status.stamp_guidance_applied(None, data_domain="d", dataset="ds", version="v1")


# --- write_benchmark_kpi (recursive-improvement KPI rows) -------------------


class _PutDDB:
    def __init__(self, raise_on_put: bool = False):
        self.puts: list[dict] = []
        self._raise = raise_on_put

    def put_item(self, **kwargs):
        if self._raise:
            raise RuntimeError("ddb down")
        self.puts.append(kwargs)


def test_write_benchmark_kpi_puts_bench_row():
    ddb = _PutDDB()
    status.write_benchmark_kpi(
        (ddb, "okf-registry"),
        data_domain="sport",
        dataset="formula_1",
        runtime_session_id="okf-sport-f1-abc",
        iteration=2,
        attrs={
            "iteration": 2,
            "ex_score": 0.75,
            "passed": 6,
            "failed": 2,
            "discarded": 1,
            "target_met": False,
        },
    )
    assert len(ddb.puts) == 1
    item = ddb.puts[0]["Item"]
    assert ddb.puts[0]["TableName"] == "okf-registry"
    # On the HARVEST# partition, session-scoped BENCH# sort key.
    assert item["pk"] == {"S": "HARVEST#sport#formula_1"}
    assert item["sk"] == {"S": "BENCH#okf-sport-f1-abc#2"}
    # Numbers marshalled as N, bools as BOOL, created_at stamped.
    assert item["ex_score"] == {"N": "0.75"}
    assert item["passed"] == {"N": "6"}
    assert item["target_met"] == {"BOOL": False}
    assert "created_at" in item


def test_write_benchmark_kpi_final_row():
    ddb = _PutDDB()
    status.write_benchmark_kpi(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        runtime_session_id="sess",
        iteration="final",
        attrs={"shipped_iteration": 3, "ex_score": 0.9},
    )
    assert ddb.puts[0]["Item"]["sk"] == {"S": "BENCH#sess#final"}
    assert ddb.puts[0]["Item"]["shipped_iteration"] == {"N": "3"}


def test_write_benchmark_kpi_bool_before_int():
    # bool is an int subclass — must marshal to BOOL, not N.
    ddb = _PutDDB()
    status.write_benchmark_kpi(
        (ddb, "t"), data_domain="d", dataset="ds",
        runtime_session_id="s", iteration=0, attrs={"target_met": True},
    )
    assert ddb.puts[0]["Item"]["target_met"] == {"BOOL": True}


def test_write_benchmark_kpi_best_effort_on_error():
    # A DynamoDB failure must never break a harvest.
    status.write_benchmark_kpi(
        (_PutDDB(raise_on_put=True), "t"),
        data_domain="d", dataset="ds",
        runtime_session_id="s", iteration=0, attrs={"ex_score": 1.0},
    )  # no exception


def test_write_benchmark_kpi_noop_when_registry_none():
    status.write_benchmark_kpi(
        None, data_domain="d", dataset="ds",
        runtime_session_id="s", iteration=0, attrs={"ex_score": 1.0},
    )  # no crash
