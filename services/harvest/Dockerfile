# Harvest agent container for Bedrock AgentCore Runtime (HTTP protocol).
# MUST be ARM64 — AgentCore Runtime only runs linux/arm64 images. The target
# platform comes from `docker buildx build --platform linux/arm64` (deploy.sh),
# NOT a hardcoded FROM --platform (which buildx warns against and pins the base
# image to one arch regardless of the build request).
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OKF_MOUNT_PATH=/mnt/data \
    # Vendored okf-authoring SKILL (copied under /app/harvest/skills via
    # `COPY harvest`); deepagents loads it via the /skills/ backend route.
    OKF_SKILLS_DIR=/app/harvest/skills

WORKDIR /app

# okf_core is a sibling package; the build context is the repo's services/ dir.
COPY okf_core /app/okf_core
COPY harvest/requirements.txt /app/harvest/requirements.txt
RUN pip install --no-cache-dir -e /app/okf_core \
    && pip install --no-cache-dir -r /app/harvest/requirements.txt

COPY harvest /app/harvest
RUN pip install --no-cache-dir -e /app/harvest

# HTTP-protocol AgentCore agents serve /invocations + /ping on 0.0.0.0:8080.
EXPOSE 8080
# Run under ADOT auto-instrumentation (`opentelemetry-instrument`) so LLM, tool,
# and deepagents sub-agent spans are emitted to CloudWatch. The OTEL_* config +
# the OTLP endpoint come from the runtime env (infra/compute/agentcore_runtimes.tf).
# NOTE: the crawl runs on a background thread — entrypoint.py copies the OTEL
# context into it so those spans stay parented under the invoke span.
CMD ["opentelemetry-instrument", "python", "-m", "harvest.entrypoint"]
