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
import logging
import threading
from types import SimpleNamespace

import boto3
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from moto import mock_aws

from chat import policy_check as pc
from okf_core import policy_rules as pc_rules
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

    def bind_tools(self, tools, tool_choice=None):
        self.tools = tools
        self.tool_choice = tool_choice
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


@pytest.fixture(autouse=True)
def _fresh_source_hash_cache():
    # The freshness fingerprint has a module-level TTL cache (amortizes the
    # S3 corpus walk across turns in one container); tests re-stub the same
    # (bucket, domain, dataset) with different content, so isolate them.
    pc._source_hash_cache.clear()
    yield
    pc._source_hash_cache.clear()


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
    # A Converse (non-OpenAI) judge is a classifier: the verdict tool is
    # FORCED, so a no-tool-call reply is structurally unreachable.
    assert judge.tool_choice == "report_violations"


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


def test_query_check_never_authored_is_silent(env):
    # No policy state on the row at all (a dataset predating the feature):
    # the check stays silent AND publishes nothing — it is a consumer of
    # policy state, never a backfill trigger.
    events = FakeEvents()
    judge = FakeToolModel([])
    checker = _checker(env, judge, events=events)
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert judge.prompts == []
    assert events.entries == []


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


def test_unscoped_checker_skips_policy_less_schemas_for_free(env):
    _seed_usable_policy(env)
    judge = FakeToolModel([])
    checker = _checker(env, judge, domain="", dataset="")
    note = checker.submit(
        'SELECT COUNT(*) FROM "other_db"."events" GROUP BY kind'
    ).result(timeout=10)
    assert note == ""
    assert judge.prompts == []  # no policy state to judge against


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
    # Production sequencing: the server prewarms at run start, so the rewrite
    # normally lands well before the first analytical query — model that here
    # (evaluators never WAIT for it; _curated_now takes it iff it landed).
    checker.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "average points per team for 2018")
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
    # Both halves of the chain persist asynchronously (curation is a pool
    # piece now) — wait for both before the next turn reads them.
    assert _wait_until(lambda: (lambda s: s["last_answer"] and s["curated_question"])(
        read_policy_state(
            env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
            thread_id="conv1",
        )
    ))

    rewrite = FakeModel([json.dumps({"question": "average points per team for 2018"})])
    judge2 = FakeToolModel(list(_CLEAN_SCRIPT))
    t2 = _checker(env, judge2, user_sub="alice", thread_id="conv1",
                  question="and for 2018?", rewrite_model=rewrite)
    t2.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "average points per team for 2018")
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
    checker.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "points for Team A in 2018")
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
    # The server prewarms AFTER the fold (the resume path), so the folded
    # rewrite has landed by the time the resumed run's first query checks.
    checker.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "championship points for Hamilton")
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
    assert "EARLIER QUESTIONS" not in text
    full = pc.build_curate_input(
        prev_question="pq", prev_answer="pa", raw_question="rq",
        qa=[{"prompt": "p", "answer": "a"}],
        earlier_questions=["eq1", "eq2"],
    )
    for fragment in ("EARLIER QUESTIONS", "PREVIOUS QUESTION",
                     "ANSWER THE USER GOT", "LATEST MESSAGE", "CLARIFICATIONS"):
        assert fragment in full
    # Earlier questions render before the previous one (chronological).
    assert full.index("eq1") < full.index("eq2") < full.index("pq")


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
    assert state == {
        "curated_question": "",
        "last_answer": "",
        "question_history": [],
        "history_last_raw": "",
    }


def test_read_policy_state_returns_none_on_a_failed_read():
    class _Boom:
        def get_item(self, **kw):
            raise RuntimeError("ddb down")

    # Unreadable is NOT absent: the caller must be able to tell a missing row
    # (turn-1 semantics, seed-write allowed) from a read failure (never write).
    assert read_policy_state(
        _Boom(), threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    ) is None


