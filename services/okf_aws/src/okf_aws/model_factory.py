"""Shared LLM client factory: build a LangChain chat model from a model id,
dispatching on provider.

This is the single construction path behind every OKF agent (harvest today, the
chat agent next). It is deliberately **pure and parameterized** — it reads NO
environment variables. Each service supplies its own env-driven config (its
region, botocore timeouts, Mantle knobs) under its own ``OKF_<SERVICE>_*``
namespace and passes the resolved values in, so the provider logic lives in one
place while the deploy-time knobs stay service-scoped.

Provider selection is by model-id prefix (see :func:`is_openai_model`):

* ``openai.<name>`` / ``gpt-…`` → :func:`build_mantle_openai` — a ``ChatOpenAI``
  pointed at the Bedrock **Mantle** OpenAI-compatible endpoint, authed with a
  short-lived SigV4-derived bearer token (no API key / Secrets Manager).
* everything else (the ``us./eu./global.anthropic.*`` inference profiles) →
  :func:`build_bedrock_converse` — a ``ChatBedrockConverse`` with adaptive
  thinking configured.

All framework imports (``langchain_aws``, ``langchain_openai``,
``aws_bedrock_token_generator``) are deferred inside the builders, so importing
this module never requires them — services and their unit tests import it freely
and stub the SDKs.
"""

from __future__ import annotations

from typing import Any

# --- Bedrock Converse (Claude / Anthropic) ----------------------------------

# Effort is passed to Bedrock's adaptive-thinking ``output_config.effort``
# VERBATIM. Bedrock is the authority on which values a given model accepts (it
# varies per model — e.g. Opus 4.8 supports "xhigh"), so we keep NO client-side
# allow-list that could reject a valid value.


def thinking_fields(effort: str, *, summarize: bool = False) -> dict[str, Any]:
    """``additionalModelRequestFields`` for adaptive thinking at ``effort``.

    ``thinking.type=adaptive`` + ``output_config.effort=<level>``. The effort
    MUST live in a SEPARATE ``output_config`` object — nesting it inside
    ``thinking`` is a Bedrock ``ValidationException``.

    ``summarize`` adds ``thinking.display="summarized"``, which is what makes
    Bedrock STREAM BACK a reasoning summary (as ``reasoning_content`` blocks). By
    default adaptive thinking runs but returns NO reasoning to the client — set
    this when the caller displays reasoning (chat). Harvest leaves it off; it
    doesn't render thinking and the extra summary tokens are pure cost there.
    (This mirrors Sparky's Opus 4.8 config: ``{"type":"adaptive","display":"summarized"}``.)
    """
    if not effort:
        raise ValueError("effort must be a non-empty string")
    thinking: dict[str, Any] = {"type": "adaptive"}
    if summarize:
        thinking["display"] = "summarized"
    return {"thinking": thinking, "output_config": {"effort": effort}}


def build_bedrock_converse(
    model: str,
    effort: str,
    max_tokens: int,
    *,
    region: str,
    botocore_config: Any = None,
    callbacks: Any = None,
    summarize_reasoning: bool = False,
):
    """Construct a ``ChatBedrockConverse`` with adaptive thinking configured.

    Built explicitly (rather than passing a model string to a higher-level
    agent factory) so the thinking config rides on the model via
    ``additional_model_request_fields`` — no reliance on kwarg forwarding. The
    caller supplies ``region`` and an optional botocore ``config`` (e.g. lifted
    read timeout + adaptive retries so a long, slow turn is retried rather than
    fatal). ``callbacks`` attach to the MODEL INSTANCE so they fire for every
    turn on every dispatch path.

    ``summarize_reasoning`` requests a streamed reasoning summary
    (``thinking.display="summarized"``) — pass it when the UI shows thinking
    (chat). Off by default so harvest is unchanged.
    """
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(
        model=model,
        region_name=region,
        max_tokens=max_tokens,
        additional_model_request_fields=thinking_fields(
            effort, summarize=summarize_reasoning
        ),
        config=botocore_config,
        callbacks=callbacks,
    )


# --- GPT on Bedrock Mantle (OpenAI-compatible) ------------------------------

