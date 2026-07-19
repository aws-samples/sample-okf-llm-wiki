"""Deterministic EX grader: PASS/FAIL/DISCARDED, set-equality, caching."""

from __future__ import annotations

from harvest.benchmark.grader import Grader, Outcome


class FakeAthena:
    """Maps a SQL string to either a row list or an Exception to raise.

    Counts executions per SQL so tests can assert the gold/prediction caches
    actually avoid re-running queries.
    """

    def __init__(self, responses):
        self._responses = responses
        self.calls: dict[str, int] = {}

    def execute(self, sql):
        self.calls[sql] = self.calls.get(sql, 0) + 1
        resp = self._responses[sql]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _rows(*tuples):
    # Emulate the source's header-keyed row dicts.
    return [{"c0": t[0], "c1": t[1]} if len(t) == 2 else {"c0": t[0]} for t in tuples]


def test_pass_on_set_equal():
    fake = FakeAthena(
        {"GOLD": _rows(("a", 1), ("b", 2)), "PRED": _rows(("b", 2), ("a", 1))}
    )
    g = Grader(fake.execute)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.PASS  # row order doesn't matter
    assert r.pred_rowcount == 2 and r.gold_rowcount == 2


def test_fail_on_set_difference():
    fake = FakeAthena({"GOLD": _rows(("a", 1)), "PRED": _rows(("a", 2))})
    g = Grader(fake.execute)
    r = g.grade(1, "GOLD", "PRED")
    assert r.outcome is Outcome.FAIL
    assert "differ" in r.reason


def test_column_order_within_row_ignored():
    # Same values, different SELECT column order → still PASS (value-set compare).
    fake = FakeAthena(
        {
            "GOLD": [{"name": "x", "n": "5"}],
            "PRED": [{"n": "5", "name": "x"}],
        }
    )
    g = Grader(fake.execute)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS


def test_null_distinguished_from_empty_string():
    fake = FakeAthena({"GOLD": [{"c": None}], "PRED": [{"c": ""}]})
    g = Grader(fake.execute)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.FAIL


def test_discarded_when_gold_errors():
    fake = FakeAthena(
        {"GOLD": RuntimeError("COLUMN_NOT_FOUND: refund_flag"), "PRED": _rows(("a",))}
    )
    g = Grader(fake.execute)
    r = g.grade(3, "GOLD", "PRED")
    assert r.outcome is Outcome.DISCARDED
    assert "refund_flag" in r.discard_reason
    # Predicted must NOT even run when gold is unrunnable.
    assert "PRED" not in fake.calls


def test_fail_when_predicted_errors_but_gold_ok():
    fake = FakeAthena({"GOLD": _rows(("a",)), "PRED": RuntimeError("SYNTAX_ERROR")})
    g = Grader(fake.execute)
    r = g.grade(4, "GOLD", "PRED")
    assert r.outcome is Outcome.FAIL
    assert "SYNTAX_ERROR" in r.reason


def test_empty_prediction_is_fail_not_discard():
    fake = FakeAthena({"GOLD": _rows(("a",))})
    g = Grader(fake.execute)
    r = g.grade(5, "GOLD", "  ")
    assert r.outcome is Outcome.FAIL
    assert "empty" in r.reason


def test_gold_cache_runs_gold_once_across_rounds():
    fake = FakeAthena({"GOLD": _rows(("a",)), "P1": _rows(("a",)), "P2": _rows(("b",))})
    g = Grader(fake.execute)
    g.grade(0, "GOLD", "P1")  # round 1
    g.grade(0, "GOLD", "P2")  # round 2, same gold, changed prediction
    assert fake.calls["GOLD"] == 1  # gold executed once, reused


def test_prediction_cache_skips_identical_pred():
    fake = FakeAthena({"GOLD": _rows(("a",)), "PRED": _rows(("a",))})
    g = Grader(fake.execute)
    g.grade(0, "GOLD", "PRED")
    g.grade(0, "GOLD", "PRED")  # identical → cached
    assert fake.calls["PRED"] == 1


def test_discard_cache_not_rerun():
    fake = FakeAthena({"GOLD": RuntimeError("boom")})
    g = Grader(fake.execute)
    g.grade(0, "GOLD", "P1")
    g.grade(0, "GOLD", "P2")
    assert fake.calls["GOLD"] == 1  # a dead gold is memoized, never re-run


def test_gold_rows_and_sql_never_on_result():
    # The QuestionResult must not carry gold SQL or gold rows (only pred-side).
    fake = FakeAthena({"GOLD": _rows(("secret", 9)), "PRED": _rows(("x", 1))})
    g = Grader(fake.execute)
    r = g.grade(0, "GOLD", "PRED")
    blob = repr(r)
    assert "GOLD" not in blob and "secret" not in blob
