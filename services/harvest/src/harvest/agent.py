"""Build the harvest deep agent for one dataset session.

Wires together, per the design:
- a ``FilesystemBackend(root_dir=<dataset root>, virtual_mode=True)`` for
  per-dataset containment (blocks ``../``/``~``/absolute escapes), wrapped in a
  ``CompositeBackend`` so deepagents' internal scratch files stay ephemeral;
- ``GlueAthenaSource`` LIVE tools (sample_rows / run_sql); static Glue metadata is
  snapshotted to the read-only ``.metadata/`` dir before the run and read with the
  built-in file tools (see ``metadata_export``);
- a per-session ``LinkGraph`` with ``get_backlinks``/``get_links`` tools;
- ``OKFGuardMiddleware`` (frontmatter + augmentation guard, timestamp auto-fill,
  graph dirty-flag) — attached to the main agent AND the per-table sub-agent
  (sub-agent middleware/tools REPLACE, so we pass them explicitly);
- four dynamic sub-agents the supervisor fans out via ``task()``: ``table-author``
  (one per table), ``reference-author`` (one per cross-cutting reference — metric,
  named-set, glossary term, known-issue, or the usage-guardrails contract),
  ``reviewer`` (one per authored doc, adversarial read-only verification), and
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
from harvest.glue_source import GlueAthenaSource
from harvest.graph_tools import make_graph_tools
from harvest.guard_engine import OKFGuardEngine
from harvest.okf_guard import OKFGuardMiddleware
from harvest.prompts import (
    CONTEXT_EXTRACTOR_PROMPT,
    REFERENCE_AUTHOR_PROMPT,
    REVIEWER_PROMPT,
    TABLE_AUTHOR_PROMPT,
    build_supervisor_prompt,
)
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


def _build_model(model: str, effort: str, max_tokens: int, callbacks=None):
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
    """
    if _is_openai_model(model):
        return _build_mantle_openai(model, effort, max_tokens, callbacks=callbacks)
    return _build_bedrock_converse(model, effort, max_tokens, callbacks=callbacks)


def _build_bedrock_converse(model: str, effort: str, max_tokens: int, callbacks=None):
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


def _build_mantle_openai(model: str, effort: str, max_tokens: int, callbacks=None):
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
        callbacks=callbacks,
    )


@dataclass
class HarvestAgent:
    """A built agent plus the session objects the entrypoint needs."""

    agent: Any  # compiled deepagents graph
    source: GlueAthenaSource
    link_graph: LinkGraph
    dataset_root: Path
    # Present only on a recursive-improvement run: the per-run benchmark session,
    # so the runner can read best_round()/rounds after the crawl for checkpoint
    # selection and the terminal KPI row. None on a normal harvest.
    benchmark_session: Any = None


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


def _build_benchmark_session(
    *,
    ri_config: dict[str, Any],
    run: dict[str, Any],
    questions: Any,
    chat_model: Any,
    source: GlueAthenaSource,
    source_tools: list[Any],
    dataset_root: Path,
    step_emitter: Any,
    persist_kpi: Any,
) -> Any:
    """Construct the per-run BenchmarkSession behind the run_benchmark tool.

    The solver reuses ``chat_model`` (bundle-blind, rooted per-round at a snapshot
    the session builds); the adjudicator reuses ``chat_model`` + the source tools
    (``run_sql``/``sample_rows``) so it can diagnose gaps against live data. Both
    ride the shared instrumented model, so benchmark tokens meter into the run
    total automatically. ``persist_kpi(iteration, attrs)`` and the step emitter are
    wired so each round writes a BENCH# row and a live ``kind:"benchmark"`` event.
    """
    from harvest.benchmark.adjudicator import make_adjudicator
    from harvest.benchmark.grader import Grader
    from harvest.benchmark.runner import BenchmarkSession
    from harvest.benchmark.solver import make_solver

    grader = Grader(source.run_query)
    adjudicate = make_adjudicator(chat_model, source_tools)

    def emit_event(event: dict) -> None:
        if step_emitter is not None:
            # StepEmitter._emit is internal but stable; benchmark events reuse the
            # same OKF_STEP sink the feed already ships to CloudWatch.
            step_emitter._emit(event)

    # Per-question solver observability (a ReAct solver's turns don't reach the
    # StepEmitter, so this is the only window into what each solver actually did).
    solver_emit = emit_event if step_emitter is not None else None

    return BenchmarkSession(
        data_domain=run.get("data_domain", ""),
        dataset=run.get("dataset", ""),
        dataset_root=str(dataset_root),
        runtime_session_id=run.get("runtime_session_id", ""),
        config=ri_config,
        questions=list(questions),
        make_solver=lambda snap_dir: make_solver(chat_model, snap_dir, solver_emit),
        grader=grader,
        adjudicate=adjudicate,
        persist_kpi=persist_kpi,
        emit_event=emit_event if step_emitter is not None else None,
    )


