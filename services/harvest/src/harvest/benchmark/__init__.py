"""Benchmark Studio — standalone, human-led wiki evaluation.

Runs as its own invocation mode on the harvest runtime (never inside a
harvest — the in-run recursive-improvement loop is retired). Layered so the
deterministic, LLM-free core is unit-testable offline:

* :mod:`okf_core.benchmark_questions` (shared) — parse the multi-gold CSV, cap
  to ``MAX_QUESTIONS``, per-check participation; gold stays in tool-process
  memory, never on any agent-visible surface.
* :mod:`harvest.benchmark.checks` — the checks' solver protocols + grading
  (SQL EX / Behavior).
* :mod:`harvest.benchmark.grader` — the deterministic Athena EX comparator.
* :mod:`harvest.benchmark.solver` — the bundle-blind ReAct solver + read-only
  file tools; :mod:`harvest.benchmark.trace` — its bounded solve traces.
* :mod:`harvest.benchmark.judge` — the mandatory pass/fail review over SQL EX
  failures, plus the per-run behavior grader (the judge as the grader).
* :mod:`harvest.benchmark.report_run` — the N-runs × checks engine.
* :mod:`harvest.benchmark.s3_snapshot` / :mod:`.report_store` — S3-materialized
  wiki snapshots (live or version-pinned) and report/row persistence.
* :mod:`harvest.benchmark.studio` — the ``benchmark`` and
  ``aggregate_annotations`` mode runners.

See ``docs/CONVENTIONS.md`` for the payload + REPORT# contracts and
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