def test_unreadable_state_never_seeds_over_the_chain(env):
    # A transient GetItem failure mid-conversation must not take the turn-1
    # branch and clobber a good rolling chain with the raw fragment.
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="good question",
        last_answer="good answer", question_history=["q1", "good question"],
    )

    class _FailingReads:
        def __init__(self, inner):
            self._inner = inner

        def get_item(self, **kw):
            if kw.get("TableName") == THREADS_TABLE:
                raise RuntimeError("throttled")
            return self._inner.get_item(**kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    checker = pc.PolicyChecker(
        chat_config=_cfg(),
        tracks=("computational",),
        data_domain=DOMAIN,
        dataset=DATASET,
        question="and for 2019?",
        user_sub="alice",
        thread_id="conv1",
        clients={"ddb": _FailingReads(env["ddb"]), "s3": env["s3"],
                 "events": FakeEvents()},
        judge_model=judge,
        # The rewrite must never run on unreadable state.
        rewrite_model=_RaisingModel(),
    )
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert "and for 2019?" in judge.prompts[0]  # raw fallback for THIS turn
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["curated_question"] == "good question"
    assert state["question_history"] == ["q1", "good question"]


def test_fold_replaces_the_same_turns_history_entry(env):
    # An ask_human fold re-curates the SAME turn: the folded question must
    # REPLACE the turn's pre-fold history entry (matched via the stored raw
    # question — durable across the resume's checker rebuild), never stack a
    # second entry for one turn.
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="q1", last_answer="a1",
        question_history=["q1"], history_last_raw="raw-q1",
    )
    # The pre-fold turn: rewrite lands "points? (pre-fold)".
    pre = _checker(
        env, FakeToolModel(list(_CLEAN_SCRIPT)),
        user_sub="alice", thread_id="conv1", question="points?",
        rewrite_model=FakeModel([json.dumps({"question": "points? (pre-fold)"})]),
    )
    pre.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "points? (pre-fold)")
    # The resume: a FRESH checker (production rebuilds it), fold, re-curate.
    post = _checker(
        env, FakeToolModel(list(_CLEAN_SCRIPT)),
        user_sub="alice", thread_id="conv1", question="",
        rewrite_model=FakeModel(
            [json.dumps({"question": "championship points? (folded)"})]
        ),
    )
    post.fold_clarifications(
        "points?", [{"prompt": "Which points?", "answer": "championship"}]
    )
    post.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "championship points? (folded)")
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    # ONE entry for the turn — q1 survives; the unclarified variant is gone.
    assert state["question_history"] == ["q1", "championship points? (folded)"]


def test_identical_reask_appends_after_the_answer_clears_the_fold_signal(env):
    # The fold-replace signal must not outlive its turn: the stream-end
    # answer write clears history_last_raw, so a user RE-ASKING the identical
    # raw question next turn (a common retry after a bad answer) appends its
    # own history entry instead of silently overwriting the previous turn's.
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="qN", last_answer="aN",
        question_history=["q1", "qN"], history_last_raw="points?",
    )

    def _state():
        return read_policy_state(
            env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
            thread_id="conv1",
        )

    # Turn N ends: the final-answer write clears the same-turn signal.
    ender = _checker(env, FakeToolModel([]), user_sub="alice", thread_id="conv1")
    ender.record_final_answer("the final answer")
    assert _wait_until(lambda: _state()["history_last_raw"] == "")

    # Turn N+1 re-asks the IDENTICAL raw question; the landed rewrite APPENDS.
    t = _checker(
        env, FakeToolModel(list(_CLEAN_SCRIPT)),
        user_sub="alice", thread_id="conv1", question="points?",
        rewrite_model=FakeModel([json.dumps({"question": "qN+1"})]),
    )
    t.prewarm()
    assert _wait_until(lambda: _state()["curated_question"] == "qN+1")
    # qN survives — no history slot was lost to the fold heuristic.
    assert _state()["question_history"] == ["q1", "qN", "qN+1"]


def test_write_policy_state_trims_and_roundtrips_the_history(env):
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", question_history=["dropped", "q2", "q3", "q4"],
    )
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["question_history"] == ["q2", "q3", "q4"]


