"""Recursive-improvement benchmark: the black-box ``run_benchmark`` tool.

Layered so the deterministic, LLM-free core is unit-testable offline:

* :mod:`harvest.benchmark.questions` — parse the ``question,gold_sql`` CSV, cap to
  ``MAX_QUESTIONS``, hold gold in tool-process memory (never on the mount).
* :mod:`harvest.benchmark.grader` — Athena EX comparator, PASS/FAIL/DISCARDED,
  gold + prediction result caches.
* :mod:`harvest.benchmark.snapshot` — copy the authored bundle (no dot-dirs) into
  a temp dir so the solver is physically bundle-blind.
* :mod:`harvest.benchmark.tool` — assemble solver fan-out → grader → adjudicator
  into the single opaque ``run_benchmark`` tool the supervisor calls.

See ``docs/CONVENTIONS.md`` for the payload + KPI contract and
``docs/BENCHMARK_GUIDE.md`` for how the feature is used.
"""

from __future__ import annotations

from harvest.benchmark.grader import Grader, Outcome, QuestionResult
from harvest.benchmark.questions import BenchmarkQuestion, load_questions

__all__ = [
    "Grader",
    "Outcome",
    "QuestionResult",
    "BenchmarkQuestion",
    "load_questions",
]
