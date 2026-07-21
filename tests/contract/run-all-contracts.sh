#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

TESTS=(
  "$ROOT/tests/contract/test-service-mesh-runner.sh"
  "$ROOT/tests/contract/test-approval-authority-runner.sh"
  "$ROOT/tests/contract/test-approval-resume-idempotency.sh"
  "$ROOT/tests/contract/test-approval-run-status.sh"
  "$ROOT/tests/contract/test-run-transition-guard.sh"
  "$ROOT/tests/contract/test-identifier-validation.sh"
  "$ROOT/tests/contract/test-catalog-service.sh"
  "$ROOT/tests/contract/test-plugin-host-execution.sh"
  "$ROOT/tests/contract/test-policy-catalog-gates.sh"
  "$ROOT/tests/contract/test-router-catalog-routing.sh"
  "$ROOT/tests/contract/test-agent-package-runtime.sh"
  "$ROOT/tests/contract/test-session-converse.sh"
  "$ROOT/tests/contract/test-model-broker-service.sh"
  "$ROOT/tests/contract/test-model-provider-config.sh"
  "$ROOT/tests/contract/test-session-converse-provider-configured.sh"
  "$ROOT/tests/contract/test-platform-stack-launcher.sh"
  "$ROOT/tests/contract/test-platform-tui-smoke.sh"
  "$ROOT/tests/contract/test-platform-tui-conversation.sh"
  "$ROOT/tests/contract/test-session-runtime.sh"
  "$ROOT/tests/contract/test-memory-get-skill.sh"
  "$ROOT/tests/contract/test-memory-search-skill.sh"
  "$ROOT/tests/contract/test-memory-link-skill.sh"
  "$ROOT/tests/contract/test-policy-runtime-registry-split.sh"
  "$ROOT/tests/contract/test-failure-reporting.sh"
  "$ROOT/tests/contract/test-memory-core-concurrency.sh"
  "$ROOT/tests/contract/test-memory-store-thread-safety.sh"
  "$ROOT/tests/contract/test-memory-core-shared-e2e.sh"
  "$ROOT/tests/contract/test-execution-native-memory-boundary.sh"
  "$ROOT/tests/contract/test-memory-boundary-promote-hydrate.sh"
  "$ROOT/tests/contract/test-memory-boundary-queue-dlq.sh"
  "$ROOT/tests/contract/test-memory-boundary-policy-hotpath.sh"
  "$ROOT/tests/contract/test-policy-multi-agent-gates.sh"
  "$ROOT/tests/contract/test-service-manager.sh"
  "$ROOT/tests/contract/test-memory-durable-bringup.sh"
  "$ROOT/tests/contract/test-bootstrap-register-agent.sh"
  "$ROOT/tests/contract/test-service-auth-mesh.sh"
  "$ROOT/tests/contract/test-service-auth-gating.sh"
  "$ROOT/tests/contract/test-umbrellactl-smoke.sh"
)

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[contract-gate] start $started_at"

pass_count=0
fail_count=0
results=()

for t in "${TESTS[@]}"; do
  name="${t##*/}"
  echo "[contract-gate] RUN $name"
  if "$t"; then
    echo "[contract-gate] PASS $name"
    results+=("PASS  $name")
    pass_count=$((pass_count + 1))
  else
    echo "[contract-gate] FAIL $name"
    results+=("FAIL  $name")
    fail_count=$((fail_count + 1))
  fi
done

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[contract-gate] done $finished_at"
echo "[contract-gate] summary"
for row in "${results[@]}"; do
  echo "  $row"
done
echo "[contract-gate] totals: pass=$pass_count fail=$fail_count total=$((pass_count + fail_count))"

if [[ "$fail_count" -gt 0 ]]; then
  exit 1
fi
