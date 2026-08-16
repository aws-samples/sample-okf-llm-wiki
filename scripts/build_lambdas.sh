#!/usr/bin/env bash
# Assemble the Lambda deployment packages Terraform zips (infra/compute/.build/
# packages/<svc>). Each package vendors the service code + the shared libraries
# (okf_core, okf_aws) + pip deps into one directory, so the zip is self-contained.
#
# Lambdas: reindex, incremental (handler + reconcile), control_api.
# Written for bash 3.2 (macOS default) — no associative arrays.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/infra/compute/.build/packages"
PY="${PYTHON:-python3}"

# "<svc>:<src package name>" pairs.
SERVICES="reindex:reindex incremental:incremental control_api:control_api"

rm -rf "$BUILD"
mkdir -p "$BUILD"

for pair in $SERVICES; do
  svc="${pair%%:*}"
  pkg="${pair##*:}"
  dest="$BUILD/$svc"
  mkdir -p "$dest"
  echo ">> packaging $svc -> $dest"

  # 1) the service package
  cp -R "$ROOT/services/$svc/src/$pkg" "$dest/"
  # 2) the shared libraries (pure-python; safe to vendor)
  cp -R "$ROOT/services/okf_core/src/okf_core" "$dest/"
  cp -R "$ROOT/services/okf_aws/src/okf_aws" "$dest/"
  # 3) third-party deps (pyyaml, networkx). boto3 is provided by the Lambda
  #    runtime, so it's excluded to keep the zip small.
  #    --no-warn-conflicts: a `--target` install still inspects the AMBIENT env
  #    and prints its conflicts (e.g. dev-only langchain-aws/checkov pins) as
  #    ERROR lines that don't affect this package and don't fail the install —
  #    suppress them so a real packaging failure isn't buried in false alarms.
  "$PY" -m pip install --quiet --no-warn-conflicts --target "$dest" pyyaml networkx

  # prune caches / dist-info to shrink the zip
  find "$dest" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
  find "$dest" -type d -name '*.dist-info' -exec rm -rf {} + 2>/dev/null || true
done

echo "Lambda packages assembled under $BUILD"
echo "Handlers:"
echo "  reindex     -> reindex.handler.lambda_handler"
echo "  incremental -> incremental.handler.lambda_handler (+ incremental.reconcile.reconcile_handler)"
echo "  control_api -> control_api.app.lambda_handler"
