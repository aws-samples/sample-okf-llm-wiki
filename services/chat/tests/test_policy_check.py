"""Mid-turn policy checks (v3): tracks, curated question, gates, middleware.

The load-bearing contracts, in test order: the exploration gate and dataset
resolution are pure and spend nothing; the computational track judges ONLY
computational policies against the curated question + SQL, with dedup and a
per-turn budget; the behavioural track batches over the steps-so-far, judges
ONLY behavioural policies, injects marker messages, and never nags twice; the
rolling curated question costs zero model calls on turn 1, folds ask_human
answers on resume (the deliberate v3 reversal), and survives reload on the
THREAD row; judges answer through ONE enforced tool and fail open as missing
shards; a stale/pre-v3 document NEVER renders a verdict (it flags the row and
publishes the rebuild event — the self-heal migration). EventBridge is a hand
fake; DynamoDB + S3 run on moto.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import boto3
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from moto import mock_aws

from chat import policy_check as pc
from chat import server
from chat.threads import read_policy_state, write_policy_state
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

_ANALYTICAL_SQL = "SELECT SUM(points) FROM driverstandings"


# --- scripted models -----------------------------------------------------------


class FakeModel:
    """Plain-text scripted model (the curated-question rewrite path)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append(
            messages[0][1] if isinstance(messages[0], tuple) else str(messages)
        )
        return AIMessage(content=self.replies.pop(0))


class _RaisingModel:
    def invoke(self, *a, **k):
        raise AssertionError("this call must never happen")


class FakeToolModel:
    """Scripted tool-calling model. Each script entry is (tool_name, args) or
    None (a prose reply with no tool call). Thread-safe pop for the pool."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        prompt = messages[0][1] if isinstance(messages[0], tuple) else str(messages)
        with self._lock:
            self.prompts.append(prompt)
            script = self.scripts.pop(0) if self.scripts else None
        if script is None:
            return AIMessage(content="prose, no tool call")
        name, args = script
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": "t1", "type": "tool_call"}],
        )


class FakeEvents:
    def __init__(self):
        self.entries: list[dict] = []

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0}


def _wait_until(predicate, timeout=10.0):
    """Poll for an async side effect (the checker pool has 8 workers, so a
    barrier submit no longer guarantees ordering)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- fixtures -------------------------------------------------------------------


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


_POLICY_DOC = """\
policies:
  - id: P001
    type: computational
    condition: figures are stated from a query
    action: never state figures without the documented dedup
    source: references/usage_guardrails.md
  - id: P002
    type: behavioural
    condition: an ambiguous points term is answered without pinning a reading
    action: ask for clarification first
    source: references/usage_guardrails.md
"""


def _seed_usable_policy(env, dataset=DATASET, doc=None, glue_database=None):
    """A registry row whose stored fingerprint matches the live wiki + a doc."""
    env["s3"].put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{dataset}/references/usage_guardrails.md",
        Body=b"the rules",
    )
    fresh = ap.source_hash(env["s3"], BUCKET, DOMAIN, dataset)
    item = {
        **ap.registry_key(DOMAIN, dataset),
        ap.ATTR_ENROLLED: {"BOOL": True},
        ap.ATTR_BUILD_STATUS: {"S": "ready"},
        ap.ATTR_SOURCE_HASH: {"S": fresh},
    }
    if glue_database:
        item["glue_database"] = {"S": glue_database}
    env["ddb"].put_item(TableName=REGISTRY_TABLE, Item=item)
    ap.put_policy_doc(
        env["s3"], bucket=BUCKET, data_domain=DOMAIN, dataset=dataset,
        doc_text=doc if doc is not None else _POLICY_DOC,
    )


def _cfg():
    return SimpleNamespace(
        threads_table=THREADS_TABLE,
        registry_table=REGISTRY_TABLE,
        bundle_bucket=BUCKET,
        region=REGION,
        policy_check_model="fake-model",
        policy_check_enabled=True,
        policy_shard_size=10,
        policy_judge_effort="low",
        policy_query_timeout_s=10,
        policy_query_max_per_turn=3,
    )


def _checker(env, judge, *, cfg=None, events=None, domain=DOMAIN, dataset=DATASET,
             tracks=("computational",), question="career points?",
             user_sub="", thread_id="", rewrite_model=None):
    return pc.PolicyChecker(
        chat_config=cfg or _cfg(),
        tracks=tracks,
        data_domain=domain,
        dataset=dataset,
        question=question,
        user_sub=user_sub,
        thread_id=thread_id,
        clients={"ddb": env["ddb"], "s3": env["s3"],
                 "events": events or FakeEvents()},
        judge_model=judge,
        # Tests that never exercise the rewrite must never build a live model.
        rewrite_model=rewrite_model or _RaisingModel(),
    )


