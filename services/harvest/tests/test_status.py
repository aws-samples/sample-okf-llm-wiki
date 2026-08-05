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


def test_report_status_records_subagent_override():
    # A run that chose a separate sub-agent config stamps it alongside the
    # supervisor's pair; absence (next test) means "same as harvester".
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="running",
        model="global.anthropic.claude-opus-4-8",
        effort="xhigh",
        subagent_model="openai.gpt-5.6-sol",
        subagent_effort="high",
    )
    call = ddb.calls[0]
    assert "subagent_model = :sm" in call["UpdateExpression"]
    assert call["ExpressionAttributeValues"][":sm"] == {"S": "openai.gpt-5.6-sol"}
    assert call["ExpressionAttributeValues"][":se"] == {"S": "high"}


def test_report_status_records_reviewer_override():
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="running",
        reviewer_model="openai.gpt-5.6-sol",
        reviewer_effort="high",
    )
    call = ddb.calls[0]
    assert "reviewer_model = :rm" in call["UpdateExpression"]
    assert call["ExpressionAttributeValues"][":rm"] == {"S": "openai.gpt-5.6-sol"}
    assert call["ExpressionAttributeValues"][":re"] == {"S": "high"}


def test_report_status_omits_subagent_fields_when_absent():
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="running",
        model="global.anthropic.claude-opus-4-8",
        effort="xhigh",
    )
    call = ddb.calls[0]
    assert ":sm" not in call["ExpressionAttributeValues"]
    assert ":se" not in call["ExpressionAttributeValues"]


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
    # Without a session_id (tests, local runs) no run-identity clause exists.
    assert "runtime_session_id" not in cond


def test_report_status_only_if_active_pins_run_identity():
    # A hung run's LATE terminal write must not land on a successor's
    # queued/running row (the staleness escape lets a new run start over a
    # dead one — status alone can't tell them apart). With the runner's
    # session_id, the condition also requires the row to still be THIS run's.
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="complete",
        only_if_active=True,
        session_id="okf-d-ds-abc123",
    )
    call = ddb.calls[0]
    cond = call["ConditionExpression"]
    assert "runtime_session_id = :sid" in cond
    # Rows written out-of-band without a session id are still writable.
    assert "attribute_not_exists(runtime_session_id)" in cond
    assert call["ExpressionAttributeValues"][":sid"] == {"S": "okf-d-ds-abc123"}


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


# --- read_run_identity + the started_at run-identity pin ---------------------
# The incremental path's runtime_session_id is deterministic per dataset, so
# the session pin can't tell two successive incremental runs apart. started_at
# (rewritten by every lease acquire, untouched afterwards) can.


class _FakeDDBWithGet(_FakeDDB):
    def __init__(self, item: dict | None = None, raise_on_get: bool = False):
        super().__init__()
        self._item = item
        self._raise_get = raise_on_get
        self.get_calls: list[dict] = []

    def get_item(self, **kwargs):
        if self._raise_get:
            raise RuntimeError("ddb down")
        self.get_calls.append(kwargs)
        return {"Item": self._item} if self._item else {}


def test_read_run_identity_returns_started_at():
    ddb = _FakeDDBWithGet(item={"started_at": {"S": "2026-08-05T12:00:00+00:00"}})
    got = status.read_run_identity((ddb, "t"), data_domain="d", dataset="ds")
    assert got == "2026-08-05T12:00:00+00:00"
    call = ddb.get_calls[0]
    assert call["Key"] == {"pk": {"S": "HARVEST#d#ds"}, "sk": {"S": "STATUS"}}


def test_read_run_identity_none_when_row_or_field_missing():
    ddb = _FakeDDBWithGet()  # no row at all
    assert status.read_run_identity((ddb, "t"), data_domain="d", dataset="ds") is None
    assert status.read_run_identity(None, data_domain="d", dataset="ds") is None


def test_read_run_identity_never_raises():
    # Identity capture is best-effort: a registry blip falls back to the
    # session-pin-only condition, never a crashed run.
    ddb = _FakeDDBWithGet(raise_on_get=True)
    assert status.read_run_identity((ddb, "t"), data_domain="d", dataset="ds") is None


def test_report_status_pins_started_at_alongside_the_session():
    # Both identity pins compose, each tolerating rows missing the attribute
    # (out-of-band writes).
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="failed",
        only_if_active=True,
        session_id="okf-d-ds-abc123",
        run_started_at="2026-08-05T12:00:00+00:00",
    )
    call = ddb.calls[0]
    cond = call["ConditionExpression"]
    assert "runtime_session_id = :sid" in cond
    assert "started_at = :rsa" in cond
    assert "attribute_not_exists(started_at)" in cond
    assert call["ExpressionAttributeValues"][":rsa"] == {
        "S": "2026-08-05T12:00:00+00:00"
    }


def test_report_status_started_at_pin_works_without_a_session_pin():
    ddb = _FakeDDB()
    status.report_status(
        (ddb, "t"),
        data_domain="d",
        dataset="ds",
        status="complete",
        only_if_active=True,
        run_started_at="t0",
    )
    cond = ddb.calls[0]["ConditionExpression"]
    assert "started_at = :rsa" in cond
    assert "runtime_session_id" not in cond


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
