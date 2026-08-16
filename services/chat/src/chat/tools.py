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
