"""The harvest.benchmark.questions path re-exports the shared okf_core parser.

The parse+cap logic itself is tested in okf_core (test_benchmark_questions.py);
here we only assert the historical harvest import path still resolves to the same
implementation, so runtime code importing it keeps working.
"""

from __future__ import annotations

from harvest.benchmark import questions as hq
from okf_core import benchmark_questions as oq


def test_reexports_shared_parser():
    assert hq.load_questions is oq.load_questions
    assert hq.BenchmarkQuestion is oq.BenchmarkQuestion
    assert hq.BenchmarkCSVError is oq.BenchmarkCSVError


def test_reexport_parses_end_to_end():
    res = hq.load_questions("question,gold_sql\nHow many?,SELECT count(*) FROM t\n")
    assert len(res.questions) == 1
    assert res.questions[0].q_id == 0
    assert res.questions[0].gold_sql == "SELECT count(*) FROM t"
