"""Benchmark CSV parsing: header resolution, blank-skip, hard cap, stable ids."""

from __future__ import annotations

import pytest

from okf_core.benchmark_questions import BenchmarkCSVError, load_questions


def test_parses_basic_csv():
    csv_text = "question,gold_sql\nHow many races?,SELECT count(*) FROM races\n"
    out = load_questions(csv_text)
    assert out.total_in_csv == 1 and out.dropped == 0
    q = out.questions[0]
    assert q.q_id == 0
    assert q.question == "How many races?"
    assert q.gold_sql == "SELECT count(*) FROM races"


def test_accepts_gold_column_synonyms():
    out = load_questions("nl,query\nQ1,SELECT 1\n")
    assert out.questions[0].question == "Q1"
    assert out.questions[0].gold_sql == "SELECT 1"


def test_missing_required_column_raises():
    with pytest.raises(BenchmarkCSVError):
        load_questions("question,notes\nQ1,hello\n")


def test_no_header_raises():
    with pytest.raises(BenchmarkCSVError):
        load_questions("")


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