def test_question_history_rolls_forward_and_caps_at_three(env):
    # Turn 1 seeds the history with the raw question; each later turn's
    # landed curated question appends; only the last 3 survive.
    _seed_usable_policy(env)
    t1 = _checker(env, FakeToolModel(list(_CLEAN_SCRIPT)),
                  user_sub="alice", thread_id="conv1", question="q1")
    assert t1.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["question_history"] == ["q1"])
    for n in (2, 3, 4):
        t = _checker(env, FakeToolModel(list(_CLEAN_SCRIPT)),
                     user_sub="alice", thread_id="conv1",
                     question=f"fragment {n}",
                     rewrite_model=FakeModel([json.dumps({"question": f"q{n}"})]))
        t.prewarm()
        # The history rides the same write as the curated question.
        assert _wait_until(lambda: read_policy_state(
            env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
            thread_id="conv1",
        )["curated_question"] == f"q{n}")
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["question_history"] == ["q2", "q3", "q4"]


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
                     additional_kwargs={STEERING_MARKER: "repetition"}),
        HumanMessage(content="<system-reminder>policy</system-reminder>",
                     additional_kwargs={pc.POLICY_MARKER: "behavioural"}),
    ]
    text = pc.build_steps_evidence(turn, question="q")
    assert "steer" not in text and "policy</system-reminder>" not in text


def test_rewrite_receives_the_earlier_questions_but_judges_never_do(env):
    # A thread with 3 stored questions: the REWRITE sees the two before the
    # previous one as labeled resolution context (the previous question keeps
    # its own section); the judges get ONLY the curated anchor.
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="prior-q3", last_answer="a3",
        question_history=["prior-q1", "prior-q2", "prior-q3"],
    )
    rewrite = FakeModel([json.dumps({"question": "curated-q4"})])
    judge = FakeToolModel(list(_CLEAN_SCRIPT) * 2)
    checker = _checker(
        env, judge, tracks=("computational", "behavioural"),
        user_sub="alice", thread_id="conv1",
        question="back to the first thing?", rewrite_model=rewrite,
    )
    checker.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "curated-q4")
    prompt = rewrite.prompts[0]
    assert "EARLIER QUESTIONS IN THIS CONVERSATION" in prompt
    assert "prior-q1" in prompt and "prior-q2" in prompt
    # The previous question renders in its OWN section, after the earlier ones.
    assert prompt.index("prior-q2") < prompt.index("THE PREVIOUS QUESTION")
    assert "prior-q3" in prompt
    # ...and the history rolled forward with the landed rewrite.
    assert read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["question_history"] == ["prior-q2", "prior-q3", "curated-q4"]
    # Neither track's judge ever sees the earlier questions.
    assert checker.submit_behavioural(_behaviour_turn()).result(timeout=10) == ""
    assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
    for judged in judge.prompts:
        assert "curated-q4" in judged
        assert "EARLIER QUESTIONS" not in judged and "prior-q1" not in judged


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


def test_make_policy_checker_gates_on_deploy_flag_only():
    cfg = _cfg()
    on = pc.make_policy_checker(
        cfg, tracks={"computational"}, scope={"data_domain": DOMAIN, "dataset": DATASET},
        question="q", user_sub="alice", thread_id="conv1",
    )
    assert isinstance(on, pc.PolicyChecker)
    assert on.wants("computational") and not on.wants("behavioural")
    # No tracks → still a checker (curation-only: the rolling chain must not
    # go stale on unarmed turns), but it judges NOTHING — wants() is False
    # for both tracks, so neither the SQL tool nor the middleware wires in.
    unarmed = pc.make_policy_checker(
        cfg, tracks=set(), scope=None, question="q",
        user_sub="alice", thread_id="conv1",
    )
    assert isinstance(unarmed, pc.PolicyChecker)
    assert not unarmed.wants("computational") and not unarmed.wants("behavioural")
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


# --- regressions from the policy-commit review ------------------------------------


