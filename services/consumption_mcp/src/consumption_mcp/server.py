"""FastMCP wiring for the consumption MCP server (AgentCore Runtime).

AgentCore hosts MCP servers as **stateless streamable-HTTP** on
``0.0.0.0:8000/mcp`` with Cognito JWT inbound auth enforced by the *runtime*,
not this code (docs/API_REFERENCE.md §2). We therefore only build the FastMCP
app, register thin tool wrappers that delegate to the pure ``tools.py`` logic,
and serve.

The ``mcp`` package may not be installed in the unit-test venv, so — mirroring
``harvest/entrypoint.py`` — the FastMCP import and app construction are guarded:
importing this module never fails, and ``build_clients`` / ``build_tools`` stay
importable for wiring tests. Only ``__main__`` (the container entrypoint)
requires FastMCP to be present.
"""

from __future__ import annotations

import os

from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools


def build_clients(config: ConsumptionConfig):
    """Construct the boto3 clients + registry table for a live deployment.

    Kept separate from the tool logic so the runtime builds real clients from
    the AgentCore execution role while tests inject fakes/moto directly into
    :class:`ConsumptionTools`.
    """
    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region)
    s3vectors = boto3.client("s3vectors", region_name=region)
    bedrock_runtime = boto3.client("bedrock-runtime", region_name=region)
    ddb = boto3.resource("dynamodb", region_name=region).Table(config.registry_table)
    return s3, s3vectors, bedrock_runtime, ddb


def build_tools(config: ConsumptionConfig | None = None) -> ConsumptionTools:
    """Assemble a :class:`ConsumptionTools` from env-resolved clients + config."""
    config = config or ConsumptionConfig.from_env()
    s3, s3vectors, bedrock_runtime, ddb = build_clients(config)
    return ConsumptionTools(
        s3=s3,
        s3vectors=s3vectors,
        bedrock_runtime=bedrock_runtime,
        ddb=ddb,
        config=config,
    )


def register_tools(mcp, tools: ConsumptionTools) -> None:
    """Register thin ``@mcp.tool`` wrappers that delegate to ``tools``.

    The wrappers only adapt names/signatures for the MCP protocol; every bit of
    behaviour lives in the injected :class:`ConsumptionTools` so it stays
    unit-tested without FastMCP.
    """

    @mcp.tool()
    def list_domains() -> list[dict]:
        """List the registered (data_domain, dataset) pairs available to read.

        Each result includes the dataset's parent domain description (if declared).
        """
        return tools.list_domains()

    @mcp.tool()
    def list_declared_domains() -> list[dict]:
        """List all declared data domains with their description and context.

        Use this to discover which domains exist and what they cover before
        drilling into specific datasets. Each domain groups related Glue databases
        under a shared business context.
        """
        return tools.list_declared_domains()

    @mcp.tool()
    def search_domains(query: str, top_k: int = 5) -> list[dict]:
        """Semantic search over declared domains — find which domain best matches
        a natural-language question.

        Returns domain concepts ranked by relevance. Use list_declared_domains for
        a full listing, or this tool when you want fuzzy matching on domain
        descriptions/context.
        """
        return tools.search_domains(query, top_k=top_k)

    @mcp.tool()
    def list_directory(data_domain: str, dataset: str, path: str = "") -> dict:
        """Read the index.md at a bundle subtree level (progressive disclosure).

        Falls back to listing the directory's child concepts if no index exists.
        """
        return tools.list_directory(data_domain, dataset, path)

    @mcp.tool()
    def read_page(
        concept_id: str,
        data_domain: str,
        dataset: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        """Return a concept's markdown from S3 (paginate large docs by lines)."""
        return tools.read_page(concept_id, data_domain, dataset, offset, limit)

    @mcp.tool()
    def get_backlinks(concept_id: str, data_domain: str, dataset: str) -> list[dict]:
        """Return the concepts in the dataset that link to this concept."""
        return tools.get_backlinks(concept_id, data_domain, dataset)

    @mcp.tool()
    def glob(pattern: str, data_domain: str, dataset: str) -> list[dict]:
        """Find concept ids by shell-style path pattern (e.g. `tables/*`, `**/*orders*`).

        `*` matches within one path segment, `**` across directories. Use when you
        know the shape of a name but not where it lives; use list_directory to walk
        level by level.
        """
        return tools.glob(pattern, data_domain, dataset)

    @mcp.tool()
    def grep(
        pattern: str,
        data_domain: str,
        dataset: str,
        ignore_case: bool = True,
        max_results: int = 100,
    ) -> dict:
        """Regex search over concept contents; returns matching lines (concept_id, line_number, line).

        The keyword counterpart to semantic_search: use for exact tokens (a column
        name, enum value, table name), semantic_search for meaning.
        """
        return tools.grep(
            pattern,
            data_domain,
            dataset,
            ignore_case=ignore_case,
            max_results=max_results,
        )

    @mcp.tool()
    def semantic_search(
        query: str,
        data_domain: str | None = None,
        dataset: str | None = None,
        table: str | None = None,
        type: str | None = None,  # noqa: A002 - MCP tool param name
        tags: list[str] | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Semantic search over concepts; returns candidates to then read_page."""
        return tools.semantic_search(
            query,
            data_domain=data_domain,
            dataset=dataset,
            table=table,
            type=type,
            tags=tags,
            top_k=top_k,
        )


def build_app():
    """Build the FastMCP app wired to live clients. Requires ``mcp`` installed.

    Stateless streamable-HTTP is mandatory on AgentCore: each request may hit a
    different runtime replica, so no per-connection server state is retained.
    """
    from mcp.server.fastmcp import FastMCP

    # nosec B104 - binding 0.0.0.0 is REQUIRED inside the AgentCore container: the
    # runtime reaches the server on the container's private IP, and there is no
    # public exposure (network isolation + the JWT authorizer are the boundary).
    mcp = FastMCP(host="0.0.0.0", stateless_http=True)  # nosec B104
    register_tools(mcp, build_tools())
    return mcp


if __name__ == "__main__":  # pragma: no cover - exercised only in the container
    app = build_app()
    # streamable-http transport serves on port 8000 at path /mcp.
    app.run(transport="streamable-http")
