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
    advances one round each call, recording each round's :class:`RoundResult`.

    The AGENT owns the bundle: it revises docs between rounds and decides when to
    stop. There is deliberately NO checkpoint/restore here — the benchmark reads
    the live bundle (via a throwaway per-round snapshot) and NEVER writes to or
    rolls back the mount. Whatever the agent authored is what ships.
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
        """Run one stateless round against the CURRENT live bundle; return the dict.

        Each round makes a throwaway bundle-only snapshot for the solver to read
        (physically confined to the authored docs as they stand now — no dot-dirs),
        and ALWAYS deletes it afterward. The snapshot is a read copy for the
        examiner, never a checkpoint: the benchmark does not modify or roll back
        the mount.
        """
        iteration = self._iteration
        self._iteration += 1

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
                progress=self._make_progress(iteration),
            )
        finally:
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
        answers — only the aggregated feedback. IMPORTANT: the wiki ships EXACTLY
        as you leave it — there is no automatic rollback to a best round. If an
        edit lowers the score, fix or revert it before you finish; don't end on a
        worse version than you already had.
        """
        return asyncio.run(session.run_next())

    return run_benchmark
