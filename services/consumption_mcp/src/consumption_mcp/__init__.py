"""okf consumption MCP server — the read side of OKF for consuming agents.

A stateless streamable-HTTP MCP server (AgentCore Runtime, 0.0.0.0:8000/mcp,
Cognito JWT inbound auth enforced by the runtime). Tool logic lives in
``tools.py`` (pure, testable, client-injected); ``server.py`` wraps it in
FastMCP ``@mcp.tool()`` handlers with a guarded import so tests need not have
the ``mcp`` package installed.
"""

from consumption_mcp.tools import ConsumptionConfig, ConsumptionTools

__all__ = ["ConsumptionConfig", "ConsumptionTools"]
