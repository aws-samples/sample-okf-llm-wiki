"""Shared LLM client factory: build a LangChain chat model from a model id,
dispatching on provider.

This is the shared construction path for harvest, benchmarks, and chat.
It is deliberately **pure and parameterized** — it reads NO
environment variables. Each service supplies its own env-driven config (its
region, botocore timeouts, OpenAI client knobs) under its own ``OKF_<SERVICE>_*``
namespace and passes the resolved values in, so the provider logic lives in one
place while the deploy-time knobs stay service-scoped.

Provider selection is by model-id prefix (see :func:`is_openai_model`):

* GPT IDs, including ``global.openai.*`` → :func:`build_bedrock_openai` —
  ``ChatOpenAI`` using the Bedrock Runtime Responses API and a renewable,
  short-term Bedrock bearer. Bare GPT IDs gain a ``global.`` profile.
* everything else (the ``us./eu./global.anthropic.*`` inference profiles) →
  :func:`build_bedrock_converse` — a ``ChatBedrockConverse`` with adaptive
  thinking configured.

Reasoning is on by default because every agent path here wants it, but both
builders can also be driven as a plain, deterministic completion client
(``thinking=False`` / a minimal GPT effort, plus ``temperature``) for the cheap
extraction passes that surround the agents — a transcript summariser, a
question rewrite, a docs→rules conversion. Those want a fixed sampling
temperature, which is precisely what a reasoning model cannot honour.

All framework imports (``langchain_aws``, ``langchain_openai``,
``aws_bedrock_token_generator``) are deferred inside the builders, so importing
this module never requires them — services and their unit tests import it freely
and stub the SDKs.
"""

from __future__ import annotations

import re
from typing import Any

from okf_core.harvest_models import is_openai_model, normalize_model_id

# --- Bedrock Converse (Claude / Anthropic) ----------------------------------

# Effort is passed to Bedrock's adaptive-thinking ``output_config.effort``
# VERBATIM. Bedrock is the authority on which values a given model accepts (it
# varies per model — e.g. Opus 4.8 supports "xhigh"), so we keep NO client-side
# allow-list that could reject a valid value.


# Claude version extractor for the adaptive-thinking capability gate. Both id
# shapes are covered: family-first (``claude-haiku-4-5-20251001``,
# ``claude-opus-4-8-…``) and version-first (``claude-3-5-sonnet-20241022``).
_CLAUDE_VERSION_RE = re.compile(r"claude-(?:[a-z]+-)?(\d+)[.-](\d+)")

# Adaptive thinking (``thinking.type=adaptive`` + ``output_config.effort``)
# arrived with the Claude 4.6 generation.
_ADAPTIVE_SINCE = (4, 6)


