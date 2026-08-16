# Consumption MCP server container for Bedrock AgentCore Runtime (MCP protocol).
# MUST be ARM64 — AgentCore Runtime only runs linux/arm64 images. The target
# platform comes from `docker buildx build --platform linux/arm64` (deploy.sh),
# NOT a hardcoded FROM --platform (which buildx warns against and pins the base
# image to one arch regardless of the build request).
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# okf_core + okf_aws are sibling packages; the build context is the repo's
# services/ dir.
COPY okf_core /app/okf_core
COPY okf_aws /app/okf_aws
COPY consumption_mcp/requirements.txt /app/consumption_mcp/requirements.txt
RUN pip install --no-cache-dir -e /app/okf_core \
    && pip install --no-cache-dir -e /app/okf_aws \
    && pip install --no-cache-dir -r /app/consumption_mcp/requirements.txt

COPY consumption_mcp /app/consumption_mcp
RUN pip install --no-cache-dir -e /app/consumption_mcp

# Stateless streamable-HTTP MCP servers serve /mcp on 0.0.0.0:8000.
EXPOSE 8000
# Run under ADOT auto-instrumentation so MCP tool + boto3 (S3 Vectors / Bedrock)
# spans are emitted to CloudWatch. OTEL_* config + OTLP endpoint come from the
# runtime env (infra/compute/agentcore_runtimes.tf). This server is synchronous
# and stateless — no thread-context caveat like the harvest agent.
CMD ["opentelemetry-instrument", "python", "-m", "consumption_mcp.server"]
