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

# Chat defaults. Opus 4.8 (Converse) with adaptive thinking, like harvest — but
# an operator can point OKF_CHAT_MODEL at a lighter/faster model for interactive
# chat without touching harvest. A GPT id (openai.*) routes to Bedrock Mantle.
DEFAULT_MODEL = "us.anthropic.claude-opus-4-8"

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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChatConfig":
        env = env if env is not None else dict(os.environ)
        ttl_raw = env.get("OKF_CHAT_CHECKPOINT_TTL_SECONDS")
        return cls(
            bundle_bucket=env["OKF_BUNDLE_BUCKET"],
            vector_bucket=env["OKF_VECTOR_BUCKET"],
            vector_index=env["OKF_VECTOR_INDEX"],
            registry_table=env.get("OKF_REGISTRY_TABLE", "okf-registry"),
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
