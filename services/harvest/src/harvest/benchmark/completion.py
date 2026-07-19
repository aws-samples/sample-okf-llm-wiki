"""Compel the benchmark loop from *inside* the agent graph.

The runner's ``_finish_benchmark`` is a POST-HOC compel check: if an RI-enabled
run finished without any benchmark round it raises and the run is reported failed.
That catches "never benchmarked" but only after the agent has already stopped — it
can't nudge the agent to *keep going* when it stops early (benchmarked once, didn't
meet the target, but still had iteration budget left).

``BenchmarkCompletionMiddleware`` closes that gap live. It hooks ``after_model`` on
the MAIN supervisor (never the subagents — only the supervisor decides when the
whole run ends) and, when the agent tries to END the run (a model turn with no tool
calls) before the benchmark requirements are satisfied, it injects a human message
and jumps back to the model so the agent must continue.

The requirements, exactly as specified:

* RI must be **enabled** for this run (else the middleware isn't even attached — a
  normal harvest is untouched).
* The benchmark must have **run at least once**.
* The agent may finish once the **latest round met the target** (``target_met``)
  — no further rounds are needed — OR the **iteration budget is exhausted**
  (``rounds_run >= max_iterations``; the guard would refuse more ``run_benchmark``
  calls anyway, so forcing more would livelock).
* Otherwise (ran, didn't pass, budget remains) the agent is re-prompted to revise
  and re-benchmark.

The decision logic is a pure, dependency-free :class:`BenchmarkCompletionPolicy`
(fully offline-testable, mirroring ``OKFGuardEngine``); the middleware is a thin
langchain adapter over it (mirroring ``OKFGuardMiddleware``). A loop-safety cap
stops re-prompting if the agent keeps ignoring the nudge, so a stuck model can
never spin against the recursion limit — the post-hoc compel check remains the
final backstop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("harvest.benchmark.completion")

try:  # deepagents / langchain are only present in the runtime image
    from langchain.agents.middleware import AgentMiddleware, hook_config
    from langchain.messages import HumanMessage

    _HAVE_LANGCHAIN = True
except Exception:  # pragma: no cover - exercised only when langchain is absent
    AgentMiddleware = object  # type: ignore[assignment,misc]
    HumanMessage = None  # type: ignore[assignment]

    def hook_config(*_a: Any, **_k: Any):  # type: ignore[misc]
        """No-op stand-in so the class body imports where langchain is absent."""

        def _decorate(fn):
            return fn

        return _decorate

    _HAVE_LANGCHAIN = False

# Loop-safety. Even a well-behaved run re-prompts at most a few times (once per
# revise→re-benchmark cycle). These cap a MISbehaving agent that keeps trying to
# end without ever running a new round, so it can't spin against the recursion
# limit — the runner's post-hoc compel check is the final backstop.
_MAX_STUCK_REPROMPTS = 3  # consecutive nudges with NO new round → give up
_REPROMPT_GRACE = 5  # absolute ceiling = max_iterations + this


@dataclass
class CompletionDecision:
    """Outcome of one ``after_model`` evaluation.

    ``enforce`` True → inject ``message`` and loop back to the model; False → let
    the agent proceed (either it's still working, or it's allowed to finish).
    ``reason`` is for server-side logging only.
    """

    enforce: bool
    message: str = ""
    reason: str = ""


class BenchmarkCompletionPolicy:
    """Pure decision logic for compelling the in-run benchmark loop.

    Dependency-free so it's fully offline-testable. Holds the small amount of
    loop-safety state (re-prompt counters) across ``decide`` calls within one run;
    the middleware constructs exactly one per run, and main-supervisor turns are
    sequential, so no locking is needed.
    """

    def __init__(self, *, enabled: bool, max_iterations: int):
        self.enabled = bool(enabled)
        self.max_iterations = max(0, int(max_iterations))
        # Loop-safety across decide() calls.
        self._reprompts = 0
        self._stuck = 0
        self._last_rounds = -1

    def _max_reprompts(self) -> int:
        return self.max_iterations + _REPROMPT_GRACE

    def decide(
        self,
        *,
        is_final_turn: bool,
        rounds_run: int,
        latest_passed: bool,
    ) -> CompletionDecision:
        """Decide whether to compel another benchmark cycle.

        ``is_final_turn`` — the model just produced a tool-call-free message (it's
        trying to end the run). ``rounds_run`` — benchmark rounds completed so far.
        ``latest_passed`` — the most recent round met the target.
        """
        if not self.enabled:
            return CompletionDecision(False, reason="ri-disabled")
        # Still working (emitted tool calls) — never interfere mid-flight.
        if not is_final_turn:
            return CompletionDecision(False, reason="not-final-turn")
        # Target met on the latest round → no further rounds needed; let it finish.
        if latest_passed:
            return CompletionDecision(False, reason="target-met")
        # Budget spent → the guard refuses more run_benchmark calls anyway, so
        # forcing another round would livelock. Allow the run to finalize.
        if rounds_run >= self.max_iterations:
            return CompletionDecision(False, reason="budget-exhausted")

        # Ran (or not) but didn't pass and budget remains → compel another cycle,
        # unless the agent is ignoring us (loop-safety).
        if rounds_run == self._last_rounds:
            self._stuck += 1
        else:
            self._stuck = 0
        self._last_rounds = rounds_run
        self._reprompts += 1
        if self._stuck >= _MAX_STUCK_REPROMPTS or self._reprompts > self._max_reprompts():
            # The agent keeps trying to end without benchmarking; stop nudging so
            # we don't burn the recursion limit. The post-hoc compel check still
            # fails the run if zero rounds ever ran.
            log.warning(
                "Benchmark completion: re-prompt cap reached (reprompts=%d, "
                "stuck=%d, rounds_run=%d); ceasing enforcement.",
                self._reprompts,
                self._stuck,
                rounds_run,
            )
            return CompletionDecision(False, reason="reprompt-cap")

        return CompletionDecision(
            True, message=self._message(rounds_run), reason="incomplete"
        )

    def _message(self, rounds_run: int) -> str:
        """The human message injected to steer the agent — aggregate state only.

        References only iteration counts / met-or-not (never gold or questions), so
        it preserves the gold-blindness boundary."""
        if rounds_run <= 0:
            return (
                "Recursive improvement is ENABLED for this run, but you have not run "
                "the benchmark yet — you cannot finish until you have. Call the "
                "`run_benchmark` tool now; then read its `improvements`, revise the "
                "wiki docs to address them, and repeat. Keep going until "
                "`target_met` is true or you have used all "
                f"{self.max_iterations} benchmark iteration(s). Do not end the run yet."
            )
        remaining = self.max_iterations - rounds_run
        return (
            "The latest benchmark round did NOT meet the target, and you still have "
            f"{remaining} of {self.max_iterations} benchmark iteration(s) left. Do "
            "not end the run yet: revise the wiki docs to address the `improvements` "
            "from your last `run_benchmark` result, then call `run_benchmark` again. "
            "Finish only once `target_met` is true or your iteration budget is spent."
        )


def _is_final_ai_turn(msg: Any) -> bool:
    """True iff ``msg`` is an AI message with NO tool calls (the agent is ending).

    A message with tool calls routes to the tools node (still working); a
    tool-call-free AI message routes to the graph end — that's when we evaluate."""
    if msg is None:
        return False
    if getattr(msg, "type", "") not in ("ai", "assistant"):
        return False
    return not getattr(msg, "tool_calls", None)


