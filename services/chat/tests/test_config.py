"""ChatConfig env resolution + model/effort validation (the trust boundary).

Pure — no AWS, no langchain. Catalog validation is the security boundary: an
arbitrary client-supplied (model, effort) must never pass through to a model
build. build_chat_model construction is covered by the model-factory tests in
okf_aws; here we only assert dispatch + the config wiring, with the SDKs stubbed.
"""

from __future__ import annotations

import json

import pytest

from chat.config import DEFAULT_MODEL, ChatConfig, build_chat_model
from okf_core.harvest_models import ModelCatalogError

from .fakes import CHAT_CATALOG


def _env(**overrides) -> dict[str, str]:
    # The RUNTIME reads OKF_CHAT_MODEL_CATALOG as RAW JSON (Terraform sets it
    # directly as a container env var, like the Control API's harvest catalog).
    # Only the UI gets the base64 form via ui_env.
    env = {
        "OKF_BUNDLE_BUCKET": "okf-bundles",
        "OKF_VECTOR_BUCKET": "okf-vectors",
        "OKF_VECTOR_INDEX": "okf-index",
        "OKF_CHAT_MODEL_CATALOG": json.dumps(CHAT_CATALOG),
    }
    env.update(overrides)
    return env


def test_from_env_defaults():
    cfg = ChatConfig.from_env(_env())
    assert cfg.bundle_bucket == "okf-bundles"
    assert cfg.registry_table == "okf-registry"  # default
    assert cfg.checkpoint_table == "okf-chat-checkpoints"  # default
    assert cfg.default_model == DEFAULT_MODEL
    assert cfg.mantle_region == "us-east-2"
    assert cfg.checkpoint_ttl_seconds is None
    # SQL is OFF by default (deploy-gated) — the browser can't self-enable it.
    assert cfg.sql_enabled is False
    # So is web search, and it defaults to the connector's original region.
    assert cfg.web_search_enabled is False
    assert cfg.web_search_gateway_url == ""
    assert cfg.web_search_region == "us-east-1"
    # And so are its request-level filters (a pre-1.2.0 target silently
    # ignores them, so only Terraform's pin may turn them on).
    assert cfg.web_search_filters_enabled is False
    assert [e["model"] for e in cfg.catalog] == [
        "us.anthropic.claude-opus-4-8",
        "openai.gpt-5.6-sol",
    ]


def test_from_env_sql_flag_and_athena():
    cfg = ChatConfig.from_env(
        _env(
            OKF_CHAT_SQL_ENABLED="true",
            OKF_ATHENA_WORKGROUP="primary",
            OKF_ATHENA_OUTPUT="s3://results/chat/",
            OKF_CHAT_SQL_MAX_ROWS="50",
        )
    )
    assert cfg.sql_enabled is True
    assert cfg.athena_workgroup == "primary"
    assert cfg.athena_output == "s3://results/chat/"
    assert cfg.sql_max_rows == 50


def test_from_env_web_search():
    cfg = ChatConfig.from_env(
        _env(
            OKF_WEB_SEARCH_ENABLED="true",
            OKF_WEB_SEARCH_GATEWAY_URL="https://gw.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
            OKF_WEB_SEARCH_TOOL_NAME="okf-web-search___WebSearch",
            OKF_WEB_SEARCH_MAX_RESULTS="12",
            OKF_WEB_SEARCH_FILTERS_ENABLED="true",
        )
    )
    assert cfg.web_search_enabled is True
    assert cfg.web_search_gateway_url.endswith("/mcp")
    assert cfg.web_search_tool_name == "okf-web-search___WebSearch"
    assert cfg.web_search_max_results == 12
    assert cfg.web_search_filters_enabled is True
    # The gateway region is independent of the runtime's (it defaults to the
    # connector's original region; var.web_search_region can move it).
    assert cfg.web_search_region == "us-east-1"


def test_from_env_overrides():
    cfg = ChatConfig.from_env(
        _env(
            OKF_CHAT_MODEL="openai.gpt-5.6-sol",
            OKF_CHAT_EFFORT="max",
            OKF_CHAT_MAX_TOKENS="16000",
            OKF_CHAT_CHECKPOINT_TABLE="my-checkpoints",
            OKF_CHAT_CHECKPOINT_TTL_SECONDS="604800",
            AWS_REGION="eu-west-1",
            OKF_CHAT_MANTLE_REGION="us-west-2",
        )
    )
    assert cfg.default_model == "openai.gpt-5.6-sol"
    assert cfg.default_effort == "max"
    assert cfg.default_max_tokens == 16000
    assert cfg.checkpoint_table == "my-checkpoints"
    assert cfg.checkpoint_ttl_seconds == 604800
    assert cfg.region == "eu-west-1"
    assert cfg.mantle_region == "us-west-2"


def test_resolve_model_effort_fills_default_effort():
    cfg = ChatConfig.from_env(_env())
    model, effort = cfg.resolve_model_effort("us.anthropic.claude-opus-4-8", None)
    assert model == "us.anthropic.claude-opus-4-8"
    assert effort == "high"  # the model's default_effort


