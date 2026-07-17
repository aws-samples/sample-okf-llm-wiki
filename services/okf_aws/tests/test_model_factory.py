"""The shared LLM client factory (okf_aws.model_factory).

Provider dispatch, effort mapping, adaptive-thinking fields, and the Mantle GPT
builder wiring — all exercised against STUBBED SDKs (langchain_aws /
langchain_openai / aws_bedrock_token_generator are imported lazily inside the
builders, so the module imports without them and tests inject fakes). This is
the source-of-truth suite; harvest's test_model_config.py covers the thin
env-reading wrappers that delegate here.
"""

import sys
import types

import pytest

from okf_aws import model_factory as mf


# --- provider detection ------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    ["openai.gpt-5.6-sol", "openai.gpt-5.4", "openai.gpt-oss-120b", "gpt-5.5"],
)
def test_is_openai_model_true_for_gpt(model):
    assert mf.is_openai_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "us.anthropic.claude-opus-4-8",
        "eu.anthropic.claude-opus-4-8",
        "global.anthropic.claude-opus-4-8",
    ],
)
def test_is_openai_model_false_for_claude(model):
    assert mf.is_openai_model(model) is False


# --- adaptive-thinking fields (Converse) -------------------------------------


def test_thinking_fields_shape():
    fields = mf.thinking_fields("xhigh")
    # adaptive thinking, with effort in a SEPARATE output_config object
    assert fields["thinking"] == {"type": "adaptive"}
    assert fields["output_config"] == {"effort": "xhigh"}
    # effort must NOT be nested inside `thinking` (that 400s on Bedrock)
    assert "effort" not in fields["thinking"]
    # default: NO display key -> Bedrock runs thinking but streams no summary
    assert "display" not in fields["thinking"]