class BenchmarkCompletionMiddleware(AgentMiddleware):  # type: ignore[misc]
    """Adapter: enforce :class:`BenchmarkCompletionPolicy` via ``after_model``.

    Attach to the MAIN supervisor only (never subagents — the supervisor owns the
    run's end). ``session`` is the per-run ``BenchmarkSession`` whose ``rounds`` are
    inspected each turn. When the policy says to compel, we return a ``jump_to:
    model`` state update plus an injected ``HumanMessage`` — the supported LangGraph
    way to loop the agent back to the model with new steering (see API_REFERENCE §1;
    ``hook_config(can_jump_to=["model"])`` establishes the conditional edge).

    Sync-only ``after_model`` is deliberate: the main supervisor runs via sync
    ``.stream()``/``.invoke()`` (see ``runner._run_agent``), and ``RunnableCallable``
    falls back to the sync hook even under async — so unlike ``OKFGuardMiddleware``
    (which needs an async ``awrap_tool_call`` for the concurrent subagent fan-out),
    no async variant is required here.
    """

    def __init__(self, policy: BenchmarkCompletionPolicy, session: Any):
        super().__init__()
        self.policy = policy
        self.session = session

    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime):  # type: ignore[override]
        messages = (state or {}).get("messages") or []
        last = messages[-1] if messages else None
        rounds = list(getattr(self.session, "rounds", None) or [])
        decision = self.policy.decide(
            is_final_turn=_is_final_ai_turn(last),
            rounds_run=len(rounds),
            latest_passed=bool(rounds and getattr(rounds[-1], "target_met", False)),
        )
        if not decision.enforce:
            return None
        log.info(
            "Benchmark completion: re-prompting the agent to continue (%s).",
            decision.reason,
        )
        return {"jump_to": "model", "messages": [HumanMessage(content=decision.message)]}