def test_is_analytical_sql_sees_through_comments_and_set_ops():
    # A leading comment must not disguise an analytical query as exploration:
    # the engine strips comments too, so the query RUNS fine either way — the
    # only casualty of a raw-text head check would be the (opted-in) checks.
    assert pc.is_analytical_sql("-- career points\nSELECT SUM(points) FROM t")
    assert pc.is_analytical_sql("/* probe */ SELECT a FROM t JOIN u ON 1=1")
    # Set differences compute answers exactly like UNION does.
    assert pc.is_analytical_sql("SELECT a FROM t EXCEPT SELECT a FROM u")
    assert pc.is_analytical_sql("SELECT a FROM t INTERSECT SELECT a FROM u")
    # Comment-prefixed exploration is still exploration.
    assert not pc.is_analytical_sql("-- peek\nSELECT * FROM t LIMIT 5")


def test_extract_sql_schemas_walks_comma_joins():
    cases = [
        # old-style comma join, bare and aliased
        ("SELECT SUM(r.points) FROM formula_1.results r, football.teams t "
         "WHERE r.id = t.id",
         ["formula_1", "football"]),
        ('SELECT COUNT(*) FROM "formula_1"."results" AS r, "football"."teams" AS t',
         ["formula_1", "football"]),
        ("SELECT COUNT(*) FROM formula_1.results, formula_1.races",
         ["formula_1"]),
        # a SELECT-list comma is NOT a FROM continuation: alias.col stays out
        ("SELECT a.b, c.d FROM formula_1.results", ["formula_1"]),
    ]
    for sql, expected in cases:
        assert pc.extract_sql_schemas(sql) == expected, sql


def test_judge_policies_validates_ids_per_shard():
    # With 2+ shards, a judge hallucinating an id from ANOTHER shard (whose
    # own judge said clean) must be dropped: an id is only accepted from the
    # judge that was actually shown that policy.
    class _ShardAwareJudge:
        def __init__(self):
            self.prompts: list[str] = []
            self._lock = threading.Lock()

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            prompt = messages[0][1]
            with self._lock:
                self.prompts.append(prompt)
            # The P001 shard's judge "flags" P002 — an id it was never shown.
            flags = ["P002"] if "id: P001" in prompt else []
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "report_violations",
                    "args": {"violations": flags},
                    "id": "t1", "type": "tool_call",
                }],
            )

    flagged, failed, total = pc.judge_policies(
        _ShardAwareJudge(), _POLICIES, "e", shard_size=1,
        prompt=pc._QUERY_JUDGE_PROMPT,
    )
    assert flagged == [] and failed == 0 and total == 2


def test_ask_human_qa_excludes_the_harness_note():
    # The ask_human payload's "note" is ALWAYS a fixed harness instruction
    # (chat.ask_human_middleware), never user speech — echoing it into the
    # evidence would attribute harness text to the user.
    payload = json.dumps({
        "status": "answered",
        "answers": [{"id": "q1", "prompt": "Which points?", "answer": "career"}],
        "note": "The user answered your clarifying questions (above). Use these "
                "answers to continue; do not ask them again.",
    })
    qa = pc.ask_human_qa([_tool("ask_human", payload)])
    assert qa == [{"prompt": "Which points?", "answer": "career"}]


def test_rewrite_failure_never_clobbers_the_stored_chain(env):
    # A transient rewrite failure falls back for THIS turn but must not
    # PERSIST the raw fragment over the previous good curated question —
    # that would poison every later turn's rewrite, not just this one.
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
    state = read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )
    assert state["curated_question"] == "prior question"


def test_close_releases_the_pool_and_later_work_fails_open(env):
    checker = _checker(env, FakeToolModel([]), user_sub="alice", thread_id="c1")
    checker.close()
    checker.close()  # idempotent
    # New submits are refused (run_sql wraps submit and fails open there)…
    with pytest.raises(RuntimeError):
        checker.submit(_ANALYTICAL_SQL)
    # …and the teardown-side helpers swallow it themselves.
    checker.record_final_answer("the answer")  # must not raise
    checker.prewarm()  # must not raise


