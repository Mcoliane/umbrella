# Umbrella0.4 Pattern Evidence

Update this file after each full gate run.

## Last Verified Run

- Date (UTC): `2026-03-11T17:04:30Z`
- Contract gate command: `./tests/contract/run-all-contracts.sh`
- Pattern verifier command: `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs`
- Pattern verifier result: `PASS` (all 7 patterns reported `FULL`) at `2026-03-11T15:52:18.161407+00:00`
- Contract log path: `/tmp/umbrella04-contract-gate.out`
- Contract execution note: service-process contract scripts require non-sandbox execution to bind local ports; latest non-sandbox log shows passing through `test-service-manager.sh` plus the new `test-run-transition-guard.sh` PASS.

## Evidence Pointers

- Runtime: `control-plane/observability/runs/run-service-manager-smoke-*`, `run-service-mesh-smoke-*`, `run-auth-mesh-good-*`
- Orchestration: `run-service-mesh-smoke-*`, `run-umbrella04-approval-authority-*`
- Reliability: `run-umbrella04-approval-idempotency-*`, `run-umbrella04-approval-run-status-*`
- Memory:
  - short-term/automatic memory-core path: `run-memory-core-shared-*`
  - long-term/explicit node-memory path: node APIs/tools (`/v1/nodes`, `/v1/edges`, `scripts/tools/memory-*`)
- Drift-Guard: drift policy files + contract policy gates
- Operator Loop: `run-umbrellactl-smoke-*`, approval status transitions
- Capability-Parity: policy multi-agent gates + bootstrap registration path
