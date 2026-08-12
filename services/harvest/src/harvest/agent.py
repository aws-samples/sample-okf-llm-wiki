"""Build the harvest deep agent for one dataset session.

Wires together, per the design:
- a ``FilesystemBackend(root_dir=<dataset root>, virtual_mode=True)`` for
  per-dataset containment (blocks ``../``/``~``/absolute escapes), wrapped in a
  ``CompositeBackend`` so deepagents' internal scratch files stay ephemeral;
- ``GlueAthenaSource`` LIVE tools (sample_rows / run_sql); static Glue metadata is
  snapshotted to the read-only ``.metadata/`` dir before the run and read with the
  built-in file tools (see ``metadata_export``);
- a per-session ``LinkGraph`` with ``get_backlinks``/``get_links``/
  ``cluster_concepts`` tools;
- ``OKFGuardMiddleware`` (frontmatter + augmentation guard, timestamp auto-fill,
  graph dirty-flag) — attached to the main agent AND the per-table sub-agent
  (sub-agent middleware/tools REPLACE, so we pass them explicitly);
- four dynamic sub-agents the supervisor fans out via ``task()``: ``table-author``
  (one per table), ``reference-author`` (one per cross-cutting reference — metric,
  named-set, glossary term, known-issue, or the usage-guardrails contract),
  ``reviewer`` (one per link-cluster of ≤5 authored docs from
  ``cluster_concepts``, adversarial read-only verification), and
  ``context-extractor`` (read-only; mines the uploaded ``.context/`` docs for
  verified facts and returns a routed digest — fanned out one-per-doc/group for a
  large ``.context/`` so the heavy reading happens once);
- an optional ``run_code`` tool backed by a network-isolated AgentCore Code
  Interpreter sandbox (when a ``CodeSandbox`` is supplied), so the agent can
  extract text from binary ``.context/`` docs (PDF/DOCX/PPTX/XLSX) the built-in
  ``read_file`` can't decode. Added to ``all_tools`` so it reaches the main agent
  AND both sub-agents (which REPLACE tools) for free.

All framework imports are deferred to ``build_harvest_agent`` so the module (and
the pieces it composes) import cleanly for unit tests without deepagents/AWS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harvest.fsutil import mkdirs
from harvest.graph_tools import make_graph_tools
from harvest.guard_engine import OKFGuardEngine
from harvest.lint_tool import make_lint_tool
from harvest.stats_tool import make_stats_tool
from harvest.okf_guard import (
    OKFGuardMiddleware,
    SubagentDispatchGuard,
    ToolErrorMiddleware,
)
from harvest.prompts import (
    build_context_extractor_prompt,
    build_context_reviewer_prompt,
    build_cross_author_prompt,
    build_cross_reviewer_prompt,
    build_cross_supervisor_prompt,
    build_fixer_prompt,
    build_reference_author_prompt,
    build_reviewer_prompt,
    build_supervisor_prompt,
    build_table_author_prompt,
)
from harvest.source_base import Source
from harvest.source_tools import make_source_tools
from okf_core.link_graph import LinkGraph

# Claude Opus 4.8 on Bedrock via the Converse API. Default to the US cross-region
# inference profile id; override per-region (eu./jp./au.) or to global. via the
# OKF_HARVEST_MODEL env var. Opus 4.8 ONLY supports adaptive thinking (manual
# budget_tokens / disabled would 400), so we always send thinking.type=adaptive.
DEFAULT_MODEL = "us.anthropic.claude-opus-4-8"

# Opus 4.8 authoring wants maximum reasoning per table. The effort value is
# passed through to Bedrock's adaptive-thinking output_config.effort; Bedrock is
# the authority on which values a given model accepts (it varies by model —
# e.g. Opus 4.8 supports "xhigh"), so we do NOT keep a client-side allow-list
# that could reject a valid value. Override via OKF_HARVEST_EFFORT.
DEFAULT_EFFORT = "xhigh"

# Opus 4.8 allows up to 128K output tokens; give the authoring agent the full
# headroom since adaptive-max reasoning tokens count against this budget.
DEFAULT_MAX_TOKENS = 128000

# Max dynamic subagents (reviewer/table-author) allowed to run CONCURRENTLY when
# the supervisor fans out via the QuickJS task() global. langchain_quickjs gates
# task() calls with an asyncio.Semaphore per REPL (default 32); we lower it so a
# Promise.all over N tables only keeps this many Opus 4.8 crawls in flight at
# once (the rest queue transparently) — bounding Bedrock throttling + peak cost.
# Override via OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY.
DEFAULT_SUBAGENT_CONCURRENCY = 5

# botocore's default read_timeout is 60s, but Opus 4.8 at xhigh effort on a large
# authoring/planning prompt can spend minutes generating before the first byte of
# the Converse response arrives — a single slow turn then trips ReadTimeoutError
# and fails the whole harvest. Give the bedrock-runtime client generous headroom
# and let botocore retry transient timeouts. Overridable via env.
DEFAULT_BEDROCK_READ_TIMEOUT = 600
DEFAULT_BEDROCK_CONNECT_TIMEOUT = 10
DEFAULT_BEDROCK_MAX_ATTEMPTS = 5

# --- GPT on Bedrock Mantle --------------------------------------------------
# The harvest agent can also run on OpenAI GPT models, which Bedrock serves via
# the Mantle endpoint's OpenAI-COMPATIBLE API (Chat Completions), NOT the native
# Converse API. So the LangChain client is ChatOpenAI (langchain-openai) pointed
# at the Mantle base URL, authed with a short-lived bearer token from
# aws_bedrock_token_generator.provide_token() — a SigV4-derived Bedrock API key
# that inherits the runtime's IAM identity (no API key / Secrets Manager). A
# model id starting with "openai." / "gpt-" selects this path (see
# _is_openai_model); anything else stays on Converse. Set OKF_HARVEST_MODEL to
# e.g. "openai.gpt-5.6-sol" to run GPT.
#
# Region is INDEPENDENT of AWS_REGION: GPT-5.x on Mantle lives only in
# us-east-2 / us-west-2, while the harvest runtime itself may deploy elsewhere
# (e.g. eu-west-1). So the Mantle region has its own env var and both the base
# URL and the token are minted for it. Override via OKF_HARVEST_MANTLE_REGION.
DEFAULT_MANTLE_REGION = "us-east-2"

# GPT reasoning models cap output well below Opus 4.8's 128K; give the GPT path
# its own default so we don't send a Claude-sized ceiling. Overridable via the
# usual OKF_HARVEST_MAX_TOKENS (resolve_model_config), but this is the fallback
# the GPT builder uses if the caller didn't lower it from the Claude default.
DEFAULT_GPT_MAX_TOKENS = 32000

# Converse effort levels map onto OpenAI's reasoning_effort scale verbatim (the
# mapping now lives in okf_aws.model_factory.GPT_EFFORT_MAP — GPT-5.6 added "max"
# above xhigh, so every Converse level has a same-named OpenAI level and nothing
# is collapsed). _gpt_effort delegates there; this fallback constant is retained
# for callers/tests that reference it directly.
DEFAULT_GPT_REASONING_EFFORT = "xhigh"


def _skill_root() -> Path | None:
    """Locate the vendored skills directory (the parent of ``okf-authoring/``).

    deepagents' ``skills=[...]`` wants a top-level skills dir containing one
    subdir per skill. We ship ``services/harvest/skills/okf-authoring/`` in the
    image. Resolution order: ``OKF_SKILLS_DIR`` env override, then the packaged
    location relative to this module. Returns None if not found (agent still
    runs with the OKF procedure inlined in the prompts as a fallback).
    """
    candidates: list[Path] = []
    env = os.environ.get("OKF_SKILLS_DIR")
    if env:
        candidates.append(Path(env))
    # __file__ = .../harvest/src/harvest/agent.py -> parents[2] = .../harvest
    candidates.append(Path(__file__).resolve().parents[2] / "skills")
    for c in candidates:
        if (c / "okf-authoring" / "SKILL.md").is_file():
            return c.resolve()
    return None


def _cap_subagent_concurrency(limit: int) -> None:
    """Lower langchain_quickjs's per-REPL concurrent-task() cap to ``limit``.

    langchain_quickjs bounds concurrent ``task()`` dispatches with an
    ``asyncio.Semaphore(_MAX_TASK_CALLS_PER_THREAD)`` sized from a module
    constant (default 32), read lazily when a REPL is first built. There's no
    public constructor knob, so we set the constant BEFORE constructing
    ``CodeInterpreterMiddleware`` (which builds the REPL). A ``Promise.all`` over
    N subagents then keeps only ``limit`` in flight; the rest queue on the
    semaphore. Best-effort: if the internal module/attr moves in a future
    version, we log and fall back to the library default rather than crash.
    """
    if limit is None or limit < 1:
        return
    try:
        from langchain_quickjs import _repl

        _repl._MAX_TASK_CALLS_PER_THREAD = int(limit)
    except Exception:  # noqa: BLE001 - concurrency cap is best-effort
        import logging

        logging.getLogger(__name__).warning(
            "Could not set langchain_quickjs subagent concurrency cap to %s "
            "(internal API may have changed); using library default.",
            limit,
            exc_info=True,
        )


def resolve_model_config(
    model_override: str | None = None,
    effort_override: str | None = None,
) -> dict[str, Any]:
    """Model config, with Opus 4.8 / adaptive-max defaults.

    Precedence for model + effort: the per-invocation OVERRIDE (from the harvest
    payload, chosen in the UI and validated by the Control API) wins; else the
    deploy-time env var (OKF_HARVEST_MODEL / OKF_HARVEST_EFFORT); else the built-in
    default. max_tokens + subagent concurrency remain env/deploy-time only.

    The ``max_tokens`` FALLBACK is provider-aware: OpenAI GPT models cap output
    well below Opus 4.8's 128K, so an unset OKF_HARVEST_MAX_TOKENS defaults to
    the GPT ceiling for GPT ids and the Opus ceiling otherwise — and it keys off
    the RESOLVED model, so a per-run switch to GPT lowers the ceiling correctly.
    An explicit OKF_HARVEST_MAX_TOKENS always wins.
    """
    model = model_override or os.environ.get("OKF_HARVEST_MODEL", DEFAULT_MODEL)
    effort = effort_override or os.environ.get("OKF_HARVEST_EFFORT", DEFAULT_EFFORT)
    max_tokens_raw = os.environ.get("OKF_HARVEST_MAX_TOKENS")
    conc_raw = os.environ.get("OKF_HARVEST_MAX_SUBAGENT_CONCURRENCY")
    default_max_tokens = (
        DEFAULT_GPT_MAX_TOKENS if _is_openai_model(model) else DEFAULT_MAX_TOKENS
    )
    return {
        "model": model,
        "effort": effort,
        "max_tokens": int(max_tokens_raw) if max_tokens_raw else default_max_tokens,
        "subagent_concurrency": (
            int(conc_raw) if conc_raw else DEFAULT_SUBAGENT_CONCURRENCY
        ),
    }


def _thinking_fields(effort: str) -> dict[str, Any]:
    """additionalModelRequestFields for adaptive thinking at the given effort.

    Thin wrapper over the shared :func:`okf_aws.model_factory.thinking_fields`
    (kept as a module-private alias so harvest's callers/tests are unchanged).
    """
    from okf_aws.model_factory import thinking_fields

    return thinking_fields(effort)


def _int_env(name: str, default: int) -> int:
    """Read an int from env ``name``, falling back to ``default`` when unset."""
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _bedrock_config():
    """botocore Config for the bedrock-runtime client behind ChatBedrockConverse.

    Extends the read timeout well past botocore's 60s default (a single xhigh
    Opus 4.8 turn can generate for minutes) and enables adaptive retries, so a
    transient slow/stalled Converse response is retried instead of failing the
    whole harvest with ReadTimeoutError. All three knobs are env-overridable.
    """
    from botocore.config import Config

    return Config(
        read_timeout=_int_env(
            "OKF_HARVEST_BEDROCK_READ_TIMEOUT", DEFAULT_BEDROCK_READ_TIMEOUT
        ),
        connect_timeout=_int_env(
            "OKF_HARVEST_BEDROCK_CONNECT_TIMEOUT", DEFAULT_BEDROCK_CONNECT_TIMEOUT
        ),
        retries={
            "max_attempts": _int_env(
                "OKF_HARVEST_BEDROCK_MAX_ATTEMPTS", DEFAULT_BEDROCK_MAX_ATTEMPTS
            ),
            "mode": "adaptive",
        },
    )


def _is_openai_model(model: str) -> bool:
    """True when ``model`` names an OpenAI GPT model served on Bedrock Mantle.

    Thin wrapper over the shared :func:`okf_aws.model_factory.is_openai_model`.
    """
    from okf_aws.model_factory import is_openai_model

    return is_openai_model(model)


def _gpt_effort(effort: str) -> str:
    """Map a Converse effort level onto OpenAI's ``reasoning_effort`` scale.

    Thin wrapper over the shared :func:`okf_aws.model_factory.gpt_effort`.
    """
    from okf_aws.model_factory import gpt_effort

    return gpt_effort(effort)


def _build_model(
    model: str,
    effort: str,
    max_tokens: int,
    callbacks=None,
    *,
    surface_reasoning: bool = False,
):
    """Build the harvest chat model, dispatching on the model id's provider.

    ``openai.``/``gpt-`` ids build a ChatOpenAI against the Bedrock Mantle
    OpenAI-compatible endpoint (see ``_build_mantle_openai``); everything else
    builds a ChatBedrockConverse (see ``_build_bedrock_converse``). Either way
    the model is a plain ``BaseChatModel`` that ``create_deep_agent`` accepts and
    both sub-agents inherit.

    ``callbacks`` are attached to the MODEL INSTANCE (not the run config) so they
    fire for every turn on every dispatch path — including QuickJS ``task()``
    sub-agents that run on their own asyncio tasks and never reach the parent
    run's callbacks. This is how token usage is metered completely (see
    ``UsageForwarder``); sub-agents inherit this same model, so they inherit the
    callback too.

    ``surface_reasoning`` makes the model RETURN its reasoning to the client
    (Converse: ``thinking.display="summarized"`` → ``reasoning_content`` blocks;
    Mantle GPT: ``reasoning={effort, summary:"auto"}`` → ``reasoning`` blocks).
    Both models think regardless — this only controls whether the thinking comes
    back. Harvests leave it off (nothing renders it; the summary tokens are pure
    cost); the benchmark SOLVER turns it on, because its reasoning is the heart
    of the solver trace the judge and the report UI read.
    """
    if _is_openai_model(model):
        return _build_mantle_openai(
            model,
            effort,
            max_tokens,
            callbacks=callbacks,
            reasoning_summary="auto" if surface_reasoning else None,
        )
    return _build_bedrock_converse(
        model,
        effort,
        max_tokens,
        callbacks=callbacks,
        summarize_reasoning=surface_reasoning,
    )


def _build_bedrock_converse(
    model: str,
    effort: str,
    max_tokens: int,
    callbacks=None,
    *,
    summarize_reasoning: bool = False,
):
    """Construct a ChatBedrockConverse with adaptive thinking configured.

    Reads harvest's deploy-time knobs (AWS_REGION, the OKF_HARVEST_BEDROCK_*
    botocore timeouts) and delegates construction to the shared factory. Built
    explicitly (rather than passing a model string to create_deep_agent) so the
    thinking config rides on the model — no reliance on kwarg-forwarding through
    deepagents. The botocore ``config`` lifts the read timeout + retries so long
    Opus 4.8 turns don't ReadTimeout.
    """
    from okf_aws.model_factory import build_bedrock_converse

    return build_bedrock_converse(
        model,
        effort,
        max_tokens,
        region=os.environ.get("AWS_REGION", "us-east-1"),
        botocore_config=_bedrock_config(),
        callbacks=callbacks,
        summarize_reasoning=summarize_reasoning,
    )


# How long a minted Mantle bearer token is trusted before we re-mint. The token
# is a SigV4-PRESIGNED URL, so its effective life is min(requested expiry, the
# life of the signing credentials). On AgentCore the signing creds are TEMPORARY
# role creds (~1h), so a token minted once and cached for a whole 8h harvest dies
# mid-run ("security token ... is expired"). We re-mint well inside that window.
_MANTLE_TOKEN_TTL_SECONDS = 1800  # 30 min: comfortably under the ~1h creds life


def _mantle_token_provider(region: str):
    """A callable that returns a FRESH Mantle bearer token, cached briefly.

    Thin wrapper over :func:`okf_aws.model_factory.mantle_token_provider`, pinned
    to harvest's ``_MANTLE_TOKEN_TTL_SECONDS``. The token is a SigV4-presigned
    URL whose life is bounded by the signing role creds (~1h on AgentCore), so a
    single token can't cover an 8h harvest; the openai SDK re-invokes this
    callable per request, so each call re-reads a fresh (cached ~30 min) token.
    """
    from okf_aws.model_factory import mantle_token_provider

    return mantle_token_provider(region, ttl_seconds=_MANTLE_TOKEN_TTL_SECONDS)


def _build_mantle_openai(
    model: str,
    effort: str,
    max_tokens: int,
    callbacks=None,
    *,
    reasoning_summary: str | None = None,
):
    """Construct a ChatOpenAI pointed at the Bedrock Mantle OpenAI endpoint.

    Reads harvest's Mantle knobs (OKF_HARVEST_MANTLE_*) and delegates
    construction to the shared factory. Auth is a short-lived bearer token: a
    SigV4-derived Bedrock API key that inherits the runtime role's IAM (so the
    existing ``bedrock:InvokeModel*`` grant covers it — no API key or Secrets
    Manager), passed as a PROVIDER CALLABLE the openai SDK re-invokes per request.

    The Mantle REGION is deliberately separate from ``AWS_REGION`` (GPT-5.x is
    only in us-east-2/us-west-2); both the base URL and the token use it. GPT-5.x
    is served ONLY on the Responses API (/openai/v1); an operator running a
    gpt-oss model can flip OKF_HARVEST_MANTLE_USE_RESPONSES_API=false (→ /v1 Chat
    Completions). The botocore config doesn't apply to ChatOpenAI (httpx client),
    so the read timeout + retry budget map onto ``timeout``/``max_retries``.
    """
    from okf_aws.model_factory import build_mantle_openai

    region = os.environ.get("OKF_HARVEST_MANTLE_REGION", DEFAULT_MANTLE_REGION)
    use_responses = os.environ.get(
        "OKF_HARVEST_MANTLE_USE_RESPONSES_API", "true"
    ).lower() not in ("false", "0", "")
    # An explicit base-URL override wins; otherwise the shared factory derives it
    # from region + the Responses/ChatCompletions choice.
    base_url = os.environ.get("OKF_HARVEST_MANTLE_BASE_URL")
    return build_mantle_openai(
        model,
        effort,
        max_tokens,
        region=region,
        use_responses_api=use_responses,
        base_url=base_url,
        timeout=_int_env("OKF_HARVEST_MANTLE_READ_TIMEOUT", DEFAULT_BEDROCK_READ_TIMEOUT),
        max_retries=_int_env(
            "OKF_HARVEST_MANTLE_MAX_ATTEMPTS", DEFAULT_BEDROCK_MAX_ATTEMPTS
        ),
        token_ttl_seconds=_MANTLE_TOKEN_TTL_SECONDS,
        reasoning_summary=reasoning_summary,
        callbacks=callbacks,
    )


@dataclass
class HarvestAgent:
    """A built agent plus the session objects the entrypoint needs."""

    agent: Any  # compiled deepagents graph
    source: Source
    link_graph: LinkGraph
    dataset_root: Path


def _make_read_current(dataset_root: Path):
    """Return a ``read_current(file_path) -> str | None`` for the guard.

    ``file_path`` is the virtual path the agent uses (relative to the dataset
    root, possibly with a leading ``/``). We resolve it under the real root and
    read the current on-disk text, or None if it doesn't exist.
    """

    root = dataset_root.resolve()

    def read_current(file_path: str) -> str | None:
        rel = str(file_path).lstrip("/")
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None  # outside the dataset root; virtual_mode blocks it anyway
        if not target.is_file():
            return None
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            return None

    return read_current


def build_harvest_agent(
    source: Source,
    dataset_root: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    subagent_concurrency: int = DEFAULT_SUBAGENT_CONCURRENCY,
    subagent_config: dict[str, Any] | None = None,
    reviewer_config: dict[str, Any] | None = None,
    sandbox: Any = None,
    step_emitter: Any = None,
    cross_target: dict[str, Any] | None = None,
    supervisor_prompt: str | None = None,
) -> HarvestAgent:
    """Build the harvest deep agent (see the module docstring for the wiring).

    ``cross_target`` (``{data_domain, dataset}``) switches the build into
    CROSS-DATASET mode: the supervisor gets the cross prompt, the authoring
    fan-out is a single ``cross-author`` sub-agent (plus the reviewer), and the
    guard confines every write to the pair subtree
    ``external/<target_domain>/<target_dataset>/``. None = a normal build.

    ``supervisor_prompt`` replaces the full-harvest supervisor SYSTEM prompt for
    the SCOPED run modes (annotation / incremental — see
    ``prompts.build_annotation_supervisor_prompt`` /
    ``build_maintenance_supervisor_prompt``): those jobs must not inherit the
    full-harvest workflow (per-table fan-out + whole-bundle review). Sub-agents
    are unchanged. Mutually exclusive with ``cross_target``.

    ``model``/``effort``/``max_tokens`` configure the SUPERVISOR's model.
    ``subagent_config`` (optional ``{model, effort, max_tokens}``, resolved by
    ``resolve_model_config`` upstream) configures the model the dispatched
    sub-agents run on — table-author, reference-author, context-extractor. When
    None, the sub-agents use the supervisor's config. ``reviewer_config`` does
    the same for the adversarial ``reviewer`` ONLY — reviewing with a different
    model family than the one that authored improves coverage (a fresh model
    doesn't share the author's blind spots); when None the reviewer uses the
    sub-agents' config. Each scope runs on a SEPARATE model instance so token
    usage meters per scope (supervisor / subagents / reviewer — see
    steps.UsageForwarder).

    Benchmarking is NOT part of a harvest: the retired in-run RI loop's
    ``run_benchmark`` tool is gone, and the standalone Benchmark Studio runs in
    its own invocation mode (see ``harvest/benchmark/studio.py``).
    """
    from deepagents import create_deep_agent
    from deepagents.backends import (
        CompositeBackend,
        FilesystemBackend,
        StateBackend,
    )
    # deepagents 0.7.0 no longer includes TodoListMiddleware by default, and
    # the supervisor prompts PLAN with `write_todos` (one item per table /
    # reference / candidate) — so it is attached explicitly to the MAIN agent.
    # Sub-agent prompts don't use todos; keeping their stacks lean is the 0.7
    # default we want there.
    from langchain.agents.middleware import TodoListMiddleware

    # QuickJS code interpreter → enables DYNAMIC subagents: the agent can write JS
    # that calls the task({description, subagentType}) global to fan out + collect
    # in parallel (loops, Promise.all). We use it for adversarial REVIEW: after
    # authoring, fan out one independent `reviewer` per link-cluster of ≤5 docs
    # (from cluster_concepts) to verify claims against live data, then fix only
    # confirmed findings. Optional — if the
    # interpreter package isn't present, fall back to the static `task` tool.
    #
    # Bound how many task() dispatches run concurrently BEFORE building the
    # middleware (it builds the REPL that reads the cap). langchain_quickjs gates
    # task() with an asyncio.Semaphore per REPL — see _cap_subagent_concurrency.
    # config={"max_concurrency": N} on invoke does NOT apply here: the fan-out is
    # a QuickJS Promise.all scheduled via raw asyncio, not LangGraph's batch
    # executor, so the semaphore is the only lever that bounds it.
    interpreter_mw = None
    try:
        from langchain_quickjs import CodeInterpreterMiddleware

        _cap_subagent_concurrency(subagent_concurrency)
        interpreter_mw = CodeInterpreterMiddleware()
    except Exception:  # noqa: BLE001 - dynamic dispatch is a nice-to-have
        interpreter_mw = None
    if interpreter_mw is not None:
        # The library's lifecycle events don't carry a dispatch's full brief or
        # its final answer — shim its task() choke point so both ride the
        # custom stream for the UI's fleet drill-in. Its own try: losing the
        # drill-in's I/O must never cost the interpreter itself.
        try:
            from harvest.subagent_io import install_quickjs_io_forwarding

            install_quickjs_io_forwarding()
        except Exception:  # noqa: BLE001 - observability only
            import logging

            logging.getLogger(__name__).warning(
                "Sub-agent I/O forwarding unavailable", exc_info=True
            )

    # Adaptive thinking rides on the model instances. A scope-tagged
    # UsageForwarder on each MODEL INSTANCE meters token usage for every turn —
    # including QuickJS task() sub-agents, which run on their own asyncio tasks
    # and never reach the run-config StepEmitter. TWO instances are always built:
    # the supervisor's, and the sub-agents' (which the four dynamic sub-agents
    # are pinned to via their spec's "model"). Separate instances are what make
    # the per-scope usage
    # split exact — attribution is by instance, no callback discrimination — and
    # what lets a run give sub-agents a DIFFERENT model/effort (subagent_config)
    # than the supervisor. Best-effort: if steps can't be imported, the models
    # are built without metering (usage just isn't tracked).
    def _usage_callbacks(scope: str):
        if step_emitter is None:
            return None
        try:
            from harvest.steps import UsageForwarder

            return [UsageForwarder(step_emitter, scope=scope)]
        except Exception:  # noqa: BLE001 - usage metering is an enhancement
            return None

    chat_model = _build_model(
        model, effort, max_tokens, callbacks=_usage_callbacks("supervisor")
    )
    sub_cfg = subagent_config or {}
    subagent_chat_model = _build_model(
        sub_cfg.get("model") or model,
        sub_cfg.get("effort") or effort,
        sub_cfg.get("max_tokens") or max_tokens,
        callbacks=_usage_callbacks("subagents"),
    )
    # The adversarial reviewer gets a THIRD instance. Fallback chain: its own
    # override -> the shared sub-agent config -> the supervisor's. A separate
    # instance even without an override keeps the review pass's token spend
    # visible as its own scope.
    rev_cfg = reviewer_config or {}
    reviewer_model_id = rev_cfg.get("model") or sub_cfg.get("model") or model
    reviewer_chat_model = _build_model(
        reviewer_model_id,
        rev_cfg.get("effort") or sub_cfg.get("effort") or effort,
        rev_cfg.get("max_tokens") or sub_cfg.get("max_tokens") or max_tokens,
        callbacks=_usage_callbacks("reviewer"),
    )

    dataset_root = Path(dataset_root)
    mkdirs(dataset_root)  # NFS-resilient (tolerates transient ESTALE on the mount)

    # Pin the run's dataset root for the context-digest recorder: the capture
    # sites (the QuickJS shim and the step feed) see extractor dispatches but
    # don't know where the bundle lives. See harvest.context_digests.
    from harvest.context_digests import configure as _configure_digest_recorder

    _configure_digest_recorder(dataset_root)

    link_graph = LinkGraph(dataset_root)
    engine = OKFGuardEngine(link_graph)

    # Cross-dataset mode: confine every write to the pair subtree. The prefix is
    # built here (not passed in) so the guard and the prompts can never disagree
    # about where the run may write. external_pair_prefix VALIDATES both
    # segments — a "/", "..", or "#" in a target name must never become part of
    # a write-confinement prefix.
    writable_prefix = None
    if cross_target:
        from okf_core.paths import external_pair_prefix

        writable_prefix = external_pair_prefix(
            cross_target["data_domain"], cross_target["dataset"]
        )

    # A FULL harvest is the only mode that owns bundle-level shape: it wipes and
    # re-authors everything, runs the lint gate, and is the one told to fix its
    # `stale-table-doc` findings. Scoped modes (annotation/incremental) and cross
    # runs replace the supervisor prompt, so they'd carry the tool with no
    # guidance on when to use it — the same reason the lint gate is gated here.
    full_harvest = cross_target is None and supervisor_prompt is None

    guard = OKFGuardMiddleware(
        engine,
        read_current=_make_read_current(dataset_root),
        writable_prefix=writable_prefix,
    )
    # The SUPERVISOR's own guard: identical, plus `delete` for retiring a stale
    # doc (one .md file, never a dot-dir or a directory — see okf_guard). In
    # EVERY mode: a scoped/incremental run whose table was DROPPED from the
    # catalog must retire that doc, and an annotation can retire one too. Cross
    # mode keeps `writable_prefix`, so its deletes are confined to the pair
    # subtree like its writes. The authoring sub-agents keep `guard` above,
    # which still refuses delete: a table-author has no business removing
    # anything.
    main_guard = OKFGuardMiddleware(
        engine,
        read_current=_make_read_current(dataset_root),
        writable_prefix=writable_prefix,
        allow_delete=True,
    )
    # The verify-and-report sub-agents' guard: refuses EVERY write/edit (and,
    # like the main guard, the recursive `delete` deepagents ≥0.7 exposes).
    # deepagents hands every sub-agent the backend's write tools, so read-only
    # must be enforced at the tool boundary, not promised by the prompt.
    readonly_guard = OKFGuardMiddleware(
        engine,
        read_current=_make_read_current(dataset_root),
        read_only=True,
    )
    # The fix-author's guard: writes allowed ONLY to the doc paths of the
    # review cluster bound to the CURRENT dispatch (run_review sets it via a
    # contextvar around each fixer's task-tool call — see harvest.review).
    # Fails closed: with no cluster bound (e.g. the model dispatching
    # fix-author by hand), every write is refused.
    from harvest.review import current_fix_allowlist

    fixer_guard = OKFGuardMiddleware(
        engine,
        read_current=_make_read_current(dataset_root),
        writable_prefix=writable_prefix,
        write_allowlist=current_fix_allowlist,
    )
    # Tool-boundary safety net: a raising tool (e.g. a PermissionError from the
    # S3 Files mount mid-write) becomes a ToolMessage(status="error") the model
    # can react to, instead of an exception that aborts the whole run. Stateless,
    # so one instance is shared; attached to the MAIN agent and EVERY sub-agent
    # below (sub-agent middleware REPLACES — same footgun as the guard).
    tool_errors = ToolErrorMiddleware()

    # Bedrock prompt caching — the same setup as the chat agent and the
    # benchmark's ReAct roles (chat/server.py, benchmark/react.py). A harvest
    # re-sends its ~3k-token static prefix (plus the growing conversation) on
    # EVERY turn of the supervisor and every sub-agent across a multi-hour run;
    # with the middleware, ChatBedrockConverse inserts cachePoint blocks at
    # request time and that traffic bills as cache READS (~0.1x input) instead
    # of full price. On a Mantle GPT model it warns once and no-ops (the
    # Responses API caches prefixes implicitly server-side). Attached to the
    # MAIN agent and EVERY sub-agent (middleware REPLACES, never inherits).
    from langchain_aws.middleware import BedrockPromptCachingMiddleware

    prompt_cache = BedrockPromptCachingMiddleware()

    source_tools = make_source_tools(source)
    graph_tools = make_graph_tools(link_graph)
    all_tools = [*source_tools, *graph_tools]

    # A code-execution tool for extracting text from binary .context/ docs
    # (PDF/DOCX/PPTX/XLSX) the built-in read_file only base64-encodes. Backed by a
    # network-isolated AgentCore Code Interpreter sandbox with NO Glue/Athena/
    # bundle creds (credential isolation). Appended to all_tools so it reaches the
    # main agent AND both sub-agents (whose tool lists REPLACE, not inherit). It is
    # NOT the default backend: deepagents only wires its built-in execute tool to
    # the default backend, and the bundle must stay on the FilesystemBackend mount
    # (finalize/reindex read from there) — so the sandbox is a separate tool only.
    run_code_tool = None
    if sandbox is not None:
        from harvest.code_interpreter import make_run_code_tool

        run_code_tool = make_run_code_tool(sandbox)
        all_tools.append(run_code_tool)

    # Whole-bundle lint gate — MAIN AGENT ONLY, every mode except cross. The
    # full-harvest supervisor runs its fix-to-zero workflow twice; scoped
    # supervisors (annotation/incremental hand in supervisor_prompt) run it as
    # a FINAL check on the docs they touched (their prompt bodies scope the
    # fix obligation — pre-existing errors elsewhere are reported, not
    # ballooned into the run). Cross runs stay excluded: their writes are
    # guard-confined to the pair subtree, so a bundle-wide error the tool
    # reports would be unfixable there. Sub-agent specs below get all_tools
    # (authors/reviewers work one doc/cluster at a time — a bundle-wide scan
    # in their hands is wasted tokens). No-arg by design: expected tables come
    # from the .metadata/ snapshot on disk and EXPLAIN availability from this
    # run's source, so there is nothing for the model to pass (or get wrong).
    main_tools = list(all_tools)
    # Bundle inventory — EVERY supervisor mode (full/scoped/cross): counts by
    # concept type instead of glob-and-count-in-context. Counts only, no
    # judgment (see stats_tool.py) — deliberately NOT in the sub-agent specs.
    main_tools.append(make_stats_tool(dataset_root))
    if cross_target is None:
        main_tools.append(make_lint_tool(source, dataset_root))
    if full_harvest:
        # The deterministic review workflow (ONE call replaces the old
        # cluster→eval→Promise.all orchestration): clusters computed here,
        # one read-only reviewer per cluster, findings piped into a
        # cluster-confined fix-author, everything surfaced as fleet squares,
        # full transcript in .harvest/review/. FULL harvests only: scoped
        # runs fix what they touched, they don't re-review the bundle.
        from harvest.review import make_run_review_tool

        main_tools.append(
            make_run_review_tool(
                link_graph=link_graph,
                dataset_root=dataset_root,
                concurrency=subagent_concurrency,
            )
        )

    # Containment: bundle files (bare paths like tables/races.md) go to the
    # dataset root on disk via the DEFAULT FilesystemBackend; deepagents'
    # internal scratch (offloaded large tool results + conversation history,
    # which it writes under the documented /large_tool_results/ and
    # /conversation_history/ prefixes) is ROUTED to an ephemeral StateBackend so
    # it never lands in — and pollutes — the published bundle.
    #
    # This is the inverse of the docs' default=StateBackend + route "/workspace/"
    # pattern, chosen deliberately: keeping FilesystemBackend as the default lets
    # the agent author with bare concept paths (no /workspace/ prefix), so
    # finalize/LinkGraph/reindex/read_current all operate on the same paths with
    # zero duality. Only the two enumerated internal prefixes need diverting.
    _ephemeral = StateBackend()
    routes: dict[str, Any] = {
        "/large_tool_results/": _ephemeral,
        "/conversation_history/": _ephemeral,
    }
    # Mount the vendored okf-authoring SKILL under a dedicated /skills/ route
    # (read-only, its own FilesystemBackend root) so deepagents' native skills
    # support can load it via the POSIX path "/skills/". This keeps the canonical
    # OKF authoring procedure + its references/templates/source-adapters OUT of
    # the dataset bundle (no pollution) while the agent reads them on demand
    # through the built-in read_file — progressive disclosure: only the SKILL.md
    # name/description sit in the system prompt until the task activates it.
    skills_arg: list[str] = []
    skill_root = _skill_root()
    if skill_root is not None:
        routes["/skills/"] = FilesystemBackend(
            root_dir=str(skill_root), virtual_mode=True
        )
        skills_arg = ["/skills/"]

    backend = CompositeBackend(
        default=FilesystemBackend(
            root_dir=str(dataset_root.resolve()), virtual_mode=True
        ),
        routes=routes,
    )

    # Every prompt is built for THIS run's source (correct type strings, dialect,
    # adapter, resource form) — a Redshift run must not be told to write Glue types.
    prompt_profile = source.prompt_profile

    # And for THIS agent's model family: a GPT model gets the GPT-family
    # addendum (persistence / context-gathering / output discipline — see
    # prompts._GPT_ADDENDUM). Keyed PER SCOPE because the supervisor and the
    # sub-agents can now run different families.
    supervisor_gpt = _is_openai_model(model)
    subagent_gpt = _is_openai_model(sub_cfg.get("model") or model)
    reviewer_gpt = _is_openai_model(reviewer_model_id)

    # One dynamic sub-agent, dispatched once per table. Its tools + middleware
    # REPLACE the defaults, so we pass the guard and the same source/graph tools
    # explicitly (the sub-agent does the writing). Sub-agents inherit the skills
    # made available on the agent's backend. Every sub-agent spec pins "model"
    # to the SUB-AGENT model instance (its own config + its own usage scope) —
    # otherwise it would inherit the supervisor's instance and the per-scope
    # token split (and any per-scope model choice) would be lost.
    table_author = {
        "name": "table-author",
        "description": (
            "Enrich exactly one source table and write its OKF markdown doc. "
            "Pass the table's concept id, e.g. 'tables/races'."
        ),
        "system_prompt": build_table_author_prompt(prompt_profile, gpt=subagent_gpt),
        "tools": all_tools,
        "middleware": [guard, tool_errors, prompt_cache],
        "model": subagent_chat_model,
    }

    # Dynamic sub-agent, dispatched once per CROSS-CUTTING reference item (a
    # metric, named-set, glossary term, known-issue, or the dataset's usage-
    # guardrails contract). Mirrors table-author (guard + source/graph tools, it
    # does the writing) so a reference gets the same dedicated, verify-against-live
    # attention a table does — instead of the supervisor first-drafting them all
    # serially. Per-table enums/joins stay with the table-author (co-located with
    # the table they verified); this one owns the references that span tables.
    reference_author = {
        "name": "reference-author",
        "description": (
            "Author exactly one CROSS-CUTTING reference doc and write its file: a "
            "metric (references/metrics/*), named_set/lifecycle (references/"
            "named_sets/*), glossary term (references/glossary/*), known-issue "
            "(references/known_issues/*), or the dataset usage-guardrails contract "
            "(references/usage_guardrails.md). Pass the concept id + fact type + a "
            "grounding brief. NOT for per-table enums/joins (table-author owns those)."
        ),
        "system_prompt": build_reference_author_prompt(prompt_profile, gpt=subagent_gpt),
        "tools": all_tools,
        "middleware": [guard, tool_errors, prompt_cache],
        "model": subagent_chat_model,
    }

    # Adversarial reviewer — READ-ONLY (enforced by readonly_guard, which
    # refuses every write/edit/delete at the tool boundary). Verifies a
    # link-cluster of authored docs' load-bearing claims (the stated grain,
    # join keys, gotchas, SQL — plus cross-doc contradictions within the
    # cluster) against LIVE data via run_sql/sample_rows, and reports only
    # findings it could reproduce; the supervisor applies the confirmed fixes.
    reviewer = {
        "name": "reviewer",
        "description": (
            "Adversarially verify a CLUSTER of related authored OKF concept docs "
            "against live data (dispatched by the run_review tool, one per link "
            "cluster). Pass the concept ids (e.g. 'tables/races, references/"
            "joins/circuits__races') and what to scrutinize. Replies with a "
            "first-line verdict — CLEAN, or FINDINGS grouped by doc (wrong "
            "grain, bad join key, mis-stated gotcha, cross-doc contradiction, "
            "SQL that errors/returns wrong rows), each with the query that "
            "proves it."
        ),
        "system_prompt": build_reviewer_prompt(prompt_profile, gpt=reviewer_gpt),
        "tools": all_tools,  # source + graph tools; read_file comes from the backend
        "middleware": [readonly_guard, tool_errors, prompt_cache],
        "model": reviewer_chat_model,
    }

    # Context-fidelity reviewer — READ-ONLY, dispatched ONLY by run_review's
    # context phase (after all cluster fixes), one per pair of recorded
    # context-extractor digests (.harvest/context/). Audits whether the bundle
    # faithfully represents each extracted fact (semantic loss, dropped codes,
    # weakened caveats, mis-routed rules); confirmed losses pipe into a
    # fix-author. Rides the reviewer model instance — it IS review spend.
    context_reviewer = {
        "name": "context-reviewer",
        "description": (
            "Audit how faithfully the bundle represents the facts in recorded "
            "context-extraction digests. Dispatched by the run_review tool "
            "only — do not dispatch it directly. READ-ONLY — returns "
            "plain-text findings, writes nothing."
        ),
        "system_prompt": build_context_reviewer_prompt(
            prompt_profile, gpt=reviewer_gpt
        ),
        "tools": all_tools,  # bundle reads via the backend; live probes if needed
        "middleware": [readonly_guard, tool_errors, prompt_cache],
        "model": reviewer_chat_model,
    }

    # The review workflow's fix applier — dispatched ONLY by the run_review
    # tool, one per cluster with confirmed findings. Its guard (fixer_guard)
    # confines writes to that dispatch's cluster paths and fails closed, so a
    # stray manual task() dispatch of this type cannot write anything.
    fix_author = {
        "name": "fix-author",
        "description": (
            "Apply an adversarial reviewer's confirmed findings to the docs of "
            "ONE review cluster. Dispatched by the run_review tool only — its "
            "write access is bound to that cluster's files; do not dispatch it "
            "directly."
        ),
        "system_prompt": build_fixer_prompt(prompt_profile, gpt=subagent_gpt),
        "tools": all_tools,
        "middleware": [fixer_guard, tool_errors, prompt_cache],
        "model": subagent_chat_model,
    }

    # Context fact-extractor — READ-ONLY. Reads the uploaded `.context/` docs once
    # (text via read_file, binary via the run_code sandbox), mines them for the
    # fact types (enums, joins, metrics, grain, caveats), verifies each against
    # live data, and returns a compact routed digest the supervisor threads into
    # the table-authors. Fanned out one-per-doc/group for a LARGE `.context/` so
    # the heavy reading happens once, off the supervisor's and authors' context.
    # READ-ONLY via readonly_guard (findings return as plain text, never as
    # bundle writes); it gets the same source + graph + run_code tools
    # (read_file comes from the backend).
    context_extractor = {
        "name": "context-extractor",
        "description": (
            "Extract verified facts from the uploaded `.context/` source docs and "
            "return a compact, routed digest (enum legends, joins, metrics, grain, "
            "caveats — each tagged with the target concept id + section). Pass which "
            "`.context/` doc(s) to cover. Use for LARGE `.context/` folders so the "
            "docs are read once, not re-read by every table-author. READ-ONLY — "
            "returns plain-text findings, writes nothing."
        ),
        "system_prompt": build_context_extractor_prompt(prompt_profile, gpt=subagent_gpt),
        "tools": all_tools,  # source + graph + run_code; read_file from the backend
        "middleware": [readonly_guard, tool_errors, prompt_cache],
        "model": subagent_chat_model,
    }

    # Cross-dataset mode's single authoring sub-agent: one per VERIFIED
    # cross-dataset relationship. Shares the guard (so the pair-subtree
    # confinement applies to it too) and the same source/graph tools — verifying
    # a cross join is qualified run_sql, no new tool surface.
    cross_author = {
        "name": "cross-author",
        "description": (
            "Author exactly one CROSS-DATASET reference doc and write its file "
            "under the run's pair folder (external/<target_domain>/<target_"
            "dataset>/...): a verified cross-dataset join (joins/*), metric "
            "(metrics/*), or other canonical fact type spanning the two "
            "datasets. Pass the concept id + a grounding brief (verifying "
            "queries, measured cardinality/overlap)."
        ),
        "system_prompt": build_cross_author_prompt(prompt_profile, gpt=subagent_gpt),
        "tools": all_tools,
        "middleware": [guard, tool_errors, prompt_cache],
        "model": subagent_chat_model,
    }

    # main_guard (not guard): the supervisor's variant also permits `delete` on
    # a single stale .md doc — see the guard construction above.
    # SubagentDispatchGuard: the model may not dispatch workflow-only types
    # (fix-author) itself, in ANY mode — a small ad-hoc fix is the
    # supervisor's own edit_file; run_review owns the fixer fan-out. (The
    # QuickJS task() path is blocked by the same list in subagent_io.)
    main_middleware = [
        main_guard,
        SubagentDispatchGuard(),
        tool_errors,
        prompt_cache,
        TodoListMiddleware(),
    ]
    if interpreter_mw is not None:
        main_middleware.append(interpreter_mw)

    # Cross mode swaps the supervisor prompt and the authoring fan-out. The
    # reviewer keeps its name/description/tools/READ-ONLY guard (deepagents
    # hands every sub-agent the backend's write_file/edit_file, and in cross
    # mode the reviewer reads a verbatim copy of ANOTHER dataset's wiki — the
    # run's prompt-injection surface — so the hard write refusal matters MORE
    # here, not less; its prompt says "READ-ONLY, you do NOT write files") but
    # runs the CROSS reviewer prompt — the standard table-doc checklist
    # ("probe for a join the doc misses") makes cross reviewers re-do
    # discovery with many slow cross-database queries; the cross body verifies
    # what the pair docs CLAIM on a two-queries-per-doc budget instead.
    if cross_target:
        if supervisor_prompt is not None:
            raise ValueError("supervisor_prompt and cross_target are mutually exclusive")
        system_prompt = build_cross_supervisor_prompt(
            prompt_profile, gpt=supervisor_gpt
        )
        cross_reviewer = {
            **reviewer,
            "system_prompt": build_cross_reviewer_prompt(
                prompt_profile, gpt=reviewer_gpt
            ),
            "middleware": [readonly_guard, tool_errors, prompt_cache],
        }
        subagents = [cross_author, cross_reviewer]
    else:
        # Scoped modes (annotation / incremental) hand in their own supervisor
        # prompt; the sub-agent roster stays standard (their prompts tell them
        # not to fan out, and an unused sub-agent definition costs nothing).
        system_prompt = supervisor_prompt or build_supervisor_prompt(
            profile=prompt_profile,
            gpt=supervisor_gpt,
        )
        subagents = [
            table_author,
            reference_author,
            reviewer,
            context_reviewer,
            fix_author,
            context_extractor,
        ]

    agent = create_deep_agent(
        model=chat_model,
        tools=main_tools,
        system_prompt=system_prompt,
        middleware=main_middleware,
        subagents=subagents,
        backend=backend,
        skills=skills_arg,
    )

    return HarvestAgent(
        agent=agent,
        source=source,
        link_graph=link_graph,
        dataset_root=dataset_root,
    )