def test_identical_parallel_queries_share_one_fleet_and_budget(env):
    # Two identical (mod whitespace) queries racing the _notes cache must
    # share ONE fleet round and ONE budget unit — the non-owner waits on the
    # owner's in-flight verdict instead of re-judging.
    _seed_usable_policy(env)
    gate = threading.Event()

    class _BlockingJudge(FakeToolModel):
        def invoke(self, messages):
            gate.wait(10)
            return super().invoke(messages)

    judge = _BlockingJudge(list(_CLEAN_SCRIPT) * 2)
    checker = _checker(env, judge)
    f1 = checker.submit(_ANALYTICAL_SQL)
    f2 = checker.submit("  SELECT   SUM(points)  FROM   driverstandings  ")
    gate.set()
    assert f1.result(timeout=15) == "" and f2.result(timeout=15) == ""
    assert len(judge.prompts) == 1  # one fleet, not two
    assert checker._checks == 1  # one budget unit


def test_evaluators_never_wait_for_the_rewrite(env):
    # The fleet fires the moment it is triggered: a mid-flight rewrite must
    # not delay the verdict — the judges get the RAW question instead
    # (_curated_now), and the rewrite still lands in the background to
    # advance the durable chain for the NEXT turn.
    _seed_usable_policy(env)
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="prior question",
        last_answer="prior answer",
    )
    release = threading.Event()

    class _BlockedRewrite:
        def invoke(self, *a, **k):
            release.wait(10)
            return AIMessage(content=json.dumps({"question": "rewritten q"}))

    judge = FakeToolModel(list(_CLEAN_SCRIPT))
    checker = _checker(env, judge, user_sub="alice", thread_id="conv1",
                       question="and for 2018?", rewrite_model=_BlockedRewrite())
    try:
        checker.prewarm()
        # Verdict arrives while the rewrite is still blocked...
        assert checker.submit(_ANALYTICAL_SQL).result(timeout=10) == ""
        # ...judged against the RAW question, not the stale stored one.
        assert "and for 2018?" in judge.prompts[0]
        assert "prior question" not in judge.prompts[0]
    finally:
        release.set()
    # The background rewrite still advances the durable chain.
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "rewritten q")


def test_unarmed_checker_still_maintains_the_curated_chain(env):
    # Policy not armed this turn: the checker exists purely as the chain's
    # keeper — prewarm runs the (turn-1) curation and persists it, so a later
    # ARMED turn chains from real state instead of starting over.
    checker = pc.PolicyChecker(
        chat_config=_cfg(), tracks=frozenset(),
        data_domain="", dataset="",
        question="average points per team for 2019?",
        user_sub="alice", thread_id="conv1",
        clients={"ddb": env["ddb"], "s3": env["s3"], "events": FakeEvents()},
        judge_model=FakeToolModel([]),
        rewrite_model=_RaisingModel(),  # turn 1 must cost zero model calls
    )
    checker.prewarm()
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["curated_question"] == "average points per team for 2019?")
    checker.record_final_answer("Team A led 2019 with 400 points.")
    assert _wait_until(lambda: read_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1",
    )["last_answer"])
    checker.close()


def test_slow_rewrite_never_blocks_unrelated_checker_state(env):
    # The curated-question rewrite is a network call; while it is mid-flight
    # the SHARED lock must be free — the old lock-across-model-call stalled
    # every other checker operation (including the server's teardown-time
    # client lookup, i.e. the event loop) behind it.
    import time

    _seed_usable_policy(env)
    started, release = threading.Event(), threading.Event()

    class _SlowRewrite:
        def invoke(self, *a, **k):
            started.set()
            release.wait(10)
            return AIMessage(content=json.dumps({"question": "q"}))

    # Prior state forces the rewrite path (turn-1 raw-question shortcut off).
    write_policy_state(
        env["ddb"], threads_table=THREADS_TABLE, user_sub="alice",
        thread_id="conv1", curated_question="prior question",
    )
    checker = _checker(env, FakeToolModel([]), user_sub="alice",
                       thread_id="conv1", question="and for 2018?",
                       rewrite_model=_SlowRewrite())
    try:
        checker.prewarm()
        assert started.wait(10)
        t0 = time.monotonic()
        checker._boto("ddb")  # shared-lock user: must not wait on the rewrite
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
    checker.close()


