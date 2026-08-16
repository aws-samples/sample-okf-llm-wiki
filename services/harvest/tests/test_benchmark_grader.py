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


def test_transposed_values_within_row_fail():
    # BIRD compares rows POSITIONALLY: the same values in swapped columns is a
    # different answer (gold ('x','5') vs predicted ('5','x') must NOT pass —
    # per-row value sorting used to admit exactly this false PASS).
    fake = FakeAthena(
        {
            "GOLD": [{"name": "x", "n": "5"}],
            "PRED": [{"n": "5", "name": "x"}],
        }
    )
    g = Grader(fake.execute)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.FAIL


def test_numeric_strings_compare_by_value():
    # Athena stringifies every cell; BIRD compares native values where
    # 3 == 3.0. A COUNT(*) gold vs an equivalent SUM prediction must PASS.
    fake = FakeAthena(
        {
            "GOLD": [{"c": "3", "avg": "2.50"}],
            "PRED": [{"c": "3.0", "avg": "2.5"}],
        }
    )
    g = Grader(fake.execute)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS


def test_numeric_lookalikes_stay_strings():
    # '007' is an identifier, not the number 7 (Athena never renders a numeric
    # cell with leading zeros); 'NaN' must not become Decimal NaN (NaN != NaN
    # would make identical result sets compare unequal).
    fake = FakeAthena(
        {
            "GOLD": [{"c": "007"}],
            "PRED": [{"c": "7"}],
            "NAN_G": [{"c": "NaN"}],
            "NAN_P": [{"c": "NaN"}],
        }
    )
    g = Grader(fake.execute)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.FAIL
    assert g.grade(1, "NAN_G", "NAN_P").outcome is Outcome.PASS


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


def test_discard_cache_not_rerun_for_semantic_errors():
    # SEMANTIC gold failures (bad column, syntax) are deterministic → memoized.
    # Transient faults are deliberately NOT (see the transient tests below).
    fake = FakeAthena({"GOLD": RuntimeError("COLUMN_NOT_FOUND: boom")})
    g = Grader(fake.execute)
    r1 = g.grade(0, "GOLD", "P1")
    r2 = g.grade(0, "GOLD", "P2")
    assert fake.calls["GOLD"] == 1  # a dead gold is memoized, never re-run
    assert r1.outcome is Outcome.DISCARDED and r2.outcome is Outcome.DISCARDED


# -- transient vs semantic classification (retry, no memoization) ---------------


class _ThrottleError(Exception):
    """A botocore-shaped ClientError carrying a transient error code."""

    def __init__(self, code="ThrottlingException", msg="rate exceeded"):
        super().__init__(msg)
        self.response = {"Error": {"Code": code}}


class ScriptedExecute:
    """Consumes a per-SQL list of results/exceptions in order (last repeats)."""

    def __init__(self, script):
        self._script = {k: list(v) for k, v in script.items()}
        self.calls: dict[str, int] = {}

    def set(self, sql, seq):
        self._script[sql] = list(seq)

    def __call__(self, sql):
        self.calls[sql] = self.calls.get(sql, 0) + 1
        seq = self._script[sql]
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item


def test_transient_gold_error_retries_then_succeeds():
    rows = [["1"]]
    fake = ScriptedExecute(
        {"GOLD": [_ThrottleError(), _ThrottleError(), rows], "PRED": [rows]}
    )
    slept = []
    g = Grader(fake, sleep=slept.append)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS
    assert fake.calls["GOLD"] == 3  # two retries with backoff, then success
    assert slept == [1.0, 2.0]


