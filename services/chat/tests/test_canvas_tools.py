"""The canvas delegation quartet: the skeleton validates the tile plan
in-chat, create requires the agreed spec (job row + runtime invoke, spec on
the payload), status reads scoped to the caller, revision requires an owned
canvas (optional updated spec), and an invoke failure lands as a FAILED job
row instead of a silent loss."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from okf_core import canvas as cv

from chat.tools import make_canvas_tools

TABLE = "okf-canvas"
ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/canvas-abc"
SUB = "user-1"


def _spec():
    return {
        "title": "Revenue overview",
        "widgets": [
            {
                "title": "Revenue by month",
                "widget": "bar",
                "represents": "monthly gross revenue",
            }
        ],
        "dials": [{"name": "year", "label": "Year", "filters": "reporting year"}],
    }


class FakeAgentCore:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def invoke_agent_runtime(self, **kwargs):
        if self.fail:
            raise RuntimeError("runtime unavailable")
        self.calls.append(kwargs)
        return {"statusCode": 200}


@pytest.fixture
def table():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
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
        yield boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)


def _tools(table, agentcore, scope=None, thread_id=""):
    return {
        t.name: t
        for t in make_canvas_tools(
            table, agentcore, runtime_arn=ARN, user_sub=SUB,
            dataset_scope=scope, thread_id=thread_id,
        )
    }


def _canvas_row(canvas_id, *, dataset="orders", updated="t2", config=None):
    return {
        "pk": cv.canvas_pk(SUB),
        "sk": canvas_id,
        "canvas_id": canvas_id,
        "title": f"Canvas {canvas_id}",
        "data_domain": "sales",
        "dataset": dataset,
        "status": cv.STATUS_CREATED,
        "config": json.dumps(config or {}),
        "created_at": "t1",
        "updated_at": updated,
    }


def test_list_canvases_sorts_and_filters(table):
    tools = _tools(table, FakeAgentCore())
    table.put_item(Item=_canvas_row("c1", dataset="orders", updated="2026-01-01"))
    table.put_item(Item=_canvas_row("c2", dataset="returns", updated="2026-06-01"))
    out = tools["list_canvases"].invoke({})
    # Newest-updated first, both datasets, no filter recorded.
    assert [c["canvas_id"] for c in out["canvases"]] == ["c2", "c1"]
    assert out["filtered_by"] is None
    # Filtered to one dataset.
    out = tools["list_canvases"].invoke({"data_domain": "sales", "dataset": "orders"})
    assert [c["canvas_id"] for c in out["canvases"]] == ["c1"]
    assert out["filtered_by"] == {"data_domain": "sales", "dataset": "orders"}


def test_describe_canvas_exposes_queries_and_bindings(table):
    tools = _tools(table, FakeAgentCore())
    config = {
        "canvas_version": 1,
        "dials": [{"name": "year", "label": "Year", "default": 2024}],
        "tiles": [
            {
                "id": "t1",
                "title": "Revenue by month",
                "widget": "bar",
                "layout": {"w": 6, "h": 4},
                "data": {
                    "computation": "monthly_revenue",
                    "bind": {"year": {"dial": "year"}},
                },
            },
            {
                "id": "t2",
                "title": "Top regions",
                "widget": "table",
                "data": {
                    "sql": "SELECT region FROM sales WHERE yr = @year",
                    "parameters": [{"name": "year", "type": "integer"}],
                    "bind": {"year": {"dial": "year"}},
                },
            },
        ],
    }
    table.put_item(Item=_canvas_row("c9", config=config))
    out = tools["describe_canvas"].invoke({"canvas_id": "c9"})
    assert out["title"] == "Canvas c9" and out["status"] == cv.STATUS_CREATED
    assert out["dials"][0]["name"] == "year"
    comp_tile, sql_tile = out["tiles"]
    # A computation tile points at the frozen doc; an ad-hoc tile carries SQL.
    assert comp_tile["computation"] == "monthly_revenue"
    assert "describe_computation" in comp_tile["sql_source"]
    assert comp_tile["bind"] == {"year": {"dial": "year"}}
    assert sql_tile["sql"].startswith("SELECT region")
    assert sql_tile["parameters"][0]["name"] == "year"
    # The raw config only on request.
    assert "config" not in out
    assert tools["describe_canvas"].invoke(
        {"canvas_id": "c9", "include_config": True}
    )["config"]["canvas_version"] == 1
    # A foreign / unknown id resolves to a corrective error, not a crash.
    assert tools["describe_canvas"].invoke({"canvas_id": "ghost"}).startswith("error:")


def test_skeleton_normalizes_and_guides(table):
    tools = _tools(table, FakeAgentCore())
    out = json.loads(tools["canvas_skeleton"].invoke(
        {"title": "Revenue overview", "widgets": _spec()["widgets"], "dials": _spec()["dials"]}
    ))
    # The normalized spec comes back for the model to carry into create_canvas
    # (layout defaults filled); the wireframe itself renders off the ARGS.
    assert out["spec"]["widgets"][0]["layout"] == {"w": 6, "h": 4}
    assert "create_canvas" in out["note"]


def test_skeleton_refuses_off_catalog_widget(table):
    tools = _tools(table, FakeAgentCore())
    widgets = [{"title": "T", "widget": "hologram", "represents": "x"}]
    out = tools["canvas_skeleton"].invoke({"title": "T", "widgets": widgets})
    assert out.startswith("error:") and "not in the catalog" in out


def test_create_writes_row_and_invokes(table):
    ac = FakeAgentCore()
    tools = _tools(
        table, ac, scope={"data_domain": "sales", "dataset": "orders"},
        thread_id="thread-42",
    )
    out = tools["create_canvas"].invoke(
        {"title": "Revenue overview", "spec": _spec(), "request": "revenue overview"}
    )
    assert "started canvas job" in out
    (call,) = ac.calls
    assert call["agentRuntimeArn"] == ARN
    payload = json.loads(call["payload"])
    assert payload["mode"] == "canvas" and payload["user_sub"] == SUB
    assert payload["data_domain"] == "sales" and payload["request"] == "revenue overview"
    # The agreed plan rides the payload NORMALIZED — the CI runtime treats it
    # as requirements.
    assert payload["spec"]["widgets"][0]["widget"] == "bar"
    assert payload["spec"]["widgets"][0]["layout"] == {"w": 6, "h": 4}
    assert payload["spec"]["dials"][0]["name"] == "year"
    # The delegating conversation rides the payload AND the row — the UI's
    # jobs indicator is chat-scoped.
    assert payload["thread_id"] == "thread-42"
    assert payload["title"] == "Revenue overview"
    # The QUEUED row exists under the caller's pk with the job id from the ack.
    job_id = payload["job_id"]
    row = table.get_item(Key={"pk": cv.canvas_job_pk(SUB), "sk": job_id})["Item"]
    assert row["status"] == cv.JOB_QUEUED
    assert row["thread_id"] == "thread-42"
    assert row["title"] == "Revenue overview"
    # Job rows are history: DynamoDB TTL reaps them (epoch seconds).
    import time

    assert int(row["expires_at"]) > time.time() + cv.JOB_TTL_SECONDS - 60
    # Status reads it back; a foreign id doesn't resolve.
    status = tools["get_canvas_status"].invoke({"job_id": job_id})
    assert status["status"] == cv.JOB_QUEUED
    assert "error:" in tools["get_canvas_status"].invoke({"job_id": "nope"})


def test_unscoped_create_requires_dataset(table):
    tools = _tools(table, FakeAgentCore())
    out = tools["create_canvas"].invoke({"title": "KPIs", "spec": _spec()})
    assert out.startswith("error:")


def test_create_refuses_invalid_spec(table):
    ac = FakeAgentCore()
    tools = _tools(table, ac, scope={"data_domain": "sales", "dataset": "orders"})
    out = tools["create_canvas"].invoke(
        {"title": "KPIs", "spec": {"title": "KPIs", "widgets": []}}
    )
    assert out.startswith("error:") and "canvas_skeleton" in out
    assert ac.calls == []  # nothing dispatched, no job row side effects


def test_invoke_failure_lands_failed_row(table):
    tools = _tools(
        table, FakeAgentCore(fail=True),
        scope={"data_domain": "sales", "dataset": "orders"},
    )
    # Contained, not a crash: the canvas tools ride the same error guard as
    # the consumption tools, so an infra failure comes back as a tool RESULT
    # the model can relay — while the job row still records the failure.
    out = tools["create_canvas"].invoke({"title": "KPIs", "spec": _spec()})
    assert out.startswith("Error:") and "could not start" in out
    rows = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
            cv.canvas_job_pk(SUB)
        )
    )["Items"]
    assert len(rows) == 1 and rows[0]["status"] == cv.JOB_FAILED
    assert "could not start" in rows[0]["error"]


def test_revise_requires_owned_canvas(table):
    ac = FakeAgentCore()
    tools = _tools(table, ac, scope={"data_domain": "sales", "dataset": "orders"})
    out = tools["revise_canvas"].invoke(
        {"canvas_id": "ghost", "feedback": "bigger"}
    )
    assert out.startswith("error:")
    table.put_item(
        Item={
            "pk": cv.canvas_pk(SUB), "sk": "c1", "canvas_id": "c1",
            "title": "Rev", "data_domain": "sales", "dataset": "orders",
            "status": cv.STATUS_CREATED, "config": "{}",
            "created_at": "t", "updated_at": "t",
        }
    )
    out = tools["revise_canvas"].invoke({"canvas_id": "c1", "feedback": "bigger"})
    assert "started revision job" in out
    payload = json.loads(ac.calls[-1]["payload"])
    assert payload["canvas_id"] == "c1" and payload["feedback"] == "bigger"
    assert "spec" not in payload  # cosmetic feedback travels bare
    # A structural revision carries the updated agreed plan.
    out = tools["revise_canvas"].invoke(
        {"canvas_id": "c1", "feedback": "add a KPI tile", "spec": _spec()}
    )
    assert "started revision job" in out
    payload = json.loads(ac.calls[-1]["payload"])
    assert payload["spec"]["widgets"][0]["widget"] == "bar"


class RejectingAgentCore(FakeAgentCore):
    """Acks like the real entrypoint does on validation failure: an HTTP-shape
    response whose body is {"status": "rejected", "error": ...} — and writes
    no DDB of its own."""

    def invoke_agent_runtime(self, **kwargs):
        import io

        self.calls.append(kwargs)
        body = json.dumps({"status": "rejected", "error": "bad payload"})
        return {"response": io.BytesIO(body.encode("utf-8"))}


def test_rejected_ack_fails_the_job_row(table):
    # The runtime's synchronous rejection writes no DDB — if chat ignored the
    # ack, the queued row would strand forever with the jobs panel polling a
    # spinner that can never resolve.
    tools = _tools(
        table, RejectingAgentCore(),
        scope={"data_domain": "sales", "dataset": "orders"},
    )
    out = tools["create_canvas"].invoke({"title": "KPIs", "spec": _spec()})
    assert out.startswith("Error:") and "rejected" in out
    rows = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
            cv.canvas_job_pk(SUB)
        )
    )["Items"]
    assert len(rows) == 1 and rows[0]["status"] == cv.JOB_FAILED
    assert "bad payload" in rows[0]["error"]


def test_revise_uses_the_canvas_rows_dataset_not_the_pinned_scope(table):
    # list_canvases deliberately spans datasets, so a revision may target a
    # canvas OUTSIDE the conversation's pinned scope. The canvas row is
    # authoritative — the pinned scope must not reassign the job (the CI's
    # scope guard would refuse every save, failing the job after minutes).
    ac = FakeAgentCore()
    tools = _tools(table, ac, scope={"data_domain": "sales", "dataset": "orders"})
    table.put_item(
        Item={
            "pk": cv.canvas_pk(SUB), "sk": "cx", "canvas_id": "cx",
            "title": "Standings", "data_domain": "sport", "dataset": "formula_1",
            "status": cv.STATUS_CREATED, "config": "{}",
            "created_at": "t", "updated_at": "t",
        }
    )
    out = tools["revise_canvas"].invoke({"canvas_id": "cx", "feedback": "bigger"})
    assert "started revision job" in out
    payload = json.loads(ac.calls[-1]["payload"])
    assert payload["data_domain"] == "sport" and payload["dataset"] == "formula_1"