# --- the deterministic rules tier -------------------------------------------------


# sqlglot gates only the DETERMINISTIC-tier tests below — never the judge
# suite above it (a module-level importorskip would skip that too).
_needs_sqlglot = pytest.mark.skipif(
    not pc_rules.sqlglot_available(), reason="sqlglot is not installed"
)

_RULED_DOC = """\
policies:
  - id: P010
    type: computational
    condition: aggregating standings points across rounds
    action: never SUM cumulative snapshot columns; read one round's row
    source: references/usage_guardrails.md
    rules:
      - dimension: forbidden_aggregation
        targets: [driverstandings.points]
        examples:
          violation: SELECT SUM(points) FROM driverstandings
          pass: SELECT points FROM driverstandings WHERE raceid = 900
  - id: P011
    type: computational
    condition: figures are stated from a query with duplicates
    action: apply the documented dedup before stating figures
    source: references/usage_guardrails.md
"""

_RULES_SCHEMA = {
    DATASET: {"driverstandings": ["raceid", "driverid", "points"]}
}

_VIOLATING_SQL = "SELECT SUM(points) FROM driverstandings"


def _seed_ruled(env):
    _seed_usable_policy(env, doc=_RULED_DOC)
    ap.put_rules_schema(
        env["s3"], bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        databases=_RULES_SCHEMA,
    )


@_needs_sqlglot
def test_evaluate_rules_violation_advises_never_refuses(env):
    _seed_ruled(env)
    checker = _checker(env, judge=_RaisingModel())
    out = checker.evaluate_rules(_VIOLATING_SQL, default_database=DATASET)
    assert out is not None
    assert "refusals" not in out  # the tier has no refuse path at all
    assert "deterministic rules" in out["note"]
    assert "P010" in out["note"] and "<system-reminder>" in out["note"]
    # The advisory note rides the same UI shield channel as judge flags.
    _stripped, display = pc.split_policy_reminder("results\n\n" + out["note"])
    assert "P010" in display
    checker.close()


@_needs_sqlglot
def test_decided_policies_never_reach_the_judges(env, monkeypatch):
    _seed_ruled(env)
    seen: list[list[str]] = []

    def _recording_judge(model, policies, evidence, **kw):
        seen.append([p["id"] for p in policies])
        return [], 0, len(policies)

    monkeypatch.setattr(pc, "judge_policies", _recording_judge)
    checker = _checker(env, judge=_RaisingModel())
    checker.evaluate_rules(_VIOLATING_SQL, default_database=DATASET)
    note = checker.submit(_VIOLATING_SQL).result(timeout=10)
    assert note == ""
    # P010 was decided deterministically; only the prose policy is judged.
    assert seen == [["P011"]]
    checker.close()


@_needs_sqlglot
def test_unknown_verdicts_fall_through_to_the_judges(env, monkeypatch):
    _seed_ruled(env)
    seen: list[list[str]] = []

    def _recording_judge(model, policies, evidence, **kw):
        seen.append([p["id"] for p in policies])
        return [], 0, len(policies)

    monkeypatch.setattr(pc, "judge_policies", _recording_judge)
    checker = _checker(env, judge=_RaisingModel())
    cte = (
        "WITH s AS (SELECT driverid, points FROM driverstandings) "
        "SELECT driverid, SUM(points) FROM s GROUP BY 1"
    )
    out = checker.evaluate_rules(cte, default_database=DATASET)
    assert out is None  # nothing decided, nothing to say
    checker.submit(cte).result(timeout=10)
    assert seen == [["P010", "P011"]]
    checker.close()


@_needs_sqlglot
def test_missing_sidecar_leaves_everything_to_the_judges(env, monkeypatch):
    _seed_usable_policy(env, doc=_RULED_DOC)  # doc has rules, NO sidecar
    seen: list[list[str]] = []

    def _recording_judge(model, policies, evidence, **kw):
        seen.append([p["id"] for p in policies])
        return [], 0, len(policies)

    monkeypatch.setattr(pc, "judge_policies", _recording_judge)
    checker = _checker(env, judge=_RaisingModel())
    assert checker.evaluate_rules(_VIOLATING_SQL, default_database=DATASET) is None
    checker.submit(_VIOLATING_SQL).result(timeout=10)
    assert seen == [["P010", "P011"]]
    checker.close()


