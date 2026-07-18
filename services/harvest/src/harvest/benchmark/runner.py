"""``run_benchmark`` tool factory — assembles the black box into one agent tool.

The supervisor sees exactly one opaque tool. Each call runs ONE stateless
benchmark round against the *current* authored bundle and returns the gold-free /
question-free public dict (the aggregated-feedback boundary). Per call it:

1. snapshots the authored bundle into a fresh temp dir (bundle-blind solver root);
2. fans out solvers (concurrency ``OKF_BENCHMARK_MAX_CONCURRENCY``) → grades
   (deterministic Athena EX, discards excluded) → adjudicates FAILs;
3. persists a ``BENCH#`` KPI row + emits an ``OKF_STEP kind:"benchmark"`` live
   event;
4. returns ``{iteration, ex_score, judge_accuracy, passed, failed, discarded,
   graded, threshold_met, improvements}``.

The iteration counter lives on the factory closure (one per run). The guard
middleware independently caps the call count as a backstop (see ``okf_guard``).
Agent-framework imports are deferred so the module imports in the test venv; the
round engine (:func:`harvest.benchmark.tool.run_round`) is injected for tests.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Awaitable, Callable

from harvest.benchmark.grader import Grader
from harvest.benchmark.questions import BenchmarkQuestion
from harvest.benchmark.snapshot import snapshot_bundle
from harvest.benchmark.tool import RoundResult, run_round

# Default solver fan-out width; see docs/CONVENTIONS.md OKF_BENCHMARK_MAX_CONCURRENCY.
_DEFAULT_CONCURRENCY = 10


def _concurrency() -> int:
    try:
        return max(1, int(os.environ.get("OKF_BENCHMARK_MAX_CONCURRENCY", "")))
    except (TypeError, ValueError):
        return _DEFAULT_CONCURRENCY


class BenchmarkSession:
    """Per-run benchmark state behind the ``run_benchmark`` tool.

    Holds the loaded questions, the config, the injected solver/grader/adjudicator
    factories, and the round counter. One instance per harvest run; ``run_next``
    advances one round each call. Also records every round's :class:`RoundResult`
    so the runner can pick the best-scoring checkpoint at finalize.
    """

    def __init__(
        self,
        *,
        data_domain: str,
        dataset: str,
        dataset_root: str,
        runtime_session_id: str,
        config: dict,
        questions: list[BenchmarkQuestion],
        make_solver: Callable[[str], Callable[[str], Awaitable[str]]],
        grader: Grader,
        adjudicate: Callable[[list[Any]], Awaitable[Any]],
        persist_kpi: Callable[[int | str, dict], None] | None = None,
        emit_event: Callable[[dict], None] | None = None,
        concurrency: int | None = None,
    ):
        self.data_domain = data_domain
        self.dataset = dataset
        self.dataset_root = dataset_root
        self.runtime_session_id = runtime_session_id
        self.config = config
        self.questions = questions
        self._make_solver = make_solver
        self._grader = grader
        self._adjudicate = adjudicate
        self._persist_kpi = persist_kpi
        self._emit_event = emit_event
        self._concurrency = concurrency or _concurrency()
        self._iteration = 0
        self.rounds: list[RoundResult] = []

    async def run_next(self) -> dict:
        """Run one stateless round against the current bundle; return the public dict."""
        iteration = self._iteration
        self._iteration += 1

        # Fresh bundle-only snapshot each round: the solver is physically confined
        # to the authored docs as they stand right now (no dot-dirs).
        snap_dir = tempfile.mkdtemp(prefix=f"okf-bench-{iteration}-")
        try:
            snapshot_bundle(self.dataset_root, snap_dir)
            solve = self._make_solver(snap_dir)
            result = await run_round(
                iteration=iteration,
                questions=self.questions,
                config=self.config,
                solve=solve,
                grader=self._grader,
                adjudicate=self._adjudicate,
                concurrency=self._concurrency,
                runtime_session_id=self.runtime_session_id,
            )
        finally:
            _rmtree_quiet(snap_dir)

        self.rounds.append(result)
        self._persist(result)
        return result.to_public_dict()

    def best_round(self) -> RoundResult | None:
        """The highest-EX round so far (ties → earliest), for checkpoint selection."""
        if not self.rounds:
            return None
        return max(self.rounds, key=lambda r: (r.ex_score, -r.iteration))

    def _persist(self, result: RoundResult) -> None:
        attrs = result.to_kpi_attrs(self.runtime_session_id)
        if self._persist_kpi is not None:
            self._persist_kpi(result.iteration, attrs)
        if self._emit_event is not None:
            self._emit_event({"kind": "benchmark", **result.to_public_dict()})


def _rmtree_quiet(path: str) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - cleanup is best-effort
        pass


def make_run_benchmark_tool(session: BenchmarkSession) -> Any:
    """A LangChain ``run_benchmark`` tool that advances the session one round."""
    from langchain_core.tools import tool

    @tool
    def run_benchmark() -> dict:
        """Benchmark the CURRENT wiki against the configured question set, once.

        Runs the whole question set through independent solvers that may read ONLY
        the wiki (not the raw schema), grades each answer's SQL against the data,
        and returns a score plus a consolidated, anonymous list of what the wiki is
        missing or should improve. Call it, read `improvements`, revise the wiki
        docs to address those themes, then call it again — repeat until
        `threshold_met` is true or you've used your iteration budget. It returns:
        `{iteration, ex_score, judge_accuracy, passed, failed, discarded, graded,
        threshold_met, improvements}`. You never see the questions or the expected
        answers — only the aggregated feedback. The best-scoring iteration is kept
        automatically, so it is safe to keep improving.
        """
        return asyncio.run(session.run_next())

    return run_benchmark
