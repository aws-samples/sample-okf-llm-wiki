"""The checks: spec registry, gold-blindness, grade-fn binding."""

from __future__ import annotations

import pytest

from okf_core.benchmark_questions import (
    ALL_CHECKS,
    CHECK_BEHAVIOR,
    CHECK_SQL,
    BenchmarkQuestion,
)

from harvest.benchmark.checks import (
    BEHAVIOR_SOLVER_PROMPT,
    CHECK_SPECS,
    SQL_SOLVER_PROMPT,
    make_grade_fn,
)
from harvest.benchmark.extract import extract_text
from harvest.benchmark.grader import Grader, Outcome

# -- the spec registry ---------------------------------------------------------


def test_registry_covers_all_checks_with_distinct_protocols():
    assert set(CHECK_SPECS) == set(ALL_CHECKS)
    prompts = {spec.solver_prompt for spec in CHECK_SPECS.values()}
    assert len(prompts) == 2  # distinct protocols, never a shared solve
    assert CHECK_SPECS[CHECK_SQL].uses_athena is True
    assert CHECK_SPECS[CHECK_SQL].judge_graded is False
    # Behavior: no deterministic grade, nothing to run under the Athena
    # semaphore — the judge grades every attempt in the judge phase.
    assert CHECK_SPECS[CHECK_BEHAVIOR].uses_athena is False
    assert CHECK_SPECS[CHECK_BEHAVIOR].judge_graded is True


def test_prompts_are_gold_blind():
    for prompt in (SQL_SOLVER_PROMPT, BEHAVIOR_SOLVER_PROMPT):
        assert "gold" not in prompt.lower()
    # The behavior solver must never learn there IS an expectation to game.
    assert "expected_behavior" not in BEHAVIOR_SOLVER_PROMPT
    assert "expectation" not in BEHAVIOR_SOLVER_PROMPT.lower()


def test_behavior_solver_prompt_teaches_the_wiki_structure():
    # The Behavior solver simulates a REAL consumer, so its prompt carries the
    # wiki's structural affordances (the other checks stay deliberately lean).
    for hint in ("index.md", "tables/", "LITERAL"):
        assert hint in BEHAVIOR_SOLVER_PROMPT


def test_behavior_parse_is_plain_text():
    spec = CHECK_SPECS[CHECK_BEHAVIOR]
    assert spec.parse is extract_text
    assert spec.parse("  The wiki does not track this.  ") == (
        "The wiki does not track this."
    )
    # A fenced block is NOT extracted — the whole reply is the prediction.
    reply = "Answer:\n```sql\nSELECT 1\n```\ndone"
    assert spec.parse(reply) == reply


def test_make_grade_fn_binds_sql_and_refuses_behavior():
    q = BenchmarkQuestion(
        q_id=0, question="Q", gold_sql="GOLD", expected_behavior="Should refuse."
    )
    # SQL EX → through the shared Grader (gold executes, sets compare).
    grader = Grader(lambda sql: [{"c": "1"}])
    sql_grade = make_grade_fn(CHECK_SPECS[CHECK_SQL], grader=grader)
    assert sql_grade(q, "PRED").outcome is Outcome.PASS  # both return [{"c":"1"}]
    # Behavior is judge-graded — asking for a deterministic grade fn is a
    # wiring bug and must fail loudly, not silently grade nothing.
    with pytest.raises(ValueError):
        make_grade_fn(CHECK_SPECS[CHECK_BEHAVIOR], grader=grader)


def test_every_solver_prompt_directs_read_me_first():
    # Both checks' solvers carry the read_me primer tool; the prompts' method
    # step 1 must point at it so the primer is read before exploration starts.
    for prompt in (SQL_SOLVER_PROMPT, BEHAVIOR_SOLVER_PROMPT):
        assert "read_me" in prompt


def test_solver_protocol_grants_live_sql_to_behavior_only():
    from harvest.benchmark.checks import (
        BEHAVIOR_SOLVER_PROMPT_LIVE_SQL,
        solver_protocol,
    )

    # Default: every check keeps its own prompt (the Glue build — the prompts
    # are rebuilt per run with the source's dialect, so compare by content),
    # no SQL grant.
    for spec in CHECK_SPECS.values():
        proto = solver_protocol(spec)
        assert proto.prompt == spec.solver_prompt
        assert proto.wants_sql is False

    # Flag ON: only the Behavior check flips to the live-SQL protocol — the
    # SQL EX solver stays data-blind whatever the run config says (live
    # queries would let it iterate empirically to the answer).
    proto = solver_protocol(CHECK_SPECS[CHECK_BEHAVIOR], behavior_live_sql=True)
    assert proto.prompt == BEHAVIOR_SOLVER_PROMPT_LIVE_SQL
    assert proto.wants_sql is True
    proto = solver_protocol(CHECK_SPECS[CHECK_SQL], behavior_live_sql=True)
    assert proto.prompt == CHECK_SPECS[CHECK_SQL].solver_prompt
    assert proto.wants_sql is False