# GPT-5.x on Mantle lives only in us-east-2 / us-west-2 — independent of a
# service's own AWS_REGION — so the Mantle region is always an explicit param.
DEFAULT_MANTLE_REGION = "us-east-2"

# Map Converse effort levels onto OpenAI's ``reasoning_effort`` scale
# (none|minimal|low|medium|high|xhigh|max). GPT-5.6 (Sol/Luna/Terra) added
# "max" as a distinct level ABOVE xhigh, so the whole vocabulary passes through
# verbatim — every Converse level has a same-named OpenAI level. In particular
# DON'T collapse "max"->"xhigh" (that silently downgrades a deliberately-max
# run). Which efforts a given model actually accepts is model-specific and is
# enforced by the model catalog (the trust boundary) + Bedrock, NOT here.
# Unknown values fall through to xhigh so a stray effort never quietly
# downgrades the model.
GPT_EFFORT_MAP = {
    "max": "max",
    "xhigh": "xhigh",
    "high": "high",
    "medium": "medium",
    "low": "low",
}
DEFAULT_GPT_REASONING_EFFORT = "xhigh"

# How long a minted Mantle bearer token is trusted before we re-mint. The token
# is a SigV4-PRESIGNED URL, so its effective life is min(requested expiry, life
# of the signing credentials). On AgentCore the signing creds are TEMPORARY
# role creds (~1h), so a token minted once and cached for a whole multi-hour run
# would die mid-run. Re-mint well inside that window.
DEFAULT_TOKEN_TTL_SECONDS = 1800  # 30 min: comfortably under the ~1h creds life


def is_openai_model(model: str) -> bool:
    """True when ``model`` names an OpenAI GPT model served on Bedrock Mantle.

    Mantle GPT ids are ``openai.<name>`` (e.g. ``openai.gpt-5.6-sol``); the bare
    ``gpt-`` form is accepted too for local/dev use. Everything else — the
    ``us./eu./global.anthropic.*`` Converse profiles — stays on Converse.
    """
    return model.startswith("openai.") or model.startswith("gpt-")


def gpt_effort(effort: str) -> str:
    """Map a Converse effort level onto OpenAI's ``reasoning_effort`` scale."""
    if not effort:
        raise ValueError("effort must be a non-empty string")
    return GPT_EFFORT_MAP.get(effort, DEFAULT_GPT_REASONING_EFFORT)


def mantle_token_provider(region: str, *, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS):
    """A callable that returns a FRESH Mantle bearer token, cached briefly.

    ``langchain_openai`` / the openai SDK accept ``api_key`` as a
    ``Callable[[], str]`` and invoke it PER REQUEST, so returning a callable
    here — rather than a pre-minted string — is what keeps a long run
    authenticated: every request re-reads a currently-valid token. We cache for
    ``ttl_seconds`` so we don't run a SigV4 presign on every call, while staying
    well under the signing creds' ~1h life. ``provide_token`` re-signs with
    whatever creds the default chain currently holds, so it naturally picks up
    refreshed role credentials.
    """
    import time

    from aws_bedrock_token_generator import provide_token

    cache: dict[str, Any] = {"token": None, "exp": 0.0}

    def _provider() -> str:
        now = time.time()
        if cache["token"] is None or now >= cache["exp"]:
            cache["token"] = provide_token(region=region)
            cache["exp"] = now + ttl_seconds
        return cache["token"]

    return _provider


