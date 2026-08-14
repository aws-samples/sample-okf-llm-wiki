"""Tests for okf_aws.redshift_query.run_select against a scripted fake client."""

from __future__ import annotations

import pytest
from okf_aws.redshift_query import run_select


class FakeRedshiftData:
    def __init__(self, statuses=("STARTED", "FINISHED"), error="", has_result=True):
        self._statuses = list(statuses)
        self._error = error
        self._has_result = has_result
        self.executed: list[dict] = []
        self.cancelled: list[str] = []

    def execute_statement(self, **kwargs):
        self.executed.append(kwargs)
        return {"Id": "stmt-1"}

    def describe_statement(self, Id):
        status = (
            self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        )
        return {
            "Status": status,
            "Error": self._error,
            "HasResultSet": self._has_result,
            "Duration": 5_000_000,  # ns -> 5ms
        }

    def get_statement_result(self, **kwargs):
        return {
            "ColumnMetadata": [{"label": "season"}, {"name": "total_points"}],
            "Records": [
                [{"longValue": 2024}, {"doubleValue": 1025.0}],
                [{"stringValue": "2025"}, {"isNull": True}],
                [{"booleanValue": True}, {"stringValue": "x"}],
            ],
        }

    def cancel_statement(self, Id):
        self.cancelled.append(Id)


def test_run_select_polls_and_shapes_rows():
    fake = FakeRedshiftData()
    out = run_select(
        fake,
        sql="SELECT 1",
        database="dev",
        workgroup_name="analytics-wg",
        secret_arn="arn:secret",
        sleep=lambda s: None,
    )
    assert out["columns"] == ["season", "total_points"]
    assert out["rows"] == [["2024", "1025.0"], ["2025", None], ["true", "x"]]
    assert out["truncated"] is False
    assert out["stats"]["engine_execution_ms"] == 5
    started = fake.executed[0]
    assert started["WorkgroupName"] == "analytics-wg"
    assert started["SecretArn"] == "arn:secret"
    assert "ClusterIdentifier" not in started


def test_run_select_prefers_cluster_and_caps_rows():
    fake = FakeRedshiftData()
    out = run_select(
        fake,
        sql="SELECT 1",
        database="dev",
        cluster_identifier="prod-cluster",
        secret_arn="arn:secret",
        max_rows=1,
        sleep=lambda s: None,
    )
    assert fake.executed[0]["ClusterIdentifier"] == "prod-cluster"
    assert out["rows"] == [["2024", "1025.0"]]
    assert out["truncated"] is True


def test_run_select_raises_on_failure_with_reason():
    fake = FakeRedshiftData(statuses=("FAILED",), error="permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        run_select(
            fake,
            sql="SELECT 1",
            database="dev",
            workgroup_name="wg",
            secret_arn="arn:s",
            sleep=lambda s: None,
        )


def test_run_select_refuses_non_select_and_bad_descriptor():
    with pytest.raises(RuntimeError, match="non-SELECT"):
        run_select(
            FakeRedshiftData(),
            sql="DROP TABLE t",
            database="dev",
            workgroup_name="wg",
            secret_arn="arn:s",
        )
    with pytest.raises(RuntimeError, match="cluster_identifier or"):
        run_select(
            FakeRedshiftData(), sql="SELECT 1", database="dev", secret_arn="arn:s"
        )
    with pytest.raises(RuntimeError, match="secret_arn"):
        run_select(
            FakeRedshiftData(), sql="SELECT 1", database="dev", workgroup_name="wg"
        )


def test_run_select_empty_result_set():
    fake = FakeRedshiftData(has_result=False)
    out = run_select(
        fake,
        sql="SELECT 1",
        database="dev",
        workgroup_name="wg",
        secret_arn="arn:s",
        sleep=lambda s: None,
    )
    assert out["rows"] == [] and out["columns"] == []