_QUERY_VIOLATION_SCRIPT = [
    ("report_violations", {"violations": ["P001"]}),
]

_CLEAN_SCRIPT = [("report_violations", {"violations": []})]


# --- the exploration gate + dataset resolution (pure) ----------------------------


def test_is_analytical_sql_gate():
    analytical = [
        "SELECT COUNT(*) FROM results",
        "SELECT SUM(points) FROM driverstandings WHERE driverid = 1",
        "SELECT a FROM t JOIN u ON t.id = u.id LIMIT 5",
        "WITH x AS (SELECT raceid FROM races)\n"
        "SELECT raceid, ROW_NUMBER() OVER (ORDER BY raceid) FROM x",
        "select year, count(*) from races group by year",
    ]
    exploration = [
        "SELECT * FROM results LIMIT 5",
        "SELECT DISTINCT status FROM results LIMIT 20",
        "SHOW TABLES",
        "EXPLAIN SELECT 1",
        "DESCRIBE results",
        "SELECT table_name FROM information_schema.tables",
        "",
    ]
    for sql in analytical:
        assert pc.is_analytical_sql(sql), sql
    for sql in exploration:
        assert not pc.is_analytical_sql(sql), sql


def test_extract_sql_schemas():
    cases = [
        # quoted two-part refs, deduped across FROM and JOIN
        ('SELECT COUNT(*) FROM "formula_1"."results" res '
         'JOIN "formula_1"."races" r ON res.raceid = r.raceid',
         ["formula_1"]),
        # catalog-qualified three-part ref → the middle part
        ('SELECT COUNT(*) FROM "awsdatacatalog"."formula_1"."results"',
         ["formula_1"]),
        # unquoted refs, two schemas in first-use order
        ("select count(*) from football.appearances "
         "join formula_1.results on 1=1",
         ["football", "formula_1"]),
        # alias.column is NOT a schema (only FROM/JOIN position counts)
        ('SELECT ds."points" FROM "formula_1"."driverstandings" ds', ["formula_1"]),
        # CTE names are unqualified and never match
        ('WITH poles AS (SELECT raceid FROM "formula_1"."qualifying") '
         "SELECT COUNT(*) FROM poles",
         ["formula_1"]),
        # string literals and comments cannot fake a table reference
        ("SELECT COUNT(*) FROM \"formula_1\".\"results\" "
         "WHERE name = 'from fake.table'",
         ["formula_1"]),
        ("-- from ghost.tbl\nSELECT COUNT(*) FROM formula_1.results",
         ["formula_1"]),
        ("SELECT 1", []),
    ]
    for sql, expected in cases:
        assert pc.extract_sql_schemas(sql) == expected, sql


def test_analytical_sqls_matches_results_to_their_calls():
    turn = [
        _user("q"),
        _ai(tool_calls=[
            ("run_sql", {"sql": _ANALYTICAL_SQL}, "c1"),
            ("run_sql", {"sql": "SELECT * FROM races LIMIT 5"}, "c2"),
            ("run_sql", {"sql": "SELECT COUNT(*) FROM races"}, "c3"),
        ]),
        _tool("run_sql", _SQL_RESULT, "c1"),
        _tool("run_sql", _SQL_RESULT, "c2"),  # exploration: never counts
        _tool("run_sql", "Error: boom", "c3", status="error"),  # errored: ditto
    ]
    assert pc.analytical_sqls(turn) == [_ANALYTICAL_SQL]


# --- the judge fleet -------------------------------------------------------------


_POLICIES = [
    {"id": "P001", "type": "computational", "condition": "figures from empty results",
     "action": "never state figures", "source": "references/usage_guardrails.md"},
    {"id": "P002", "type": "behavioural", "condition": "ambiguous points term",
     "action": "ask first", "source": "references/usage_guardrails.md"},
]


def test_judge_shard_reads_the_enforced_tool_call():
    # IDS ONLY: the judge tool contract carries no evidence/explanations —
    # the reminder is built from the authored policy text, so judge prose
    # never reaches the main agent's context (and output tokens stay tiny).
    model = FakeToolModel([
        ("report_violations", {"violations": ["P001", "  ", ""]}),
    ])
    out = pc.judge_shard(
        model, shard_text="…", evidence="…", prompt=pc._QUERY_JUDGE_PROMPT
    )
    assert out == ["P001"]  # blanks dropped


