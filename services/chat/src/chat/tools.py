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
from typing import Any, Callable

from langchain_core.tools import StructuredTool

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
        return method(**kwargs)

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
