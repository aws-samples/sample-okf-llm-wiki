"""Job lifecycle through the runner (fake agent factory — no model, no
bedrock) and the entrypoint's validation/ack behavior."""

from __future__ import annotations

import json

import pytest

from okf_core import canvas as cv

from canvas import entrypoint, jobs
from canvas.runner import run_canvas_job
from tests.conftest import BUCKET, CANVAS, DATASET, DOMAIN, REGISTRY, SUB, VALID_CONFIG


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OKF_BUNDLE_BUCKET", BUCKET)
    monkeypatch.setenv("OKF_REGISTRY_TABLE", REGISTRY)
    monkeypatch.setenv("OKF_CANVAS_TABLE", CANVAS)
    monkeypatch.delenv("OKF_CHAT_CHECKPOINT_TABLE", raising=False)
    monkeypatch.delenv("OKF_CANVAS_HARNESS_PATH", raising=False)


class ScriptedAgent:
    """Drives the REAL tools the way the model would, then answers — firing
    the run's callbacks like the real harness does, so the step feed's
    on_llm_end path (text-between-tools) is exercised, not bypassed."""

    def __init__(self, ctx, script):
        self.ctx = ctx
        self.script = script

    def invoke(self, _state, config=None):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult

        from canvas.tools import build_canvas_tools

        tools = {t.name: t for t in build_canvas_tools(self.ctx)}
        for name, args in self.script:
            tools[name].invoke(args)

        final = AIMessage(content="Submitted the revenue dashboard.")
        for cb in (config or {}).get("callbacks") or []:
            cb.on_llm_end(
                LLMResult(generations=[[ChatGeneration(message=final)]])
            )
        return {"messages": [final]}


def _clients(aws):
    import boto3

    from tests.conftest import FakeAthena

    registry = boto3.resource("dynamodb", region_name="us-east-1").Table(REGISTRY)
    return {
        "s3": aws["s3"], "ddb": aws["ddb"], "athena": FakeAthena(),
        "registry": registry,
    }


def _payload(**over):
    return {
        "mode": "canvas",
        "job_id": "job1",
        "user_sub": SUB,
        "data_domain": DOMAIN,
        "dataset": DATASET,
        "request": "revenue overview",
        **over,
    }


def test_job_submits_and_rows_reflect_it(aws):
    script = [
        ("save_canvas", {"config_json": json.dumps(VALID_CONFIG)}),
    ]
    out = run_canvas_job(
        _payload(),
        clients=_clients(aws),
        agent_factory=lambda ctx, env: ScriptedAgent(ctx, script),
    )
    assert out["status"] == "completed"
    job = aws["ddb"].get_item(
        TableName=CANVAS, Key=jobs.job_key(SUB, "job1")
    )["Item"]
    assert job["status"]["S"] == "completed"
    assert job["canvas_id"]["S"] == out["canvas_id"]
    assert int(job["expires_at"]["N"]) > 0  # TTL-reaped history
    # The final text landed on the step feed (emit-time filtered — no
    # reasoning ever exists to filter, but the shape is text/tool only).
    steps = json.loads(job["steps"]["S"])
    assert steps[-1]["type"] == "text"
    canvas_row = aws["ddb"].get_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_pk(SUB)}, "sk": {"S": out["canvas_id"]}},
    )["Item"]
    assert canvas_row["status"]["S"] == cv.STATUS_CREATED


def test_job_without_save_fails_honestly(aws):
    out = run_canvas_job(
        _payload(job_id="job2"),
        clients=_clients(aws),
        agent_factory=lambda ctx, env: ScriptedAgent(ctx, []),
    )
    assert out["status"] == "failed"
    job = aws["ddb"].get_item(TableName=CANVAS, Key=jobs.job_key(SUB, "job2"))["Item"]
    assert job["status"]["S"] == "failed"


def test_job_crash_lands_on_the_row(aws):
    def exploding_factory(ctx, env):
        raise RuntimeError("model unavailable")

    out = run_canvas_job(
        _payload(job_id="job3"),
        clients=_clients(aws),
        agent_factory=exploding_factory,
    )
    assert out["status"] == "failed"
    job = aws["ddb"].get_item(TableName=CANVAS, Key=jobs.job_key(SUB, "job3"))["Item"]
    assert "model unavailable" in job["error"]["S"]