def test_judge_shard_retries_once_then_fails_open():
    # First reply has no tool call; the retry demands it and succeeds.
    model = FakeToolModel([
        None,
        ("report_violations", {"violations": []}),
    ])
    assert pc.judge_shard(
        model, shard_text="…", evidence="…", prompt=pc._QUERY_JUDGE_PROMPT
    ) == []
    assert len(model.prompts) == 2
    assert "report_violations" in model.prompts[1]  # the demand rode along
    # Two tool-less replies: the shard goes unjudged, never fabricated.
    stubborn = FakeToolModel([None, None])
    assert pc.judge_shard(
        stubborn, shard_text="…", evidence="…", prompt=pc._QUERY_JUDGE_PROMPT
    ) is None


def test_judge_policies_drops_ids_outside_the_judged_set():
    model = FakeToolModel([
        ("report_violations", {"violations": ["P001", "P999", "P001"]}),
    ])
    flagged, failed, total = pc.judge_policies(
        model, _POLICIES, "e", shard_size=10, prompt=pc._QUERY_JUDGE_PROMPT
    )
    assert flagged == ["P001"]  # ghost id dropped, duplicate collapsed
    assert (failed, total) == (0, 1)


def test_judge_policies_counts_failed_shards():
    model = FakeToolModel([None, None])  # one shard, both attempts tool-less
    flagged, failed, total = pc.judge_policies(
        model, _POLICIES, "e", shard_size=10, prompt=pc._QUERY_JUDGE_PROMPT
    )
    assert flagged == [] and failed == 1 and total == 1


def test_render_judged_findings_joins_the_authored_policy_text():
    findings = pc.render_judged_findings(
        ["P002"], {p["id"]: p for p in _POLICIES}
    )
    assert findings == [{
        "policy_id": "P002",
        "condition": "ambiguous points term", "action": "ask first",
        "source": "references/usage_guardrails.md",
    }]


# --- the reminder + its UI split ---------------------------------------------------


def test_split_policy_reminder_roundtrips_the_composed_block():
    findings = [
        {"policy_id": "P001",
         "condition": "standings points are aggregated",
         "action": "use the final checkpoint",
         "source": "references/usage_guardrails.md"},
    ]
    reminder = pc.compose_policy_reminder(
        findings, "bird/formula_1",
        subject=pc._QUERY_SUBJECT, closing=pc._QUERY_CLOSING,
    )
    payload = '{"rows": [1]}'
    clean, display = pc.split_policy_reminder(payload + "\n\n" + reminder)
    assert clean == payload
    # The finding line is AUTHORED policy text only — the ids-only judge
    # contract means no judge prose can appear here.
    assert display == (
        "- [P001] When standings points are aggregated — the agent must "
        "use the final checkpoint (source: references/usage_guardrails.md)"
    )
    # A cross-dataset result carries TWO blocks — both stripped, findings merged.
    two = payload + "\n\n" + reminder + "\n\n" + pc.compose_policy_reminder(
        [{"policy_id": "P101", "condition": "c", "action": "a",
          "source": "s.md"}],
        "bird/football",
        subject=pc._QUERY_SUBJECT, closing=pc._QUERY_CLOSING,
    )
    clean2, display2 = pc.split_policy_reminder(two)
    assert clean2 == payload
    assert "- [P001]" in display2 and "- [P101]" in display2
    # No reminder = untouched passthrough.
    assert pc.split_policy_reminder(payload) == (payload, "")


def test_policy_display_extracts_finding_lines_only():
    findings = [
        {"policy_id": "P002", "why": "computed on ambiguity.", "condition": "c",
         "action": "ask first", "source": "s.md"},
    ]
    note = pc.compose_policy_reminder(
        findings, LABEL, subject=pc._STEPS_SUBJECT, closing=pc._STEPS_CLOSING
    )
    assert "the steps you have taken so far this turn" in note
    display = pc.policy_display(note)
    assert display.startswith("- [P002]")
    assert "own judgment" not in display and "Do not mention" not in display
    assert pc.policy_display("no findings here") == "A documented policy was flagged."


# --- the computational track --------------------------------------------------------