def converse_supports_adaptive(model: str) -> bool:
    """Whether a Converse (Anthropic) id accepts the ADAPTIVE thinking shape.

    Pre-adaptive generations (< 4.6 — Haiku 4.5, Sonnet 4.5, every 3.x) take
    a token budget (``thinking.type=enabled`` + ``budget_tokens``) and REJECT
    the adaptive form with a ``ValidationException`` — a caller that guesses
    the encoding by family-name substring silently mis-encodes every other
    pre-adaptive id. This predicate is the single owner of that generation
    knowledge; callers pick ``thinking_budget`` vs effort off it. Unknown /
    unparseable ids default to adaptive (current generations are the common
    case, and a wrong guess there fails loudly at invoke time either way).
    """
    m = _CLAUDE_VERSION_RE.search(model)
    if not m:
        return True
    return (int(m.group(1)), int(m.group(2))) >= _ADAPTIVE_SINCE


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
    thinking: bool = True,
    temperature: float | None = None,
    thinking_budget: int | None = None,
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

    ``thinking=False`` OMITS ``additional_model_request_fields`` entirely
    (rather than sending a disabled thinking block, which Converse has no
    encoding for) — the model runs as a plain completion client. ``effort`` is
    then unused, so callers of the non-reasoning path may pass any value.
    That switch is what makes ``temperature`` meaningful: Converse rejects a
    caller-set temperature while thinking is on, so a deterministic extraction
    pass MUST turn thinking off, not merely pin the temperature. ``temperature``
    is forwarded only when given, so the default remains the model's own.
    """
    from langchain_aws import ChatBedrockConverse

    kwargs: dict[str, Any] = {
        "model": model,
        "region_name": region,
        "max_tokens": max_tokens,
        "config": botocore_config,
        "callbacks": callbacks,
    }
    if thinking:
        # Two thinking encodings, model-generation dependent: adaptive+effort is
        # the 4.6+ shape; pre-adaptive models (e.g. Haiku 4.5) take a token
        # budget instead and REJECT the adaptive form. thinking_budget selects
        # the budget encoding; the caller's max_tokens must exceed it.
        if thinking_budget is not None:
            kwargs["additional_model_request_fields"] = {
                "thinking": {"type": "enabled", "budget_tokens": int(thinking_budget)}
            }
        else:
            kwargs["additional_model_request_fields"] = thinking_fields(
                effort, summarize=summarize_reasoning
            )
    if temperature is not None:
        kwargs["temperature"] = temperature
    return ChatBedrockConverse(**kwargs)


# --- GPT on Bedrock Runtime (OpenAI-compatible) ------------------------------

# Keep the configured context and output budgets available to LangChain even
# when its registry does not recognize Bedrock inference-profile IDs.
_GPT_PROFILES = {
    "openai.gpt-6-astra": {"max_input_tokens": 1050000, "max_output_tokens": 128000},
    "openai.gpt-6-sol": {"max_input_tokens": 1000000, "max_output_tokens": 32000},
    "openai.gpt-6-luna": {"max_input_tokens": 1000000, "max_output_tokens": 32000},
    "openai.gpt-5.6-terra": {"max_input_tokens": 1000000, "max_output_tokens": 32000},
}


def gpt_profile(model: str) -> dict[str, int]:
    """Configured limits for a known GPT model, independent of its profile prefix."""
    normalized = normalize_model_id(model)
    return dict(_GPT_PROFILES.get(normalized.split(".", 1)[-1], {}))


# Map Converse effort levels onto OpenAI's ``reasoning_effort`` scale
# (none|minimal|low|medium|high|xhigh|max). GPT-5.6 (Sol/Luna/Terra) added
# "max" as a distinct level ABOVE xhigh, so the whole vocabulary passes through
# verbatim — every Converse level has a same-named OpenAI level. In particular
# DON'T collapse "max"->"xhigh" (that silently downgrades a deliberately-max
# run). Which efforts a given model actually accepts is model-specific and is
# enforced by the model catalog (the trust boundary) + Bedrock, NOT here.
# Unknown values fall through to xhigh so a stray effort never quietly
# downgrades the model.
#
# "none"/"minimal" are the deliberate exception to that fallthrough: they mean
# "this call is extraction/classification, not reasoning" (a transcript pass,
# a rewrite, a policy judge), and
# letting them reach the xhigh default would silently bill a max-reasoning run
# for a job that wanted none. Keep the existing mapping: "none" passes through
# for the policy classifiers, and "minimal" floors to "low". Bedrock validates
# the actual model's supported levels at invocation.
FLOOR_GPT_REASONING_EFFORT = "low"
GPT_EFFORT_MAP = {
    "max": "max",
    "xhigh": "xhigh",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "minimal": FLOOR_GPT_REASONING_EFFORT,
    "none": "none",
}
DEFAULT_GPT_REASONING_EFFORT = "xhigh"

# How long a minted Bedrock Runtime bearer token is trusted before we re-mint. The token
# is a SigV4-PRESIGNED URL, so its effective life is min(requested expiry, life
# of the signing credentials). On AgentCore the signing creds are TEMPORARY
# role creds (~1h), so a token minted once and cached for a whole multi-hour run
# would die mid-run. Re-mint well inside that window.
DEFAULT_TOKEN_TTL_SECONDS = 1800  # 30 min: comfortably under the ~1h creds life


def gpt_effort(effort: str) -> str:
    """Map a Converse effort level onto OpenAI's ``reasoning_effort`` scale."""
    if not effort:
        raise ValueError("effort must be a non-empty string")
    return GPT_EFFORT_MAP.get(effort, DEFAULT_GPT_REASONING_EFFORT)