def build_mantle_openai(
    model: str,
    effort: str,
    max_tokens: int,
    *,
    region: str,
    use_responses_api: bool = True,
    base_url: str | None = None,
    timeout: int = 600,
    max_retries: int = 5,
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    reasoning_summary: str | None = None,
    callbacks: Any = None,
):
    """Construct a ``ChatOpenAI`` pointed at the Bedrock Mantle OpenAI endpoint.

    Auth is a short-lived bearer token minted by ``provide_token(region=...)``:
    a SigV4-derived Bedrock API key that inherits the runtime role's IAM (so an
    existing ``bedrock:InvokeModel*`` grant covers it — no API key or Secrets
    Manager). We pass a TOKEN PROVIDER CALLABLE (not a pre-minted string): the
    token is a presigned URL whose life is bounded by the signing role creds
    (~1h on AgentCore), so a single token can't cover a multi-hour run; the
    openai SDK re-invokes the callable per request (see
    :func:`mantle_token_provider`).

    ``base_url`` defaults to the Mantle endpoint for ``region``: the Responses
    API at ``/openai/v1`` (which GPT-5.x requires) unless ``use_responses_api``
    is False, in which case Chat Completions at ``/v1`` (for gpt-oss models).
    Pass an explicit ``base_url`` to override. The botocore config doesn't apply
    to ``ChatOpenAI`` (it's an httpx client), so read timeout + retry budget map
    onto ``timeout`` / ``max_retries``.

    ``reasoning_summary`` (e.g. ``"auto"``/``"detailed"``): on the Responses API,
    reasoning models THINK regardless of ``reasoning_effort`` but only RETURN the
    thinking to the client when a summary is requested. Pass this when the UI
    displays reasoning (chat) — it maps to ``reasoning={effort, summary}``. When
    None (harvest's default), we keep the plain ``reasoning_effort`` so nothing
    changes for callers that don't surface thinking. Only meaningful with the
    Responses API; ignored for Chat Completions (gpt-oss).
    """
    from langchain_openai import ChatOpenAI

    if base_url is None:
        default_path = "openai/v1" if use_responses_api else "v1"
        base_url = f"https://bedrock-mantle.{region}.api.aws/{default_path}"
    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": mantle_token_provider(region, ttl_seconds=token_ttl_seconds),
        "use_responses_api": use_responses_api,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "max_retries": max_retries,
        "callbacks": callbacks,
    }
    if reasoning_summary and use_responses_api:
        # The `reasoning` object controls BOTH effort and whether a summary is
        # returned; use it INSTEAD of reasoning_effort (they're the same knob).
        kwargs["reasoning"] = {
            "effort": gpt_effort(effort),
            "summary": reasoning_summary,
        }
        # output_version="responses/v1" puts the reasoning SUMMARY into the
        # message CONTENT as {"type":"reasoning","summary":[{"text":…}]} blocks
        # (streamable + where our chunk parser reads it). Without it the summary
        # lands in additional_kwargs and never surfaces in the stream — the
        # "GPT shows no reasoning" bug.
        kwargs["output_version"] = "responses/v1"
    else:
        kwargs["reasoning_effort"] = gpt_effort(effort)
    return ChatOpenAI(**kwargs)


# --- Dispatcher --------------------------------------------------------------


def build_model(
    model: str,
    effort: str,
    max_tokens: int,
    *,
    region: str,
    botocore_config: Any = None,
    mantle_region: str | None = None,
    mantle_use_responses_api: bool = True,
    mantle_base_url: str | None = None,
    mantle_timeout: int = 600,
    mantle_max_retries: int = 5,
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    mantle_reasoning_summary: str | None = None,
    callbacks: Any = None,
):
    """Build the chat model, dispatching on the model id's provider.

    ``openai.``/``gpt-`` ids build a Mantle ``ChatOpenAI`` (using the
    ``mantle_*`` params, with ``mantle_region`` defaulting to
    :data:`DEFAULT_MANTLE_REGION`); everything else builds a Converse model in
    ``region``. Either way the result is a plain ``BaseChatModel``.

    A convenience for callers that don't need per-field env overrides; services
    with their own ``OKF_<SERVICE>_*`` knobs may instead dispatch on
    :func:`is_openai_model` and call the two builders directly (as harvest
    does).
    """
    if is_openai_model(model):
        return build_mantle_openai(
            model,
            effort,
            max_tokens,
            region=mantle_region or DEFAULT_MANTLE_REGION,
            use_responses_api=mantle_use_responses_api,
            base_url=mantle_base_url,
            timeout=mantle_timeout,
            max_retries=mantle_max_retries,
            token_ttl_seconds=token_ttl_seconds,
            reasoning_summary=mantle_reasoning_summary,
            callbacks=callbacks,
        )
    return build_bedrock_converse(
        model,
        effort,
        max_tokens,
        region=region,
        botocore_config=botocore_config,
        callbacks=callbacks,
    )
