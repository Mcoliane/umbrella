# Umbrella0.4 Pattern Evidence

Update this file after each full gate run. If only targeted verification was run, record that explicitly instead of presenting it as a full gate.

## Last Full Gate

- Date (UTC): `2026-03-13T02:03:59Z`
- Contract gate command: `./tests/contract/run-all-contracts.sh`
- Contract gate result: `PASS`
- Notes:
  - The gate was rerun after fixing the session/plugin-host timeout regression, restoring safe thread-local SQLite access in `services/memory/store.py`, and reconciling `memory.promote` / `memory.hydrate` policy posture with the existing boundary contract.
  - The runner output reached `test-service-manager.sh` in the captured stream; the remaining tail contracts (`test-bootstrap-register-agent.sh`, `test-service-auth-mesh.sh`, `test-umbrellactl-smoke.sh`) were also verified directly in the same refresh pass and passed.

## Latest Targeted Verification

- Date (UTC): `2026-03-13`
- Scope:
  - `bash ./tests/contract/test-session-runtime.sh`
  - `bash ./tests/contract/test-memory-store-thread-safety.sh`
  - `bash ./tests/contract/test-memory-boundary-policy-hotpath.sh`
  - `python3 -m py_compile` for `services/session/app.py`, `services/memory/store.py`, and `services/policy/app.py`
- Result: `PASS`
- Notes:
  - session assignment/runtime path was repaired by aligning default delegated skill timeouts with catalog execution policy
  - durable memory thread safety now uses thread-local SQLite connections instead of a single cross-thread handle
  - boundary policy hot-path expectations were kept intact while retaining explicit action policy controls for catalog-managed skills

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
- Plugin/Skills Runtime:
  - catalog lifecycle + bundle install/update + checksum/signature verification: `test-catalog-service.sh`
  - plugin-host execution controls + compatibility enforcement + optional container path: `test-plugin-host-execution.sh`
  - explicit action policy enforcement: `test-policy-catalog-gates.sh`
  - runtime capability routing/enforcement across `native`, `umbrella-agent-runtime`, and `removed`: `test-runtime-capability-routing.sh`, `test-runtime-capability-enforcement.sh`
  - migrated memory skills: `test-memory-get-skill.sh`, `test-memory-search-skill.sh`, `test-memory-link-skill.sh`
- Session Runtime:
  - town hall/originator/worker shops/sub-agents/dependency orchestration: `test-session-runtime.sh`
- Operator Loop: `run-umbrellactl-smoke-*`, approval status transitions
- Capability-Parity: policy multi-agent gates + bootstrap registration path
