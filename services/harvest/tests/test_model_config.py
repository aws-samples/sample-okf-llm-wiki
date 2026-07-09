"""The harvest model config: Opus 4.8 defaults + adaptive-thinking fields.

Effort is passed through to Bedrock verbatim (Bedrock validates it per-model —
valid values differ by model, e.g. Opus 4.8 supports 'xhigh'), so there is NO
client-side allow-list. No AWS/langchain needed — these exercise the pure
helpers only.
"""

import pytest

from harvest import agent as ag


def test_defaults_are_opus_48_xhigh():
    assert ag.DEFAULT_MODEL == "us.anthropic.claude-opus-4-8"
    assert ag.DEFAULT_EFFORT == "xhigh"


def test_thinking_fields_shape():
    fields = ag._thinking_fields("xhigh")
    # adaptive thinking, with effort in a SEPARATE output_config object
    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"] == {"effort": "xhigh"}
    # effort must NOT be nested inside `thinking` (that 400s on Bedrock)
    assert "effort" not in fields["thinking"]


@pytest.mark.parametrize("effort", ["xhigh", "max", "high", "medium", "low"])
def test_effort_passed_through_verbatim(effort):
    # No client-side allow-list — whatever the caller sets reaches Bedrock as-is.
    assert ag._thinking_fields(effort)["output_config"]["effort"] == effort


def test_empty_effort_rejected():
    with pytest.raises(ValueError):
        ag._thinking_fields("")


def test_resolve_model_config_env_overrides(monkeypatch):
    monkeypatch.setenv("OKF_HARVEST_MODEL", "eu.anthropic.claude-opus-4-8")
    monkeypatch.setenv("OKF_HARVEST_EFFORT", "high")
    monkeypatch.setenv("OKF_HARVEST_MAX_TOKENS", "64000")
    # Use a value distinct from DEFAULT_SUBAGENT_CONCURRENCY so this proves the
    # env override is honored (not just coincidentally equal to the default).
    monkeypatch.setenv("OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY", "8")
    cfg = ag.resolve_model_config()
    assert cfg == {
        "model": "eu.anthropic.claude-opus-4-8",
        "effort": "high",
        "max_tokens": 64000,
        "subagent_concurrency": 8,
    }