def test_query_check_flags_and_judges_only_computational_policies(env):
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_QUERY_VIOLATION_SCRIPT))
    checker = _checker(env, judge)
    note = checker.submit(_ANALYTICAL_SQL).result(timeout=10)
    assert note.startswith("<system-reminder>")
    assert note.endswith("</system-reminder>")
    assert "[P001]" in note
    assert "never state figures without the documented dedup" in note
    assert "results above are real" in note
    # The flags ship unverified and say so — the agent is the verifier.
    assert "false positives" in note and "your own judgment" in note
    # Exactly ONE model round (the shard): no second verification pass.
    assert len(judge.prompts) == 1
    # The mid-turn framing, the question, and the SQL all reached the judge —
    # and ONLY the computational subset did (the type split).
    assert "IN PROGRESS" in judge.prompts[0]
    assert "career points?" in judge.prompts[0]
    assert _ANALYTICAL_SQL in judge.prompts[0]
    assert "P001" in judge.prompts[0] and "P002" not in judge.prompts[0]


def test_query_check_clean_verdict_is_cached_by_normalized_sql(env):
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    checker = _checker(env, judge)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    # Same query modulo whitespace: served from the cache, no second fleet.
    reworded = "  SELECT   SUM(points)\n FROM driverstandings "
    assert checker.submit(reworded).result(timeout=10) == ""
    assert len(judge.prompts) == 1


def test_query_check_skips_exploration_without_any_spend(env):
    _seed_usable_policy(env)
    judge = FakeToolModel([])
    checker = _checker(env, judge)
    assert checker.submit("SELECT * FROM results LIMIT 5").result(timeout=10) == ""
    assert judge.prompts == []  # the gate never judged


def test_query_check_honors_the_per_turn_budget(env):
    _seed_usable_policy(env)
    cfg = _cfg()
    cfg.policy_query_max_per_turn = 1
    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    checker = _checker(env, judge, cfg=cfg)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    over = checker.submit(
        "SELECT raceid, COUNT(*) FROM results GROUP BY raceid"
    ).result(timeout=10)
    assert over == ""
    assert len(judge.prompts) == 1  # the second analytical query was over budget


def test_query_check_stale_fingerprint_publishes_rebuild_and_skips(env):
    _seed_usable_policy(env)
    # The live wiki moves on after the seed → the stored fingerprint is stale.
    env["s3"].put_object(
        Bucket=BUCKET,
        Key=f"okf/{DOMAIN}/{DATASET}/references/usage_guardrails.md",
        Body=b"the rules, revised",
    )
    events = FakeEvents()
    judge = FakeToolModel([])
    checker = _checker(env, judge, events=events)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert judge.prompts == []  # stale is never judged
    assert len(events.entries) == 1
    assert json.loads(events.entries[0]["Detail"])["data_domain"] == DOMAIN


def test_pre_v3_document_without_types_self_heals(env):
    # The migration path: an old document (no `type`) fails the parse → the
    # check publishes the rebuild and stays silent, never judges half-blind.
    _seed_usable_policy(env, doc="""\
policies:
  - id: P001
    condition: figures are stated from a query
    action: never state figures
    source: references/usage_guardrails.md
""")
    events = FakeEvents()
    judge = FakeToolModel([])
    checker = _checker(env, judge, events=events)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert judge.prompts == []
    assert len(events.entries) == 1


def test_query_check_not_enrolled_is_silent(env):
    judge = FakeToolModel([])
    checker = _checker(env, judge)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert judge.prompts == []


# --- unscoped runs: dataset resolution from the SQL itself -------------------------


def test_unscoped_checker_resolves_dataset_from_qualified_sql(env):
    _seed_usable_policy(env)  # glue db falls back to the dataset id
    judge = FakeToolModel(list(_QUERY_VIOLATION_SCRIPT))
    checker = _checker(env, judge, domain="", dataset="")
    note = checker.submit(
        'SELECT SUM(points) FROM "formula_1"."driverstandings"'
    ).result(timeout=10)
    assert "[P001]" in note and "bird/formula_1" in note


def test_unscoped_checker_uses_the_registry_glue_database(env):
    # The mapping's Glue DB name differs from the dataset id — the row's
    # glue_database attribute must win (same contract as scope enrichment).
    _seed_usable_policy(env, glue_database="f1_glue")
    judge = FakeToolModel(list(_QUERY_VIOLATION_SCRIPT))
    checker = _checker(env, judge, domain="", dataset="")
    note = checker.submit(
        'SELECT SUM(points) FROM "f1_glue"."driverstandings"'
    ).result(timeout=10)
    assert "[P001]" in note and "bird/formula_1" in note


