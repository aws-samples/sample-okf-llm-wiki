"""The post-turn AR policy check: anatomy, pre-pass, fingerprint gate, reports.

The load-bearing contracts, in test order: turn ordinals are index-identical to
the server's history fold (the UI's ``turn_key`` addresses the right turn);
eligibility spends nothing on turns without data claims (and never counts SQL
inside thinking blocks); extraction is deterministic and strips the anomaly
reminder; the pre-pass fails OPEN and its input is blind to the final answer;
a stale policy NEVER renders a verdict (it flags the row and publishes the
rebuild event instead); reports are idempotent per (thread, turn) and isolated
per user. Bedrock-runtime and EventBridge are hand fakes; DynamoDB + S3 run on
moto.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import boto3
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from moto import mock_aws

from chat import policy_check as pc
from chat import server
from okf_aws import ar_policy as ap

REGION = "us-east-1"
THREADS_TABLE = "okf-chat"
REGISTRY_TABLE = "okf-registry"
BUCKET = "okf-bundles"
DOMAIN, DATASET = "bird", "formula_1"
LABEL = f"{DOMAIN}/{DATASET}"


# --- message builders --------------------------------------------------------


def _user(text, scoped=False):
    if scoped:
        text = server.scoped_prompt(text, {"data_domain": DOMAIN, "dataset": DATASET})
    return HumanMessage(content=text)


def _ai(text="", tool_calls=None, thinking=""):
    content: list = []
    if thinking:
        content.append(
            {"type": "reasoning_content", "reasoning_content": {"text": thinking}}
        )
    if text:
        content.append({"type": "text", "text": text})
    calls = [
        {"name": name, "args": args, "id": cid, "type": "tool_call"}
        for name, args, cid in (tool_calls or [])
    ]
    return AIMessage(content=content, tool_calls=calls)


def _tool(name, content, cid="c1", status="success"):
    return ToolMessage(content=content, name=name, tool_call_id=cid, status=status)


_SQL_RESULT = json.dumps(
    {"columns": ["team", "pts"], "rows": [["A", 1]], "row_count": 1, "truncated": False}
)


def _sql_turn(question="avg points?", answer="Team A leads with 1 point."):
    return [
        _user(question, scoped=True),
        _ai(tool_calls=[("run_sql", {"sql": "SELECT 1"}, "c1")], thinking="plan"),
        _tool("run_sql", _SQL_RESULT, "c1"),
        _ai(text=answer),
    ]


# --- turn anatomy ---------------------------------------------------------------


def test_turn_slices_are_index_identical_to_the_history_fold():
    from chat.steering import STEERING_MARKER

    msgs = [
        _user("q1"),
        _ai(text="a1"),
        _user("q2", scoped=True),
        HumanMessage(content="<system-reminder>x</system-reminder>",
                     additional_kwargs={STEERING_MARKER: "silence"}),
        _ai(tool_calls=[("run_sql", {"sql": "SELECT 1"}, "c1")]),
        _tool("run_sql", _SQL_RESULT, "c1"),
        _ai(text="a2"),
        _user("q3"),
        _ai(text="a3"),
    ]
    slices = pc.turn_slices(msgs)
    turns = server._messages_to_turns(msgs)
    assert len(slices) == len(turns) == 3
    for i, turn in enumerate(turns):
        assert turn["id"] == f"turn_{i}"
        assert server.strip_scope_prefix(pc._text(slices[i][0].content)) == (
            turn["userMessage"]
        )


# --- eligibility ------------------------------------------------------------------


def test_sql_turn_is_eligible():
    flags = pc.eligibility(_sql_turn())
    assert flags["turn_ran_sql"] and flags["transcript_eligible"]
    assert flags["check_eligible"]


def test_prose_only_turn_is_not_eligible():
    turn = [_user("what is this dataset?"), _ai(text="It documents F1 races.")]
    assert not pc.eligibility(turn)["check_eligible"]


def test_answered_ask_human_makeses_the_transcript_eligible():
    turn = [
        _user("q"),
        _ai(tool_calls=[("ask_human", {"questions": []}, "c1")]),
        _tool("ask_human", json.dumps({"status": "answered", "answers": []}), "c1"),
        _ai(text="ok"),
    ]
    flags = pc.eligibility(turn)
    assert flags["turn_asked_human"] and flags["check_eligible"]


def test_errored_ask_human_is_not_an_interaction():
    turn = [
        _user("q"),
        _ai(tool_calls=[("ask_human", {"questions": "bad"}, "c1")]),
        _tool("ask_human", json.dumps({"status": "error"}), "c1", status="error"),
        _ai(text="ok"),
    ]
    assert not pc.eligibility(turn)["check_eligible"]


def test_recommended_sql_in_the_answer_is_eligible_without_execution():
    turn = [_user("q"), _ai(text="Run this:\n```sql\nSELECT 1\n```")]
    flags = pc.eligibility(turn)
    assert flags["answer_ships_sql"] and flags["check_eligible"]
    assert not flags["transcript_eligible"]


def test_sql_inside_a_thinking_block_is_not_an_answer_claim():
    turn = [_user("q"), _ai(text="No data needed.", thinking="```sql\nSELECT 1\n```")]
    assert not pc.eligibility(turn)["check_eligible"]


def test_plain_fence_with_select_lead_is_the_best_effort_fallback():
    assert pc.detect_fenced_sql("```\nSELECT a FROM t\n```")
    assert not pc.detect_fenced_sql("```\nnot a query\n```")
    assert pc.recommended_sql("```sql\nSELECT 1\n```") == ["SELECT 1"]


# --- deterministic extraction -------------------------------------------------------


def test_executed_queries_pairs_calls_with_measured_shapes():
    (q,) = pc.executed_queries(_sql_turn())
    assert q["sql"] == "SELECT 1"
    assert q["row_count"] == 1 and q["truncated"] is False
    assert q["columns"] == ["team", "pts"]


def test_executed_queries_strips_the_appended_anomaly_reminder():
    body = _SQL_RESULT + "\n\n<system-reminder>zero rows…</system-reminder>"
    turn = [
        _user("q"),
        _ai(tool_calls=[("run_sql", {"sql": "SELECT 1"}, "c1")]),
        _tool("run_sql", body, "c1"),
    ]
    (q,) = pc.executed_queries(turn)
    assert q["row_count"] == 1


def test_failed_queries_are_omitted():
    turn = [
        _user("q"),
        _ai(tool_calls=[("run_sql", {"sql": "SELECT boom"}, "c1")]),
        _tool("run_sql", "Error: COLUMN_NOT_FOUND", "c1", status="error"),
    ]
    assert pc.executed_queries(turn) == []


def test_ask_human_qa_extraction():
    answered = json.dumps(
        {
            "status": "answered",
            "answers": [{"id": "a", "prompt": "Which season?", "answer": "2019"}],
            "note": "calendar year please",
        }
    )
    turn = [_user("q"), _ai(tool_calls=[("ask_human", {}, "c1")]),
            _tool("ask_human", answered, "c1")]
    qa = pc.ask_human_qa(turn)
    assert {"prompt": "Which season?", "answer": "2019"} in qa
    assert any("calendar year" in x["answer"] for x in qa)


def test_datasets_touched_from_scope_args_and_search_results():
    hits = json.dumps([{"path": "sales/orders/tables/orders", "title": "t"}])
    turn = [
        _user("q", scoped=True),  # bird/formula_1 via the scope preamble
        _ai(tool_calls=[
            ("read_page", {"concept_id": "x", "data_domain": "hr", "dataset": "people"}, "c1"),
            ("semantic_search", {"query": "orders"}, "c2"),
        ]),
        _tool("read_page", "{}", "c1"),
        _tool("semantic_search", hits, "c2"),
    ]
    assert pc.datasets_touched(turn) == [LABEL, "hr/people", "sales/orders"]


def test_thread_fallback_uses_the_nearest_prior_turn():
    slices = [_sql_turn(), [_user("and for 2019?"), _ai(text="…")]]
    assert pc._thread_fallback(slices, 1) == [LABEL]


# --- the pre-pass --------------------------------------------------------------------


def test_prepass_input_is_blind_to_the_final_answer():
    turn = _sql_turn(answer="THE-ANSWER-TEXT never leaks")
    text = pc.build_prepass_input([], turn)
    assert "THE-ANSWER-TEXT" not in text
    assert "SELECT 1" in text  # tool calls present
    assert "plan" in text  # thinking present (assumption material)
    assert "Query 1 returned 1 rows" in text  # measured, told not derived


def test_prepass_input_carries_prior_turn_text_for_the_rewrite():
    prior = _sql_turn(question="points per team in 2019?", answer="A: 400")
    turn = [_user("and for 2018?"), _ai(text="B")]
    text = pc.build_prepass_input([prior], turn)
    assert "points per team in 2019?" in text and "A: 400" in text
    assert "and for 2018?" in text


def test_prepass_input_lists_the_policy_vocabulary():
    # Dataset-specific variables the build derived: the transcript must be
    # able to NAME them (paraphrase is what breeds TRANSLATION_AMBIGUOUS —
    # live-observed on formula_1). Descriptions are capped; no vocabulary,
    # no section.
    turn = _sql_turn()
    vocab = [{"name": "lastDriverStandingsRowSelected", "description": "d" * 400}]
    text = pc.build_prepass_input([], turn, vocabulary=vocab)
    assert "POLICY VOCABULARY" in text
    assert "lastDriverStandingsRowSelected" in text
    assert "d" * pc._VOCAB_DESC_CHARS in text
    assert "d" * (pc._VOCAB_DESC_CHARS + 1) not in text
    assert "POLICY VOCABULARY" not in pc.build_prepass_input([], turn)


def test_gather_policy_vocabulary_dedupes_and_drops_core_terms():
    import io

    from okf_aws.ar_policy import vocabulary_key

    class FakeS3:
        def __init__(self, blobs):
            self.blobs = blobs

        def get_object(self, Bucket, Key):
            if Key not in self.blobs:
                raise KeyError(Key)
            return {"Body": io.BytesIO(self.blobs[Key])}

    doc = json.dumps(
        [
            # A core term re-derived by the build: already in the contract,
            # listing it twice would only dilute the section.
            {"name": "queryExecuted", "description": "core duplicate"},
            {"name": "lastRowSelected", "description": "keep me"},
        ]
    ).encode()
    s3 = FakeS3(
        {
            vocabulary_key("bird", "f1"): doc,
            vocabulary_key("bird", "f2"): doc,  # same names: deduped across sets
        }
    )
    out = pc.gather_policy_vocabulary(
        s3, bucket="b", labels=["bird/f1", "bird/f2", "bird/never-built"]
    )
    assert out == [{"name": "lastRowSelected", "description": "keep me"}]


_GOOD_PREPASS = json.dumps(
    {
        "standalone_question": "average points per team for 2019",
        "rewritten": True,
        "transcript": "One query executed. dedupApplied is not determinable.",
        "assumptions": ["season read as calendar year"],
    }
)


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return AIMessage(content=reply)


def test_run_prepass_retries_once_then_parses():
    model = FakeModel(["not json", _GOOD_PREPASS])
    out = pc.run_prepass(model, "input")
    assert out and out["rewritten"] is True
    assert len(model.calls) == 2
    assert "ONLY the JSON" in str(model.calls[1])


def test_run_prepass_fails_open():
    assert pc.run_prepass(FakeModel(["nope", "still nope"]), "x") is None
    assert pc.run_prepass(FakeModel([RuntimeError("model down")]), "x") is None


def test_parse_prepass_accepts_a_fenced_object():
    assert pc.parse_prepass(f"```json\n{_GOOD_PREPASS}\n```") is not None
    assert pc.parse_prepass('{"rewritten": true}') is None  # missing question


# --- verdicts -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "types,expected",
    [
        (["INVALID"], "violation"),
        (["IMPOSSIBLE", "SATISFIABLE"], "violation"),
        (["SATISFIABLE", "VALID"], "consistent"),
        # NO_TRANSLATIONS is per-content-unit noise (a chart sentence, a
        # courtesy line) — it only decides the verdict when it is ALL there is
        # (live-observed: one untranslatable paragraph next to real findings
        # must not veto them).
        (["NO_TRANSLATIONS", "SATISFIABLE"], "consistent"),
        (["NO_TRANSLATIONS", "VALID"], "consistent"),
        (["NO_TRANSLATIONS", "INVALID"], "violation"),
        (["NO_TRANSLATIONS", "TRANSLATION_AMBIGUOUS"], "not_checkable"),
        (["NO_TRANSLATIONS"], "not_checkable"),
        (["TOO_COMPLEX", "VALID"], "not_checkable"),
        ([], "consistent"),
    ],
)
def test_dataset_verdict_worst_of(types, expected):
    assert pc.dataset_verdict([{"type": t} for t in types]) == expected


def test_render_findings_quotes_the_grounded_rule():
    grounding = {
        "RULE1": {"rule_text": "IF zeroRows THEN no figures",
                  "rule_source_page": "references/usage_guardrails.md"}
    }
    (f,) = pc.render_findings(
        [{"type": "INVALID", "claim": "c", "rule_ids": ["RULE1"], "scenario": [],
          "confidence": 0.9}],
        grounding,
    )
    assert f["rule_text"] == "IF zeroRows THEN no figures"
    assert f["rule_source_page"] == "references/usage_guardrails.md"


# --- the orchestrator ------------------------------------------------------------------


class FakeBedrockRuntime:
    def __init__(self, finding=None):
        self.finding = finding
        self.calls: list[dict] = []

    def apply_guardrail(self, **kw):
        self.calls.append(kw)
        findings = [self.finding] if self.finding else []
        return {
            "usage": {"automatedReasoningPolicyUnits": 1},
            "assessments": [{"automatedReasoningPolicy": {"findings": findings}}],
        }


_INVALID_FINDING = {
    "invalid": {
        "translation": {
            "premises": [], "claims": [{"naturalLanguage": "states figures"}],
            "untranslatedPremises": [], "untranslatedClaims": [], "confidence": 0.9,
        },
        "contradictingRules": [{"identifier": "RULE1", "policyVersionArn": "arn:p:1"}],
    }
}


class FakeEvents:
    def __init__(self):
        self.entries: list[dict] = []

    def put_events(self, *, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


class FakeGraph:
    def __init__(self, messages):
        self.messages = messages

    def get_state(self, cfg):
        return SimpleNamespace(values={"messages": self.messages})


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "t")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "t")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name in (THREADS_TABLE, REGISTRY_TABLE):
            ddb.create_table(
                TableName=name,
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
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        yield {"ddb": ddb, "s3": s3}


def _seed_usable_policy(env):
    """A registry row whose stored fingerprint matches the live wiki."""
    env["s3"].put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"the rules",
    )
    fresh = ap.source_hash(env["s3"], BUCKET, DOMAIN, DATASET)
    env["ddb"].put_item(
        TableName=REGISTRY_TABLE,
        Item={
            **ap.registry_key(DOMAIN, DATASET),
            ap.ATTR_ENROLLED: {"BOOL": True},
            ap.ATTR_BUILD_STATUS: {"S": "ready"},
            ap.ATTR_SOURCE_HASH: {"S": fresh},
            "ar_guardrail_id": {"S": "g1"},
            "ar_guardrail_version": {"S": "2"},
            "ar_policy_version": {"S": "1"},
        },
    )
    ap.put_grounding(
        env["s3"], bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        grounding={"RULE1": {"rule_text": "IF zeroRows THEN no figures",
                              "rule_source_page": "references/usage_guardrails.md"}},
    )


def _cfg():
    return SimpleNamespace(
        threads_table=THREADS_TABLE,
        registry_table=REGISTRY_TABLE,
        bundle_bucket=BUCKET,
        region=REGION,
        policy_check_model="fake-model",
        policy_check_enabled=True,
    )


def _run(env, turn_key=0, messages=None, model=None, bedrock=None, events=None,
         force=False, user="alice"):
    messages = messages if messages is not None else _sql_turn()
    clients = {
        "ddb": env["ddb"],
        "s3": env["s3"],
        "bedrock_runtime": bedrock or FakeBedrockRuntime(_INVALID_FINDING),
        "events": events or FakeEvents(),
        "model": model or FakeModel([_GOOD_PREPASS, "The agent recommended a query that sums points."]),
    }
    return pc.run_policy_check(
        {"turn_key": turn_key, "force": force},
        user_sub=user,
        client_thread_id="conv1",
        internal_thread_id=f"{user}:conv1",
        chat_config=_cfg(),
        build_agent=lambda *a, **k: FakeGraph(messages),
        checkpointer=object(),
        clients=clients,
    )


def test_complete_check_renders_a_grounded_violation(env):
    _seed_usable_policy(env)
    bedrock = FakeBedrockRuntime(_INVALID_FINDING)
    out = _run(env, bedrock=bedrock)
    assert out["status"] == "complete" and out["eligible"] is True
    (ds,) = out["datasets"]
    assert ds["verdict"] == "violation"
    (finding,) = ds["findings"]
    assert finding["rule_text"] == "IF zeroRows THEN no figures"
    assert finding["rule_source_page"] == "references/usage_guardrails.md"
    assert out["policy_versions_used"] == {LABEL: "1"}
    # The speaker split on the wire: the question is a premise, the answer text
    # and transcript are claims.
    (call,) = bedrock.calls
    quals = {b["text"]["text"]: b["text"]["qualifiers"] for b in call["content"]}
    assert quals["average points per team for 2019"] == ["query"]
    assert any(
        q == ["guard_content"] and "Team A leads" in t for t, q in quals.items()
    )
    assert call["guardrailIdentifier"] == "g1" and call["guardrailVersion"] == "2"


def test_reports_are_idempotent_and_force_reruns(env):
    _seed_usable_policy(env)
    first = _run(env)
    model = FakeModel([])  # would raise on any call
    again = _run(env, model=model)
    assert again == first and model.calls == []
    rerun = _run(env, force=True)
    assert rerun["status"] == "complete"


def test_not_eligible_is_persisted_and_returned(env):
    turn = [_user("hi"), _ai(text="hello!")]
    out = _run(env, messages=turn)
    assert out["status"] == "not_eligible" and out["eligible"] is False
    stored = pc.read_report(env["ddb"], THREADS_TABLE, "alice", "conv1", 0)
    assert stored and stored["status"] == "not_eligible"


def test_stale_policy_never_renders_a_verdict(env):
    _seed_usable_policy(env)
    # The wiki moves after the build: fingerprints no longer match.
    env["s3"].put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"the rules, amended",
    )
    bedrock, events = FakeBedrockRuntime(_INVALID_FINDING), FakeEvents()
    out = _run(env, bedrock=bedrock, events=events)
    (ds,) = out["datasets"]
    assert ds["verdict"] == "stale" and ds["findings"] == []
    assert bedrock.calls == []  # the hard gate: no AR call against a stale policy
    (entry,) = events.entries
    assert entry["DetailType"] == "policy_rebuild"
    assert json.loads(entry["Detail"])["dataset"] == DATASET
    row = env["ddb"].get_item(
        TableName=REGISTRY_TABLE, Key=ap.registry_key(DOMAIN, DATASET)
    )["Item"]
    assert row[ap.ATTR_BUILD_STATUS]["S"] == "stale"


def test_building_and_absent_policies_report_their_states(env):
    env["ddb"].put_item(
        TableName=REGISTRY_TABLE,
        Item={**ap.registry_key(DOMAIN, DATASET),
              ap.ATTR_ENROLLED: {"BOOL": True},
              ap.ATTR_BUILD_STATUS: {"S": "building"}},
    )
    out = _run(env)
    (ds,) = out["datasets"]
    assert ds["verdict"] == "building"

    env["ddb"].delete_item(
        TableName=REGISTRY_TABLE, Key=ap.registry_key(DOMAIN, DATASET)
    )
    out = _run(env, force=True)
    (ds,) = out["datasets"]
    assert ds["verdict"] == "not_enrolled"


def test_unenrolled_dataset_reports_not_enrolled_and_is_never_checked(env):
    # A mapping row with NO ar attrs (registered, never opted in): the sidebar
    # gets the enrollment pointer, and neither bedrock nor events is touched.
    env["ddb"].put_item(
        TableName=REGISTRY_TABLE,
        Item={**ap.registry_key(DOMAIN, DATASET), "data_domain": {"S": DOMAIN}},
    )
    bedrock, events = FakeBedrockRuntime(_INVALID_FINDING), FakeEvents()
    out = _run(env, bedrock=bedrock, events=events)
    (ds,) = out["datasets"]
    assert ds["verdict"] == "not_enrolled" and ds["findings"] == []
    assert bedrock.calls == [] and events.entries == []


def test_enrolled_but_never_built_reports_no_policy(env):
    env["ddb"].put_item(
        TableName=REGISTRY_TABLE,
        Item={**ap.registry_key(DOMAIN, DATASET),
              ap.ATTR_ENROLLED: {"BOOL": True}},
    )
    out = _run(env)
    (ds,) = out["datasets"]
    assert ds["verdict"] == "no_policy"


def test_prepass_failure_is_unavailable_and_not_persisted(env):
    _seed_usable_policy(env)
    out = _run(env, model=FakeModel(["junk", "junk"]))
    assert out["status"] == "unavailable"
    assert pc.read_report(env["ddb"], THREADS_TABLE, "alice", "conv1", 0) is None
    # A later click with a healthy model completes normally.
    assert _run(env)["status"] == "complete"


def test_bad_and_out_of_range_turn_keys(env):
    assert _run(env, turn_key="x")["error_code"] == "bad_request"
    assert _run(env, turn_key=7)["error_code"] == "not_found"


def test_in_flight_turn_reports_running(env, monkeypatch):
    from chat import live_streams

    monkeypatch.setattr(live_streams, "get", lambda key: SimpleNamespace(user_message="q"))
    monkeypatch.setattr(live_streams, "is_active", lambda key: True)
    out = _run(env, turn_key=0)
    assert out == {"type": "policy_check", "turn_key": 0, "status": "running"}


def test_reports_are_isolated_per_user(env):
    _seed_usable_policy(env)
    _run(env, user="alice")
    assert pc.read_report(env["ddb"], THREADS_TABLE, "bob", "conv1", 0) is None


# --- the server branch -------------------------------------------------------------


def _post_policy_check(app, enabled_sub="alice"):
    import jwt
    from fastapi.testclient import TestClient

    token = jwt.encode({"sub": enabled_sub}, "k" * 32, algorithm="HS256")
    return TestClient(app).post(
        "/invocations",
        json={"input": {"type": "policy_check", "turn_key": 0}},
        headers={
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "conv1",
            "Authorization": f"Bearer {token}",
        },
    )


def _stub_app(enabled: bool, monkeypatch=None):
    class _Cfg:
        checkpoint_table = "okf-chat-checkpoints"
        threads_table = THREADS_TABLE
        region = REGION
        checkpoint_ttl_seconds = None
        policy_check_enabled = enabled

    def _fake_checkpointer(cfg):
        return object()

    import chat.server as srv

    if monkeypatch:
        monkeypatch.setattr(srv, "make_checkpointer", _fake_checkpointer)
    return srv.build_app(
        chat_config=_Cfg(),
        build_agent=lambda *a, **k: FakeGraph([]),
        index_writer=lambda **kw: None,
        policy_clients={"ddb": None, "s3": None, "bedrock_runtime": None,
                        "events": None, "model": None},
    )


def test_disabled_deployment_refuses_the_request_type(monkeypatch):
    resp = _post_policy_check(_stub_app(False, monkeypatch))
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "error" and body["error_code"] == "disabled"


def test_enabled_deployment_dispatches_to_the_handler(monkeypatch):
    seen: list[dict] = []

    def _fake_run(input_data, **kw):
        seen.append(input_data)
        return {"type": "policy_check", "turn_key": 0, "status": "not_eligible"}

    import chat.policy_check as pcmod

    monkeypatch.setattr(pcmod, "run_policy_check", _fake_run)
    resp = _post_policy_check(_stub_app(True, monkeypatch))
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_eligible"
    assert seen == [{"type": "policy_check", "turn_key": 0}]
