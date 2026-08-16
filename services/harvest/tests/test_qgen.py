"""qgen — the submit-time gates, the quota store, and the payload contract.

Agent-free by design: the store + tools are plain Python (the langchain @tool
wrapper is peeled via .func), so every validation gate is exercised without a
model. The quota middleware's decision logic is tested through the store's
``unresolved`` contract it keys on.
"""

from __future__ import annotations

import threading

import pytest

from harvest.benchmark import qgen
from harvest.benchmark.qgen import (
    QuestionStore,
    _leaked_identifier,
    _normalize_question,
    _physical_identifiers,
    make_author_tools,
    validate_qgen_payload,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _store(slots=None, seen=None):
    return QuestionStore(
        slots
        or {
            1: {"dimension": "direct_retrieval", "tier": "easy", "check": "sql"},
            2: {"dimension": "unanswerable", "tier": "medium", "check": "behavior"},
        },
        seen=seen if seen is not None else set(),
        seen_lock=threading.Lock(),
    )


def _tools(store, *, rows=None, error="", idents=None):
    executed = []

    def execute_sql(sql):
        executed.append(sql)
        return ([] if error else (rows if rows is not None else [["x"]])), error

    submit, forfeit = make_author_tools(
        store, idents=idents or [], execute_sql=execute_sql
    )
    return submit.func, forfeit.func, executed


# -- submit-time gates -----------------------------------------------------------


def test_accuracy_submit_executes_gold_and_fills_the_slot():
    store = _store()
    submit, _forfeit, executed = _tools(store, rows=[["a"], ["b"]])
    out = submit(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="SELECT winner FROM races",
    )
    assert out.startswith("accepted")
    assert executed == ["SELECT winner FROM races"]
    q = store.filled[1]
    assert q["validation"] == {"executed": True, "row_count": 2}
    assert q["check"] == "sql" and q["tier"] == "easy"


def test_gold_that_fails_or_returns_nothing_is_a_corrective_rejection():
    store = _store()
    submit, _f, _e = _tools(store, error="SYNTAX_ERROR: line 1")
    out = submit(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="SELECT bogus FROM races",
    )
    assert "did not execute" in out and "SYNTAX_ERROR" in out
    assert store.unresolved() == [1, 2]

    submit2, _f2, _e2 = _tools(store, rows=[])
    out2 = submit2(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="SELECT winner FROM races WHERE 1=0",
    )
    assert "0 rows" in out2
    # A rejected submit releases its dedup claim — the fixed resubmit of the
    # SAME question text must not be refused as a duplicate of itself.
    submit3, _f3, _e3 = _tools(store, rows=[["a"]])
    assert submit3(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="SELECT winner FROM races",
    ).startswith("accepted")


def test_check_shape_mismatches_are_rejected_with_direction():
    store = _store()
    submit, _f, _e = _tools(store)
    assert "ACCURACY slot" in submit(slot=1, question="Which team won the most races?")
    assert "BEHAVIOR slot" in submit(
        slot=2, question="How long do pit stops usually take?"
    )
    assert "ONE SELECT/WITH" in submit(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="DROP TABLE races",
    )


def test_leakage_lint_rejects_schema_speak_but_not_meta_slots():
    idents = _physical_identifiers(
        {"races": {"race_id": "bigint", "year": "int"}, "pit_stops": {"ms": "int"}}
    )
    store = QuestionStore(
        {
            1: {"dimension": "direct_retrieval", "tier": "easy", "check": "sql"},
            2: {"dimension": "meta_introspection", "tier": "easy", "check": "behavior"},
        },
        seen=set(),
        seen_lock=threading.Lock(),
    )
    submit, _f, _e = _tools(store, idents=idents)
    out = submit(
        slot=1,
        question="What is the highest race_id recorded in a season?",
        gold_sql="SELECT 1",
    )
    assert "race_id" in out and "business language" in out
    # snake_case TABLE names leak too.
    out2 = submit(
        slot=1,
        question="How many pit_stops happened in 2020?",
        gold_sql="SELECT 1",
    )
    assert "pit_stops" in out2
    # Bare English column names are NOT schema-speak ("year").
    assert submit(
        slot=1,
        question="Which season year had the most races run?",
        gold_sql="SELECT 1",
    ).startswith("accepted")
    # Meta/introspection is exempt: naming tables IS the point there.
    assert submit(
        slot=2,
        question="Does this dataset contain a pit_stops table, and at what grain?",
        expected_behavior="Names the table and its per-stop grain.",
    ).startswith("accepted")


def test_duplicate_questions_are_rejected_across_stores():
    seen: set[str] = set()
    lock = threading.Lock()
    a = QuestionStore(
        {1: {"dimension": "direct_retrieval", "tier": "easy", "check": "sql"}},
        seen=seen,
        seen_lock=lock,
    )
    b = QuestionStore(
        {2: {"dimension": "comparison", "tier": "easy", "check": "sql"}},
        seen=seen,
        seen_lock=lock,
    )
    submit_a, _f, _e = _tools(a)
    submit_b, _f2, _e2 = _tools(b)
    assert submit_a(
        slot=1, question="Which team won the most races?", gold_sql="SELECT 1"
    ).startswith("accepted")
    out = submit_b(
        slot=2, question="Which  team won the most races??", gold_sql="SELECT 2"
    )
    assert "already submitted" in out


def test_forfeit_needs_a_real_reason_and_a_late_fill_beats_it():
    store = _store()
    submit, forfeit, _e = _tools(store)
    assert "concrete reason" in forfeit(slot=2, reason="no")
    assert forfeit(
        slot=2, reason="the schema has no temporal columns to project from"
    ).startswith("slot 2 forfeited")
    assert store.unresolved() == [1]
    # The author changes its mind: filling a forfeited slot un-forfeits it.
    assert submit(
        slot=2,
        question="How long do pit stops usually take at street circuits?",
        expected_behavior="Should say pit-stop durations are not tracked.",
    ).startswith("accepted")
    assert store.forfeited == {}


def test_rejected_resubmission_preserves_the_forfeit_and_its_reason():
    # A forfeit only clears when a submission is ACCEPTED. The old code popped
    # it before validation, so a rejected retry erased the author's concrete
    # reason and re-opened a slot the author had already resolved.
    store = _store()
    submit, forfeit, _e = _tools(store, error="SYNTAX_ERROR: line 1")
    forfeit(slot=1, reason="no business phrasing exists for this dataset")
    out = submit(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="SELECT bogus FROM races",
    )
    assert "did not execute" in out
    assert store.forfeited[1] == "no business phrasing exists for this dataset"
    assert 1 not in store.unresolved()


def test_fill_wins_over_a_forfeit_that_lands_mid_validation():
    # Parallel tool calls: a forfeit arriving during the (unlocked) gold
    # execution window must not leave the slot BOTH filled and forfeited —
    # the accepted fill clears it atomically at commit time.
    store = _store()
    holder = {}

    def execute_sql(sql):
        holder["forfeit"](slot=1, reason="giving up mid-flight in parallel")
        return [["a"]], ""

    submit, forfeit = make_author_tools(store, idents=[], execute_sql=execute_sql)
    holder["forfeit"] = forfeit.func
    out = submit.func(
        slot=1,
        question="Which team won the most races overall?",
        gold_sql="SELECT winner FROM races",
    )
    assert out.startswith("accepted")
    assert 1 in store.filled and 1 not in store.forfeited


def test_semicolon_inside_a_string_literal_is_not_multiple_statements():
    # `code = 'A;B'` is data, not a statement separator — the one-statement
    # guard reads the literal-masked SQL. A real second statement still fails.
    store = _store()
    submit, _f, _e = _tools(store, rows=[["a"]])
    assert submit(
        slot=1,
        question="Which delimiter-coded entries exist in the catalog?",
        gold_sql="SELECT code FROM codes WHERE code = 'A;B'",
    ).startswith("accepted")
    store2 = _store()
    submit2, _f2, _e2 = _tools(store2)
    assert "ONE SELECT/WITH" in submit2(
        slot=1,
        question="Which drivers won the most races overall?",
        gold_sql="SELECT 1; DROP TABLE races",
    )


def test_unknown_and_double_filled_slots_are_rejected():
    store = _store()
    submit, _f, _e = _tools(store)
    assert "not on your worklist" in submit(
        slot=9, question="Which driver has the most wins?", gold_sql="SELECT 1"
    )
    assert submit(
        slot=1, question="Which driver has the most wins overall?", gold_sql="SELECT 1"
    ).startswith("accepted")
    assert "already filled" in submit(
        slot=1, question="Who won the most races in total?", gold_sql="SELECT 1"
    )


# -- the quota contract -------------------------------------------------------------


def test_unresolved_is_what_the_quota_gate_keys_on():
    store = _store()
    submit, forfeit, _e = _tools(store)
    assert store.unresolved() == [1, 2]
    submit(slot=1, question="Which driver has the most wins?", gold_sql="SELECT 1")
    assert store.unresolved() == [2]
    forfeit(slot=2, reason="nothing in this schema supports the premise")
    assert store.unresolved() == []


def test_progress_counts_fills_and_forfeits(monkeypatch):
    writes = []
    p = qgen._Progress(lambda attrs: writes.append(attrs), total=4)
    store = QuestionStore(
        {1: {"dimension": "direct_retrieval", "tier": "easy", "check": "sql"}},
        seen=set(),
        seen_lock=threading.Lock(),
        on_change=p.tick,
    )
    p.watch(store)
    submit, _f, _e = _tools(store)
    submit(slot=1, question="Which driver has the most wins?", gold_sql="SELECT 1")
    assert writes and writes[-1]["progress_current"] == 1
    assert writes[-1]["progress_total"] == 4 and writes[-1]["phase"] == "authoring"


def test_progress_counts_each_slot_once_across_backfill_stores():
    # A backfill store RE-ISSUES round-1 forfeits/misses. Summing every store
    # double-counted those slots (old forfeit + new fill) and pinned the bar
    # at 100% for the whole backfill phase; ownership goes to the LATEST
    # store that carries the slot.
    writes = []
    p = qgen._Progress(lambda attrs: writes.append(attrs), total=2)
    spec = {"dimension": "direct_retrieval", "tier": "easy", "check": "sql"}
    seen, lock = set(), threading.Lock()
    s1 = QuestionStore({1: spec, 2: spec}, seen=seen, seen_lock=lock)
    p.watch(s1)
    s1.forfeited[1] = "round-1 forfeit, re-issued to the backfill"
    s1.filled[2] = {"question": "q2"}
    s2 = QuestionStore({1: spec}, seen=seen, seen_lock=lock)  # the backfill
    p.watch(s2)
    p.phase = "backfill"
    p.tick(force=True)
    assert writes[-1]["progress_current"] == 1  # slot 1 is open again, not 2/2
    s2.filled[1] = {"question": "q1"}
    p.tick(force=True)
    assert writes[-1]["progress_current"] == 2


def test_backfill_worklists_are_chunked_to_round1_size():
    spec = {"dimension": "direct_retrieval", "tier": "easy", "check": "sql"}
    unresolved = {n: spec for n in range(1, 21)}
    chunks = qgen._chunk_slots(unresolved)
    assert all(len(c) <= qgen._BACKFILL_BATCH_SLOTS for c in chunks)
    assert sorted(n for c in chunks for n in c) == list(range(1, 21))
    assert len(chunks) == 3  # 8 + 8 + 4


def _ai(content="", tool_calls=None):
    from langchain_core.messages import AIMessage

    return AIMessage(content=content, tool_calls=tool_calls or [])


def _human(content):
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=content)


