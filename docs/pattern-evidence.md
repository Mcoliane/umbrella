# Umbrella0.4 Pattern Evidence

Update this file after each full gate run. If only targeted verification was run, record that explicitly instead of presenting it as a full gate.

## Last Full Gate

- Date (UTC): `2026-03-11T17:04:30Z`
- Contract gate command: `./tests/contract/run-all-contracts.sh`
- Pattern verifier command: `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs`
- Pattern verifier result: `PASS` (all 7 patterns reported `FULL`) at `2026-03-11T15:52:18.161407+00:00`
- Contract log path: `tmp/umbrella04-contract-gate.out`

## Latest Targeted Verification

- Date (UTC): `2026-03-12`
- Scope:
  - `bash ./tests/contract/test-policy-runtime-registry-split.sh`
  - `bash ./tests/contract/test-policy-multi-agent-gates.sh`
  - `python3 -B -` syntax check for `services/policy/app.py`
- Result: `PASS`
- Notes:
  - policy seed/runtime registry split verified
  - recent remediation work also added targeted contracts for identifier validation, failure reporting, memory-core concurrency, memory-store thread safety, and memory import restore behavior
  - a new full contract gate + pattern verifier run should refresh this file again once executed

## Evidence Pointers

- Runtime: `control-plane/observability/runs/run-service-manager-smoke-*`, `run-service-mesh-smoke-*`, `run-auth-mesh-good-*`
- Orchestration: `run-service-mesh-smoke-*`, `run-umbrella04-approval-authority-*`
- Reliability: `run-umbrella04-approval-idempotency-*`, `run-umbrella04-approval-run-status-*`
- Memory:
  - short-term/automatic memory-core path: `run-memory-core-shared-*`
  - long-term/explicit node-memory path: node APIs/tools (`/v1/nodes`, `/v1/edges`, `scripts/tools/memory-*`)
  - explicit cross-layer operations: `test-memory-boundary-promote-hydrate.sh`
  - async promotion queue + DLQ replay + hydration guardrails: `test-memory-boundary-queue-dlq.sh`
  - policy hard-fail hot-path boundary guards: `test-memory-boundary-policy-hotpath.sh`
- Policy-Governance: umbrella policy metadata + runtime agent registry split + contract policy gates
- Operator Loop: `run-umbrellactl-smoke-*`, approval status transitions
- Capability-Parity: policy multi-agent gates + bootstrap registration path
