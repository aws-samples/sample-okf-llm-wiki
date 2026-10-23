# Canvas CI container for Bedrock AgentCore Runtime (HTTP protocol).
# MUST be ARM64 — the platform comes from `docker buildx build --platform
# linux/arm64` (deploy.sh), same arrangement as the harvest image.
#
# What makes this image different from harvest: headless Chromium (playwright)
# + the static harness page (ui/dist-harness, built by deploy.sh before the
# docker build) — the render_dashboard tool's visual-inspection loop. No
# S3-Files mount, no bundle writes: the runtime reads the wiki from S3 and
# writes only its own DynamoDB rows.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OKF_CANVAS_HARNESS_PATH=/app/harness/harness.html \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

WORKDIR /app

# okf_core + okf_aws + consumption_mcp are sibling packages; the build context
# is the repo's services/ dir (deploy.sh copies ui/dist-harness in beforehand).
COPY okf_core /app/okf_core
COPY okf_aws /app/okf_aws
COPY consumption_mcp /app/consumption_mcp
COPY canvas/requirements.txt /app/canvas/requirements.txt
RUN pip install --no-cache-dir -e /app/okf_core \
    && pip install --no-cache-dir -e /app/okf_aws \
    && pip install --no-cache-dir -e /app/consumption_mcp \
    && pip install --no-cache-dir -r /app/canvas/requirements.txt \
    # Chromium + its system deps for the render harness. ARM64 builds exist
    # upstream; --with-deps pulls the apt libraries the browser needs.
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY canvas /app/canvas
# The pre-built harness bundle (Vite `harness` entry): deploy.sh stages it at
# services/canvas/.harness/ before `docker build`.
COPY canvas/.harness /app/harness
RUN pip install --no-cache-dir -e /app/canvas

EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "-m", "canvas.entrypoint"]
