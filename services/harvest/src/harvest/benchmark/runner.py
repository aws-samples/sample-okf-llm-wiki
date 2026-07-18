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
        # The retained snapshot dir of the best-scoring round so far, for
        # checkpoint restore before finalize. A later round can regress the bundle;
        # keeping the best round's bundle-only snapshot lets the runner roll back to
        # it. Non-best snapshots are deleted as we go.
        self._best_snapshot: str | None = None
        self._best_key: tuple[float, int] | None = None

    async def run_next(self) -> dict:
        """Run one stateless round against the current bundle; return the public dict."""
        iteration = self._iteration
        self._iteration += 1

        # Fresh bundle-only snapshot each round: the solver is physically confined
        # to the authored docs as they stand right now (no dot-dirs). The snapshot
        # doubles as the round's checkpoint (the bundle that produced this score).
        snap_dir = tempfile.mkdtemp(prefix=f"okf-bench-{iteration}-")
        keep_snapshot = False
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
                progress=self._make_progress(iteration),
            )
            keep_snapshot = self._maybe_checkpoint(result, snap_dir)
        finally:
            if not keep_snapshot:
                _rmtree_quiet(snap_dir)

        self.rounds.append(result)
        self._persist(result)
        return result.to_public_dict()

    def _max_iterations(self) -> int:
        from okf_core.recursive_improvement import FIELD_MAX_ITERATIONS, MAX_ITERATIONS

        return int(self.config.get(FIELD_MAX_ITERATIONS, MAX_ITERATIONS))

    def _make_progress(self, iteration: int):
        """A (phase, current, total) callback that emits benchmark_progress events.

        Best-effort: a feed emission must never break a round, so a failing
        emit_event is swallowed. No emitter → a no-op callback. The event carries
        an in-place-update key (iteration+phase) plus a human label the UI shows.
        """
        if self._emit_event is None:
            return None
        max_iter = self._max_iterations()

        def progress(phase: str, current: int, total: int) -> None:
            label = (
                f"Benchmark round {iteration + 1}/{max_iter} — {phase} "
                f"{current}/{total}"
            )
            try:
                self._emit_event(
                    {
                        "kind": "benchmark_progress",
                        "label": label,
                        "phase": phase,
                        "iteration": iteration,
                        "max_iterations": max_iter,
                        "current": current,
                        "total": total,
                    }
                )
            except Exception:  # noqa: BLE001 - progress is best-effort
                pass

        return progress

    def _maybe_checkpoint(self, result: RoundResult, snap_dir: str) -> bool:
        """Retain ``snap_dir`` iff this round is the new best; free the prior best.

        Best = highest EX, ties broken toward the EARLIEST round (a later tie
        doesn't displace an equal earlier checkpoint). Returns True if the caller
        should keep ``snap_dir`` (it became the checkpoint).
        """
        key = (result.ex_score, -result.iteration)
        if self._best_key is None or key > self._best_key:
            if self._best_snapshot:
                _rmtree_quiet(self._best_snapshot)
            self._best_snapshot = snap_dir
            self._best_key = key
            return True
        return False

    @property
    def best_snapshot(self) -> str | None:
        """The retained snapshot dir of the best round (for restore), or None."""
        return self._best_snapshot

    def best_round(self) -> RoundResult | None:
        """The highest-EX round so far (ties → earliest), for checkpoint selection."""
        if not self.rounds:
            return None
        return max(self.rounds, key=lambda r: (r.ex_score, -r.iteration))

    def persist_final(self, shipped: RoundResult) -> None:
        """Write the terminal BENCH#…#final KPI row for the shipped iteration.

        Records the shipped round's KPIs plus ``shipped_iteration`` (which round's
        checkpoint finalize restored), so the durable record shows both the
        trajectory (per-round rows) and what actually shipped.
        """
        if self._persist_kpi is None:
            return
        from okf_core.recursive_improvement import FINAL_ITERATION

        attrs = shipped.to_kpi_attrs(self.runtime_session_id)
        attrs["shipped_iteration"] = shipped.iteration
        self._persist_kpi(FINAL_ITERATION, attrs)

    def cleanup(self) -> None:
        """Delete any retained checkpoint snapshot (call after finalize/restore)."""
        if self._best_snapshot:
            _rmtree_quiet(self._best_snapshot)
            self._best_snapshot = None

    def _persist(self, result: RoundResult) -> None:
        attrs = result.to_kpi_attrs(self.runtime_session_id)
        if self._persist_kpi is not None:
            self._persist_kpi(result.iteration, attrs)
        if self._emit_event is not None:
            max_iter = self._max_iterations()
            pub = result.to_public_dict()
            met = "threshold met" if result.threshold_met else "below threshold"
            label = (
                f"Benchmark round {result.iteration + 1}/{max_iter} done — "
                f"EX {pub['ex_score']:.2f}, judge {pub['judge_accuracy']:.2f} "
                f"({result.passed}/{result.graded} passed, "
                f"{result.discarded} discarded) — {met}"
            )
            self._emit_event(
                {
                    "kind": "benchmark",
                    "label": label,
                    "phase": "done",
                    "max_iterations": max_iter,
                    **pub,
                }
            )


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