def bedrock_token_provider(region: str, *, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS):
    """A callable that returns a FRESH Bedrock Runtime bearer token, cached briefly.

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


def build_bedrock_openai(
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
    temperature: float | None = None,
):
    """Construct ``ChatOpenAI`` for Bedrock Runtime's OpenAI-compatible route.

    Auth is a short-lived bearer token minted by ``provide_token(region=...)``:
    a SigV4-derived Bedrock API key that inherits the runtime role's IAM (so an
    existing ``bedrock:InvokeModel*`` grant covers it — no API key or Secrets
    Manager). We pass a TOKEN PROVIDER CALLABLE (not a pre-minted string): the
    token is a presigned URL whose life is bounded by the signing role creds
    (~1h on AgentCore), so a single token can't cover a multi-hour run; the
    openai SDK re-invokes the callable per request (see
    :func:`bedrock_token_provider`).

    ``base_url`` defaults to ``bedrock-runtime.<region>.amazonaws.com/openai/v1``.
    The SDK calls Responses by default, or Chat Completions when
    ``use_responses_api=False``; both use the same base URL.
    Pass an explicit ``base_url`` to override. The botocore config doesn't apply
    to ``ChatOpenAI`` (it's an httpx client), so read timeout + retry budget map
    onto ``timeout`` / ``max_retries``.

    GPT IDs carry an inference profile; bare IDs gain ``global.``. Known GPT
    models supply context metadata and clamp ``max_tokens`` to their configured
    output ceiling. Responses requests set ``store=False`` and replay local
    conversation history rather than referring to a previous server response.

    ``reasoning_summary`` (e.g. ``"auto"``/``"detailed"``): on the Responses API,
    reasoning models THINK regardless of ``reasoning_effort`` but only RETURN the
    thinking to the client when a summary is requested. Pass this when the UI
    displays reasoning (chat) — it maps to ``reasoning={effort, summary}``. When
    None (harvest's default), we keep the plain ``reasoning_effort`` so nothing
    changes for callers that don't surface thinking. Only meaningful with the
    Responses API; ignored for Chat Completions (gpt-oss).

    ``temperature`` is forwarded only when given (default: the model's own), for
    extraction passes that need determinism. Pair it with effort
    ``"none"``/``"minimal"`` — see :data:`GPT_EFFORT_MAP`; a GPT model at a high
    reasoning effort samples regardless of what temperature asks for.
    """
    from langchain_openai import ChatOpenAI

    if base_url is None:
        base_url = f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
    kwargs: dict[str, Any] = {
        "model": normalize_model_id(model),
        "base_url": base_url,
        "api_key": bedrock_token_provider(region, ttl_seconds=token_ttl_seconds),
        "use_responses_api": use_responses_api,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "max_retries": max_retries,
        "callbacks": callbacks,
    }
    profile = gpt_profile(model)
    if profile:
        kwargs["profile"] = profile
        kwargs["max_tokens"] = min(max_tokens, profile["max_output_tokens"])
    if temperature is not None:
        kwargs["temperature"] = temperature
    if use_responses_api:
        # Conversation history lives in our checkpoints and is replayed on each
        # request. Do not depend on provider-side stored response IDs.
        kwargs["store"] = False
        kwargs["use_previous_response_id"] = False
        kwargs["output_version"] = "responses/v1"
    if reasoning_summary and use_responses_api:
        # The `reasoning` object controls BOTH effort and whether a summary is
        # returned; use it INSTEAD of reasoning_effort (they're the same knob).
        kwargs["reasoning"] = {
            "effort": gpt_effort(effort),
            "summary": reasoning_summary,
        }
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
    openai_use_responses_api: bool = True,
    openai_base_url: str | None = None,
    openai_timeout: int = 600,
    openai_max_retries: int = 5,
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    openai_reasoning_summary: str | None = None,
    callbacks: Any = None,
):
    """Build the chat model, dispatching on the model id's provider.

    GPT IDs build a Bedrock Runtime ``ChatOpenAI`` using the ``openai_*``
    params; everything else builds a Converse model. Both use the supplied
    deployment ``region``. Either way the result is a plain ``BaseChatModel``.

    A convenience for callers that don't need per-field env overrides; services
    with their own ``OKF_<SERVICE>_*`` knobs may instead dispatch on
    :func:`is_openai_model` and call the two builders directly (as harvest
    does).
    """
    if is_openai_model(model):
        return build_bedrock_openai(
            model,
            effort,
            max_tokens,
            region=region,
            use_responses_api=openai_use_responses_api,
            base_url=openai_base_url,
            timeout=openai_timeout,
            max_retries=openai_max_retries,
            token_ttl_seconds=token_ttl_seconds,
            reasoning_summary=openai_reasoning_summary,
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
