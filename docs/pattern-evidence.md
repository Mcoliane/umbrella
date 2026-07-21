# Umbrella0.4 Pattern Evidence

Update this file after each full gate run. If only targeted verification was run, record that explicitly instead of presenting it as a full gate.

## Last Full Gate

- Date (UTC): `2026-07-21T06:02:00Z`
- Contract gate command: `./tests/contract/run-all-contracts.sh`
- Contract gate result: `PASS` — `totals: pass=40 fail=0 total=40`
- Pattern gate: `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs` — all 7 patterns `FULL`, `ok: true`
- Notes:
  - First full gate since 2026-03-13. Covers the completion-plan P0 rounds
    (see docs/COMPLETION_PLAN.md): shipped `memory-core-reconcile` unblocking
    the capability-parity preflight, per-test summary runner, durable memory
    service launched by both launchers with mesh-token auth, token-gated
    session/catalog/plugin-host with authenticated outbound calls, promotion
    queue auto-drain with enqueue validation and DLQ parking, disabled-by-default
    broker config template, TUI model-provenance surfacing, and memory skills
    enabled for the default agent packages.
  - Two new contracts added this cycle: `test-memory-durable-bringup.sh` and
    `test-service-auth-gating.sh`. Memory contract tests are now hermetic
    (explicit `--db-path`/`--boundary-root` under `tmp/`), so gate runs no
    longer mutate live repo state.

## Latest Targeted Verification

- Date (UTC): `2026-07-21`
- Scope: full suite (see above) plus isolated re-runs of the governance,
  launcher, policy, and plugin-host contracts after coordinator fixes
  (plugin-host `--mesh-token`, policy outbound catalog auth, runtime-root
  anchored memory state, DLQ raw-payload preservation).
- Result: `PASS`

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
  - migrated memory skills: `test-memory-get-skill.sh`, `test-memory-search-skill.sh`, `test-memory-link-skill.sh`
- Session Runtime:
  - town hall/originator/worker shops/sub-agents/dependency orchestration: `test-session-runtime.sh`
- Operator Loop: `run-umbrellactl-smoke-*`, approval status transitions
- Capability-Parity: policy multi-agent gates + bootstrap registration path