def test_error_after_save_still_completes_with_the_canvas(aws):
    """save_canvas made the canvas live; a LATER failure (the summary turn's
    Bedrock timeout) must not report the job failed without its canvas id —
    the drawer would hide "Open canvas" and invite a duplicate."""

    class SaveThenDie(ScriptedAgent):
        def invoke(self, state, config=None):
            from canvas.tools import build_canvas_tools

            tools = {t.name: t for t in build_canvas_tools(self.ctx)}
            tools["save_canvas"].invoke({"config_json": json.dumps(VALID_CONFIG)})
            raise TimeoutError("ReadTimeoutError on /converse")

    out = run_canvas_job(
        _payload(job_id="job-late"),
        clients=_clients(aws),
        agent_factory=lambda ctx, env: SaveThenDie(ctx, []),
    )
    assert out["status"] == "completed" and out["canvas_id"]
    job = aws["ddb"].get_item(
        TableName=CANVAS, Key=jobs.job_key(SUB, "job-late")
    )["Item"]
    assert job["status"]["S"] == "completed"
    assert job["canvas_id"]["S"] == out["canvas_id"]


def test_revision_seeds_the_stored_config(aws):
    jobs.write_canvas_row(
        aws["ddb"], table=CANVAS, user_sub=SUB, canvas_id="c9",
        title="Revenue", data_domain=DOMAIN, dataset=DATASET,
        status=cv.STATUS_CREATED, config=VALID_CONFIG,
    )
    seen = {}

    def factory(ctx, env):
        seen["canvas_id"] = ctx.canvas_id
        return ScriptedAgent(
            ctx, [("save_canvas", {"config_json": json.dumps(VALID_CONFIG)})]
        )

    out = run_canvas_job(
        _payload(job_id="job4", request="", canvas_id="c9", feedback="bigger trend"),
        clients=_clients(aws),
        agent_factory=factory,
    )
    # The revision pinned the EXISTING canvas id, so save_canvas overwrote the
    # same row instead of minting a new dashboard.
    assert seen["canvas_id"] == "c9"
    assert out["status"] == "completed" and out["canvas_id"] == "c9"


