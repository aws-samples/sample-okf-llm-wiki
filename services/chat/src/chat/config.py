"""Chat-agent runtime configuration + model resolution.

Two responsibilities:

1. :class:`ChatConfig` — the deploy-time knobs read from ``OKF_CHAT_*`` env
   (bundle/vector/registry pointers reused by the tools, the chat model catalog,
   the default model/effort/max-tokens, the Mantle region, and the checkpoint
   table). Resolved once and passed explicitly so nothing reads process env at
   call time (mirrors ``ConsumptionConfig``).

2. Per-conversation model **resolution + validation**. Model + effort are chosen
   in the UI and arrive per-run in the request's ``input`` envelope
   (``model_id``/``effort``). Because the browser calls the runtime DIRECTLY (no
   Control-API proxy in the hot path), the ``(model, effort)`` pair is validated
   **here, in the runtime**, against the catalog before it can reach
   ``bedrock:InvokeModel`` — the same trust boundary harvest enforces in the
   Control API. Construction is delegated to the shared ``okf_aws.model_factory``
   so chat and harvest build identical clients.

The model is **pinned per thread**: the first run stamps it into the graph state;
later runs ignore the client-sent value (see ``graph``/``server``). Switching
model is a NEW thread, because Opus/GPT checkpoints are not portable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from okf_core.harvest_models import (
    DEFAULT_EFFORT,
    parse_catalog,
    validate_model_effort,
)

# Chat defaults. Opus 5 (Converse) with adaptive thinking, like harvest — but
# an operator can point OKF_CHAT_MODEL at a lighter/faster model for interactive
# chat without touching harvest. A GPT id (openai.*) routes to Bedrock Mantle.
# Opus 5 streaming requires langchain-aws >= 1.6.4 (pinned in requirements).
DEFAULT_MODEL = "global.anthropic.claude-opus-5"

# GPT reasoning models cap output below Opus; the shared factory keys the
# provider-aware default off the resolved model, so we only need the Converse
# ceiling as our env default (an explicit OKF_CHAT_MAX_TOKENS always wins).
DEFAULT_MAX_TOKENS = 32000

# Interactive chat wants snappier turns than the heavyweight authoring agent;
# keep botocore's read timeout generous but below harvest's (a chat turn that
# runs for minutes is a bug, not the norm).
DEFAULT_BEDROCK_READ_TIMEOUT = 300
DEFAULT_BEDROCK_CONNECT_TIMEOUT = 10
DEFAULT_BEDROCK_MAX_ATTEMPTS = 5

# GPT-5.x on Mantle lives only in us-east-2 / us-west-2 — independent of the
# runtime's own AWS_REGION.
DEFAULT_MANTLE_REGION = "us-east-2"

# The policy check's pre-pass (chat/policy_check.py) is EXTRACTION — rewrite the
# question, narrate the chain — so it runs with no reasoning pass rather than
# on the conversation's own model. Deploy-time only: unlike the per-run chat
# model this never comes from a client, so it deliberately bypasses the catalog
# trust boundary (`resolve_model_effort`). Sonnet 5, no reasoning — mirrors
# var.chat_policy_check_model's default. An openai.* value means the chat
# role's Mantle grants must be on — infra derives that from the same var (see
# agentcore_iam.tf chat_mantle_enabled).
DEFAULT_POLICY_CHECK_MODEL = "global.anthropic.claude-sonnet-5"

# The rewrite emits one small JSON object; a few thousand tokens is the whole
# budget.
POLICY_CHECK_MAX_TOKENS = 4000

# Effort for the QUESTION-REWRITE call (extraction, not reasoning). Meaningless
# with reasoning off on Converse; on Mantle "none" passes VERBATIM (the whole
# GPT-5.6 fleet accepts it — a genuine no-reasoning pass).
POLICY_CHECK_EFFORT = "none"

# The JUDGE calls are judgment work — reasoning ON, but at the LOW end of the
# ladder: the judges run inside a live query's verdict window, and live tuning
# (2026-08-02) showed medium-effort rounds regularly blowing past it. The
# judges run CLASSIFIER-style on EVERY model family — thinking off on a
# Converse (Anthropic) id, reasoning "none" on an OpenAI (GPT 5.6) id — with
# a FORCED report_violations tool choice (see build_policy_judge_model —
# live 2026-08-03, reasoning judges spent 40-50s per shard and sometimes
# exhausted into prose with no tool call). No effort knob remains: shallow
# is not a tuning choice, it is the judge's contract.
POLICY_JUDGE_MAX_TOKENS = 8000

# Judges lose precision when a single rubric grows long; ≤ this many policies
# per mini-judge (okf_core.policy_doc.DEFAULT_SHARD_SIZE is the format's own
# default — this is the deploy-time override surface).
DEFAULT_POLICY_SHARD_SIZE = 10

# The query-time soft check (policy_check.PolicyChecker): how long run_sql
# waits for a verdict AFTER the query's results are back (the judges run
# concurrently with the engine, so this is the residual wait, not the total),
# and how many analytical queries per turn get judged at all. 60s because a
# medium-effort judge round can exceed 20s even warm and a dropped verdict is
# a silently skipped check (observed live 2026-08-02) — landing the verdict
# outranks the wait.
DEFAULT_POLICY_QUERY_TIMEOUT_S = 60
DEFAULT_POLICY_QUERY_MAX_PER_TURN = 3


def _int_env(name: str, default: int, env: dict[str, str]) -> int:
    raw = env.get(name)
    return int(raw) if raw else default


@dataclass
class ChatConfig:
    """Chat-agent config resolved from ``OKF_CHAT_*`` env (see docs/CONVENTIONS.md)."""

    # Reused by the tools (same pointers the consumption runtime reads).
    bundle_bucket: str
    vector_bucket: str
    vector_index: str
    registry_table: str

    # Conversation memory + the per-user conversation INDEX (sidebar list).
    checkpoint_table: str
    threads_table: str

    # Model selection.
    catalog: list[dict[str, Any]]
    default_model: str = DEFAULT_MODEL

    # Wiki annotations (the agent's submit_annotation writes on the user's
    # behalf). Same table the Control API + harvest reconcile use. Defaulted
    # (dataclass ordering) but always set from OKF_ANNOTATIONS_TABLE in from_env.
    annotations_table: str = "okf-annotations"

    # Report authoring (create_report / present_report). The harness path is
    # the chart-render page baked into the image (empty = no Chromium render
    # path: chart blocks are refused; markdown/table/kpi reports still work).
    report_harness_path: str = ""
    report_max_bytes: int = 8_000_000

    # Long-term memory (AgentCore Memory — chat.memory). Empty id = feature
    # off: no client is built, no recall, no event writes. The id is the
    # deploy-time master gate; each user has their own switch on top (a
    # settings row on the threads table, written by the Control API).
    memory_id: str = ""
    # What a MISSING per-user switch row means: True = opt-out (memory on
    # until the user turns it off — the default), False = opt-in (memory
    # stays off until the user explicitly enables it on the Memory page).
    # Deploy policy, not user state — OKF_CHAT_MEMORY_DEFAULT_ON, set from
    # var.chat_memory_default_on. The Control API reads the same env so the
    # switch the Memory page shows agrees with what the runtime does.
    memory_default_on: bool = True
    default_effort: str = DEFAULT_EFFORT
    default_max_tokens: int = DEFAULT_MAX_TOKENS

    # Regions + botocore knobs.
    region: str = "us-east-1"
    mantle_region: str = DEFAULT_MANTLE_REGION
    bedrock_read_timeout: int = DEFAULT_BEDROCK_READ_TIMEOUT
    bedrock_connect_timeout: int = DEFAULT_BEDROCK_CONNECT_TIMEOUT
    bedrock_max_attempts: int = DEFAULT_BEDROCK_MAX_ATTEMPTS
    mantle_use_responses_api: bool = True
    mantle_base_url: str | None = None

    # Optional TTL (seconds) for checkpoint rows; None = no expiry.
    checkpoint_ttl_seconds: int | None = None

    # Optional S3 bucket for checkpoint blobs that exceed DynamoDB's 400KB item
    # cap (a long turn with big tool results otherwise dies with
    # "PutItem ... Item size has exceeded the maximum allowed size"). Empty =
    # no offload (DynamoDB only).
    checkpoint_offload_bucket: str = ""

    # Optional read-only SQL tool — the ONE tool that touches source data.
    # Deploy-gated by OKF_CHAT_SQL_ENABLED (the IAM role only carries Glue/Athena
    # when var.enable_chat_sql is set); also requires a per-run opt-in
    # (features:["sql"]). The Athena knobs mirror harvest's OKF_ATHENA_* env.
    sql_enabled: bool = False
    athena_workgroup: str | None = None
    athena_output: str | None = None
    athena_catalog: str = "AwsDataCatalog"
    sql_max_rows: int = 200
    # Whether Amazon Redshift is deploy-enabled (OKF_REDSHIFT_ENABLED, set from
    # var.enable_redshift). With sql_enabled, a conversation @-scoped to a
    # Redshift-backed dataset gets run_sql against THAT dataset's cluster/
    # workgroup via the Redshift Data API (connection read from the mapping's
    # source descriptor). Off -> a Redshift-scoped run simply gets no SQL tool
    # (it must never silently fall back to Athena — wrong backend).
    redshift_enabled: bool = False

    # Public web search via the AgentCore Gateway web-search connector (see
    # chat/web_search.py). Deploy-gated by OKF_WEB_SEARCH_ENABLED **and** a
    # gateway URL — no per-run opt-in (it reads no source data). The gateway
    # lives in web_search_region (the connector is us-east-1 only), which may
    # differ from the runtime's own region. web_search_tool_name is the
    # gateway-side name (`<target>___WebSearch`); empty = discover via tools/list.
    web_search_enabled: bool = False
    web_search_gateway_url: str = ""
    web_search_region: str = "us-east-1"
    web_search_tool_name: str = ""
    web_search_max_results: int = 10

    # Mid-turn policy checks (chat/policy_check.py) — the deploy-time MASTER
    # gate above the per-run opt-in (the composer's Policy feature, carried as
    # ``features: ["sql", "policy:…"]``). Default OFF: with it unset no checker
    # is ever constructed, whatever the client sends — the feature degrades to
    # absent, exactly like OKF_CHAT_SQL_ENABLED above the SQL opt-in.
    policy_check_enabled: bool = False
    policy_check_model: str = DEFAULT_POLICY_CHECK_MODEL
    policy_shard_size: int = DEFAULT_POLICY_SHARD_SIZE
    policy_query_timeout_s: int = DEFAULT_POLICY_QUERY_TIMEOUT_S
    policy_query_max_per_turn: int = DEFAULT_POLICY_QUERY_MAX_PER_TURN

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChatConfig":
        env = env if env is not None else dict(os.environ)
        ttl_raw = env.get("OKF_CHAT_CHECKPOINT_TTL_SECONDS")
        return cls(
            bundle_bucket=env["OKF_BUNDLE_BUCKET"],
            vector_bucket=env["OKF_VECTOR_BUCKET"],
            vector_index=env["OKF_VECTOR_INDEX"],
            registry_table=env.get("OKF_REGISTRY_TABLE", "okf-registry"),
            annotations_table=env.get("OKF_ANNOTATIONS_TABLE", "okf-annotations"),
            report_harness_path=env.get("OKF_CHAT_REPORT_HARNESS_PATH", ""),
            report_max_bytes=int(env.get("OKF_CHAT_REPORT_MAX_BYTES", "") or 8_000_000),
            memory_id=env.get("OKF_CHAT_MEMORY_ID", ""),
            memory_default_on=env.get("OKF_CHAT_MEMORY_DEFAULT_ON", "true").lower()
            not in ("false", "0", ""),
            checkpoint_table=env.get("OKF_CHAT_CHECKPOINT_TABLE", "okf-chat-checkpoints"),
            threads_table=env.get("OKF_CHAT_THREADS_TABLE", "okf-chat"),
            catalog=parse_catalog(env.get("OKF_CHAT_MODEL_CATALOG")),
            default_model=env.get("OKF_CHAT_MODEL", DEFAULT_MODEL),
            default_effort=env.get("OKF_CHAT_EFFORT", DEFAULT_EFFORT),
            default_max_tokens=_int_env("OKF_CHAT_MAX_TOKENS", DEFAULT_MAX_TOKENS, env),
            region=env.get("AWS_REGION", "us-east-1"),
            mantle_region=env.get("OKF_CHAT_MANTLE_REGION", DEFAULT_MANTLE_REGION),
            bedrock_read_timeout=_int_env(
                "OKF_CHAT_BEDROCK_READ_TIMEOUT", DEFAULT_BEDROCK_READ_TIMEOUT, env
            ),
            bedrock_connect_timeout=_int_env(
                "OKF_CHAT_BEDROCK_CONNECT_TIMEOUT", DEFAULT_BEDROCK_CONNECT_TIMEOUT, env
            ),
            bedrock_max_attempts=_int_env(
                "OKF_CHAT_BEDROCK_MAX_ATTEMPTS", DEFAULT_BEDROCK_MAX_ATTEMPTS, env
            ),
            mantle_use_responses_api=env.get(
                "OKF_CHAT_MANTLE_USE_RESPONSES_API", "true"
            ).lower()
            not in ("false", "0", ""),
            mantle_base_url=env.get("OKF_CHAT_MANTLE_BASE_URL"),
            checkpoint_ttl_seconds=int(ttl_raw) if ttl_raw else None,
            checkpoint_offload_bucket=env.get("OKF_CHAT_CHECKPOINT_BUCKET", ""),
            sql_enabled=env.get("OKF_CHAT_SQL_ENABLED", "").lower()
            in ("true", "1", "yes"),
            athena_workgroup=env.get("OKF_ATHENA_WORKGROUP") or None,
            athena_output=env.get("OKF_ATHENA_OUTPUT") or None,
            athena_catalog=env.get("OKF_ATHENA_CATALOG", "AwsDataCatalog"),
            sql_max_rows=_int_env("OKF_CHAT_SQL_MAX_ROWS", 200, env),
            redshift_enabled=env.get("OKF_REDSHIFT_ENABLED", "").lower()
            in ("true", "1", "yes"),
            web_search_enabled=env.get("OKF_WEB_SEARCH_ENABLED", "").lower()
            in ("true", "1", "yes"),
            web_search_gateway_url=env.get("OKF_WEB_SEARCH_GATEWAY_URL", ""),
            web_search_region=env.get("OKF_WEB_SEARCH_REGION") or "us-east-1",
            web_search_tool_name=env.get("OKF_WEB_SEARCH_TOOL_NAME", ""),
            web_search_max_results=_int_env("OKF_WEB_SEARCH_MAX_RESULTS", 10, env),
            policy_check_enabled=env.get("OKF_CHAT_POLICY_CHECK_ENABLED", "").lower()
            in ("true", "1", "yes"),
            policy_check_model=env.get(
                "OKF_CHAT_POLICY_CHECK_MODEL", DEFAULT_POLICY_CHECK_MODEL
            ),
            policy_shard_size=_int_env(
                "OKF_CHAT_POLICY_SHARD_SIZE", DEFAULT_POLICY_SHARD_SIZE, env
            ),
            policy_query_timeout_s=_int_env(
                "OKF_CHAT_POLICY_QUERY_TIMEOUT_S", DEFAULT_POLICY_QUERY_TIMEOUT_S, env
            ),
            policy_query_max_per_turn=_int_env(
                "OKF_CHAT_POLICY_QUERY_MAX_PER_TURN",
                DEFAULT_POLICY_QUERY_MAX_PER_TURN,
                env,
            ),
        )

    def resolve_model_effort(
        self, model: str | None, effort: str | None
    ) -> tuple[str, str]:
        """Validate a per-run ``(model, effort)`` against the catalog; fill defaults.

        A ``None`` model falls back to the deploy-time default; the pair is then
        validated against the catalog (raises ``ModelCatalogError`` → surfaced as
        a run error). This is the trust boundary — an arbitrary client string can
        never reach ``bedrock:InvokeModel``.
        """
        return validate_model_effort(
            self.catalog, model or self.default_model, effort
        )


def _bedrock_config(cfg: ChatConfig):
    """botocore Config for the Converse client (lifted read timeout + retries)."""
    from botocore.config import Config

    return Config(
        read_timeout=cfg.bedrock_read_timeout,
        connect_timeout=cfg.bedrock_connect_timeout,
        retries={"max_attempts": cfg.bedrock_max_attempts, "mode": "adaptive"},
    )


def build_chat_model(cfg: ChatConfig, model: str, effort: str, max_tokens: int | None = None):
    """Build the pinned chat model for a conversation via the shared factory.

    Dispatches on the model id (``openai.``/``gpt-`` → Mantle ``ChatOpenAI``; else
    ``ChatBedrockConverse``) using ``cfg``'s regions/knobs. ``max_tokens`` defaults
    to the config's ceiling; the factory's own provider-aware default is not used
    here because chat pins an explicit ceiling per conversation.
    """
    from okf_aws import model_factory as mf

    max_tokens = max_tokens or cfg.default_max_tokens
    if mf.is_openai_model(model):
        return mf.build_mantle_openai(
            model,
            effort,
            max_tokens,
            region=cfg.mantle_region,
            use_responses_api=cfg.mantle_use_responses_api,
            base_url=cfg.mantle_base_url,
            timeout=cfg.bedrock_read_timeout,
            max_retries=cfg.bedrock_max_attempts,
            # The chat UI displays reasoning, so request a summary — on the
            # Responses API GPT thinks silently unless a summary is asked for.
            reasoning_summary="auto",
        )
    return mf.build_bedrock_converse(
        model,
        effort,
        max_tokens,
        region=cfg.region,
        botocore_config=_bedrock_config(cfg),
        # The chat UI displays reasoning, so ask Bedrock to STREAM a reasoning
        # summary (thinking.display="summarized"). Without this, adaptive thinking
        # runs but returns no reasoning_content — the "LLM is reasoning but I see
        # nothing" symptom. Harvest leaves this off. (Matches Sparky's Opus 4.8.)
        summarize_reasoning=True,
    )


def build_policy_check_model(cfg: ChatConfig):
    """Build the policy check's QUESTION-REWRITE client: minimal reasoning.

    The rewrite resolves cross-turn anaphora — extraction, not reasoning — so
    determinism matters more than depth. On Converse, thinking is turned OFF
    (not merely dialed down — Converse rejects a caller-set temperature while
    thinking is on) and temperature pinned to 0. On the GPT path reasoning is
    ``"none"`` (verbatim — the GPT-5.6 fleet accepts it): GPT-5.x reasoning
    models reject a non-default temperature outright, so none is sent.

    Not a plain :func:`build_chat_model` call with a small model: that one always
    configures adaptive thinking (the conversation wants it) and validates
    nothing here, since ``policy_check_model`` is deploy-time config rather than a
    client-supplied pair.
    """
    from okf_aws import model_factory as mf

    model = cfg.policy_check_model
    if mf.is_openai_model(model):
        return mf.build_mantle_openai(
            model,
            POLICY_CHECK_EFFORT,
            POLICY_CHECK_MAX_TOKENS,
            region=cfg.mantle_region,
            use_responses_api=cfg.mantle_use_responses_api,
            base_url=cfg.mantle_base_url,
            timeout=cfg.bedrock_read_timeout,
            max_retries=cfg.bedrock_max_attempts,
        )
    return mf.build_bedrock_converse(
        model,
        POLICY_CHECK_EFFORT,
        POLICY_CHECK_MAX_TOKENS,
        region=cfg.region,
        botocore_config=_bedrock_config(cfg),
        thinking=False,
        temperature=0,
    )


def build_policy_judge_model(cfg: ChatConfig):
    """Build the judge client — a CLASSIFIER, not a reasoner, on EVERY family.

    A judge checks ≤ shard-size policies against one query/window and
    answers with ids only: a Converse (Anthropic) id runs with thinking OFF
    and temperature 0; an OpenAI id (GPT 5.6 Terra) runs with reasoning
    ``"none"``. Single forward pass either way, and policy_check binds a
    FORCED ``report_violations`` tool choice (legal on Anthropic exactly
    because thinking is off), so a prose-only reply is structurally
    unreachable. Two reasons, both observed live (2026-08-03): reasoning
    judges spent 40-50s per shard (5-6k thinking tokens for a checklist),
    and occasionally exhausted into prose WITHOUT the verdict tool call,
    tripping the missing-call retry and doubling the shard. Same
    deploy-time model id as the rewrite (``OKF_CHAT_POLICY_CHECK_MODEL``).
    """
    from okf_aws import model_factory as mf

    model = cfg.policy_check_model
    if mf.is_openai_model(model):
        return mf.build_mantle_openai(
            model,
            "none",  # classifier: no reasoning pass
            POLICY_JUDGE_MAX_TOKENS,
            region=cfg.mantle_region,
            use_responses_api=cfg.mantle_use_responses_api,
            base_url=cfg.mantle_base_url,
            timeout=cfg.bedrock_read_timeout,
            max_retries=cfg.bedrock_max_attempts,
        )
    return mf.build_bedrock_converse(
        model,
        "none",  # unused: thinking is off
        POLICY_JUDGE_MAX_TOKENS,
        region=cfg.region,
        botocore_config=_bedrock_config(cfg),
        thinking=False,
        temperature=0,
    )