def _make_benchmark_tool(session: Any) -> Any:
    """The run_benchmark LangChain tool bound to ``session`` (one round per call)."""
    from harvest.benchmark.runner import make_run_benchmark_tool

    return make_run_benchmark_tool(session)


def build_harvest_agent(
    source: GlueAthenaSource,
    dataset_root: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    subagent_concurrency: int = DEFAULT_SUBAGENT_CONCURRENCY,
    sandbox: Any = None,
    step_emitter: Any = None,
    ri_config: dict[str, Any] | None = None,
    benchmark_questions: Any = None,
    benchmark_run: dict[str, Any] | None = None,
    persist_kpi: Any = None,
) -> HarvestAgent:
    from deepagents import create_deep_agent
    from deepagents.backends import (
        CompositeBackend,
        FilesystemBackend,
        StateBackend,
    )

    # QuickJS code interpreter → enables DYNAMIC subagents: the agent can write JS
    # that calls the task({description, subagentType}) global to fan out + collect
    # in parallel (loops, Promise.all). We use it for adversarial REVIEW: after
    # authoring, fan out one independent `reviewer` per doc to verify claims
    # against live data, then fix only confirmed findings. Optional — if the
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

    # Opus 4.8 with adaptive thinking; the thinking config rides on the model.
    # A UsageForwarder on the MODEL INSTANCE meters token usage for every turn —
    # including QuickJS task() sub-agents, which inherit this same model but run
    # on their own asyncio tasks and never reach the run-config StepEmitter. This
    # is the one metering path (see steps.record_usage). Best-effort: if steps
    # can't be imported, the model is built without it (usage just isn't tracked).
    model_callbacks = None
    if step_emitter is not None:
        try:
            from harvest.steps import UsageForwarder

            model_callbacks = [UsageForwarder(step_emitter)]
        except Exception:  # noqa: BLE001 - usage metering is an enhancement
            model_callbacks = None
    chat_model = _build_model(model, effort, max_tokens, callbacks=model_callbacks)

    dataset_root = Path(dataset_root)
    mkdirs(dataset_root)  # NFS-resilient (tolerates transient ESTALE on the mount)

    link_graph = LinkGraph(dataset_root)
    engine = OKFGuardEngine(link_graph)

    # Recursive improvement: when enabled, build the per-run benchmark session that
    # backs the run_benchmark tool, and hand the guard a call budget so it refuses
    # runaway looping past max_iterations (a backstop independent of the prompt).
    from okf_core import recursive_improvement as ri

    benchmark_session = None
    benchmark_budget = None
    if ri.is_enabled(ri_config):
        benchmark_budget = ri_config.get(ri.FIELD_MAX_ITERATIONS, ri.MAX_ITERATIONS)

    guard = OKFGuardMiddleware(
        engine,
        read_current=_make_read_current(dataset_root),
        benchmark_budget=benchmark_budget,
    )

    source_tools = make_source_tools(source)
    graph_tools = make_graph_tools(link_graph)
    all_tools = [*source_tools, *graph_tools]

    # Build the benchmark session now that chat_model + source tools exist. The
    # solver reuses chat_model (tokens meter for free); the adjudicator reuses
    # chat_model + the source tools (run_sql/sample_rows) for raw-data diagnosis.
    if ri.is_enabled(ri_config) and benchmark_questions:
        benchmark_session = _build_benchmark_session(
            ri_config=ri_config,
            run=benchmark_run or {},
            questions=benchmark_questions,
            chat_model=chat_model,
            source=source,
            source_tools=source_tools,
            dataset_root=dataset_root,
            step_emitter=step_emitter,
            persist_kpi=persist_kpi,
        )
        all_tools.append(_make_benchmark_tool(benchmark_session))

    # A code-execution tool for extracting text from binary .context/ docs
    # (PDF/DOCX/PPTX/XLSX) the built-in read_file only base64-encodes. Backed by a
    # network-isolated AgentCore Code Interpreter sandbox with NO Glue/Athena/
    # bundle creds (credential isolation). Appended to all_tools so it reaches the
    # main agent AND both sub-agents (whose tool lists REPLACE, not inherit). It is
    # NOT the default backend: deepagents only wires its built-in execute tool to
    # the default backend, and the bundle must stay on the FilesystemBackend mount
    # (finalize/reindex read from there) — so the sandbox is a separate tool only.
    if sandbox is not None:
        from harvest.code_interpreter import make_run_code_tool

        all_tools.append(make_run_code_tool(sandbox))

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

    # One dynamic sub-agent, dispatched once per table. Its tools + middleware
    # REPLACE the defaults, so we pass the guard and the same source/graph tools
    # explicitly (the sub-agent does the writing). Sub-agents inherit the skills
    # made available on the agent's backend.
    table_author = {
        "name": "table-author",
        "description": (
            "Enrich exactly one Glue table and write its OKF markdown doc. "
            "Pass the table's concept id, e.g. 'tables/races'."
        ),
        "system_prompt": TABLE_AUTHOR_PROMPT,
        "tools": all_tools,
        "middleware": [guard],
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
        "system_prompt": REFERENCE_AUTHOR_PROMPT,
        "tools": all_tools,
        "middleware": [guard],
    }

    # Adversarial reviewer — READ-ONLY. Verifies an authored doc's load-bearing
    # claims (the stated grain, join keys, gotchas, SQL) against LIVE data via
    # run_sql/sample_rows, and reports only findings it could reproduce. No guard
    # (it never writes); the supervisor applies the confirmed fixes.
    reviewer = {
        "name": "reviewer",
        "description": (
            "Adversarially verify one authored OKF concept doc against live data. "
            "Pass the concept id (e.g. 'tables/races') and what to scrutinize. "
            "Returns confirmed findings (wrong grain, bad join key, mis-stated "
            "gotcha, SQL that errors/returns wrong rows) with the query that "
            "proves each — or 'no issues found'."
        ),
        "system_prompt": REVIEWER_PROMPT,
        "tools": all_tools,  # source + graph tools; read_file comes from the backend
    }

    # Context fact-extractor — READ-ONLY. Reads the uploaded `.context/` docs once
    # (text via read_file, binary via the run_code sandbox), mines them for the
    # fact types (enums, joins, metrics, grain, caveats), verifies each against
    # live data, and returns a compact routed digest the supervisor threads into
    # the table-authors. Fanned out one-per-doc/group for a LARGE `.context/` so
    # the heavy reading happens once, off the supervisor's and authors' context. No
    # guard (it never writes bundle files); it gets the same source + graph +
    # run_code tools (read_file comes from the backend).
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
        "system_prompt": CONTEXT_EXTRACTOR_PROMPT,
        "tools": all_tools,  # source + graph + run_code; read_file from the backend
    }

    main_middleware = [guard]
    if interpreter_mw is not None:
        main_middleware.append(interpreter_mw)

    agent = create_deep_agent(
        model=chat_model,
        tools=all_tools,
        system_prompt=build_supervisor_prompt(
            recursive_improvement=benchmark_session is not None
        ),
        middleware=main_middleware,
        subagents=[table_author, reference_author, reviewer, context_extractor],
        backend=backend,
        skills=skills_arg,
    )

    return HarvestAgent(
        agent=agent,
        source=source,
        link_graph=link_graph,
        dataset_root=dataset_root,
        benchmark_session=benchmark_session,
    )
