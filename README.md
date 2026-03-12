# Umbrella0.4

Umbrella0.4 is a local multi-service orchestration stack with approval-gated execution, memory-core integration, policy gates, and operator tooling.

## License

This project is licensed under the `Umbrella Testing License` in [LICENSE](/Users/coolfriend/Desktop/Emcom_umbrella0.4/LICENSE). Use is limited to express-approved users for testing Emcom Umbrella only.

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
- orchestrator-service
- approval-service

Approval behavior:
- Orchestrator requests/reads approval state only through approval-service.
- Resume is executed through `POST /v1/approval/resume`.

## Quality Gates

- Contract gate: `./tests/contract/run-all-contracts.sh`
- Pattern verifier: `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs`
- CI workflow: `.github/workflows/contract-gate.yml`