def test_resolve_model_effort_none_model_falls_back_to_config_default():
    cfg = ChatConfig.from_env(_env(OKF_CHAT_MODEL="openai.gpt-5.6-sol"))
    model, effort = cfg.resolve_model_effort(None, None)
    assert model == "openai.gpt-5.6-sol"
    assert effort == "high"


def test_resolve_model_effort_rejects_unknown_model():
    cfg = ChatConfig.from_env(_env())
    with pytest.raises(ModelCatalogError):
        cfg.resolve_model_effort("openai.evil-model", "high")


def test_resolve_model_effort_rejects_effort_not_offered():
    cfg = ChatConfig.from_env(_env())
    # "extreme" is not an effort any catalog entry offers.
    with pytest.raises(ModelCatalogError):
        cfg.resolve_model_effort("us.anthropic.claude-opus-4-8", "extreme")


def test_build_chat_model_dispatches_gpt_to_mantle(monkeypatch):
    import sys
    import types

    captured: dict = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    lco = types.ModuleType("langchain_openai")
    lco.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", lco)

    tg = types.ModuleType("aws_bedrock_token_generator")
    tg.provide_token = lambda region: f"tok-{region}"
    monkeypatch.setitem(sys.modules, "aws_bedrock_token_generator", tg)

    cfg = ChatConfig.from_env(_env(OKF_CHAT_MANTLE_REGION="us-east-2"))
    build_chat_model(cfg, "openai.gpt-5.6-sol", "high")
    assert captured["model"] == "openai.gpt-5.6-sol"
    assert captured["base_url"] == "https://bedrock-mantle.us-east-2.api.aws/openai/v1"
    # Chat requests a reasoning SUMMARY so GPT returns its thinking on the
    # Responses API — this maps to `reasoning={effort, summary}` (which supersedes
    # the bare `reasoning_effort` knob). Without it, GPT thinks silently.
    assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
    assert "reasoning_effort" not in captured


def test_build_chat_model_dispatches_claude_to_converse(monkeypatch):
    import sys
    import types

    captured: dict = {}

    class _FakeConverse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    law = types.ModuleType("langchain_aws")
    law.ChatBedrockConverse = _FakeConverse
    monkeypatch.setitem(sys.modules, "langchain_aws", law)

    cfg = ChatConfig.from_env(_env(AWS_REGION="eu-west-1"))
    build_chat_model(cfg, "us.anthropic.claude-opus-4-8", "xhigh", max_tokens=99000)
    assert captured["model"] == "us.anthropic.claude-opus-4-8"
    assert captured["region_name"] == "eu-west-1"
    assert captured["max_tokens"] == 99000
    assert captured["additional_model_request_fields"]["output_config"] == {
        "effort": "xhigh"
    }
    # Chat asks Bedrock to STREAM a reasoning summary (thinking.display=
    # "summarized") so Opus's reasoning actually surfaces in the UI. Without it,
    # adaptive thinking runs silently. (Matches Sparky's Opus 4.8 config.)
    assert captured["additional_model_request_fields"]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }


def test_policy_judge_model_openai_runs_reasoning_none(monkeypatch):
    # The classifier contract on the OpenAI path too: a GPT judge (Terra)
    # runs with reasoning "none" — no ladder, no knob.
    import sys
    import types

    from chat.config import build_policy_judge_model

    captured: dict = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    lo = types.ModuleType("langchain_openai")
    lo.ChatOpenAI = _FakeOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", lo)

    cfg = ChatConfig.from_env(
        _env(OKF_CHAT_POLICY_CHECK_MODEL="openai.gpt-5.6-terra")
    )
    build_policy_judge_model(cfg)
    assert captured["model"] == "openai.gpt-5.6-terra"
    assert captured["reasoning_effort"] == "none"


def test_policy_judge_model_converse_runs_classifier_mode(monkeypatch):
    # A Converse (Anthropic) judge is a CLASSIFIER: thinking OFF, temperature
    # 0, single forward pass — whatever the generation (pre-adaptive Haiku and
    # adaptive Sonnet/Opus alike). Live 2026-08-03: thinking judges spent
    # 40-50s per shard and sometimes exhausted into prose with no tool call;
    # thinking-off is also what makes the FORCED report_violations tool
    # choice legal on Anthropic models.
    import sys
    import types

    from chat.config import POLICY_JUDGE_MAX_TOKENS, build_policy_judge_model

    captured: dict = {}

    class _FakeConverse:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    law = types.ModuleType("langchain_aws")
    law.ChatBedrockConverse = _FakeConverse
    monkeypatch.setitem(sys.modules, "langchain_aws", law)

    for model_id in (
        "us.anthropic.claude-haiku-4-5",
        "global.anthropic.claude-sonnet-4-6",
        "global.anthropic.claude-sonnet-5",  # the default
    ):
        captured.clear()
        cfg = ChatConfig.from_env(_env(OKF_CHAT_POLICY_CHECK_MODEL=model_id))
        build_policy_judge_model(cfg)
        thinking = (captured.get("additional_model_request_fields") or {}).get(
            "thinking"
        )
        assert thinking is None, model_id  # no thinking of either encoding
        assert captured["temperature"] == 0
        assert captured["max_tokens"] == POLICY_JUDGE_MAX_TOKENS
