"""Tests for okf_aws.athena_query.run_select against a scripted fake client."""

from __future__ import annotations

import pytest
from okf_aws.athena_query import run_select


class FakeAthena:
    def __init__(self, states=("RUNNING", "SUCCEEDED"), reason="", pages=None):
        self._states = list(states)
        self._reason = reason
        self.started = []
        self.stopped = []
        self._pages = pages or [
            {
                "ResultSet": {
                    "ResultSetMetadata": {
                        "ColumnInfo": [{"Label": "a"}, {"Label": "n"}]
                    },
                    "Rows": [
                        {"Data": [{"VarCharValue": "a"}, {"VarCharValue": "n"}]},
                        {"Data": [{"VarCharValue": "x"}, {"VarCharValue": "1"}]},
                        {"Data": [{"VarCharValue": "y"}, {"VarCharValue": None}]},
                    ],
                }
            }
        ]

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "qid"}

    def get_query_execution(self, QueryExecutionId):
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return {
            "QueryExecution": {
                "Status": {"State": state, "StateChangeReason": self._reason},
                "Statistics": {"DataScannedInBytes": 42},
            }
        }

    def get_query_results(self, **kwargs):
        page = dict(self._pages[len(kwargs.get("NextToken", "") or "")])
        if len(self._pages) > 1 and "NextToken" not in kwargs:
            page = dict(self._pages[0])
            page["NextToken"] = "t"
        return page

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)


def test_run_select_polls_and_strips_the_header_row():
    fake = FakeAthena()
    out = run_select(
        fake, sql="SELECT 1", database="db", workgroup="wg", sleep=lambda s: None
    )
    assert out["columns"] == ["a", "n"]
    assert out["rows"] == [["x", "1"], ["y", None]]
    assert out["truncated"] is False
    assert fake.started[0]["WorkGroup"] == "wg"
    assert out["stats"]["data_scanned_bytes"] == 42


def test_run_select_caps_rows():
    out = run_select(
        FakeAthena(), sql="SELECT 1", database="db", max_rows=1, sleep=lambda s: None
    )
    assert out["rows"] == [["x", "1"]]
    assert out["truncated"] is True


def test_run_select_raises_on_failure_with_reason():
    fake = FakeAthena(states=("FAILED",), reason="SYNTAX_ERROR: nope")
    with pytest.raises(RuntimeError, match="SYNTAX_ERROR"):
        run_select(fake, sql="SELECT 1", database="db", sleep=lambda s: None)


def test_run_select_refuses_non_select():
    with pytest.raises(RuntimeError, match="non-SELECT"):
        run_select(FakeAthena(), sql="DROP TABLE t", database="db")
    # WITH (CTE) is a legal read entry point.
    out = run_select(
        FakeAthena(), sql="WITH c AS (SELECT 1) SELECT * FROM c", database="db",
        sleep=lambda s: None,
    )
    assert out["row_count"] == 2
