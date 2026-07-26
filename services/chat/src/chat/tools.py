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


def build_consumption_tools(
    *, s3, s3vectors, bedrock_runtime, ddb, config: ConsumptionConfig
) -> ConsumptionTools:
    """Assemble a :class:`ConsumptionTools` from injected clients (live or fake)."""
    return ConsumptionTools(
        s3=s3,
        s3vectors=s3vectors,
        bedrock_runtime=bedrock_runtime,
        ddb=ddb,
        config=config,
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
        description=(method.__doc__ or method.__name__).strip(),
    )


# The read tools the agent gets, in a sensible discovery order.
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
)


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

    _DOC = """File wiki feedback on the user's behalf when the conversation uncovers a
        doc problem (a wrong join, a stale enum, a missing caveat). ALWAYS confirm
        with the user first (ask_human or a direct question) before filing — this
        writes feedback in their name. `note` is the feedback text (be specific:
        what is wrong and what the data actually shows). `concept_id` targets one
        page (e.g. `tables/races`); leave it empty for dataset-level feedback that
        doesn't belong to a single page. The note enters the user's annotation
        queue and steers the next annotation-mode re-harvest."""

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

        def submit_annotation(note: str, concept_id: str = "") -> str:
            return _file(note, dd, ds, concept_id)

    else:

        def submit_annotation(
            note: str, data_domain: str, dataset: str, concept_id: str = ""
        ) -> str:
            """`data_domain`/`dataset` name the dataset the feedback is about."""
            return _file(note, data_domain, dataset, concept_id)

    submit_annotation.__doc__ = _DOC
    return StructuredTool.from_function(
        func=submit_annotation,
        name="submit_annotation",
        description=_DOC,
    )


def make_agent_tools(
    tools: ConsumptionTools, *, dataset_scope: dict[str, str] | None = None
) -> list[StructuredTool]:
    """Build the LangChain tool list for one run.

    ``dataset_scope`` (``{"data_domain", "dataset"}``) pre-binds the location args
    on the tools that accept them; ``None`` (default) lets the agent read the
    whole wiki. Descriptions are lifted from the ``ConsumptionTools`` method
    docstrings so tool semantics stay defined in one place.
    """
    return [_make_tool(getattr(tools, name), dataset_scope) for name in _TOOL_NAMES]