def test_unscoped_checker_skips_unenrolled_schemas_for_free(env):
    _seed_usable_policy(env)
    judge = FakeToolModel([])
    checker = _checker(env, judge, domain="", dataset="")
    note = checker.submit(
        'SELECT COUNT(*) FROM "other_db"."events" GROUP BY kind'
    ).result(timeout=10)
    assert note == ""
    assert judge.prompts == []  # nothing enrolled to judge against


def test_unscoped_cross_dataset_join_judges_each_dataset(env):
    _seed_usable_policy(env)
    _seed_usable_policy(env, dataset="football", doc="""\
policies:
  - id: P101
    type: computational
    condition: any aggregate over appearances
    action: never sum appearances across seasons
    source: references/usage_guardrails.md
""")
    judge = FakeToolModel([
        ("report_violations", {"violations": ["P001"]}),
        ("report_violations", {"violations": ["P101"]}),
    ])
    checker = _checker(env, judge, domain="", dataset="")
    note = checker.submit(
        'SELECT COUNT(*) FROM "formula_1"."results" r '
        'JOIN "football"."appearances" a ON r.driverid = a.player_id'
    ).result(timeout=10)
    assert "[P001]" in note and "bird/formula_1" in note
    assert "[P101]" in note and "bird/football" in note
    assert len(judge.prompts) == 2  # one fleet per matched dataset


# --- the rolling curated question ---------------------------------------------------


def test_turn_one_uses_the_raw_question_with_zero_model_calls(env):
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    # user_sub/thread_id present, but the THREAD row has no prior state — a
    # fresh thread OR the turn where policy was first enabled MID-conversation
    # (identical by construction: no armed run ever wrote state). The RAISING
    # rewrite model proves no rewrite call happens.
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1",
                       question="career points?")
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert "career points?" in judge.prompts[0]
    # The raw question is SEEDED as the curated question — without this write
    # the next turn would find nothing to chain from and the rolling rewrite
    # could never start.
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["curated_question"] == "career points?"


def test_follow_up_turn_rewrites_and_persists_the_curated_question(env):
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
        curated_question="average points per team for 2019",
        last_answer="Team A led 2019 with 400 points.",
    )
    rewrite = FakeModel([json.dumps({"question": "average points per team for 2018"})])
    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1",
                       question="and for 2018?", rewrite_model=rewrite)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    # The rewrite saw all three pieces...
    assert "average points per team for 2019" in rewrite.prompts[0]
    assert "Team A led 2019" in rewrite.prompts[0]
    assert "and for 2018?" in rewrite.prompts[0]
    # ...the judge got the CURATED question, not the fragment...
    assert "average points per team for 2018" in judge.prompts[0]
    # ...and the curated question survived to the THREAD row (durability).
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["curated_question"] == "average points per team for 2018"


def test_the_chain_establishes_across_two_armed_turns(env):
    # End-to-end over the THREAD row, no hand-seeded state: turn 1 seeds the
    # raw question; turn 2 (a fresh checker, as the server builds per run)
    # finds it plus the recorded answer and RUNS the rewrite.
    _seed_usable_policy(env)
    t1 = _checker(env, FakeToolModel(list(_CLEAN_SCRIPT)),
                  user_sub="alice", thread_id="conv1",
                  question="average points per team for 2019?")
    assert t1.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    t1.record_final_answer("Team A led 2019 with 400 points.")
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["last_answer"])

    rewrite = FakeModel([json.dumps({"question": "average points per team for 2018"})])
    judge2 = FakeToolModel(list(_CLEAN_SCRIPT))
    t2 = _checker(env, judge2, user_sub="alice", thread_id="conv1",
                  question="and for 2018?", rewrite_model=rewrite)
    assert t2.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert "average points per team for 2019?" in rewrite.prompts[0]
    assert "Team A led 2019" in rewrite.prompts[0]
    assert "average points per team for 2018" in judge2.prompts[0]


def test_answer_only_state_still_triggers_the_rewrite(env):
    # The previous armed turn ran no SQL, so only its ANSWER was recorded (the
    # curated-question seed happens on the first submitted query). The answer
    # alone still carries the context — the rewrite must run, not fall back.
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", last_answer="Team A led 2019 with 400 points.",
    )
    rewrite = FakeModel([json.dumps({"question": "points for Team A in 2018"})])
    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1",
                       question="and for 2018?", rewrite_model=rewrite)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert "Team A led 2019" in rewrite.prompts[0]
    assert "points for Team A in 2018" in judge.prompts[0]


def test_rewrite_failure_falls_back_to_the_raw_question(env):
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="prior question",
    )
    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    broken = FakeModel(["not json at all"])
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1",
                       question="and for 2018?", rewrite_model=broken)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert "and for 2018?" in judge.prompts[0]  # raw question, fail-open


