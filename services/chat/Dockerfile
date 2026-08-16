# Chat agent container for Bedrock AgentCore Runtime (HTTP protocol).
# MUST be ARM64 — AgentCore Runtime only runs linux/arm64 images. The target
# platform comes from `docker buildx build --platform linux/arm64` (deploy.sh),
# NOT a hardcoded FROM --platform (which buildx warns against and pins the base
# image to one arch regardless of the build request).
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OKF_CHAT_REPORT_HARNESS_PATH=/app/harness/report-harness.html \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

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
    && pip install --no-cache-dir -r /app/chat/requirements.txt \
    # Chromium for create_report's chart rasterization + PDF print
    # (--with-deps pulls the apt libraries; ARM64 builds exist upstream).
    && playwright install --with-deps chromium \
    # Inter for the composed report (okf_core.reports puts it first in the
    # font stack) — without a real UI font the PDF pass falls back to the
    # image's DejaVu Sans, which reads dated next to the app.
    && apt-get update && apt-get install -y --no-install-recommends fonts-inter \
    && rm -rf /var/lib/apt/lists/*

COPY chat /app/chat
# deploy.sh builds ui/dist-report-harness (the flagged Vite entry over
# chartIframe.js) and stages it at services/chat/.harness/ before this build —
# the page create_report's renderer loads to rasterize chart blocks.
COPY chat/.harness /app/harness
RUN pip install --no-cache-dir -e /app/chat

# HTTP-protocol AgentCore agents serve /invocations + /ping on 0.0.0.0:8080. The
# container emits AG-UI events as the SSE body of /invocations.
EXPOSE 8080
# Run under ADOT auto-instrumentation (`opentelemetry-instrument`) so LLM, tool,
# and boto3 spans reach CloudWatch. OTEL_* config + the OTLP endpoint come from
# the runtime env (infra/compute/agentcore_runtimes.tf).
CMD ["opentelemetry-instrument", "python", "-m", "chat.server"]
