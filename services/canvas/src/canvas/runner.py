"""One canvas job, end to end: job row transitions, the agent loop, the
outcome. No lease, no mount, no bundle writes — the job reads the wiki +
computations from S3 and writes only its own DDB rows.

The agent runs on the chat checkpoint table with thread id
``<sub>:canvasjob-<job_id>`` (the chat convention — per-user namespacing in
the thread id), so a recycled instance can resume and the run is auditable.
Progress rides the job row's step feed (tool labels + final text), appended by
a callback at emit time.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from okf_core import canvas as cv

from canvas import jobs
from canvas.prompts import system_prompt
from canvas.tools import ToolContext, build_all_tools

log = logging.getLogger(__name__)

RECURSION_LIMIT = 300


class _StepFeed(BaseCallbackHandler):
    """Emit-time-filtered feed: tool starts (name + a short arg echo) and
    nothing else — text lands once, at the end, from the final message."""

    def __init__(self, ddb, *, table: str, user_sub: str, job_id: str):
        self._args = (ddb, table, user_sub, job_id)
        # This thread is the feed's only writer, so it owns the list: passing
        # it as append_step's cache skips the per-event GetItem (the feed was
        # a read + full-blob rewrite per event — O(n²) reads over a job).
        self._steps: list = []

    def _emit(self, kind: str, label: str, tool_input=None) -> None:
        ddb, table, user_sub, job_id = self._args
        try:
            jobs.append_step(
                ddb, table=table, user_sub=user_sub, job_id=job_id,
                kind=kind, label=label, tool_input=tool_input,
                cache=self._steps,
            )
        except Exception:  # noqa: BLE001 — the feed must never kill the job
            log.debug("step feed append failed", exc_info=True)

    def on_llm_end(self, response, **kwargs):  # noqa: ANN001
        # Assistant text BETWEEN tool calls (the model narrating its plan or
        # findings): every turn's text blocks land on the feed in order, so
        # the drawer's timeline interleaves text and tools like the chat one.
        # With surface_reasoning off there is no reasoning text to leak.
        try:
            gens = response.generations or []
            message = gens[-1][-1].message if gens and gens[-1] else None
        except Exception:  # noqa: BLE001 — shape drift must not kill the job
            return
        text = _content_text(getattr(message, "content", None)).strip()
        if text:
            self._emit("text", text)

    def on_tool_start(self, serialized, input_str, **kwargs):  # noqa: ANN001
        # Name + SANITIZED args (few keys, truncated values): the UI renders
        # curated labels off (name, args) — "Read tables/races", never raw
        # JSON — via the same wikiTools presentation the chat timeline uses.
        name = (serialized or {}).get("name", "tool")
        raw = kwargs.get("inputs")
        args: dict[str, Any] = {}
        if isinstance(raw, dict):
            for key in list(raw)[:6]:
                v = raw[key]
                if not isinstance(v, (int, float, bool)):
                    v = str(v)
                    if len(v) > 120:
                        v = v[:120] + "…"
                args[str(key)] = v
        self._emit("tool", name, tool_input=args)


def build_clients() -> dict[str, Any]:
    """Live boto3 clients (the entrypoint path); tests pass their own."""
    import boto3

    region = os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_REGION")
    clients = {
        "s3": boto3.client("s3", region_name=region),
        "ddb": boto3.client("dynamodb", region_name=region),
        "s3vectors": boto3.client("s3vectors", region_name=region),
        "bedrock_runtime": boto3.client("bedrock-runtime", region_name=region),
        "athena": boto3.client("athena", region_name=region),
    }
    # Redshift-mapped datasets execute via the Data API; client + IAM exist
    # only when the deploy enables Redshift (mirrors the consumption runtime).
    if os.environ.get("OKF_REDSHIFT_ENABLED", "").lower() in ("1", "true", "yes"):
        clients["redshift_data"] = boto3.client("redshift-data", region_name=region)
    return clients


def run_canvas_job(
    payload: dict[str, Any],
    *,
    session_id: str | None = None,
    clients: dict[str, Any] | None = None,
    agent_factory=None,
    renderer: Any = None,
) -> dict[str, Any]:
    """Execute one job. ``agent_factory``/``clients``/``renderer`` are
    injectable for tests; the entrypoint passes nothing and gets the live
    stack. Returns a summary dict (also reflected on the job row)."""
    from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

    env = os.environ
    clients = clients or build_clients()
    ddb = clients["ddb"]
    canvas_table = env.get("OKF_CANVAS_TABLE", "okf-canvas")

    job_id = payload["job_id"]
    user_sub = payload["user_sub"]
    data_domain = payload["data_domain"]
    dataset = payload["dataset"]
    request = payload.get("request") or ""
    canvas_id = payload.get("canvas_id") or ""
    feedback = payload.get("feedback") or ""
    title = payload.get("title") or ""

    jobs.put_job(
        ddb, table=canvas_table, user_sub=user_sub, job_id=job_id,
        request=request or feedback, data_domain=data_domain, dataset=dataset,
        canvas_id=canvas_id, thread_id=payload.get("thread_id") or "",
        title=title,
        ttl_seconds=int(env.get("OKF_CANVAS_JOB_TTL_SECONDS", "") or cv.JOB_TTL_SECONDS),
    )

    active_renderer = None
    ctx = None
    try:
        config = ConsumptionConfig(
            bundle_bucket=env["OKF_BUNDLE_BUCKET"],
            vector_bucket=env.get("OKF_VECTOR_BUCKET", ""),
            vector_index=env.get("OKF_VECTOR_INDEX", ""),
            registry_table=env.get("OKF_REGISTRY_TABLE", "okf-registry"),
            computations_enabled=True,  # the whole point of this runtime
            redshift_enabled=env.get("OKF_REDSHIFT_ENABLED", "").lower()
            in ("1", "true", "yes"),
            athena_workgroup=env.get("OKF_ATHENA_WORKGROUP", ""),
            athena_output=env.get("OKF_ATHENA_OUTPUT", ""),
            athena_catalog=env.get("OKF_ATHENA_CATALOG", "AwsDataCatalog"),
            computation_max_rows=int(env.get("OKF_COMPUTATION_MAX_ROWS", "") or 200),
            computation_timeout_s=float(env.get("OKF_COMPUTATION_TIMEOUT_S", "") or 120),
        )
        # ConsumptionTools takes a RESOURCE-style registry Table; the row
        # helpers keep the low-level client.
        registry = clients.get("registry")
        if registry is None:
            import boto3

            region = (
                env.get("AWS_REGION_NAME") or env.get("AWS_REGION") or "us-east-1"
            )
            registry = boto3.resource("dynamodb", region_name=region).Table(
                config.registry_table
            )
        consumption = ConsumptionTools(
            s3=clients["s3"],
            s3vectors=clients.get("s3vectors"),
            bedrock_runtime=clients.get("bedrock_runtime"),
            ddb=registry,
            config=config,
            athena=clients.get("athena"),
            redshift_data=clients.get("redshift_data"),
        )

        from okf_aws import computation_run

        def load_computation(slug: str):
            comp, _errors = computation_run.load_computation(
                clients["s3"], config.bundle_bucket, data_domain, dataset, slug
            )
            return comp

        # The OBJECT-based executor: the same call runs bundle computations
        # and synthesized ad-hoc tiles (both are Computation objects).
        from okf_core.sources import normalize_source

        try:
            item = registry.get_item(
                Key={"pk": f"DOMAIN#{data_domain}", "sk": f"DATASET#{dataset}"}
            ).get("Item") or {}
            source = normalize_source(
                item.get("source"), glue_database=item.get("glue_database")
            )
        except Exception:  # noqa: BLE001 - no descriptor => render-only receipts
            source = None

        # Successful runs are memoized for the JOB's lifetime, keyed on the
        # statement's content hash + the exact values. The model lints after
        # every edit (correctly), but each lint re-runs EVERY tile live — an
        # unchanged tile's identical query was hitting the engine once per
        # lint (observed live: the same COUNT(*) executed 13× in one job).
        # With the memo, a re-lint pays only for tiles that actually changed;
        # render_canvas rides the same cache. Failures are never cached, so a
        # transient engine error still retries on the next call.
        _memo: dict[tuple[str, str], dict[str, Any]] = {}

        def execute(comp, values):
            key = (comp.sha256, json.dumps(values or {}, sort_keys=True, default=str))
            if key in _memo:
                return _memo[key]
            receipt = computation_run.run_computation(
                comp,
                values,
                athena=clients.get("athena"),
                redshift_data=clients.get("redshift_data"),
                source=source,
                athena_workgroup=config.athena_workgroup,
                athena_output=config.athena_output,
                athena_catalog=config.athena_catalog,
                max_rows=config.computation_max_rows,
                timeout_s=config.computation_timeout_s,
                execute=True,
            )
            if not receipt.get("executed"):
                raise RuntimeError(receipt.get("note") or "execution refused")
            out = {
                "columns": receipt.get("columns") or [],
                "rows": receipt.get("rows") or [],
            }
            if len(_memo) < 512:  # a job touches dozens of (tile, dial) pairs, not thousands
                _memo[key] = out
            return out

        active_renderer = renderer if renderer is not None else _default_renderer(env)
        ctx = ToolContext(
            ddb=ddb,
            canvas_table=canvas_table,
            user_sub=user_sub,
            data_domain=data_domain,
            dataset=dataset,
            consumption=consumption,
            load_computation=load_computation,
            execute=execute,
            renderer=active_renderer,
            canvas_id=canvas_id,
            # Inline-SQL tiles execute on the dataset's engine — a hardcoded
            # athena made every ad-hoc tile refuse on redshift datasets.
            adhoc_runtime=cv.source_runtime(source),
            revising=bool(canvas_id),
        )
        # A revision job carries the stored config into the brief. A MISSING
        # row means the user deleted the canvas after requesting the
        # revision: fail loudly — falling through would silently drop the
        # feedback, rebuild a generic canvas, and resurrect the deleted id.
        existing = None
        if canvas_id:
            row = jobs.get_canvas_row(
                ddb, table=canvas_table, user_sub=user_sub, canvas_id=canvas_id
            )
            if row is None:
                raise RuntimeError(
                    f"canvas {canvas_id} no longer exists — it was deleted "
                    "after this revision was requested; nothing was revised"
                )
            existing = row.get("config", {}).get("S", "")
            ctx.row_version = row.get("updated_at", {}).get("S", "")

        factory = agent_factory or _default_agent_factory
        agent = factory(ctx, env=env)

        feed = _StepFeed(ddb, table=canvas_table, user_sub=user_sub, job_id=job_id)
        task = _job_message(
            request, feedback, existing, data_domain, dataset, title,
            spec=payload.get("spec"),
        )
        result = agent.invoke(
            {"messages": [{"role": "user", "content": task}]},
            config={
                "configurable": {"thread_id": f"{user_sub}:canvasjob-{job_id}"},
                "recursion_limit": RECURSION_LIMIT,
                "callbacks": [feed],
            },
        )
        # The final text already rode the feed via on_llm_end (it's the last
        # turn's text) — appending it again here would duplicate the step.
        final = _final_text(result)
        if ctx.saved:
            jobs.set_job_status(
                ddb, table=canvas_table, user_sub=user_sub, job_id=job_id,
                status="completed", canvas_id=ctx.canvas_id,
            )
            return {"status": "completed", "canvas_id": ctx.canvas_id, "summary": final}
        jobs.set_job_status(
            ddb, table=canvas_table, user_sub=user_sub, job_id=job_id,
            status="failed", canvas_id=ctx.canvas_id,
            error=final[:500] or "the agent finished without creating the canvas",
        )
        return {"status": "failed", "canvas_id": ctx.canvas_id, "summary": final}
    except Exception as e:  # noqa: BLE001 — the row must reflect the outcome
        log.exception("canvas job %s failed", job_id)
        if ctx is not None and ctx.saved:
            # save_canvas already made the canvas live; only a LATER turn
            # (the final summary — a Bedrock timeout, throttle, recursion
            # cap) died. Reporting "failed" with no canvas_id would hide
            # "Open canvas" and invite a duplicate re-request.
            jobs.set_job_status(
                ddb, table=canvas_table, user_sub=user_sub, job_id=job_id,
                status="completed", canvas_id=ctx.canvas_id,
            )
            return {
                "status": "completed",
                "canvas_id": ctx.canvas_id,
                "summary": f"canvas saved; the run ended early afterwards "
                f"({type(e).__name__}: {e})",
            }
        jobs.set_job_status(
            ddb, table=canvas_table, user_sub=user_sub, job_id=job_id,
            status="failed", error=f"{type(e).__name__}: {e}",
        )
        return {"status": "failed", "error": str(e)}
    finally:
        # The renderer keeps a Chromium + loopback server alive across the
        # job's render calls — release them here (the microVM outlives one
        # job). Fakes injected by tests may not expose close().
        closer = getattr(active_renderer, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — teardown must not mask the job result
                log.debug("renderer close failed", exc_info=True)


def _job_message(
    request: str,
    feedback: str,
    existing: str | None,
    domain: str,
    dataset: str,
    title: str = "",
    spec: dict | None = None,
) -> str:
    import json as _json

    # The tile plan the chat agent agreed with the user (canvas_skeleton) —
    # REQUIREMENTS for this job; the system prompt sets the honor-intent /
    # name-deviations contract for it.
    plan = (
        "\n\nThe tile plan agreed with the user — each tile's title, widget "
        "type, what it represents, and the dials:\n"
        f"```json\n{_json.dumps(spec, indent=2)}\n```"
        if spec
        else ""
    )
    if feedback and existing:
        return (
            f"Revise the existing canvas for {domain}/{dataset}.\n\n"
            f"Current config:\n```json\n{existing}\n```\n\n"
            f"The user's feedback:\n{feedback}"
            + plan
            + "\n\nApply it surgically, re-validate, re-render, and resubmit."
        )
    named = f' titled "{title}"' if title else ""
    return (
        f"Build a canvas{named} for {domain}/{dataset}."
        + (f"\n\nThe user's request:\n{request}" if request else "")
        + plan
        + (f"\n\nUse \"{title}\" as the canvas title." if title else "")
    )


def _content_text(content: Any) -> str:
    """The text blocks of an AIMessage content (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _final_text(result: Any) -> str:
    messages = (result or {}).get("messages") or []
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if getattr(msg, "type", "") == "ai" and content:
            text = _content_text(content)
            if text:
                return text
    return ""


def _default_renderer(env) -> Any:
    harness = env.get("OKF_CANVAS_HARNESS_PATH", "")
    if not harness or not os.path.exists(harness):
        return None
    try:
        from canvas.render import PlaywrightRenderer

        return PlaywrightRenderer(harness_path=harness)
    except Exception:  # noqa: BLE001 — no browser -> render degrades to unavailable
        log.warning("playwright unavailable; render_canvas degrades", exc_info=True)
        return None


def make_checkpointer(env) -> Any:
    """The job-thread DynamoDBSaver, or None when no table is configured.

    Mirrors chat's ``make_checkpointer`` exactly (chat/server.py) — the S3
    offload rides in ``s3_offload_config={"bucket_name": …}``, NOT a flat
    kwarg (a live TypeError taught us the constructor is unforgiving)."""
    table = env.get("OKF_CHAT_CHECKPOINT_TABLE", "")
    if not table:
        return None
    from langgraph_checkpoint_aws import DynamoDBSaver

    kwargs: dict[str, Any] = {}
    bucket = env.get("OKF_CHECKPOINT_OFFLOAD_BUCKET", "")
    if bucket:
        kwargs["s3_offload_config"] = {"bucket_name": bucket}
    return DynamoDBSaver(
        table_name=table,
        region_name=env.get("AWS_REGION_NAME") or env.get("AWS_REGION"),
        # A finished job's thread is pure history (revisions start FRESH
        # threads; the canvas row is the durable state) — let DynamoDB reap
        # it. 30 days default: far beyond any job's lifetime, cheap to keep.
        ttl_seconds=int(env.get("OKF_CANVAS_JOB_TTL_SECONDS", "") or 30 * 86400),
        enable_checkpoint_compression=True,
        **kwargs,
    )


def _bedrock_config(env) -> Any:
    """botocore config for the model client — harvest's convention
    (harvest/agent.py): botocore's DEFAULT 60s read_timeout is fatal here,
    because an Opus turn over a long job context can think for minutes before
    the Converse response arrives (observed live: ReadTimeoutError on
    /converse killed two canvas jobs 15+ lints in). Lift it and let botocore
    retry transient failures. All knobs env-overridable."""
    from botocore.config import Config

    return Config(
        read_timeout=int(env.get("OKF_CANVAS_BEDROCK_READ_TIMEOUT", "") or 600),
        connect_timeout=int(env.get("OKF_CANVAS_BEDROCK_CONNECT_TIMEOUT", "") or 10),
        retries={
            "max_attempts": int(env.get("OKF_CANVAS_BEDROCK_MAX_ATTEMPTS", "") or 5),
            "mode": "adaptive",
        },
    )


def _default_agent_factory(ctx: ToolContext, *, env) -> Any:
    """deepagents agent on the shared model factory. surface_reasoning stays
    OFF (the factory default): adaptive thinking runs, no reasoning text is
    returned, so none can reach checkpoints or the step feed."""
    from deepagents import (
        GeneralPurposeSubagentProfile,
        HarnessProfile,
        create_deep_agent,
        register_harness_profile,
    )

    from okf_aws.model_factory import build_model

    # This job is strictly SOLO, but deepagents auto-adds a `general-purpose`
    # subagent and binds the `task` tool even with subagents=None — a standing
    # delegation invitation (Opus 5 delegates readily) with nothing worth
    # delegating to, plus its tool-schema cost on every request. Disabling the
    # default subagent removes the tool entirely: SubAgentMiddleware only
    # attaches when subagents exist. Keyed per PROVIDER (the registry's
    # fallback for pre-built model instances) so it holds for both the
    # Converse and OpenAI paths; registration is additive merge — repeating
    # it per job is safe.
    solo = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)
    )
    for provider in ("amazon_bedrock", "openai"):
        register_harness_profile(provider, solo)

    # Fallbacks mirror chat's defaults (config.py DEFAULT_MODEL): the global.*
    # inference profile is the id Bedrock accepts — never a dated us.* guess.
    model = build_model(
        env.get("OKF_CANVAS_MODEL", "global.anthropic.claude-opus-5-5"),
        env.get("OKF_CANVAS_EFFORT", "high"),
        int(env.get("OKF_CANVAS_MAX_TOKENS", "") or 32768),
        region=env.get("AWS_REGION_NAME") or env.get("AWS_REGION") or "us-east-1",
        botocore_config=_bedrock_config(env),
    )
    return create_deep_agent(
        model=model,
        tools=build_all_tools(ctx),
        system_prompt=system_prompt(),
        checkpointer=make_checkpointer(env),
    )