def test_quota_middleware_blocks_a_silent_finish_and_names_the_slots():
    store = _store()
    mw = qgen.AuthorQuotaMiddleware(store, max_nudges=2)
    # A final AI message (no tool calls) with both slots unresolved → nudge
    # naming them, jump back to the model.
    out = mw.after_model({"messages": [_ai("done, great questions!")]}, None)
    assert out is not None and out["jump_to"] == "model"
    nudge = out["messages"][0].content
    assert "[1, 2]" in nudge and "forfeit_slot" in nudge


def test_quota_middleware_lets_a_resolved_author_finish():
    store = _store()
    submit, forfeit, _e = _tools(store)
    submit(slot=1, question="Which driver has the most wins?", gold_sql="SELECT 1")
    forfeit(slot=2, reason="the schema cannot support this premise at all")
    mw = qgen.AuthorQuotaMiddleware(store)
    assert mw.after_model({"messages": [_ai("summary")]}, None) is None


def test_quota_middleware_never_interferes_mid_flight_and_gives_up_after_budget():
    store = _store()
    mw = qgen.AuthorQuotaMiddleware(store, max_nudges=1)
    # Mid-flight (tool calls pending): hands off.
    working = _ai("", tool_calls=[{"name": "run_sql", "args": {}, "id": "t1"}])
    assert mw.after_model({"messages": [working]}, None) is None
    # First silent finish: nudged.
    first = mw.after_model({"messages": [_ai("done")]}, None)
    assert first is not None
    # Second, with the prior nudge in history: budget spent — the backfill owns
    # the rest (the middleware makes silence impossible, the pipeline enforces).
    history = [_ai("done"), first["messages"][0], _ai("still done")]
    assert mw.after_model({"messages": history}, None) is None


# -- payload contract ----------------------------------------------------------------


def test_payload_validation_names_whats_missing():
    assert "data_domain" in validate_qgen_payload({})
    assert "qbank_id" in validate_qgen_payload(
        {"data_domain": "d", "dataset": "ds"}
    )
    assert "invalid qbank_id" in validate_qgen_payload(
        {"data_domain": "d", "dataset": "ds", "qbank_id": "R!!"}
    )
    bad_config = validate_qgen_payload(
        {
            "data_domain": "d",
            "dataset": "ds",
            "qbank_id": "qb20260812t000000-aaaa1111",
            "config": {"count": 5},
        }
    )
    assert "count" in bad_config
    assert (
        validate_qgen_payload(
            {
                "data_domain": "d",
                "dataset": "ds",
                "qbank_id": "qb20260812t000000-aaaa1111",
                "config": {"count": 25},
            }
        )
        is None
    )


def test_author_prompt_carries_exactly_one_family_addendum():
    # The provider prompting guides (prompts/oai-prompting.md,
    # prompts/opus-5-prompting.md) are applied via harvest.prompts._with_gpt —
    # the SAME mechanism every harvest prompt uses. GPT gets <persistence>
    # (the quota gate's failure mode: handing back at uncertainty); Claude
    # gets <no_narration> (long authoring conversations must not pay for
    # narration every turn).
    for_gpt = qgen._author_prompt("Athena/Trino", gpt=True)
    for_claude = qgen._author_prompt("Athena/Trino", gpt=False)
    assert "<persistence>" in for_gpt and "<no_narration>" not in for_gpt
    assert "<no_narration>" in for_claude and "<persistence>" not in for_claude
    # The base body (dialect + tier briefs interpolated) is shared verbatim.
    for prompt in (for_gpt, for_claude):
        assert "Athena/Trino" in prompt
        assert "one table, direct lookup/filter/count" in prompt
        assert "submit_question" in prompt


# -- cancel: partial work must not survive -------------------------------------------


class _FakeRegistry:
    def __init__(self, status="running"):
        self.status = status
        self.updates = []

    def get_item(self, TableName=None, Key=None):
        if self.status is None:
            return {}
        return {"Item": {"status": {"S": self.status}}}

    def update_item(self, **kwargs):
        cond = kwargs.get("ConditionExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        blocked = values.get(":blocked", {}).get("S")
        if "#st <> :blocked" in cond and self.status == blocked:
            err = Exception("blocked")
            err.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise err
        self.updates.append(kwargs)
        return {}


def test_row_status_reads_the_tri_state():
    reg = _FakeRegistry(status="cancelled")
    assert qgen._row_status((reg, "t"), "d", "ds", "qb1") == "cancelled"
    reg.status = None
    assert qgen._row_status((reg, "t"), "d", "ds", "qb1") is None

    class _Broken:
        def get_item(self, **kw):
            raise RuntimeError("throttled")

    # Fail-open: a registry blip must not discard a finished bank.
    assert qgen._row_status((_Broken(), "t"), "d", "ds", "qb1") == "running"


def test_cancelled_row_discards_the_bank_instead_of_persisting(monkeypatch):
    reg = _FakeRegistry(status="cancelled")
    monkeypatch.setattr(qgen, "build_registry_client", lambda: (reg, "t"))
    monkeypatch.setattr(qgen, "_execute", lambda payload, qid, row: {"questions": []})
    puts = []

    class _S3:
        def put_object(self, **kw):
            puts.append(kw)

    monkeypatch.setattr(qgen, "_s3_client", lambda: _S3())
    monkeypatch.setattr(qgen, "default_bucket", lambda: "b")
    qgen.run_generate_questions(
        {
            "data_domain": "d",
            "dataset": "ds",
            "qbank_id": "qb20260812t000000-aaaa1111",
        }
    )
    # No artifact PUT, and no terminal write flipped the cancelled row.
    assert puts == []
    assert not any(
        v.get(":v0", {}).get("S") == "complete"
        for u in reg.updates
        for v in [u.get("ExpressionAttributeValues", {})]
    )


def test_cancelled_while_queued_does_no_work_and_never_flips_the_row(monkeypatch):
    # The cold-start hole: cancel lands while the row is still QUEUED (microVM
    # not booted). The runtime's initial `running` write must be BLOCKED (not
    # resurrect the cancelled row) and the run must exit before generating.
    reg = _FakeRegistry(status="cancelled")
    monkeypatch.setattr(qgen, "build_registry_client", lambda: (reg, "t"))
    executed = []
    monkeypatch.setattr(
        qgen, "_execute", lambda *a, **k: executed.append(1) or {"questions": []}
    )
    qgen.run_generate_questions(
        {
            "data_domain": "d",
            "dataset": "ds",
            "qbank_id": "qb20260812t000000-aaaa1111",
        }
    )
    assert executed == []  # no ground-truth pull, no authors, no artifact
    assert reg.updates == []  # every write was conditionally dropped


def test_terminal_write_cannot_resurrect_a_cancelled_row():
    # The narrowest race: cancel lands AFTER the runtime's status check but
    # before its terminal write — the conditional write must drop, not flip
    # cancelled back to complete.
    from harvest.benchmark.report_store import update_report_row

    reg = _FakeRegistry(status="cancelled")
    update_report_row(
        (reg, "t"),
        data_domain="d",
        dataset="ds",
        report_id="qb1",
        attrs={"status": "complete"},
        sk="QBANK#qb1",
        unless_status="cancelled",
    )
    assert reg.updates == []  # dropped, never retried — that IS the contract


# -- helpers -----------------------------------------------------------------------


def test_normalize_question_is_punctuation_and_case_blind():
    assert _normalize_question("Which  team WON, the most races?") == _normalize_question(
        "which team won the most races"
    )


def test_leak_matcher_wants_word_boundaries():
    idents = ["race_id"]
    assert _leaked_identifier("what is the top race_id?", idents) == "race_id"
    assert _leaked_identifier("the embrace_id_ea word", idents) is None
