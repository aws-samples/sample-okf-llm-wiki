import re

from okf_core.session import runtime_session_id


def test_meets_agentcore_length_constraint():
    # Even very short names must yield a 33-256 char id (AgentCore's min is 33).
    sid = runtime_session_id("a", "b")
    assert 33 <= len(sid) <= 256


def test_long_names_are_capped_at_256():
    sid = runtime_session_id("d" * 300, "s" * 300)
    assert len(sid) <= 256


def test_deterministic_per_dataset():
    # Same dataset -> same session id (one session per dataset, for affinity).
    assert runtime_session_id("motorsport", "formula_1") == runtime_session_id(
        "motorsport", "formula_1"
    )


def test_distinct_datasets_differ():
    assert runtime_session_id("m", "formula_1") != runtime_session_id("m", "formula_2")
    assert runtime_session_id("a", "x") != runtime_session_id("b", "x")


def test_charset_is_safe():
    # Only [A-Za-z0-9_-]; sanitizes odd chars in domain/dataset names.
    sid = runtime_session_id("sales.eu", "orders/2026")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", sid)


def test_readable_prefix_present():
    sid = runtime_session_id("motorsport", "formula_1")
    assert sid.startswith("okf-motorsport-formula-1-")


def test_unique_token_yields_fresh_but_valid_id():
    # A unique token makes each call distinct (fresh microVM per full harvest),
    # while staying length-valid and keeping the readable prefix.
    a = runtime_session_id("sport", "formula_1", unique_token="abc")  # nosec B106 - test salt, not a password
    b = runtime_session_id("sport", "formula_1", unique_token="def")  # nosec B106 - test salt, not a password
    assert a != b
    assert a != runtime_session_id("sport", "formula_1")  # differs from deterministic
    for sid in (a, b):
        assert sid.startswith("okf-sport-formula-1-")
        assert 33 <= len(sid) <= 256
