# Chat agent container for Bedrock AgentCore Runtime (HTTP protocol).
# MUST be ARM64 — AgentCore Runtime only runs linux/arm64 images. The target
# platform comes from `docker buildx build --platform linux/arm64` (deploy.sh),
# NOT a hardcoded FROM --platform (which buildx warns against and pins the base
# image to one arch regardless of the build request).
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# okf_core, okf_aws, and consumption_mcp are sibling packages; the build context
# is the repo's services/ dir. consumption_mcp is copied because the chat agent
# reuses its ConsumptionTools IN-PROCESS as the agent's tools (no MCP hop).
COPY okf_core /app/okf_core
COPY okf_aws /app/okf_aws
COPY consumption_mcp /app/consumption_mcp
COPY chat/requirements.txt /app/chat/requirements.txt
RUN pip install --no-cache-dir -e /app/okf_core \
    && pip install --no-cache-dir -e /app/okf_aws \
    && pip install --no-cache-dir -e /app/consumption_mcp \
    && pip install --no-cache-dir -r /app/chat/requirements.txt

COPY chat /app/chat
RUN pip install --no-cache-dir -e /app/chat

# HTTP-protocol AgentCore agents serve /invocations + /ping on 0.0.0.0:8080. The
# container emits AG-UI events as the SSE body of /invocations.
EXPOSE 8080
# Run under ADOT auto-instrumentation (`opentelemetry-instrument`) so LLM, tool,
# and boto3 spans reach CloudWatch. OTEL_* config + the OTLP endpoint come from
# the runtime env (infra/compute/agentcore_runtimes.tf).
CMD ["opentelemetry-instrument", "python", "-m", "chat.server"]