def test_transient_gold_failure_is_classified_and_not_sticky():
    fake = ScriptedExecute({"GOLD": [_ThrottleError()], "PRED": [[["1"]]]})
    g = Grader(fake, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    # Still DISCARDED (this grade couldn't happen) — but clearly classified,
    # never conflated with "gold doesn't execute".
    assert r.outcome is Outcome.DISCARDED
    assert "grading unavailable" in r.reason
    assert "ThrottlingException" in r.discard_reason
    assert fake.calls["GOLD"] == 3  # the full retry budget was spent
    # The blip is NOT memoized: once the service recovers, gold re-executes.
    fake.set("GOLD", [[["1"]]])
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS
    assert fake.calls["GOLD"] == 4


def test_transient_pred_failure_is_classified_and_not_sticky():
    fake = ScriptedExecute({"GOLD": [[["1"]]], "PRED": [TimeoutError("timed out")]})
    g = Grader(fake, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.FAIL
    assert r.reason.startswith("grading unavailable")
    assert fake.calls["PRED"] == 3
    fake.set("PRED", [[["1"]]])
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS  # not memoized


def test_semantic_pred_error_never_retries():
    fake = ScriptedExecute({"GOLD": [[["1"]]], "PRED": [RuntimeError("SYNTAX_ERROR")]})
    g = Grader(fake, sleep=lambda _s: None)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.FAIL
    assert fake.calls["PRED"] == 1  # deterministic → no retry, memoized
    g.grade(1, "GOLD", "PRED")
    assert fake.calls["PRED"] == 1


def test_transient_code_embedded_in_message_is_recognized():
    # An Athena FAILED state folds the underlying code into its reason text.
    fake = ScriptedExecute(
        {"GOLD": [[["1"]]],
         "PRED": [RuntimeError("Athena query FAILED: SlowDown, please retry")]}
    )
    g = Grader(fake, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert "grading unavailable" in r.reason
    assert fake.calls["PRED"] == 3


def test_botocore_network_fault_is_transient_and_never_crashes_the_classifier():
    # ReadTimeoutError is not a builtin TimeoutError and carries response=None
    # — the classifier used to raise AttributeError inside the retry handler,
    # memoizing a one-off network blip as a sticky DISCARD for all N runs.
    import botocore.exceptions as be

    fake = ScriptedExecute(
        {"GOLD": [be.ReadTimeoutError(endpoint_url="x")], "PRED": [[["1"]]]}
    )
    g = Grader(fake, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.DISCARDED
    assert "grading unavailable" in r.reason
    assert fake.calls["GOLD"] == 3  # transient → the full retry budget
    # Run 1 must NOT be poisoned: the blip was never memoized.
    fake.set("GOLD", [[["1"]]])
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS


def test_client_error_code_decides_transient_vs_semantic():
    from botocore.exceptions import ClientError

    def client_error(code):
        return ClientError(
            {"Error": {"Code": code, "Message": "m"}}, "StartQueryExecution"
        )

    throttled = ScriptedExecute(
        {"GOLD": [client_error("ThrottlingException")], "PRED": [[["1"]]]}
    )
    g = Grader(throttled, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.DISCARDED
    assert "grading unavailable" in r.reason
    assert throttled.calls["GOLD"] == 3  # a transient code retries

    denied = ScriptedExecute(
        {"GOLD": [client_error("AccessDeniedException")], "PRED": [[["1"]]]}
    )
    g = Grader(denied, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.DISCARDED
    assert "grading unavailable" not in r.reason  # a real ruling, not a blip
    assert denied.calls["GOLD"] == 1  # deterministic → no retry
    g.grade(1, "GOLD", "P2")
    assert denied.calls["GOLD"] == 1  # and memoized


def test_coded_error_quoting_a_transient_name_stays_semantic():
    # The code is authoritative: a deterministic failure whose MESSAGE merely
    # quotes 'ThrottlingException' must not be retried as transient.
    from botocore.exceptions import ClientError

    err = ClientError(
        {"Error": {"Code": "InvalidRequestException",
                   "Message": "line 1: column 'ThrottlingException' not found"}},
        "StartQueryExecution",
    )
    fake = ScriptedExecute({"GOLD": [[["1"]]], "PRED": [err]})
    g = Grader(fake, sleep=lambda _s: None)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.FAIL
    assert fake.calls["PRED"] == 1  # no retry, memoized


def test_message_lookalike_identifier_stays_semantic():
    # Word-boundary matching: an identifier that merely CONTAINS a transient
    # name is not a transient fault.
    fake = ScriptedExecute(
        {"GOLD": [[["1"]]],
         "PRED": [RuntimeError("table ThrottlingExceptionLog missing")]}
    )
    g = Grader(fake, sleep=lambda _s: None)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.FAIL
    assert fake.calls["PRED"] == 1


# -- row cap (deterministic, classified) -----------------------------------------


def test_row_cap_on_gold_discards_with_cap_reason():
    from harvest.source_base import ResultCapExceeded

    fake = ScriptedExecute(
        {"GOLD": [ResultCapExceeded("result exceeds 50000 rows")], "PRED": [[["1"]]]}
    )
    g = Grader(fake, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.DISCARDED
    assert r.reason == "gold result exceeds 50000 rows"
    assert fake.calls["GOLD"] == 1  # deterministic → no retry
    g.grade(1, "GOLD", "PRED")
    assert fake.calls["GOLD"] == 1  # and memoized


def test_row_cap_on_prediction_fails_with_cap_reason():
    from harvest.source_base import ResultCapExceeded

    fake = ScriptedExecute(
        {"GOLD": [[["1"]]], "PRED": [ResultCapExceeded("result exceeds 50000 rows")]}
    )
    g = Grader(fake, sleep=lambda _s: None)
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.FAIL
    assert r.reason == "predicted result exceeds 50000 rows"


# -- positional rows (the production shape) ---------------------------------------


def test_positional_rows_compare_positionally():
    fake = FakeAthena({"GOLD": [["a", "1"], ["b", "2"]], "PRED": [["b", "2"], ["a", "1"]]})
    g = Grader(fake.execute)
    assert g.grade(0, "GOLD", "PRED").outcome is Outcome.PASS


def test_duplicate_select_labels_fail_through_real_collection_path():
    # `SELECT r.name, c.name` — dict rows keyed by header collapse the two
    # columns (last value wins), which made a one-column prediction PASS
    # falsely. Through the REAL Athena collection path (positional rows), the
    # missing column must FAIL.
    from harvest.benchmark.studio import _grading_execute
    from harvest.glue_source import GlueAthenaSource
    from tests.fakes import QueryKeyedAthena, f1_like_glue

    athena = QueryKeyedAthena(
        {
            "GOLD": (["name", "name"], [["hamilton", "mercedes"]]),
            "PRED": (["name"], [["mercedes"]]),
        }
    )
    src = GlueAthenaSource(database="db", glue=f1_like_glue(), athena=athena)
    g = Grader(_grading_execute(src))
    r = g.grade(0, "GOLD", "PRED")
    assert r.outcome is Outcome.FAIL


def test_gold_rows_and_sql_never_on_result():
    # The QuestionResult must not carry gold SQL or gold rows (only pred-side).
    fake = FakeAthena({"GOLD": _rows(("secret", 9)), "PRED": _rows(("x", 1))})
    g = Grader(fake.execute)
    r = g.grade(0, "GOLD", "PRED")
    blob = repr(r)
    assert "GOLD" not in blob and "secret" not in blob
