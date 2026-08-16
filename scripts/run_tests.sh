#!/usr/bin/env bash
# Run every Python service's unit tests + the offline E2E test.
# Each service is tested from its own directory (their conftest/import styles
# assume the package dir is the rootdir). Aggregates a pass/fail summary.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

SERVICES=(okf_core okf_aws harvest reindex incremental control_api consumption_mcp chat)
fail=0

for svc in "${SERVICES[@]}"; do
  echo "==================== $svc ===================="
  ( cd "services/$svc" && python -m pytest tests -q ) || fail=1
done

echo "==================== e2e (offline) ===================="
python -m pytest tests -q || fail=1

if [ "$fail" -eq 0 ]; then
  echo "ALL SUITES PASSED"
else
  echo "SOME SUITES FAILED" >&2
fi
exit "$fail"