def test_make_checkpointer_constructs_against_the_real_saver(aws):
    """Instantiate the REAL DynamoDBSaver with our kwargs — the constructor is
    unforgiving (a guessed kwarg died live with a TypeError), so the contract
    check is construction itself, with and without the S3 offload."""
    import boto3

    from canvas.runner import make_checkpointer

    ddb = boto3.client("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="okf-chat-checkpoints",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    assert make_checkpointer({}) is None
    base = {
        "OKF_CHAT_CHECKPOINT_TABLE": "okf-chat-checkpoints",
        "AWS_REGION": "us-east-1",
    }
    assert make_checkpointer(base) is not None
    assert (
        make_checkpointer({**base, "OKF_CHECKPOINT_OFFLOAD_BUCKET": "offload"})
        is not None
    )


def test_entrypoint_validation_and_ack(monkeypatch):
    assert "job_id" in entrypoint.validate({})
    assert "request" in entrypoint.validate(
        {"job_id": "j", "user_sub": "s", "data_domain": "d", "dataset": "ds"}
    )
    assert entrypoint.validate(_payload()) is None
    # Revision shape is valid without `request`.
    assert (
        entrypoint.validate(_payload(request="", canvas_id="c1", feedback="f")) is None
    )
    # A spec-only create (the chat flow: the agreed plan IS the brief).
    assert (
        entrypoint.validate(_payload(request="", spec={"title": "T", "widgets": []}))
        is None
    )

    ran = {}
    monkeypatch.setattr(
        "canvas.entrypoint.run_canvas_job",
        lambda payload, session_id=None: ran.update(payload=payload) or {},
    )
    ack = entrypoint.start_job(_payload(), session_id="sess")
    assert ack == {"status": "accepted", "job_id": "job1"}
    # The daemon thread runs the job; wait for it.
    import time

    for _ in range(50):
        if ran:
            break
        time.sleep(0.02)
    assert ran["payload"]["job_id"] == "job1"
    assert not entrypoint.is_busy()


def test_job_message_embeds_the_agreed_plan():
    from canvas.runner import _job_message

    spec = {
        "title": "Season overview",
        "widgets": [
            {"title": "Wins", "widget": "bar", "represents": "wins per team",
             "layout": {"w": 6, "h": 4}}
        ],
        "dials": [{"name": "season", "label": "Season", "filters": "season"}],
    }
    msg = _job_message("", "", None, "f1", "races", "Season overview", spec=spec)
    # The plan rides the brief as a JSON fence the agent can read verbatim.
    assert "tile plan agreed with the user" in msg
    assert '"widget": "bar"' in msg
    assert 'Use "Season overview" as the canvas title.' in msg
    # No free-form request → no dangling empty request section.
    assert "The user's request" not in msg
    # A structural revision carries the plan alongside feedback + config.
    rev = _job_message("", "bigger", "{}", "f1", "races", spec=spec)
    assert "The user's feedback" in rev and '"widget": "bar"' in rev


def test_repeat_lints_hit_the_execute_memo(aws):
    # The live loop observed in production: the model lints after every edit,
    # and each lint re-ran EVERY tile against the engine (the same COUNT(*)
    # executed 13× in one job). Successful runs are now memoized per job —
    # the second identical lint must add ZERO engine queries.
    clients = _clients(aws)
    athena = clients["athena"]
    script = [
        ("lint_canvas", {"config_json": json.dumps(VALID_CONFIG)}),
        ("lint_canvas", {"config_json": json.dumps(VALID_CONFIG)}),
        ("save_canvas", {"config_json": json.dumps(VALID_CONFIG)}),
    ]
    out = run_canvas_job(
        _payload(job_id="job-memo"),
        clients=clients,
        agent_factory=lambda ctx, env: ScriptedAgent(ctx, script),
    )
    assert out["status"] == "completed"
    first_round = len(athena.started)
    assert first_round > 0  # the first lint really executed live probes
    # lint #2 and the save's gate lint replayed the SAME (statement, values)
    # pairs — all served from the memo.
    assert len(athena.started) == first_round


def test_bedrock_config_lifts_the_read_timeout():
    from canvas.runner import _bedrock_config

    cfg = _bedrock_config({})
    # Harvest's convention: minutes of headroom (botocore's 60s default died
    # live on a long Opus turn), adaptive retries.
    assert cfg.read_timeout == 600
    assert cfg.connect_timeout == 10
    assert cfg.retries["max_attempts"] == 5
    cfg = _bedrock_config({"OKF_CANVAS_BEDROCK_READ_TIMEOUT": "1200"})
    assert cfg.read_timeout == 1200


def test_revision_on_a_deleted_canvas_fails_the_job(aws):
    # The user deleted the canvas while the revision sat queued: the job must
    # FAIL with a clear error — falling through used to drop the feedback,
    # rebuild a generic canvas, and resurrect the deleted id.
    out = run_canvas_job(
        _payload(job_id="job9", request="", canvas_id="ghost", feedback="bigger"),
        clients=_clients(aws),
        agent_factory=lambda ctx, env: ScriptedAgent(ctx, []),
    )
    assert out["status"] == "failed" and "no longer exists" in out["error"]
    row = aws["ddb"].get_item(
        TableName=CANVAS,
        Key={"pk": {"S": cv.canvas_job_pk(SUB)}, "sk": {"S": "job9"}},
    )["Item"]
    assert row["status"]["S"] == "failed"
    # Nothing was written under the deleted id.
    assert jobs.get_canvas_row(
        aws["ddb"], table=CANVAS, user_sub=SUB, canvas_id="ghost"
    ) is None


def test_save_refuses_to_resurrect_a_canvas_deleted_mid_job(aws):
    # Deletion DURING the run: write_canvas_row's conditional put refuses, and
    # save_canvas reports it instead of resurrecting the row.
    jobs.write_canvas_row(
        aws["ddb"], table=CANVAS, user_sub=SUB, canvas_id="c9",
        title="Revenue", data_domain=DOMAIN, dataset=DATASET,
        status=cv.STATUS_CREATED, config=VALID_CONFIG,
    )
    saves = {}

    def factory(ctx, env):
        class DeleteThenSave(ScriptedAgent):
            def invoke(self, state, config=None):
                from canvas.tools import build_canvas_tools

                aws["ddb"].delete_item(
                    TableName=CANVAS,
                    Key=jobs.canvas_key(SUB, "c9"),
                )
                tools = {t.name: t for t in build_canvas_tools(ctx)}
                saves["out"] = tools["save_canvas"].invoke(
                    {"config_json": json.dumps(VALID_CONFIG)}
                )
                return super().invoke(state, config)

        return DeleteThenSave(ctx, [])

    out = run_canvas_job(
        _payload(job_id="job10", request="", canvas_id="c9", feedback="bigger"),
        clients=_clients(aws),
        agent_factory=factory,
    )
    assert saves["out"]["saved"] is False
    assert "deleted while the job ran" in saves["out"]["error"]
    assert out["status"] == "failed"
    assert jobs.get_canvas_row(
        aws["ddb"], table=CANVAS, user_sub=SUB, canvas_id="c9"
    ) is None
