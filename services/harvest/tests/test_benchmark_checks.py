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

    # Default: every check keeps its own prompt, no SQL grant.
    for spec in CHECK_SPECS.values():
        prompt, wants_sql = solver_protocol(spec)
        assert prompt is spec.solver_prompt
        assert wants_sql is False

    # Flag ON: only the Behavior check flips to the live-SQL protocol — the
    # SQL EX solver stays data-blind whatever the run config says (live
    # queries would let it iterate empirically to the answer).
    prompt, wants_sql = solver_protocol(
        CHECK_SPECS[CHECK_BEHAVIOR], behavior_live_sql=True
    )
    assert prompt is BEHAVIOR_SOLVER_PROMPT_LIVE_SQL
    assert wants_sql is True
    prompt, wants_sql = solver_protocol(
        CHECK_SPECS[CHECK_SQL], behavior_live_sql=True
    )
    assert prompt is CHECK_SPECS[CHECK_SQL].solver_prompt
    assert wants_sql is False


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
