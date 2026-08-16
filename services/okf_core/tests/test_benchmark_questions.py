"""Benchmark CSV parsing: header resolution, blank-skip, hard cap, stable ids,
and per-check participation."""

from __future__ import annotations

import pytest

from okf_core.benchmark_questions import (
    ALL_CHECKS,
    CHECK_BEHAVIOR,
    CHECK_SQL,
    BenchmarkCSVError,
    load_questions,
)


def test_parses_basic_csv():
    csv_text = "question,gold_sql\nHow many races?,SELECT count(*) FROM races\n"
    out = load_questions(csv_text)
    assert out.total_in_csv == 1 and out.dropped == 0
    q = out.questions[0]
    assert q.q_id == 0
    assert q.question == "How many races?"
    assert q.gold_sql == "SELECT count(*) FROM races"
    # Legacy two-column CSVs participate in the SQL check only.
    assert q.checks() == (CHECK_SQL,)
    assert out.check_counts == {CHECK_SQL: 1, CHECK_BEHAVIOR: 0}


def test_accepts_gold_column_synonyms():
    out = load_questions("nl,query\nQ1,SELECT 1\n")
    assert out.questions[0].question == "Q1"
    assert out.questions[0].gold_sql == "SELECT 1"


def test_missing_required_column_raises():
    with pytest.raises(BenchmarkCSVError):
        load_questions("question,notes\nQ1,hello\n")


def test_gold_columns_and_participation():
    csv_text = (
        "question,gold_sql,expected_behavior\n"
        "Most wins?,SELECT 1,Should name the driver and cite the doc.\n"
        "Pit stop durations?,,Should say durations are not tracked.\n"
    )
    out = load_questions(csv_text)
    assert [q.checks() for q in out.questions] == [
        (CHECK_SQL, CHECK_BEHAVIOR),
        (CHECK_BEHAVIOR,),
    ]
    assert out.check_counts == {CHECK_SQL: 1, CHECK_BEHAVIOR: 2}
    assert (
        out.questions[0].gold_for(CHECK_BEHAVIOR)
        == "Should name the driver and cite the doc."
    )


def test_behavior_only_csv_is_valid():
    # No SQL column at all — valid: the set simply drives the other check.
    out = load_questions(
        "question,expected_behavior\nPit stops?,Should say not tracked.\n"
    )
    assert out.questions[0].checks() == (CHECK_BEHAVIOR,)


def test_retired_gold_answer_column_is_ignored():
    # Answer Match is retired: its column no longer resolves — it's just an
    # unknown extra column, and a row whose ONLY gold was a gold_answer is
    # skipped like any other goldless row.
    csv_text = (
        "question,gold_sql,gold_answer\n"
        "Q0,SELECT 0,42\n"
        "Q1,,28\n"  # answer-only row: no longer a valid row
    )
    out = load_questions(csv_text)
    assert [q.question for q in out.questions] == ["Q0"]
    assert out.questions[0].checks() == (CHECK_SQL,)
    with pytest.raises(BenchmarkCSVError):
        # An answer-only CSV now has no recognizable gold column at all.
        load_questions("question,gold_answer\nRaces in 2020?,28\n")


def test_header_resolution_is_deterministic_priority_order():
    # Two accepted spellings of the gold-SQL column in ONE csv: the priority
    # tuple must pick 'gold_sql' every time (the old set+next() picked one
    # nondeterministically between processes).
    csv_text = "question,sql,gold_sql\nQ1,WRONG,RIGHT\n"
    for _ in range(5):
        out = load_questions(csv_text)
        assert out.questions[0].gold_sql == "RIGHT"


def test_behavior_headers_never_collide_with_sql_headers():
    # 'gold' (a SQL synonym) and 'expected_behavior' both present: each
    # resolves to its own column, no crossover.
    out = load_questions(
        "question,gold,expected_behavior\nQ1,SELECT 1,Should not guess.\n"
    )
    q = out.questions[0]
    assert q.gold_sql == "SELECT 1" and q.expected_behavior == "Should not guess."
    assert set(q.checks()) == {CHECK_SQL, CHECK_BEHAVIOR}
    assert set(ALL_CHECKS) >= set(q.checks())


def test_no_header_raises():
    with pytest.raises(BenchmarkCSVError):
        load_questions("")


def test_quoted_fields_with_commas_and_embedded_newlines():
    # Real gold SQL is multi-line and comma-heavy; the csv module must see one
    # ROW per question, not one per physical line.
    csv_text = (
        "question,gold_sql,expected_behavior\n"
        '"Wins, by driver?","SELECT name, wins\nFROM standings\nORDER BY wins",\n'
        '"Pit stops?",,"Should say:\n- durations are not tracked\n- never invent one"\n'
    )
    out = load_questions(csv_text)
    assert [q.question for q in out.questions] == ["Wins, by driver?", "Pit stops?"]
    assert (
        out.questions[0].gold_sql
        == "SELECT name, wins\nFROM standings\nORDER BY wins"
    )
    assert out.questions[0].checks() == (CHECK_SQL,)
    assert (
        out.questions[1].expected_behavior
        == "Should say:\n- durations are not tracked\n- never invent one"
    )
    assert out.check_counts == {CHECK_SQL: 1, CHECK_BEHAVIOR: 1}


def test_blank_rows_skipped_and_ids_are_dense():
    csv_text = (
        "question,gold_sql\n"
        "Q0,SELECT 0\n"
        ",SELECT 1\n"  # blank question — skipped
        "Q2,\n"  # blank gold — skipped
        "Q3,SELECT 3\n"
    )
    out = load_questions(csv_text)
    assert [q.question for q in out.questions] == ["Q0", "Q3"]
    assert [q.q_id for q in out.questions] == [0, 1]  # dense, file order


def test_row_with_any_gold_cell_is_kept():
    # A row with a blank gold_sql but an expected_behavior is VALID (it just
    # doesn't participate in the SQL check).
    csv_text = (
        "question,gold_sql,expected_behavior\n"
        "Q0,SELECT 0,\n"
        "Q1,,Should refuse.\n"
        "Q2,,\n"  # no gold at all — skipped
    )
    out = load_questions(csv_text)
    assert [q.question for q in out.questions] == ["Q0", "Q1"]
    assert out.questions[1].checks() == (CHECK_BEHAVIOR,)


def test_hard_cap_takes_first_n_in_order():
    rows = "\n".join(f"Q{i},SELECT {i}" for i in range(105))
    out = load_questions("question,gold_sql\n" + rows + "\n", max_questions=100)
    assert out.total_in_csv == 105
    assert len(out.questions) == 100
    assert out.dropped == 5
    # First 100 in file order; last kept is Q99.
    assert out.questions[0].question == "Q0"
    assert out.questions[-1].question == "Q99"


def test_cap_reproducible_across_calls():
    rows = "\n".join(f"Q{i},SELECT {i}" for i in range(120))
    text = "question,gold_sql\n" + rows + "\n"
    a = load_questions(text, max_questions=100)
    b = load_questions(text, max_questions=100)
    assert [q.question for q in a.questions] == [q.question for q in b.questions]
