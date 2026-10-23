"""Expose the reused ``ConsumptionTools`` as LangChain tools for the chat agent.

The chat agent's tools ARE the consumption read tools — the same logic the MCP
server exposes to external agents — reused **in-process** (no MCP hop, no M2M
token): the chat runtime holds the same read-only IAM grants (bundle read,
Bedrock embed, S3 Vectors query, registry read). ``ConsumptionTools`` was written
with injected clients and no FastMCP dependency precisely so it can be reused
like this.

Dataset scoping (``@``-mention): when a conversation is scoped to one dataset,
the ``data_domain``/``dataset`` params are DROPPED from the tool schema the model
sees and injected at call time — so the model can't wander off-dataset by
fumbling those args, and its tool calls are simpler. Scope is advisory relevance
context, NOT a security boundary (the IAM role can read any bundle). Unscoped
(the default) exposes the full-arg tools over the whole wiki.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Any, Callable

from langchain_core.tools import StructuredTool

log = logging.getLogger("chat.tools")

# The reused pure tool logic (installed via the okf-consumption-mcp package).
from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

# The location args a scoped conversation pre-binds, in order.
_SCOPE_PARAMS = ("data_domain", "dataset")


def _model_description(doc: str | None, fallback: str) -> str:
    """Normalize a Python docstring into the text the MODEL sees as a description.

    Two cheap wins on every request's tool block (the descriptions are re-sent on
    every turn, so the waste compounds):

    * ``inspect.cleandoc`` strips the 8-space continuation indent a nested
      triple-quoted string carries — those leading spaces are tokens the model
      pays for and learns nothing from.
    * RST double-backticks (``x``) become single backticks (`x`) — Markdown is
      what an LLM tool description is read as, and the doubled pair costs an
      extra token per span while rendering as a stray backtick.
    """
    text = inspect.cleandoc(doc or fallback)
    return text.replace("``", "`").strip()


def build_consumption_tools(
    *,
    s3,
    s3vectors,
    bedrock_runtime,
    ddb,
    config: ConsumptionConfig,
    athena=None,
    redshift_data=None,
) -> ConsumptionTools:
    """Assemble a :class:`ConsumptionTools` from injected clients (live or fake).

    ``athena``/``redshift_data`` power ``run_computation`` execution; absent,
    it returns the rendered SQL without running (the chat wires them from its
    own SQL-flag-gated clients — same grants run_sql rides)."""
    return ConsumptionTools(
        s3=s3,
        s3vectors=s3vectors,
        bedrock_runtime=bedrock_runtime,
        ddb=ddb,
        config=config,
        athena=athena,
        redshift_data=redshift_data,
    )


def _make_tool(
    method: Callable[..., Any], scope: dict[str, str] | None
) -> StructuredTool:
    """Build a StructuredTool from a bound ``ConsumptionTools`` method.

    Unscoped: the method's own signature is used verbatim. Scoped: the
    ``data_domain``/``dataset`` params the method accepts are removed from the
    inferred arg schema (so the LLM never sees them) and injected from ``scope``
    at call time. Preserving the real signature (rather than a ``*args/**kwargs``
    wrapper, which collapses the schema to ``['args','kwargs']``) is what keeps
    the tool callable by the model.
    """
    sig = inspect.signature(method)
    dropped = (
        [p for p in _SCOPE_PARAMS if p in sig.parameters] if scope else []
    )
    kept = [p for name, p in sig.parameters.items() if name not in dropped]
    new_sig = sig.replace(parameters=kept)

    def wrapper(**kwargs: Any) -> Any:
        for p in dropped:
            kwargs[p] = scope[p]  # type: ignore[index]  # scope is set when dropped is non-empty
        # A tool failure (a missing bundle key, a bad regex, an S3/registry blip)
        # must come back to the model as a tool RESULT it can react to — read a
        # different doc, fix the arg, tell the user — NOT propagate out and crash
        # the whole run (which surfaced to the user as a raw NoSuchKey trace). We
        # return the error text as the tool's result; LangChain wraps it in a
        # ToolMessage and the agent loop continues. ValueError (bad tool input) is
        # kept concise; anything else is logged server-side with its type.
        try:
            return method(**kwargs)
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - a tool error is feedback, not a crash
            log.warning("chat tool %s failed", method.__name__, exc_info=True)
            return f"Error: {method.__name__} failed: {type(e).__name__}: {e}"

    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    wrapper.__annotations__ = {
        name: p.annotation
        for name, p in new_sig.parameters.items()
        if p.annotation is not inspect.Parameter.empty
    }
    return StructuredTool.from_function(
        func=wrapper,
        name=method.__name__,
        description=_model_description(method.__doc__, method.__name__),
    )


# The read tools the agent gets, in a sensible discovery order.
# list/describe_computation ride here UNGATED: they are bundle reads (the
# docs are read_page-able anyway); only run_computation executes source SQL
# and sits behind the per-run opt-in (make_run_computation_tool).
_TOOL_NAMES = (
    "list_domains",
    "list_declared_domains",
    "search_domains",
    "list_directory",
    "read_page",
    "get_backlinks",
    "glob",
    "grep",
    "semantic_search",
    "get_bundle_diff",
    "list_computations",
    "describe_computation",
)


def make_run_computation_tool(
    tools: ConsumptionTools,
    *,
    dataset_scope: dict[str, str] | None = None,
    policy_checker: Any = None,
) -> StructuredTool:
    """The chat-side ``run_computation`` — ALWAYS bound, never opt-in gated.

    A computation is the SANCTIONED execution path (frozen statement, typed
    validated values, caps), so unlike ``run_sql`` it rides every run — the
    per-run SQL opt-in must not be the price of the safe tier. Execution
    capability still follows the deployment: without the chat SQL flag the
    engine clients are absent and the receipt returns the rendered SQL
    un-executed. Delegates to :meth:`ConsumptionTools.run_computation` (the
    exact same validated-substitution path the MCP serves). The policy checker
    (when armed) sees the receipt's ``executed_sql`` — the real statement that
    ran, holes filled — and its note rides back after the payload exactly like
    run_sql's (split into a shield step by the stream)."""

    def run_computation(
        name: str,
        data_domain: str,
        dataset: str,
        parameters: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one of the dataset's Attested Computations by passing typed
        parameter VALUES (see list_computations / describe_computation) —
        never by writing SQL. The platform validates values against the
        declared contracts, renders them into the sanctioned statement, and
        runs read-only under caps. Prefer a matching computation over deriving
        SQL with run_sql: it is the blessed, human-verifiable answer to that
        question shape (the receipt's `verification` field says whether a
        human attested it). The receipt carries the exact `executed_sql` and
        `warnings` for values outside profiled domains (0 rows + a warning
        usually means a typo'd value).
        """
        receipt = tools.run_computation(
            name, data_domain, dataset, parameters=parameters
        )
        sql = receipt.get("executed_sql") if isinstance(receipt, dict) else None
        if policy_checker is None or not sql or not receipt.get("executed"):
            return receipt
        # The statement is only known post-substitution, so unlike run_sql the
        # check can't race the engine — submit after and wait within the same
        # advisory budget. Failures fail open (results return untouched).
        try:
            future = policy_checker.submit(sql)
            should_wait = getattr(policy_checker, "should_wait", None)
            wait = bool(should_wait(sql)) if should_wait else True
        except Exception:  # noqa: BLE001 - the check is advisory
            log.warning("policy check submit failed (non-fatal)", exc_info=True)
            return receipt
        if not wait:
            return receipt
        from chat.sql import _await_policy_note

        note = _await_policy_note(future, policy_checker)
        if note:
            return json.dumps(receipt) + "\n\n" + note
        return receipt

    return _make_tool(run_computation, dataset_scope)


def make_canvas_tools(
    canvas_table,
    agentcore,
    *,
    runtime_arn: str,
    user_sub: str,
    dataset_scope: dict[str, str] | None = None,
    thread_id: str = "",
):
    """The canvas tools: list / describe / skeleton / create / status / revise.

    Requirements are captured IN CHAT before any job runs: ``canvas_skeleton``
    validates the tile plan (each tile's title, widget type, and what it
    represents, plus the dials) against the shared widget catalog and the UI
    renders its args as an inline wireframe the user reacts to; the agreed
    spec then rides ``create_canvas`` and the job payload as requirements for
    the CI runtime. Authoring happens on the DEDICATED canvas CI runtime
    (async job — it reads the wiki, designs tiles over Attested Computations
    or ad-hoc SQL, lints, renders in a headless browser, saves); the chat
    agent only captures, delegates, and reports. ``canvas_table`` is a
    resource-style Table (the job rows the CI runtime updates live there);
    ``agentcore`` is the bedrock-agentcore data-plane client. ``user_sub`` is
    closed over from the validated JWT — jobs and dashboards are structurally
    the user's own.
    """
    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    from langchain_core.tools import StructuredTool

    from okf_core.session import runtime_session_id

    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _start(payload: dict, title: str) -> str:
        from okf_core import canvas as cv

        job_id = _uuid.uuid4().hex
        payload = {"mode": "canvas", "job_id": job_id, "user_sub": user_sub, **payload}
        if thread_id:
            # The delegating conversation: the UI scopes the jobs indicator to
            # the chat the job came from.
            payload["thread_id"] = thread_id
        # A queued row first, so the jobs panel sees the job even if the
        # invoke below fails (the except path flips it to failed).
        item = {
            "pk": cv.canvas_job_pk(user_sub),
            "sk": job_id,
            "job_id": job_id,
            "status": cv.JOB_QUEUED,
            # `title` is the ONLY text the jobs UI shows for this job.
            "title": title[:200],
            "request": (payload.get("request") or payload.get("feedback") or "")[:2000],
            "data_domain": payload["data_domain"],
            "dataset": payload["dataset"],
            "steps": "[]",
            "created_at": _now(),
            "updated_at": _now(),
            # DynamoDB TTL (epoch seconds): job rows are history.
            "expires_at": int(datetime.now(timezone.utc).timestamp())
            + cv.JOB_TTL_SECONDS,
        }
        if thread_id:
            item["thread_id"] = thread_id
        canvas_table.put_item(Item=item)

        def _fail_job(msg: str) -> None:
            canvas_table.update_item(
                Key={"pk": cv.canvas_job_pk(user_sub), "sk": job_id},
                UpdateExpression="SET #s = :s, #e = :e, updated_at = :now",
                ExpressionAttributeNames={"#s": "status", "#e": "error"},
                ExpressionAttributeValues={
                    ":s": cv.JOB_FAILED,
                    ":e": msg[:500],
                    ":now": _now(),
                },
            )

        try:
            resp = agentcore.invoke_agent_runtime(
                agentRuntimeArn=runtime_arn,
                # Deterministic per job: control calls re-hit the same microVM
                # while it lives; the durable record is the checkpoint thread.
                runtimeSessionId=runtime_session_id(
                    "canvasjob", payload["data_domain"], unique_token=job_id
                ),
                qualifier="DEFAULT",
                payload=_json.dumps(payload).encode("utf-8"),
            )
        except Exception as e:  # noqa: BLE001 — surface as a failed job, not a crash
            _fail_job(f"could not start the canvas runtime: {e}")
            # ValueError → the guard returns it as a concise tool result the
            # model can relay ("Error: could not start …").
            raise ValueError(f"could not start the canvas runtime: {e}") from e
        # The entrypoint acks synchronously: {"status": "accepted"|"rejected"}.
        # A rejection writes NO DDB on the runtime side — ignoring it stranded
        # the queued row forever, with the jobs panel polling a spinner that
        # could never resolve. An unreadable ack is NOT a failure (the runner
        # rows its own progress); only an explicit rejection flips the row.
        ack: Any = None
        try:
            body = resp.get("response") if isinstance(resp, dict) else None
            if hasattr(body, "read"):
                body = body.read()
            if isinstance(body, (bytes, bytearray)):
                body = body.decode("utf-8", "replace")
            if isinstance(body, str) and body.strip():
                ack = _json.loads(body)
        except Exception:  # noqa: BLE001 — tolerate any ack transport shape
            ack = None
        if isinstance(ack, dict) and ack.get("status") == "rejected":
            reason = str(ack.get("error") or "the canvas runtime rejected the job")
            _fail_job(reason)
            raise ValueError(f"the canvas runtime rejected the job: {reason}")
        return job_id

    def _scope() -> tuple[str, str] | None:
        if dataset_scope:
            return dataset_scope["data_domain"], dataset_scope["dataset"]
        return None

    def _skeleton(title: str, widgets: list, dials: list | None = None) -> str:
        from okf_core import canvas as cv

        # The UI renders this call's ARGS as the wireframe the moment the call
        # streams — the return value is for the MODEL: the normalized spec to
        # carry into create_canvas, or corrective errors to fix and re-sketch.
        spec, errors = cv.normalize_skeleton(
            {"title": title, "widgets": widgets or [], "dials": dials or []}
        )
        if errors:
            return "error: " + "; ".join(errors)
        return _json.dumps(
            {
                "spec": spec,
                "note": "the user now sees this wireframe inline — iterate on "
                "their reaction, and pass this spec to create_canvas once "
                "they agree",
            }
        )

    def _create(
        title: str, spec: dict, request: str = "", data_domain: str = "", dataset: str = ""
    ) -> str:
        from okf_core import canvas as cv

        pinned = _scope()
        if pinned:
            data_domain, dataset = pinned
        if not (data_domain and dataset):
            return (
                "error: name the dataset — data_domain + dataset "
                "(see list_domains)"
            )
        if not (title or "").strip():
            return "error: give the canvas a short human title"
        normalized, errors = cv.normalize_skeleton(spec)
        if errors:
            return (
                "error: the spec does not validate — fix it via canvas_skeleton "
                "(the user must see the corrected plan): " + "; ".join(errors)
            )
        normalized["title"] = title.strip()
        job_id = _start(
            {
                "data_domain": data_domain,
                "dataset": dataset,
                "request": (request or "").strip(),
                "spec": normalized,
                "title": title.strip(),
            },
            title.strip(),
        )
        return (
            f"started canvas job {job_id} for {data_domain}/{dataset}. It runs "
            f"in the background (typically several minutes) — the user can watch "
            f"it via the jobs indicator in this chat, or ask you to check with "
            f"get_canvas_status('{job_id}')."
        )

    def _list(data_domain: str = "", dataset: str = "") -> Any:
        from boto3.dynamodb.conditions import Key

        from okf_core import canvas as cv

        items: list[dict] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(cv.canvas_pk(user_sub))
        }
        while True:
            resp = canvas_table.query(**kwargs)
            items.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
            kwargs["ExclusiveStartKey"] = start
        dd = (data_domain or "").strip()
        ds = (dataset or "").strip()
        rows = [
            {
                "canvas_id": i.get("canvas_id"),
                "title": i.get("title"),
                "data_domain": i.get("data_domain"),
                "dataset": i.get("dataset"),
                "status": i.get("status"),
                "updated_at": i.get("updated_at"),
            }
            for i in items
            if (not dd or i.get("data_domain") == dd)
            and (not ds or i.get("dataset") == ds)
        ]
        rows.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
        return {
            "canvases": rows,
            "filtered_by": {"data_domain": dd, "dataset": ds} if (dd or ds) else None,
        }

    def _describe(canvas_id: str, include_config: bool = False) -> Any:
        from okf_core import canvas as cv

        canvas_id = (canvas_id or "").strip()
        row = canvas_table.get_item(
            Key={"pk": cv.canvas_pk(user_sub), "sk": canvas_id}
        ).get("Item")
        if not row:
            return f"error: no canvas {canvas_id!r} for this user"
        try:
            config = _json.loads(row.get("config") or "{}")
        except ValueError:
            return f"error: the stored config for {canvas_id!r} is not JSON"
        tiles = []
        for t in config.get("tiles") or []:
            data = t.get("data") or {}
            tile: dict[str, Any] = {
                "id": t.get("id"),
                "title": t.get("title"),
                "widget": t.get("widget"),
                "layout": t.get("layout"),
                # Dial/constant bindings for this tile's parameters.
                "bind": data.get("bind") or {},
            }
            # The SQL behind the tile: verbatim for an ad-hoc tile; for a
            # computation-backed tile the frozen statement lives in the wiki
            # doc, which is the authoritative (human-verifiable) copy.
            if data.get("sql"):
                tile["sql"] = data["sql"]
                tile["parameters"] = data.get("parameters") or []
            elif data.get("computation"):
                tile["computation"] = data["computation"]
                tile["sql_source"] = (
                    f"frozen SQL in the wiki — describe_computation("
                    f"{data['computation']!r})"
                )
            tiles.append(tile)
        out: dict[str, Any] = {
            "canvas_id": row.get("canvas_id"),
            "title": row.get("title"),
            "status": row.get("status"),
            "data_domain": row.get("data_domain"),
            "dataset": row.get("dataset"),
            "updated_at": row.get("updated_at"),
            # The CONFIG is the source of truth for what the canvas does — the
            # authoring runtime may have deviated from the agreed plan where
            # the data forced it, so this is derived from the config, never
            # from the skeleton the user originally approved.
            "dials": config.get("dials") or [],
            "tiles": tiles,
        }
        if include_config:
            out["config"] = config
        return out

    def _status(job_id: str) -> Any:
        from okf_core import canvas as cv

        item = canvas_table.get_item(
            Key={"pk": cv.canvas_job_pk(user_sub), "sk": (job_id or "").strip()}
        ).get("Item")
        if not item:
            return f"error: no canvas job {job_id!r} for this user"
        try:
            steps = _json.loads(item.get("steps") or "[]")
        except ValueError:
            steps = []
        return {
            "job_id": item.get("job_id"),
            "status": item.get("status"),
            "canvas_id": item.get("canvas_id"),
            "error": item.get("error"),
            "recent_steps": [s.get("label", "") for s in steps[-8:]],
            "note": "status `completed` means the canvas is LIVE on the "
            "user's Canvas page (#/canvas/<canvas_id>)",
        }

    def _revise(
        canvas_id: str,
        feedback: str,
        spec: dict | None = None,
    ) -> str:
        from okf_core import canvas as cv

        canvas_id = (canvas_id or "").strip()
        if not canvas_id or not (feedback or "").strip():
            return "error: revise needs the canvas_id and the user's feedback"
        row = canvas_table.get_item(
            Key={"pk": cv.canvas_pk(user_sub), "sk": canvas_id}
        ).get("Item")
        if not row:
            return f"error: no canvas {canvas_id!r} for this user"
        payload = {
            # The canvas ROW is authoritative: a revision continues that
            # canvas's dataset whatever this conversation is pinned to.
            # (list_canvases deliberately spans datasets — letting a pinned
            # scope override here made every cross-dataset revision fail the
            # CI's scope guard, or worse, reassign the canvas.)
            "data_domain": row.get("data_domain", ""),
            "dataset": row.get("dataset", ""),
            "canvas_id": canvas_id,
            "feedback": feedback.strip(),
            "title": row.get("title", ""),
        }
        if spec:
            # A structural revision carries the UPDATED plan the user agreed
            # to (via canvas_skeleton) — cosmetic feedback travels bare.
            normalized, errors = cv.normalize_skeleton(spec)
            if errors:
                return (
                    "error: the spec does not validate — fix it via "
                    "canvas_skeleton: " + "; ".join(errors)
                )
            payload["spec"] = normalized
        job_id = _start(
            payload,
            # A revision job shows under the canvas's existing title.
            row.get("title", "") or "canvas revision",
        )
        return (
            f"started revision job {job_id} on canvas {canvas_id}; check with "
            f"get_canvas_status('{job_id}')."
        )

    from okf_core.canvas import WIDGETS as _WIDGETS

    # The widget vocabulary is DERIVED from the catalog, so a new widget type
    # reaches the sketching agent the moment okf_core ships it (a hardcoded
    # list here silently hid pivot & co. from the model).
    _catalog = ", ".join(_WIDGETS)
    skeleton_doc = inspect.cleandoc(
        f"""Sketch the canvas PLAN as a wireframe the user sees inline in this
        chat — always call this before create_canvas, and again after every
        change the user asks for. `widgets` is the tile list, each
        `{{title, widget, represents, layout?}}`: `widget` from the fixed
        catalog ({_catalog}), `represents` one
        business-language line on what the tile shows, `layout` optional
        `{{w, h}}` in 12-column grid units. `dials` are the dashboard-level
        filters, each `{{name, label, filters}}` (`filters` = what it scopes).
        Ground the plan in the wiki FIRST (tables, columns, computations —
        every widget must be answerable from documented data), propose a
        COMPLETE skeleton on your first pass, and ask the user only the
        questions that would change the design. Returns the normalized spec —
        pass it to create_canvas verbatim once the user agrees."""
    )
    create_doc = inspect.cleandoc(
        """Delegate building a CANVAS — a persistent, dial-driven dashboard — to
        the dedicated authoring runtime. Use when the user wants a reusable
        artifact ("build me a dashboard/canvas of…", recurring KPIs they keep
        asking for) — NOT for one-off charts (render_chart handles those
        inline). `spec` is the tile plan agreed with the user — the exact
        spec canvas_skeleton returned; do NOT call this before the user has
        seen the wireframe and agreed to it. `title` is the short human name
        for the canvas (e.g. "Season overview") — it is the only text the
        user sees while the job runs, so make it read like a dashboard name,
        not a prompt. `request` is optional context the structure can't carry
        (caveats, emphasis, the user's own phrasing). The job is async — tell
        the user it's running and that the finished canvas appears on their
        Canvas page."""
    )
    list_doc = inspect.cleandoc(
        """List the user's existing canvases (newest-updated first): canvas_id,
        title, dataset, status, updated_at. Call this when the user refers to a
        dashboard they already have ("update my season canvas", "what
        dashboards do I have?") — you need the canvas_id before
        describe_canvas or revise_canvas. Optional `data_domain`/`dataset`
        filter to one dataset; omit both to list every canvas the user owns
        across datasets. ALSO check here before building something new: an
        existing canvas may already answer the request, and revising beats
        creating a near-duplicate."""
    )
    describe_doc = inspect.cleandoc(
        """Read what an existing canvas actually contains: its dials, and per
        tile the title, widget type, layout, parameter bindings, and the QUERY
        behind it — inline ad-hoc SQL verbatim, or the computation slug whose
        frozen SQL you can read with describe_computation. Use it to answer
        questions about a canvas ("what does the trend tile measure?", "where
        does this number come from?") and to ground a revision in what is
        really there. This reflects the SAVED config, which is authoritative:
        the authoring runtime may have deviated from the originally agreed
        plan where the data forced it. `include_config` also returns the raw
        config JSON — ask for it only when you need the exact encodings."""
    )
    status_doc = inspect.cleandoc(
        """Check a canvas job started earlier this conversation (or reported by
        the user). Returns lifecycle status, recent activity, and — once
        submitted — the canvas_id the user reviews on the Canvas page."""
    )
    revise_doc = inspect.cleandoc(
        """Send the user's feedback on an existing canvas back to the authoring
        runtime as a revision job. `canvas_id` identifies the canvas (from
        get_canvas_status or the user's Canvas page); `feedback` is what
        should change, verbatim from the user where possible. When the
        feedback changes the STRUCTURE (tiles added / removed / retyped, new
        dials), agree the updated plan via canvas_skeleton first and pass it
        as `spec`; omit `spec` for cosmetic feedback."""
    )
    unscoped = (
        "" if dataset_scope else " `data_domain`/`dataset` name the dataset "
        "(see list_domains)."
    )

    def _guarded(fn, name: str):
        # The same containment _make_tool gives the consumption tools: an
        # unexpected raise (a DDB throttle, expired creds) must come back as
        # a tool RESULT the model can react to, not abort the whole run.
        # functools.wraps carries __wrapped__, so StructuredTool still infers
        # the real schema from fn's signature.
        @functools.wraps(fn)
        def wrapper(**kwargs: Any) -> Any:
            try:
                return fn(**kwargs)
            except ValueError as e:
                return f"Error: {e}"
            except Exception as e:  # noqa: BLE001 — feedback, not a crash
                log.warning("chat tool %s failed", name, exc_info=True)
                return f"Error: {name} failed: {type(e).__name__}: {e}"

        return wrapper

    return [
        StructuredTool.from_function(
            _guarded(_list, "list_canvases"),
            name="list_canvases", description=list_doc,
        ),
        StructuredTool.from_function(
            _guarded(_describe, "describe_canvas"),
            name="describe_canvas", description=describe_doc,
        ),
        StructuredTool.from_function(
            _guarded(_skeleton, "canvas_skeleton"),
            name="canvas_skeleton", description=skeleton_doc,
        ),
        StructuredTool.from_function(
            _guarded(_create, "create_canvas"),
            name="create_canvas", description=create_doc + unscoped,
        ),
        StructuredTool.from_function(
            _guarded(_status, "get_canvas_status"),
            name="get_canvas_status", description=status_doc,
        ),
        StructuredTool.from_function(
            _guarded(_revise, "revise_canvas"),
            name="revise_canvas", description=revise_doc,
        ),
    ]


def make_submit_annotation_tool(annotations_table, *, user_sub, dataset_scope=None):
    """Build the per-run ``submit_annotation`` tool (agent files on the user's behalf).

    ALWAYS available when a verified subject exists: ``user_sub`` is closed over
    from the validated JWT and keys the annotation partition — identical
    isolation to the UI path; the stored ``submitted_via="agent"`` is display
    provenance, not authorization. Scoped runs drop ``data_domain``/``dataset``
    from the model-facing schema and inject them (same convention as the read
    tools); unscoped runs expose them as required args. Writes DynamoDB
    in-process (the chat role has a scoped PutItem grant).
    """
    from langchain_core.tools import StructuredTool

    from okf_core import annotations as anno
    from okf_core.paths import parse_concept_id

    # cleandoc'd (like the read tools' descriptions) so the model isn't billed for
    # this nested string's 8-space continuation indent on every request.
    _DOC = inspect.cleandoc(
        """File wiki feedback on the user's behalf when the conversation uncovers a
        doc problem (a wrong join, a stale enum, a missing caveat) — or an
        improvement to request: PROPOSING A NEW ATTESTED COMPUTATION is a
        first-class use (the user keeps asking the same parameterizable question,
        or wants a metric made canonical and runnable). ALWAYS confirm
        with the user first (ask_human or a direct question) before filing — this
        writes feedback in their name. `note` is the feedback text (be specific:
        what is wrong and what the data actually shows; for a computation
        proposal, state the question shape, the parameters it should take, and
        the intended logic/SQL — the next annotation harvest authors and
        verifies it as `references/computations/<slug>.md`). `concept_id` targets one
        page (e.g. `tables/races`, or the related metric doc for a
        computation proposal); leave it empty for dataset-level feedback that
        doesn't belong to a single page. The note enters the user's annotation
        queue and steers the next annotation-mode re-harvest."""
    )
    # The unscoped variant takes two MORE args than the scoped one, so it needs the
    # extra clause explaining them. (Assigning `_DOC` to __doc__ below would
    # otherwise discard the only place they were documented.)
    _UNSCOPED_ARGS_DOC = (
        "`data_domain`/`dataset` name the dataset the feedback is about "
        "(see list_domains)."
    )

    def _file(note: str, data_domain: str, dataset: str, concept_id: str) -> str:
        cleaned = (note or "").strip()
        if not cleaned:
            return json.dumps({"error": "note must not be empty"})
        if not (data_domain or "").strip() or not (dataset or "").strip():
            return json.dumps(
                {"error": "data_domain and dataset are required (see list_domains)"}
            )
        cid = (concept_id or "").strip() or anno.DATASET_WIDE_CONCEPT
        try:
            parse_concept_id(cid)
        except ValueError:
            return json.dumps({"error": f"invalid concept_id: {cid!r}"})
        try:
            item = anno.new_annotation_item(
                data_domain=data_domain.strip(),
                dataset=dataset.strip(),
                user_sub=user_sub,
                concept_id=cid,
                note=cleaned,
                submitted_via=anno.SUBMITTED_VIA_AGENT,
            )
            annotations_table.put_item(Item=item)
        except Exception as e:  # noqa: BLE001 - tool errors go back to the model
            log.warning("submit_annotation failed: %s", e, exc_info=True)
            return json.dumps({"error": f"could not file the annotation: {e}"})
        return json.dumps(
            {
                "status": "filed",
                "annotation_id": item["annotation_id"],
                "concept_id": cid,
                "dataset_wide": cid == anno.DATASET_WIDE_CONCEPT,
            }
        )

    if dataset_scope:
        dd = dataset_scope["data_domain"]
        ds = dataset_scope["dataset"]
        description = _DOC

        def submit_annotation(note: str, concept_id: str = "") -> str:
            return _file(note, dd, ds, concept_id)

    else:
        description = f"{_DOC} {_UNSCOPED_ARGS_DOC}"

        def submit_annotation(
            note: str, data_domain: str, dataset: str, concept_id: str = ""
        ) -> str:
            return _file(note, data_domain, dataset, concept_id)

    submit_annotation.__doc__ = description
    return StructuredTool.from_function(
        func=submit_annotation,
        name="submit_annotation",
        description=description,
    )


def make_agent_tools(
    tools: ConsumptionTools, *, dataset_scope: dict[str, str] | None = None
) -> list[StructuredTool]:
    """Build the LangChain tool list for one run.

    ``dataset_scope`` (``{"data_domain", "dataset"}``) pre-binds the location args
    on the tools that accept them; ``None`` (default) lets the agent read the
    whole wiki. Descriptions are lifted from the ``ConsumptionTools`` method
    docstrings (via :func:`_model_description`) so tool semantics stay defined in
    one place — which is why those docstrings are written MODEL-facing, with the
    maintainer notes in body comments instead.
    """
    return [_make_tool(getattr(tools, name), dataset_scope) for name in _TOOL_NAMES]
