# Umbrella0.4

Umbrella0.4 is a local multi-service control plane and agent runtime with approval-gated execution, policy enforcement, operator tooling, and a split memory model for hot operational state and durable knowledge.

The plugin/skills runtime is now part of the stack: catalog manages skill/plugin discovery and lifecycle, plugin-host executes dynamic actions behind a controlled boundary, and session provides the town/shop runtime for mayor, originator, worker shops, and sub-agents.

## License

This project is licensed under the `Umbrella Testing License` in [LICENSE](LICENSE). Use is limited to express-approved users for testing Emcom Umbrella only.

## Fast Path

```bash
./install.sh
source ~/.local/umbrella0.4/env.sh
umbrella-manage bringup
umbrella-manage status
```

## User Docs

- Install guide: `docs/INSTALL.md`
- 5-minute quickstart: `docs/QUICKSTART.md`
- Troubleshooting: `docs/TROUBLESHOOTING.md`
- Upgrade notes: `docs/UPGRADE.md`
- Uninstall/reset: `docs/UNINSTALL_RESET.md`
- Known limitations: `docs/KNOWN_LIMITATIONS.md`
- Memory boundary contract: `docs/memory-boundary-contract.md`

## Core Commands

- Service lifecycle:
  - `umbrella-manage bringup`
  - `umbrella-manage status`
  - `umbrella-manage shutdown`
- CLI:
  - `umbrellactl run --plan control-plane/planner/plans/service-mesh-smoke.json --run-id run-<id>`
  - `umbrellactl run-status --approval-key <key>`
  - `umbrellactl memory put --namespace team --key hello --value '{"v":"world"}'`
- Runner:
  - `umbrella-runner --plan control-plane/planner/plans/service-mesh-smoke.json --run-id run-<id>`

## Runtime + Packaging

- Runtime lock: `runtime/runtime.lock.json`
- Pinned tooling deps: `runtime/requirements-tools.txt`
- Build release artifact:
  - `./scripts/dist/build-release.sh`
- Docker runtime image:
  - `docker build -t umbrella0.4:<version> .`

## Architecture

Services:
- policy-service
- lifecycle-service
- router-service
- scheduler-service
- execution-service
- memory-core-service
- memory-service
- orchestrator-service
- approval-service
- catalog-service
- plugin-host-service
- session-service

Approval behavior:
- Orchestrator requests/reads approval state only through approval-service.
- Resume is executed through `POST /v1/approval/resume`.

Session/runtime model:
- mayor agent owns `town-hall`
- originator agent owns `originator-studio`
- worker agents own shops with shop-scoped governed actions
- turns can fan out across shops, use dependency graphs, retries, and reconciliation
- sub-agents are runtime instances of worker shops inside a session

## Quality Gates

- Contract gate: `./tests/contract/run-all-contracts.sh`
- Pattern verifier: `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs`
- CI workflow: `.github/workflows/contract-gate.yml`