def test_fold_clarifications_reruns_the_rewrite_with_the_qa(env):
    # The v3 REVERSAL: an answered ask_human IS folded into the curated
    # question (the ask-first evidence lives in the behavioural steps track).
    _seed_usable_policy(env)
    rewrite = FakeModel([json.dumps({"question": "championship points for Hamilton"})])
    judge = FakeToolModel(list(_QUERY_VIOLATION_SCRIPT))
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1",
                       question="", rewrite_model=rewrite)
    checker.fold_clarifications(
        "points for Hamilton?",
        [{"prompt": "Which points?", "answer": "championship"}],
    )
    note = checker.submit(_ANALYTICAL_SQL).result(timeout=10)
    assert "[P001]" in note
    assert "Which points?" in rewrite.prompts[0]
    assert "championship" in rewrite.prompts[0]
    assert "points for Hamilton?" in rewrite.prompts[0]
    assert "championship points for Hamilton" in judge.prompts[0]
    # The folded question is durable too.
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["curated_question"] == "championship points for Hamilton"


def test_build_curate_input_sections_are_conditional():
    text = pc.build_curate_input(raw_question="points?")
    assert "LATEST MESSAGE" in text
    assert "PREVIOUS QUESTION" not in text and "CLARIFICATIONS" not in text
    full = pc.build_curate_input(
        prev_question="pq", prev_answer="pa", raw_question="rq",
        qa=[{"prompt": "p", "answer": "a"}],
    )
    for fragment in ("PREVIOUS QUESTION", "ANSWER THE USER GOT", "LATEST MESSAGE",
                     "CLARIFICATIONS"):
        assert fragment in full


def test_record_final_answer_persists_truncated(env):
    judge = FakeToolModel([])
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1")
    checker.record_final_answer("The answer. " * 400)  # > the 2k cap
    def _state():
        return read_policy_state(
            env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
            thread_id="conv1",
        )
    assert _wait_until(lambda: _state()["last_answer"])
    state = _state()
    assert state["last_answer"].startswith("The answer.")
    assert len(state["last_answer"]) == 2000


def test_read_policy_state_is_empty_for_a_missing_row(env):
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="nobody",
        thread_id="ghost",
    )
    assert state == {"curated_question": "", "last_answer": ""}


# --- the behavioural track ------------------------------------------------------


def _behaviour_turn(sql=_ANALYTICAL_SQL):
    return [
        _user("points for Hamilton?", scoped=True),
        _ai(tool_calls=[("run_sql", {"sql": sql}, "c1")],
            thinking="private-deliberation"),
        _tool("run_sql", _SQL_RESULT, "c1"),
    ]


_STEPS_VIOLATION_SCRIPT = [
    ("report_violations", {"violations": ["P002"]}),
]


def test_behavioural_check_judges_the_steps_with_behavioural_policies(env):
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_STEPS_VIOLATION_SCRIPT))
    checker = _checker(env, judge, tracks=("behavioural",))
    note = checker.submit_behavioural(_behaviour_turn()).result(timeout=10)
    assert "[P002]" in note and "ask for clarification first" in note
    assert "the steps you have taken so far this turn" in note
    # ONLY the behavioural subset reached the judge; committed-conduct framing.
    assert "P002" in judge.prompts[0] and "P001" not in judge.prompts[0]
    assert "COMMITTED" in judge.prompts[0]
    assert "SUM(points)" in judge.prompts[0]  # the steps carry the SQL verbatim
    assert "private-deliberation" not in judge.prompts[0]  # thinking is never evidence


def test_behavioural_check_never_nags_the_same_policy_twice(env):
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_STEPS_VIOLATION_SCRIPT) * 2)
    checker = _checker(env, judge, tracks=("behavioural",))
    first = checker.submit_behavioural(_behaviour_turn()).result(timeout=10)
    assert "[P002]" in first
    # A later, larger window re-flags the same policy → suppressed.
    window = _behaviour_turn() + [
        _ai(tool_calls=[("run_sql", {"sql": "SELECT COUNT(*) FROM races"}, "c2")]),
        _tool("run_sql", _SQL_RESULT, "c2"),
    ]
    assert checker.submit_behavioural(window).result(timeout=10) == ""


