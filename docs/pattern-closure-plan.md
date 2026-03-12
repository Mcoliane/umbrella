# Umbrella0.4 Pattern Closure Plan

This file tracks implementation closure from PARTIAL/FULL-ish to FULL, with concrete evidence hooks.

## Pattern Status Targets

| Pattern | Prior | Target | Verification |
|---|---|---|---|
| Runtime | PARTIAL | FULL | `test-service-mesh-runner.sh`, `test-service-manager.sh`, `test-service-auth-mesh.sh` |
| Orchestration | FULL-ish | FULL | `test-service-mesh-runner.sh`, `test-approval-authority-runner.sh` |
| Reliability | PARTIAL | FULL | `test-approval-resume-idempotency.sh`, `test-approval-run-status.sh`, `test-run-transition-guard.sh` |
| Memory | PARTIAL | FULL | `test-memory-core-shared-e2e.sh`, `test-memory-boundary-promote-hydrate.sh`, `test-memory-boundary-queue-dlq.sh`, `test-memory-boundary-policy-hotpath.sh`, `test-umbrellactl-smoke.sh` |
| Drift-Guard | FULL | FULL | `scripts/control-plane/drift-lint`, `test-policy-multi-agent-gates.sh` |
| Operator Loop | PARTIAL | FULL | `test-umbrellactl-smoke.sh`, `test-approval-run-status.sh` |
| Capability-Parity | FULL | FULL | `scripts/control-plane/capability-parity-gate`, `test-policy-multi-agent-gates.sh`, `test-bootstrap-register-agent.sh` |

## Gate Commands

1. Contract coverage:
   - `./tests/contract/run-all-contracts.sh`
2. Pattern representation check:
   - `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs`

## Definition Of Done

1. Every pattern reports `FULL` in `verify-patterns`.
2. Contract gate passes.
3. `docs/pattern-evidence.md` includes latest run references and timestamps.