@_needs_sqlglot
def test_run_sql_executes_a_violating_query_and_appends_the_note(env):
    # Rules are advisory ONLY: a proven violation must never block the
    # engine — the results come back with the reminder appended.
    from chat.sql import AthenaSQL, make_sql_tool

    _seed_ruled(env)
    checker = _checker(env, judge=_RaisingModel())

    class _FakeEngine(AthenaSQL):
        def __init__(self):
            super().__init__(athena=None)
            self.ran = False

        def run(self, sql, *, default_database=None):
            self.ran = True
            return {"columns": ["c"], "rows": [{"c": "1"}], "row_count": 1,
                    "truncated": False}

    engine = _FakeEngine()
    tool = make_sql_tool(
        engine,
        dataset_scope={"data_domain": DOMAIN, "dataset": DATASET,
                       "glue_database": DATASET},
        policy_checker=checker,
    )
    out = tool.func(_VIOLATING_SQL)
    assert engine.ran
    assert isinstance(out, str) and '"row_count": 1' in out
    assert "deterministic rules" in out and "P010" in out
    checker.close()


@_needs_sqlglot
def test_non_reading_statements_skip_the_deterministic_tier(env):
    # SHOW/DESCRIBE parse as opaque Commands and must not block this
    # synchronous path on the policy gate's I/O.
    _seed_ruled(env)
    checker = _checker(env, judge=_RaisingModel())
    assert checker.evaluate_rules("SHOW TABLES", default_database=DATASET) is None
    assert checker.evaluate_rules(
        "DESCRIBE driverstandings", default_database=DATASET
    ) is None
    checker.close()


@_needs_sqlglot
def test_tier_gate_is_wider_than_the_judge_gate():
    # The judge gate needs an aggregate/join/window to pay for a fleet round;
    # the deterministic tier must still see plain single-table reads, which is
    # where required_guard / forbidden_usage / sequencing rules live.
    plain = "SELECT CAST(positiontext AS INTEGER) FROM results"
    assert not pc.is_analytical_sql(plain)
    assert pc.is_checkable_sql(plain)
    latest = "SELECT * FROM results ORDER BY raceid DESC LIMIT 1"
    assert not pc.is_analytical_sql(latest)
    assert pc.is_checkable_sql(latest)
    # Non-reading + catalog probes stay out.
    assert not pc.is_checkable_sql("SHOW TABLES")
    assert not pc.is_checkable_sql("DESCRIBE results")
    assert not pc.is_checkable_sql(
        "SELECT table_name FROM information_schema.tables"
    )
    # A comment must not disguise a readable statement (same rule as the
    # judge gate).
    assert pc.is_checkable_sql("-- pull one row\nSELECT time FROM results")


@_needs_sqlglot
def test_plain_read_violation_is_caught_by_the_tier(env):
    # The regression the wider gate exists for: an unguarded cast has no
    # aggregate, so the judge gate would have skipped it entirely.
    _seed_usable_policy(env, doc=_GUARD_DOC)
    ap.put_rules_schema(
        env["s3"], bucket=BUCKET, data_domain=DOMAIN, dataset=DATASET,
        databases={DATASET: {"results": ["positiontext", "raceid"]}},
    )
    checker = _checker(env, judge=_RaisingModel())
    sql = "SELECT CAST(positiontext AS INTEGER) FROM results"
    assert not pc.is_analytical_sql(sql)
    out = checker.evaluate_rules(sql, default_database=DATASET)
    assert out is not None and "P088" in out["note"]
    checker.close()


_GUARD_DOC = """\
policies:
  - id: P088
    type: computational
    condition: casting positiontext to a number
    action: guard the cast with regexp_like or use TRY_CAST
    source: references/usage_guardrails.md
    rules:
      - dimension: required_guard
        targets: [results.positiontext]
        examples:
          violation: SELECT CAST(positiontext AS INTEGER) FROM results
          pass: SELECT TRY_CAST(positiontext AS INTEGER) FROM results
"""


