"""Adjudicator: concurrent classification, progress ticks, gold-free themes."""

from __future__ import annotations

import asyncio

from harvest.benchmark.adjudicator import make_adjudicator
from harvest.benchmark.grader import Outcome, QuestionResult


class _FakeReActAgent:
    """Stands in for create_react_agent's classifier: returns a fenced-JSON verdict
    per question, and records max concurrency so we can assert the fan-out."""

    def __init__(self, verdict_for, tracker):
        self._verdict_for = verdict_for
        self._t = tracker

    async def ainvoke(self, state):
        self._t["now"] += 1
        self._t["max"] = max(self._t["max"], self._t["now"])
        await asyncio.sleep(0.01)
        self._t["now"] -= 1
        case = state["messages"][0][1]
        verdict = self._verdict_for(case)
        return {"messages": [_AI(f"```json\n{verdict}\n```")]}


class _AI:
    type = "ai"

    def __init__(self, content):
        self.content = content


class _FakeModel:
    """The shared chat_model: only the consolidator ainvoke path is exercised."""

    async def ainvoke(self, messages):
        # The consolidator prompt asks for {"improvements": [...]}.
        return _AI('```json\n{"improvements": ["document the status int code"]}\n```')


def _fail(qid, sql="SELECT 1"):
    return QuestionResult(q_id=qid, outcome=Outcome.FAIL, predicted_sql=sql, reason="differ")


def _make(verdict_for, tracker, monkeypatch):
    # Patch create_react_agent (imported lazily in _ensure_built) to our fake.
    import harvest.benchmark.adjudicator as adj

    fake = _FakeReActAgent(verdict_for, tracker)
    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent", lambda *a, **k: fake, raising=False
    )
    return adj.make_adjudicator(_FakeModel(), raw_data_tools=[])


def test_classifies_genuine_vs_noisy(monkeypatch):
    tracker = {"now": 0, "max": 0}

    def verdict_for(case):
        # Odd q_ids → genuine gap; even → noisy gold.
        return (
            '{"category": "GENUINE_ERROR", "gap": "docs miss X"}'
            if "SELECT 1" in case
            else '{"category": "NOISY_GOLD", "gap": ""}'
        )

    adjudicate = _make(verdict_for, tracker, monkeypatch)
    fails = [_fail(0, "SELECT 1"), _fail(1, "SELECT 2"), _fail(2, "SELECT 1")]
    res = asyncio.run(adjudicate(fails))
    assert res.genuine_error_count == 2  # the two "SELECT 1" cases
    assert res.noisy_or_ambiguous == 1
    assert res.improvements == ["document the status int code"]


def test_adjudication_runs_concurrently(monkeypatch):
    tracker = {"now": 0, "max": 0}
    adjudicate = _make(
        lambda case: '{"category": "NOISY_GOLD", "gap": ""}', tracker, monkeypatch
    )
    fails = [_fail(i, "SELECT 2") for i in range(20)]
    asyncio.run(adjudicate(fails))
    # Bounded fan-out, but definitely NOT sequential (which would be max=1).
    assert tracker["max"] > 1


def test_progress_ticks_per_completion(monkeypatch):
    tracker = {"now": 0, "max": 0}
    adjudicate = _make(
        lambda case: '{"category": "NOISY_GOLD", "gap": ""}', tracker, monkeypatch
    )
    ticks = []
    fails = [_fail(i) for i in range(5)]
    asyncio.run(adjudicate(fails, on_progress=lambda done, total: ticks.append((done, total))))
    assert len(ticks) == 5  # one per failure
    assert ticks[-1] == (5, 5)
    assert [d for d, _ in ticks] == sorted(d for d, _ in ticks)  # monotonic


def test_no_fails_is_empty(monkeypatch):
    adjudicate = _make(lambda case: "{}", {"now": 0, "max": 0}, monkeypatch)
    res = asyncio.run(adjudicate([]))
    assert res.genuine_error_count == 0 and res.improvements == []


def test_classifier_error_degrades_to_ambiguous(monkeypatch):
    class _BoomAgent:
        async def ainvoke(self, state):
            raise RuntimeError("model down")

    import harvest.benchmark.adjudicator as adj

    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent",
        lambda *a, **k: _BoomAgent(),
        raising=False,
    )
    adjudicate = adj.make_adjudicator(_FakeModel(), raw_data_tools=[])
    res = asyncio.run(adjudicate([_fail(0), _fail(1)]))
    # A crashing classifier is not a wiki gap and does not crash the round.
    assert res.genuine_error_count == 0
    assert res.noisy_or_ambiguous == 2