def test_steps_evidence_includes_the_clarification_exchange():
    qa = json.dumps(
        {"status": "answered",
         "answers": [{"prompt": "Which points?", "answer": "championship"}]}
    )
    turn = [
        _user("points?"),
        _ai(tool_calls=[("ask_human", {"q": "?"}, "c1")]),
        _tool("ask_human", qa, "c1"),
    ]
    text = pc.build_steps_evidence(turn, question="points?")
    assert "Which points?" in text and "championship" in text
    assert "STEPS THE AGENT HAS TAKEN" in text


def test_steps_evidence_excludes_injected_notes():
    from chat.steering import STEERING_MARKER

    turn = _behaviour_turn() + [
        HumanMessage(content="<system-reminder>steer</system-reminder>",
                     additional_kwargs={STEERING_MARKER: "silence"}),
        HumanMessage(content="<system-reminder>policy</system-reminder>",
                     additional_kwargs={pc.POLICY_MARKER: "behavioural"}),
    ]
    text = pc.build_steps_evidence(turn, question="q")
    assert "steer" not in text and "policy</system-reminder>" not in text


class _State(dict):
    """A dict-based stand-in for the middleware's state (has .get)."""


def test_middleware_kicks_once_batches_and_delivers_at_the_same_hook(env):
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_STEPS_VIOLATION_SCRIPT))
    checker = _checker(env, judge, tracks=("behavioural",))
    mw = pc.BehaviouralPolicyMiddleware(checker)

    # No analytical results yet → nothing kicked, nothing injected — but the
    # first hook pre-warms the checker (cold-start off the eval's wait path).
    opener = [_user("points for Hamilton?", scoped=True)]
    assert mw.before_model(_State(messages=opener)) is None
    assert mw._future is None
    assert _wait_until(lambda: checker._warmed)

    # TWO parallel analytical results arrive before this hook runs → ONE
    # batched eval, and the hook WAITS for it: the verdict must inject before
    # THIS model call (fire-and-forget would lose the race to a final answer
    # and never deliver — the live-observed failure mode).
    window = _behaviour_turn() + [
        _ai(tool_calls=[("run_sql", {"sql": "SELECT AVG(points) FROM results"}, "c2")]),
        _tool("run_sql", _SQL_RESULT, "c2"),
    ]
    out = mw.before_model(_State(messages=window))
    (msg,) = out["messages"]
    assert msg.additional_kwargs[pc.POLICY_MARKER] == "behavioural"
    assert "[P002]" in msg.content
    assert len(judge.prompts) == 1  # one batched eval covered both queries
    assert mw._future is None  # consumed at delivery
    # Nothing new since → the next hook neither re-kicks nor waits.
    assert mw.before_model(_State(messages=window)) is None


def test_middleware_async_hook_delivers_too(env):
    import asyncio

    _seed_usable_policy(env)
    judge = FakeToolModel(list(_STEPS_VIOLATION_SCRIPT))
    checker = _checker(env, judge, tracks=("behavioural",))
    mw = pc.BehaviouralPolicyMiddleware(checker)

    async def _drive():
        opener = [_user("points for Hamilton?", scoped=True)]
        assert await mw.abefore_model(_State(messages=opener)) is None  # baseline
        return await mw.abefore_model(_State(messages=_behaviour_turn()))

    out = asyncio.run(_drive())
    (msg,) = out["messages"]
    assert "[P002]" in msg.content


def test_middleware_never_triggers_on_a_resume_without_new_queries(env):
    # An ask_human RESUME rebuilds the middleware; its first hook sees the
    # PRE-ASK analytical results in the window. Those were already evaluated
    # by the run that issued them — the first hook BASELINES and must not
    # kick (the trigger contract is NEW complex queries, never the resume
    # itself). A genuinely new query after the resume still triggers.
    _seed_usable_policy(env)
    judge = FakeToolModel(list(_STEPS_VIOLATION_SCRIPT))
    checker = _checker(env, judge, tracks=("behavioural",))
    mw = pc.BehaviouralPolicyMiddleware(checker)

    resumed = _behaviour_turn() + [
        _ai(tool_calls=[("ask_human", {"q": "?"}, "c9")]),
        _tool("ask_human", json.dumps({"status": "answered", "answers": []}), "c9"),
    ]
    assert mw.before_model(_State(messages=resumed)) is None  # baseline, no kick
    assert mw._future is None
    assert mw.before_model(_State(messages=resumed)) is None  # still nothing new
    assert judge.prompts == []

    fresh_query = resumed + [
        _ai(tool_calls=[("run_sql", {"sql": "SELECT AVG(points) FROM results"}, "c2")]),
        _tool("run_sql", _SQL_RESULT, "c2"),
    ]
    out = mw.before_model(_State(messages=fresh_query))
    (msg,) = out["messages"]
    assert "[P002]" in msg.content
    assert len(judge.prompts) == 1