def test_resolve_model_config_defaults(monkeypatch):
    for k in (
        "OKF_HARVEST_MODEL",
        "OKF_HARVEST_EFFORT",
        "OKF_HARVEST_MAX_TOKENS",
        "OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = ag.resolve_model_config()
    assert cfg["model"] == "us.anthropic.claude-opus-4-8"
    assert cfg["effort"] == "xhigh"
    assert cfg["max_tokens"] == ag.DEFAULT_MAX_TOKENS
    assert cfg["subagent_concurrency"] == ag.DEFAULT_SUBAGENT_CONCURRENCY


def test_default_subagent_concurrency_is_five():
    assert ag.DEFAULT_SUBAGENT_CONCURRENCY == 5


def test_resolve_model_config_overrides_win_over_env(monkeypatch):
    # Per-invocation override (from the harvest payload) beats the env default.
    monkeypatch.setenv("OKF_HARVEST_MODEL", "us.anthropic.claude-opus-4-8")
    monkeypatch.setenv("OKF_HARVEST_EFFORT", "medium")
    monkeypatch.delenv("OKF_HARVEST_MAX_TOKENS", raising=False)
    cfg = ag.resolve_model_config(model_override="openai.gpt-5.5", effort_override="high")
    assert cfg["model"] == "openai.gpt-5.5"
    assert cfg["effort"] == "high"
    # max_tokens default keys off the RESOLVED (override) model -> GPT ceiling.
    assert cfg["max_tokens"] == ag.DEFAULT_GPT_MAX_TOKENS


def test_resolve_model_config_partial_override_falls_back_per_field(monkeypatch):
    # Only a model override: effort still comes from env (or its default).
    monkeypatch.setenv("OKF_HARVEST_EFFORT", "low")
    monkeypatch.delenv("OKF_HARVEST_MODEL", raising=False)
    cfg = ag.resolve_model_config(model_override="openai.gpt-5.5")
    assert cfg["model"] == "openai.gpt-5.5"
    assert cfg["effort"] == "low"  # from env, not overridden


def test_bedrock_config_defaults(monkeypatch):
    # A single xhigh Opus 4.8 turn can run for minutes; the read timeout must be
    # well above botocore's 60s default so a slow Converse response is retried,
    # not fatal. (This is the fix for the ReadTimeoutError that killed a harvest.)
    for k in (
        "OKF_HARVEST_BEDROCK_READ_TIMEOUT",
        "OKF_HARVEST_BEDROCK_CONNECT_TIMEOUT",
        "OKF_HARVEST_BEDROCK_MAX_ATTEMPTS",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = ag._bedrock_config()
    assert cfg.read_timeout == ag.DEFAULT_BEDROCK_READ_TIMEOUT >= 300
    assert cfg.connect_timeout == ag.DEFAULT_BEDROCK_CONNECT_TIMEOUT
    assert cfg.retries == {
        "max_attempts": ag.DEFAULT_BEDROCK_MAX_ATTEMPTS,
        "mode": "adaptive",
    }


def test_bedrock_config_env_overrides(monkeypatch):
    monkeypatch.setenv("OKF_HARVEST_BEDROCK_READ_TIMEOUT", "900")
    monkeypatch.setenv("OKF_HARVEST_BEDROCK_CONNECT_TIMEOUT", "15")
    monkeypatch.setenv("OKF_HARVEST_BEDROCK_MAX_ATTEMPTS", "7")
    cfg = ag._bedrock_config()
    assert cfg.read_timeout == 900
    assert cfg.connect_timeout == 15
    assert cfg.retries["max_attempts"] == 7


def test_cap_subagent_concurrency_sets_quickjs_constant(monkeypatch):
    # Stand in for langchain_quickjs._repl so we can assert the cap is applied
    # without the package installed. _cap_subagent_concurrency imports
    # `from langchain_quickjs import _repl` and sets _MAX_TASK_CALLS_PER_THREAD.
    import sys
    import types

    pkg = types.ModuleType("langchain_quickjs")
    repl = types.ModuleType("langchain_quickjs._repl")
    repl._MAX_TASK_CALLS_PER_THREAD = 32
    pkg._repl = repl
    monkeypatch.setitem(sys.modules, "langchain_quickjs", pkg)
    monkeypatch.setitem(sys.modules, "langchain_quickjs._repl", repl)

    ag._cap_subagent_concurrency(3)
    assert repl._MAX_TASK_CALLS_PER_THREAD == 3


def test_cap_subagent_concurrency_is_best_effort_when_absent(monkeypatch):
    # No langchain_quickjs installed -> swallow the ImportError, don't crash.
    import sys

    monkeypatch.setitem(sys.modules, "langchain_quickjs", None)
    ag._cap_subagent_concurrency(3)  # no exception


def test_cap_subagent_concurrency_ignores_bad_limit(monkeypatch):
    import sys
    import types

    repl = types.ModuleType("langchain_quickjs._repl")
    repl._MAX_TASK_CALLS_PER_THREAD = 32
    pkg = types.ModuleType("langchain_quickjs")
    pkg._repl = repl
    monkeypatch.setitem(sys.modules, "langchain_quickjs", pkg)
    monkeypatch.setitem(sys.modules, "langchain_quickjs._repl", repl)

    ag._cap_subagent_concurrency(0)  # < 1 -> no-op
    ag._cap_subagent_concurrency(None)  # None -> no-op
    assert repl._MAX_TASK_CALLS_PER_THREAD == 32


# --- GPT on Bedrock Mantle --------------------------------------------------
# The provider is selected by the model id's prefix; GPT ids route to a
# ChatOpenAI against the Mantle OpenAI-compatible endpoint, everything else to
# ChatBedrockConverse. These exercise the pure helpers + the GPT builder wiring
# with langchain_openai / aws_bedrock_token_generator STUBBED (neither package is
# needed to import the module — the imports are deferred inside the builder).


@pytest.mark.parametrize(
    "model",
    ["openai.gpt-5.5", "openai.gpt-5.4", "openai.gpt-oss-120b", "gpt-5.5"],
)
def test_is_openai_model_true_for_gpt(model):
    assert ag._is_openai_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "us.anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8",
        "global.anthropic.claude-opus-4-8",
    ],
)
def test_is_openai_model_false_for_claude(model):
    assert ag._is_openai_model(model) is False


def test_gpt_effort_maps_converse_levels():
    # GPT-5.5 accepts xhigh, so our top levels must NOT be capped at high (that
    # would silently downgrade the deliberately-max harvest effort).
    assert ag._gpt_effort("xhigh") == "xhigh"
    assert ag._gpt_effort("max") == "xhigh"  # no OpenAI level above xhigh
    assert ag._gpt_effort("high") == "high"
    assert ag._gpt_effort("medium") == "medium"
    assert ag._gpt_effort("low") == "low"


def test_gpt_effort_unknown_falls_back_to_xhigh():
    assert ag._gpt_effort("banana") == ag.DEFAULT_GPT_REASONING_EFFORT == "xhigh"


def test_gpt_effort_empty_rejected():
    with pytest.raises(ValueError):
        ag._gpt_effort("")


def test_resolve_model_config_gpt_max_tokens_default(monkeypatch):
    # An UNSET OKF_HARVEST_MAX_TOKENS picks the GPT ceiling for a GPT model...
    for k in ("OKF_HARVEST_MAX_TOKENS", "OKF_HARVEST_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKF_HARVEST_MODEL", "openai.gpt-5.5")
    cfg = ag.resolve_model_config()
    assert cfg["model"] == "openai.gpt-5.5"
    assert cfg["max_tokens"] == ag.DEFAULT_GPT_MAX_TOKENS


def test_resolve_model_config_explicit_max_tokens_wins_for_gpt(monkeypatch):
    # ...but an explicit value is authoritative regardless of provider.
    monkeypatch.setenv("OKF_HARVEST_MODEL", "openai.gpt-5.5")
    monkeypatch.setenv("OKF_HARVEST_MAX_TOKENS", "8000")
    assert ag.resolve_model_config()["max_tokens"] == 8000


