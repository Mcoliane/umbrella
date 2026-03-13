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
  "$ROOT/tests/contract/test-runtime-capability-routing.sh"
  "$ROOT/tests/contract/test-runtime-capability-enforcement.sh"
  "$ROOT/tests/contract/test-orchestrator-runtime-summary.sh"
  "$ROOT/tests/contract/test-session-runtime.sh"
  "$ROOT/tests/contract/test-memory-get-skill.sh"
  "$ROOT/tests/contract/test-memory-search-skill.sh"
  "$ROOT/tests/contract/test-memory-link-skill.sh"
  "$ROOT/tests/contract/test-policy-runtime-registry-split.sh"
  "$ROOT/tests/contract/test-failure-reporting.sh"
  "$ROOT/tests/contract/test-memory-core-concurrency.sh"
  "$ROOT/tests/contract/test-memory-store-thread-safety.sh"
  "$ROOT/tests/contract/test-memory-import-restore.sh"
  "$ROOT/tests/contract/test-memory-core-shared-e2e.sh"
  "$ROOT/tests/contract/test-execution-native-memory-boundary.sh"
  "$ROOT/tests/contract/test-memory-boundary-promote-hydrate.sh"
  "$ROOT/tests/contract/test-memory-boundary-queue-dlq.sh"
  "$ROOT/tests/contract/test-memory-boundary-policy-hotpath.sh"
  "$ROOT/tests/contract/test-policy-multi-agent-gates.sh"
  "$ROOT/tests/contract/test-service-manager.sh"
  "$ROOT/tests/contract/test-bootstrap-register-agent.sh"
  "$ROOT/tests/contract/test-service-auth-mesh.sh"
  "$ROOT/tests/contract/test-umbrellactl-smoke.sh"
)

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[contract-gate] start $started_at"

for t in "${TESTS[@]}"; do
  echo "[contract-gate] RUN $t"
  "$t"
  echo "[contract-gate] PASS $t"
done

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[contract-gate] done $finished_at"