def test_solver_protocol_grants_ask_human_to_behavior_only():
    # The terminal ask_human escalation makes "should ask" expectations a
    # structural outcome. Behavior holds it on EVERY question uniformly
    # (gold-blind — no per-row signal), with or without live SQL; SQL EX never
    # (its contract is one fenced query; asking is not a gradable SQL outcome).
    from harvest.benchmark.checks import solver_protocol
    assert solver_protocol(CHECK_SPECS[CHECK_BEHAVIOR]).wants_ask is True
    assert (
        solver_protocol(CHECK_SPECS[CHECK_BEHAVIOR], behavior_live_sql=True).wants_ask
        is True
    )
    assert solver_protocol(CHECK_SPECS[CHECK_SQL]).wants_ask is False
    assert (
        solver_protocol(CHECK_SPECS[CHECK_SQL], behavior_live_sql=True).wants_ask
        is False
    )


def test_behavior_prompts_document_ask_human_and_sql_prompt_does_not():
    # The prompt and the grant travel together: both Behavior variants explain
    # the tool and that calling it ENDS the run; the SQL solver (which never
    # holds it) must not mention it.
    from harvest.benchmark.checks import BEHAVIOR_SOLVER_PROMPT_LIVE_SQL

    for p in (BEHAVIOR_SOLVER_PROMPT, BEHAVIOR_SOLVER_PROMPT_LIVE_SQL):
        assert "ask_human" in p
        assert "ENDS the run" in p
        assert "Never ask to avoid the reading" in p
        # The ask policy is consequence-calibrated and honors documented
        # defaults — same contract as the production chat agent's.
        assert "confidently wrong answer" in p
        assert "documented default is not guessing" in p
    assert "ask_human" not in SQL_SOLVER_PROMPT


def test_live_sql_prompt_is_capability_accurate_and_gold_blind():
    from harvest.benchmark.checks import BEHAVIOR_SOLVER_PROMPT_LIVE_SQL

    # The wiki-only prompt disclaims querying; the live variant must NOT (a
    # prompt that disclaims a tool the solver holds teaches it to skip it).
    assert "CANNOT query" in BEHAVIOR_SOLVER_PROMPT
    assert "CANNOT query" not in BEHAVIOR_SOLVER_PROMPT_LIVE_SQL
    assert "run_sql" in BEHAVIOR_SOLVER_PROMPT_LIVE_SQL
    assert "run_sql" not in BEHAVIOR_SOLVER_PROMPT
    # Same gold-blindness contract as every solver prompt.
    assert "gold" not in BEHAVIOR_SOLVER_PROMPT_LIVE_SQL.lower()
    assert "expectation" not in BEHAVIOR_SOLVER_PROMPT_LIVE_SQL.lower()


def test_solver_protocol_is_dialect_aware():
    # Benchmark Studio has no source-type gate (unlike cross mode), so a
    # Redshift dataset can be benchmarked — its SQL-writing solvers must be
    # told the run's REAL dialect, never a hardcoded Athena/Trino.
    from harvest.benchmark.checks import solver_protocol
    from okf_core.benchmark_questions import CHECK_SQL

    proto = solver_protocol(CHECK_SPECS[CHECK_SQL], dialect="amazon-redshift")
    assert "amazon-redshift" in proto.prompt
    assert "Athena/Trino" not in proto.prompt

    live = solver_protocol(
        CHECK_SPECS[CHECK_BEHAVIOR],
        behavior_live_sql=True,
        dialect="amazon-redshift",
    )
    assert live.wants_sql is True
    assert "amazon-redshift" in live.prompt
    assert "Athena/Trino" not in live.prompt

    # Default stays the Glue dialect (back-compat for the module constants).
    assert "Athena/Trino" in solver_protocol(CHECK_SPECS[CHECK_SQL]).prompt