def _install_openai_stubs(monkeypatch):
    """Stub langchain_openai.ChatOpenAI + aws_bedrock_token_generator.provide_token.

    ChatOpenAI records its kwargs; provide_token returns a bearer that includes a
    monotonically increasing counter so a test can PROVE the token is re-minted on
    repeated calls (the fix for the presign-expiry bug). Lets _build_mantle_openai
    run with neither real package installed. Returns (captured_kwargs, call_state).
    """
    import sys
    import types

    captured: dict = {}
    state = {"mints": 0}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    lco = types.ModuleType("langchain_openai")
    lco.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", lco)

    def _fake_provide_token(region):
        state["mints"] += 1
        return f"bedrock-api-key-{region}-{state['mints']}"

    tg = types.ModuleType("aws_bedrock_token_generator")
    tg.provide_token = _fake_provide_token
    monkeypatch.setitem(sys.modules, "aws_bedrock_token_generator", tg)

    return captured, state


def test_build_mantle_openai_wiring_defaults(monkeypatch):
    for k in (
        "OKF_HARVEST_MANTLE_REGION",
        "OKF_HARVEST_MANTLE_BASE_URL",
        "OKF_HARVEST_MANTLE_USE_RESPONSES_API",
    ):
        monkeypatch.delenv(k, raising=False)
    captured, _state = _install_openai_stubs(monkeypatch)

    ag._build_mantle_openai("openai.gpt-5.5", "xhigh", 32000, callbacks=None)

    assert captured["model"] == "openai.gpt-5.5"
    # Default Mantle region (independent of AWS_REGION) drives the base URL + token.
    # GPT-5.x is served on the Responses API at the /openai/v1 path.
    assert captured["base_url"] == (
        f"https://bedrock-mantle.{ag.DEFAULT_MANTLE_REGION}.api.aws/openai/v1"
    )
    assert captured["use_responses_api"] is True
    # api_key is a PROVIDER CALLABLE (not a static string) so the SDK re-mints per
    # request — calling it yields a region-scoped bearer.
    assert callable(captured["api_key"])
    assert captured["api_key"]().startswith(
        f"bedrock-api-key-{ag.DEFAULT_MANTLE_REGION}"
    )
    assert captured["max_tokens"] == 32000
    assert captured["reasoning_effort"] == "xhigh"  # xhigh preserved, not capped


def test_build_mantle_openai_region_override(monkeypatch):
    for k in ("OKF_HARVEST_MANTLE_BASE_URL", "OKF_HARVEST_MANTLE_USE_RESPONSES_API"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKF_HARVEST_MANTLE_REGION", "us-west-2")
    captured, _state = _install_openai_stubs(monkeypatch)

    ag._build_mantle_openai("openai.gpt-5.4", "medium", 16000, callbacks=None)

    assert captured["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/openai/v1"
    assert callable(captured["api_key"])
    assert captured["api_key"]().startswith("bedrock-api-key-us-west-2")
    assert captured["reasoning_effort"] == "medium"


def test_mantle_token_provider_caches_then_remints(monkeypatch):
    # The provider caches within the TTL (so we don't presign every request) but
    # re-mints once the TTL lapses — this is the fix for the ~1h presign expiry
    # killing an 8h harvest. Drive a fake clock via monkeypatched time.time.
    _captured, state = _install_openai_stubs(monkeypatch)
    clock = {"t": 1000.0}
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: clock["t"])

    provider = ag._mantle_token_provider("us-east-2")
    first = provider()
    again = provider()  # within TTL -> same cached token, no new mint
    assert first == again
    assert state["mints"] == 1

    clock["t"] += ag._MANTLE_TOKEN_TTL_SECONDS + 1  # TTL lapses
    third = provider()
    assert third != first  # re-minted
    assert state["mints"] == 2


def test_build_mantle_openai_chat_completions_opt_out(monkeypatch):
    # gpt-oss models use Chat Completions on /v1 instead of Responses on /openai/v1.
    monkeypatch.delenv("OKF_HARVEST_MANTLE_BASE_URL", raising=False)
    monkeypatch.setenv("OKF_HARVEST_MANTLE_REGION", "us-west-2")
    monkeypatch.setenv("OKF_HARVEST_MANTLE_USE_RESPONSES_API", "false")
    captured, _state = _install_openai_stubs(monkeypatch)

    ag._build_mantle_openai("openai.gpt-oss-120b", "high", 16000, callbacks=None)

    assert captured["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/v1"
    assert captured["use_responses_api"] is False


def test_build_model_dispatches_gpt_to_mantle(monkeypatch):
    # _build_model routes a GPT id to the Mantle builder (not Converse).
    for k in ("OKF_HARVEST_MANTLE_REGION", "OKF_HARVEST_MANTLE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    captured, _state = _install_openai_stubs(monkeypatch)

    ag._build_model("openai.gpt-5.5", "high", 32000, callbacks=None)

    assert captured["model"] == "openai.gpt-5.5"  # went through the OpenAI stub