def test_middleware_slow_verdict_defers_to_the_next_hook(env):
    # The eval outlasts the wait budget: the hook gives up WITHOUT consuming
    # the future (a later hook gets another window; a turn that ends first
    # drops it — advisory), and the model call proceeds unblocked.
    import threading

    release = threading.Event()

    class _SlowChecker:
        wait_budget_s = 0.05

        def prewarm(self):
            pass

        def submit_behavioural(self, messages):
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=1)
            return self._pool.submit(lambda: release.wait(5) and "")

    mw = pc.BehaviouralPolicyMiddleware(_SlowChecker())
    opener = [_user("q", scoped=True)]
    assert mw.before_model(_State(messages=opener)) is None  # baseline
    assert mw.before_model(_State(messages=_behaviour_turn())) is None
    assert mw._future is not None  # kept for a later hook
    release.set()


def test_middleware_survives_a_failing_checker(env):
    class _Boom:
        def prewarm(self):
            pass

        def submit_behavioural(self, messages):
            raise RuntimeError("boom")

    mw = pc.BehaviouralPolicyMiddleware(_Boom())
    opener = [_user("q", scoped=True)]
    assert mw.before_model(_State(messages=opener)) is None  # baseline
    assert mw.before_model(_State(messages=_behaviour_turn())) is None  # swallowed


def test_policy_note_never_resets_the_steering_turn_slice():
    # The injected note is a HumanMessage — steering's turn_slice must not
    # treat it as a genuine user message (marker parity pinned here).
    from chat import steering

    assert pc.POLICY_MARKER in steering._INJECTED_MARKER_KEYS
    note = HumanMessage(content="<system-reminder>n</system-reminder>",
                        additional_kwargs={pc.POLICY_MARKER: "behavioural"})
    window = steering.turn_slice([_user("q"), _ai(text="a"), note])
    assert isinstance(window[0], HumanMessage) and window[0].content == "q"


# --- checker construction + arming -------------------------------------------------


def test_make_policy_checker_gates_on_deploy_flag_and_tracks():
    cfg = _cfg()
    on = pc.make_policy_checker(
        cfg, tracks={"computational"}, scope={"data_domain": DOMAIN, "dataset": DATASET},
        question="q", user_sub="alice", thread_id="conv1",
    )
    assert isinstance(on, pc.PolicyChecker)
    assert on.wants("computational") and not on.wants("behavioural")
    # No tracks → no checker, whatever the flag says.
    assert pc.make_policy_checker(
        cfg, tracks=set(), scope=None, question="q",
        user_sub="alice", thread_id="conv1",
    ) is None
    # Deploy gate off → no checker, whatever the client sent.
    cfg.policy_check_enabled = False
    assert pc.make_policy_checker(
        cfg, tracks={"computational", "behavioural"}, scope=None, question="q",
        user_sub="alice", thread_id="conv1",
    ) is None


def test_should_wait_only_for_analytical_sql(env):
    checker = _checker(env, FakeToolModel([]))
    assert checker.should_wait(_ANALYTICAL_SQL)
    assert not checker.should_wait("SELECT * FROM races LIMIT 5")


def test_concurrent_checks_run_on_a_pool_and_respect_the_budget(env):
    # The pool allows up to 8 concurrent fleet rounds (parallel tool-called
    # queries + a behavioural eval must not serialize while their callers'
    # wait budgets burn) — and the shared budget/dedup state stays correct
    # under that concurrency: 3 distinct analytical queries at a cap of 2
    # yield exactly 2 judged fleets, whatever the interleaving.
    from chat.policy_check import _POOL_WORKERS

    _seed_usable_policy(env)
    cfg = _cfg()
    cfg.policy_query_max_per_turn = 2
    judge = FakeToolModel(list(_CLEAN_SCRIPT) * 3)
    checker = _checker(env, judge, cfg=cfg)
    assert checker._pool._max_workers == _POOL_WORKERS == 8
    sqls = [
        "SELECT SUM(points) FROM driverstandings",
        "SELECT COUNT(*) FROM results GROUP BY raceid",
        "SELECT AVG(points) FROM results",
    ]
    futures = [checker.submit(s) for s in sqls]
    assert [f.result(timeout=15) for f in futures] == ["", "", ""]
    assert len(judge.prompts) == 2  # the third was over budget, atomically