@_needs_sqlglot
def test_unarmed_computational_track_never_evaluates_rules(env):
    _seed_ruled(env)
    checker = _checker(env, judge=_RaisingModel(), tracks=("behavioural",))
    assert checker.evaluate_rules(_VIOLATING_SQL, default_database=DATASET) is None
    checker.close()


@_needs_sqlglot
def test_rules_trace_is_greppable_and_names_the_fired_rule(env, caplog):
    # The operator contract: one CloudWatch filter — "[policy-rules]" — yields
    # the whole tier trace: the evaluated SQL, each rule's verdict with its
    # label, and the summary tally.
    _seed_ruled(env)
    checker = _checker(env, judge=_RaisingModel())
    with caplog.at_level(logging.INFO, logger="chat.policy_check"):
        checker.evaluate_rules(_VIOLATING_SQL, default_database=DATASET)
    trace = [r.getMessage() for r in caplog.records if "[policy-rules]" in r.getMessage()]
    assert any("evaluating" in line and "SUM(points)" in line for line in trace)
    assert any(
        "P010 rule[0] forbidden_aggregation(driverstandings.points) -> VIOLATION"
        in line
        for line in trace
    )
    assert any("advisory reminder" in line and "VIOLATION" in line
               for line in trace)
    assert any("done in" in line and "1 advisory" in line for line in trace)
    checker.close()


@_needs_sqlglot
def test_rules_trace_names_every_skip_reason(env, caplog):
    # A silently inert tier is the one failure mode an operator can't
    # diagnose from behavior — every skip must say WHY.
    _seed_usable_policy(env, doc=_RULED_DOC)  # rules but NO sidecar
    checker = _checker(env, judge=_RaisingModel())
    with caplog.at_level(logging.INFO, logger="chat.policy_check"):
        checker.evaluate_rules(_VIOLATING_SQL, default_database=DATASET)
    assert any(
        "NO rules_schema.json" in r.getMessage() for r in caplog.records
    )
    checker.close()


@_needs_sqlglot
def test_sidecar_load_failure_is_traced_not_misdiagnosed(env, caplog, monkeypatch):
    # A transient S3 failure must ride the [policy-rules] filter and say
    # "load FAILED", never "never authored — re-author to fix".
    _seed_ruled(env)

    def _boom(*a, **kw):
        raise RuntimeError("throttled")

    monkeypatch.setattr("okf_aws.ar_policy.read_rules_schema", _boom)
    checker = _checker(env, judge=_RaisingModel())
    with caplog.at_level(logging.INFO, logger="chat.policy_check"):
        assert checker.evaluate_rules(
            _VIOLATING_SQL, default_database=DATASET
        ) is None
    trace = [
        r.getMessage() for r in caplog.records
        if "[policy-rules]" in r.getMessage()
    ]
    assert any("load FAILED" in line for line in trace)
    assert not any("re-author" in line for line in trace)
    checker.close()


@_needs_sqlglot
def test_submitted_rules_still_exclude_decided_policies(env, monkeypatch):
    # run_sql now submits the tier to the pool instead of evaluating on the
    # tool thread — the judge path must WAIT on the in-flight evaluation so
    # the shard exclusions stay deterministic.
    _seed_ruled(env)
    seen: list[list[str]] = []

    def _recording_judge(model, policies, evidence, **kw):
        seen.append([p["id"] for p in policies])
        return [], 0, len(policies)

    monkeypatch.setattr(pc, "judge_policies", _recording_judge)
    checker = _checker(env, judge=_RaisingModel())
    future = checker.submit_rules(_VIOLATING_SQL, default_database=DATASET)
    assert future is not None
    note = checker.submit(_VIOLATING_SQL).result(timeout=10)
    assert note == ""
    assert seen == [["P011"]]
    outcome = future.result(timeout=10)
    assert outcome is not None and "P010" in outcome["note"]
    checker.close()