def test_thinking_fields_summarize_adds_display():
    # summarize=True -> thinking.display="summarized", which is what makes Bedrock
    # STREAM BACK a reasoning summary (the chat "I see no reasoning" fix). Matches
    # Sparky's Opus 4.8 adaptive config.
    fields = mf.thinking_fields("high", summarize=True)
    assert fields["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert fields["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("effort", ["xhigh", "max", "high", "medium", "low"])
def test_thinking_fields_effort_passed_through_verbatim(effort):
    assert mf.thinking_fields(effort)["output_config"]["effort"] == effort


def test_thinking_fields_empty_rejected():
    with pytest.raises(ValueError):
        mf.thinking_fields("")


# --- GPT effort mapping ------------------------------------------------------


def test_gpt_effort_maps_converse_levels():
    # GPT-5.6 added "max" as a distinct level above xhigh, so the whole ladder
    # passes through verbatim — "max" must NOT collapse to "xhigh".
    assert mf.gpt_effort("max") == "max"
    assert mf.gpt_effort("xhigh") == "xhigh"
    assert mf.gpt_effort("high") == "high"
    assert mf.gpt_effort("medium") == "medium"
    assert mf.gpt_effort("low") == "low"


def test_gpt_effort_unknown_falls_back_to_xhigh():
    assert mf.gpt_effort("banana") == mf.DEFAULT_GPT_REASONING_EFFORT == "xhigh"


def test_gpt_effort_empty_rejected():
    with pytest.raises(ValueError):
        mf.gpt_effort("")


# --- Mantle GPT builder wiring (stubbed SDKs) --------------------------------


def _install_openai_stubs(monkeypatch):
    """Stub langchain_openai.ChatOpenAI + aws_bedrock_token_generator.provide_token.

    ChatOpenAI records its kwargs; provide_token returns a bearer with a
    monotonically increasing counter so a test can PROVE re-minting. Returns
    (captured_kwargs, call_state).
    """
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


def test_build_mantle_openai_defaults_to_responses_api(monkeypatch):
    captured, _state = _install_openai_stubs(monkeypatch)

    mf.build_mantle_openai(
        "openai.gpt-5.6-sol", "xhigh", 32000, region=mf.DEFAULT_MANTLE_REGION
    )

    assert captured["model"] == "openai.gpt-5.6-sol"
    # Responses API at /openai/v1, derived from the region.
    assert captured["base_url"] == (
        f"https://bedrock-mantle.{mf.DEFAULT_MANTLE_REGION}.api.aws/openai/v1"
    )
    assert captured["use_responses_api"] is True
    # api_key is a PROVIDER CALLABLE (re-minted per request), not a static string.
    assert callable(captured["api_key"])
    assert captured["api_key"]().startswith(
        f"bedrock-api-key-{mf.DEFAULT_MANTLE_REGION}"
    )
    assert captured["max_tokens"] == 32000
    assert captured["reasoning_effort"] == "xhigh"  # preserved, not capped


def test_build_mantle_openai_region_drives_url_and_token(monkeypatch):
    captured, _state = _install_openai_stubs(monkeypatch)

    mf.build_mantle_openai("openai.gpt-5.4", "medium", 16000, region="us-west-2")

    assert captured["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/openai/v1"
    assert captured["api_key"]().startswith("bedrock-api-key-us-west-2")
    assert captured["reasoning_effort"] == "medium"


def test_build_mantle_openai_chat_completions_opt_out(monkeypatch):
    # gpt-oss models use Chat Completions on /v1 instead of Responses on /openai/v1.
    captured, _state = _install_openai_stubs(monkeypatch)

    mf.build_mantle_openai(
        "openai.gpt-oss-120b",
        "high",
        16000,
        region="us-west-2",
        use_responses_api=False,
    )

    assert captured["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/v1"
    assert captured["use_responses_api"] is False


def test_build_mantle_openai_explicit_base_url_wins(monkeypatch):
    captured, _state = _install_openai_stubs(monkeypatch)

    mf.build_mantle_openai(
        "openai.gpt-5.6-sol",
        "high",
        32000,
        region="us-east-2",
        base_url="https://internal.example/openai/v1",
    )

    assert captured["base_url"] == "https://internal.example/openai/v1"


def test_build_mantle_openai_no_summary_uses_reasoning_effort(monkeypatch):
    # Default (harvest): no summary requested -> plain reasoning_effort, no
    # `reasoning` object (so nothing changes for callers that don't show thinking).
    captured, _state = _install_openai_stubs(monkeypatch)
    mf.build_mantle_openai("openai.gpt-5.6-sol", "high", 32000, region="us-east-2")
    assert captured["reasoning_effort"] == "high"
    assert "reasoning" not in captured


def test_build_mantle_openai_summary_uses_reasoning_object(monkeypatch):
    # Chat: summary requested -> `reasoning={effort, summary}` so GPT RETURNS its
    # thinking on the Responses API; the bare reasoning_effort knob is superseded.
    captured, _state = _install_openai_stubs(monkeypatch)
    mf.build_mantle_openai(
        "openai.gpt-5.6-sol",
        "high",
        32000,
        region="us-east-2",
        reasoning_summary="auto",
    )
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning_effort" not in captured
    # output_version="responses/v1" is REQUIRED for the summary to land in message
    # content (streamable) rather than additional_kwargs — else the UI sees no
    # reasoning for GPT.
    assert captured["output_version"] == "responses/v1"


def test_build_mantle_openai_summary_ignored_on_chat_completions(monkeypatch):
    # Chat Completions (gpt-oss) has no reasoning-summary concept; fall back to the
    # plain reasoning_effort even if a summary was requested.
    captured, _state = _install_openai_stubs(monkeypatch)
    mf.build_mantle_openai(
        "openai.gpt-oss-120b",
        "high",
        16000,
        region="us-west-2",
        use_responses_api=False,
        reasoning_summary="auto",
    )
    assert captured["reasoning_effort"] == "high"
    assert "reasoning" not in captured


def test_mantle_token_provider_caches_then_remints(monkeypatch):
    # Caches within the TTL, re-mints once it lapses — the fix for the ~1h
    # presign expiry killing a long run. Drive a fake clock.
    _captured, state = _install_openai_stubs(monkeypatch)
    clock = {"t": 1000.0}
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: clock["t"])

    provider = mf.mantle_token_provider("us-east-2", ttl_seconds=1800)
    first = provider()
    again = provider()  # within TTL -> cached, no new mint
    assert first == again
    assert state["mints"] == 1

    clock["t"] += 1801  # TTL lapses
    third = provider()
    assert third != first  # re-minted
    assert state["mints"] == 2


# --- dispatcher --------------------------------------------------------------


def test_build_model_dispatches_gpt_to_mantle(monkeypatch):
    captured, _state = _install_openai_stubs(monkeypatch)

    mf.build_model(
        "openai.gpt-5.6-sol", "high", 32000, region="us-east-1", mantle_region="us-east-2"
    )

    # Went through the OpenAI stub (Mantle path), with the Mantle region — not
    # the Converse region.
    assert captured["model"] == "openai.gpt-5.6-sol"
    assert captured["base_url"] == "https://bedrock-mantle.us-east-2.api.aws/openai/v1"


def test_build_model_dispatches_claude_to_converse(monkeypatch):
    captured: dict = {}

    class _FakeConverse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    law = types.ModuleType("langchain_aws")
    law.ChatBedrockConverse = _FakeConverse
    monkeypatch.setitem(sys.modules, "langchain_aws", law)

    mf.build_model("us.anthropic.claude-opus-4-8", "xhigh", 128000, region="eu-west-1")

    assert captured["model"] == "us.anthropic.claude-opus-4-8"
    assert captured["region_name"] == "eu-west-1"
    assert captured["max_tokens"] == 128000
    # adaptive thinking rides on the model
    assert captured["additional_model_request_fields"]["thinking"] == {"type": "adaptive"}
    assert captured["additional_model_request_fields"]["output_config"] == {
        "effort": "xhigh"
    }


def test_build_bedrock_converse_passes_botocore_config(monkeypatch):
    captured: dict = {}

    class _FakeConverse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    law = types.ModuleType("langchain_aws")
    law.ChatBedrockConverse = _FakeConverse
    monkeypatch.setitem(sys.modules, "langchain_aws", law)

    sentinel = object()
    mf.build_bedrock_converse(
        "us.anthropic.claude-opus-4-8",
        "high",
        128000,
        region="us-east-1",
        botocore_config=sentinel,
    )
    assert captured["config"] is sentinel
    # default: adaptive thinking with NO streamed summary (harvest's shape)
    assert captured["additional_model_request_fields"]["thinking"] == {"type": "adaptive"}


def test_build_bedrock_converse_summarize_reasoning_sets_display(monkeypatch):
    captured: dict = {}

    class _FakeConverse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    law = types.ModuleType("langchain_aws")
    law.ChatBedrockConverse = _FakeConverse
    monkeypatch.setitem(sys.modules, "langchain_aws", law)

    mf.build_bedrock_converse(
        "us.anthropic.claude-opus-4-8",
        "high",
        128000,
        region="us-east-1",
        summarize_reasoning=True,
    )
    # summarize -> the model streams a reasoning summary (chat's shape)
    assert captured["additional_model_request_fields"]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }
