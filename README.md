# Umbrella0.4

Umbrella0.4 is a local control plane for agent runtimes.

It provides:
- orchestration
- policy and approval gates
- lifecycle and terminal-state validation
- hot-path and durable memory layers
- runtime-aware routing and execution
- an Umbrella-native agent runtime for shops, sessions, skills, and sub-agents

This repo is no longer just a thin Removed wrapper. It now supports multiple runtime classes and can run Umbrella-native agents under Umbrella’s own control plane.

## Status

Current project state:
- the control plane is real and usable
- the Umbrella-native runtime is real and usable
- Removed is still supported as an alternate runtime
- the runtime model is capability-based, not parity-based

That means:
- you can run your own agents under `umbrella-agent-runtime`
- you do not need one-to-one Removed action parity to do that
- some legacy Removed compatibility action families still belong to `removed`, and that is intentional

## Runtime Model

Umbrella currently manages three runtime classes:

`native`
- first-party platform and memory-boundary actions

`umbrella-agent-runtime`
- Umbrella-native agent runtime
- owns sessions, shops, sub-agents, catalog-managed skills, and plugin-host-backed execution
- owns server-side conversation through `POST /v1/sessions/{id}/converse`
- uses an internal `model-broker` service for model access
- prefers `Z.ai` as the primary live model backend
- keeps compatibility model config in `control-plane/runtime/model-provider.json`
- stores broker routing and connections in `control-plane/runtime/model-broker.json`

`removed`
- supported alternate runtime
- retains compatibility action families that Umbrella does not require every runtime to implement

The control plane reasons about:
- runtime identity
- runtime capability families
- action-family ownership
- unsupported-capability behavior

See [docs/runtime-matrix.md](docs/runtime-matrix.md) for the detailed runtime contract.

## What Umbrella Is For

Umbrella is the layer that decides:
- what is allowed to run
- what runtime should execute it
- what approvals are required
- how state transitions are validated
- how run results are summarized
- how short-term and long-term memory boundaries are enforced

It is not tied to one runtime implementation.

## Architecture

Core control-plane services:
- `policy`
- `lifecycle`
- `router`
- `scheduler`
- `execution`
- `orchestrator`
- `approval`
- `memory-core`
- `memory`

Umbrella-native runtime services:
- `catalog`
- `plugin-host`
- `session`

High-level flow:
1. a run or session action enters Umbrella
2. router resolves runtime ownership and capability metadata
3. policy authorizes the action
4. execution dispatches to:
   - `native`
   - `umbrella-agent-runtime`
   - `removed`
5. orchestrator/session persist runtime-aware results and summaries

## Umbrella Agent Runtime

`umbrella-agent-runtime` is the Umbrella-native runtime path.

It currently includes:
- catalog-managed skills and plugins
- direct conversational skill routing through `skill.chat.respond`
- plugin-host execution boundary
- town/session runtime
- shop-scoped action governance
- turn orchestration with dependency graphs and retries
- sub-agents and assignments
- runtime capability aliases for migrated memory actions

It is implemented primarily through:
- [services/catalog/app.py](services/catalog/app.py)
- [services/plugin_host/app.py](services/plugin_host/app.py)
- [services/session/app.py](services/session/app.py)

## Town Model

The Umbrella-native session model is town-shaped:

- the `mayor` is the first-contact agent
- the `mayor` runs `town-hall`
- the `originator` runs `originator-studio`
- worker agents each run a `shop`
- sub-agents are runtime instances of those workers/shops inside a session

Sessions support:
- direct server-side conversation
- shop creation and governance
- delegation
- turn orchestration
- dependency-aware plans
- retry policy
- compaction
- liveness / heartbeat tracking

## Agent Packages

Agent packages are source-controlled runtime defaults for Umbrella-native agents.

They define:
- runtime identity
- role and title defaults
- shop defaults
- default enabled actions
- capability-family metadata

Current built-in packages:
- `umbrella.mayor.v1`
- `umbrella.originator.v1`
- `umbrella.programming-agent.v1`

The civic packages now include `skill.chat.respond`, so a fresh town can answer directly before any worker shop exists.

These live in:
- [control-plane/runtime/agent-packages.json](control-plane/runtime/agent-packages.json)

The session service can use them to stamp out runtime-tuned agents and shops.

## Memory Model

Umbrella has two memory layers.

`memory-core`
- short-term operational memory
- used for active runs and CLI memory operations

`memory`
- durable node/edge/event knowledge memory
- used for explicit long-term structured knowledge

Boundary actions:
- `memory.promote`
- `memory.hydrate`

These are owned by `native`, not by Removed.

Important operational note:
- `umbrella-manage bringup` starts `memory-core`
- it does not automatically start the durable `memory` service

See [docs/memory-boundary-contract.md](docs/memory-boundary-contract.md) for the boundary rules.

## What Runs Natively vs Where

Examples owned by `native`:
- `memoryWrite`
- `memoryRead`
- `memoryDelete`
- `memoryList`
- `memory.promote`
- `memory.hydrate`

Examples owned by `umbrella-agent-runtime`:
- `skill.chat.respond`
- `skill.memory.get`
- `skill.memory.search`
- `skill.memory.link`
- session/shop/sub-agent actions

Examples retained under `removed`:
- `bootstrap.prepare`
- `bootstrap.compile`
- `mirror.verify`
- `validation.canonical_entry_consistency`
- `dist.fresh_install_sim`
- `audit.uniqueness_vs_vanilla`

This is not a bug. It is how runtime agnosticism works in this repo.

## Install

```bash
./install.sh
source ~/.local/umbrella0.4/env.sh
```

More detail:
- [docs/INSTALL.md](docs/INSTALL.md)
- [docs/UPGRADE.md](docs/UPGRADE.md)
- [docs/UNINSTALL_RESET.md](docs/UNINSTALL_RESET.md)

## Fast Start

Start the default mesh:

```bash
umbrella-manage bringup
umbrella-manage status
```

Run a smoke plan:

```bash
umbrella-runner \
  --plan control-plane/planner/plans/service-mesh-smoke.json \
  --run-id "run-smoke-$(date +%s)"
```

Use memory-core through the CLI:

```bash
umbrellactl memory put --namespace team --key hello --value '{"v":"world"}'
umbrellactl memory get --namespace team --key hello
```

Shut down:

```bash
umbrella-manage shutdown
```

## Running Your Own Agents Under Umbrella

Today, the cleanest path is:
1. install or define skills in `skills/`
2. let `catalog` discover them
3. run them through `plugin-host`
4. expose them in shops/session flows through `session`
5. package reusable workers in `control-plane/runtime/agent-packages.json`

For Umbrella-native agents, the important services are:
- `catalog`
- `plugin-host`
- `session`
- `execution`
- `policy`
- `router`

If your goal is “my own agents in my own runtime under Umbrella,” the repo is already there architecturally.

## Key Commands

Service lifecycle:
- `umbrella-manage bringup`
- `umbrella-manage status`
- `umbrella-manage shutdown`

Runner:
- `umbrella-runner --plan control-plane/planner/plans/service-mesh-smoke.json --run-id run-<id>`

CLI:
- `umbrellactl run --plan control-plane/planner/plans/service-mesh-smoke.json --run-id run-<id>`
- `umbrellactl run-status --approval-key <key>`
- `umbrellactl memory put --namespace team --key hello --value '{"v":"world"}'`
- `python3 scripts/umbrella-tui`

Quality:
- `./tests/contract/run-all-contracts.sh`
- `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs`

## Platform TUI

A working terminal UI now exists at:
- `python3 scripts/umbrella-tui`

Current direction:
- `Town Hall` is the default screen
- the TUI is transcript-first, not dashboard-first
- platform lifecycle, session selection, and target switching are exposed as commands and hotkeys around the transcript
- you can talk directly to the mayor or another agent from the main screen
- the TUI is a thin client over the session service `converse` endpoint
- mayor conversations can answer directly or orchestrate worker shops and return a mayor summary
- conversation uses the internal model broker when a real provider connection is configured
- the recommended real-model path is a `zai` broker connection configured through `/model setup`
- the fastest Z.ai path is `/model glm47`, which presets the coding endpoint and `glm-4.7`

Current controls:
- `Enter` send a message to the current target
- `/` open slash-command mode
- `/model`, `/model setup`, `/model glm47`, `/model test`, `/model use <model>`, `/model disable`
- `Tab` cycle the current target
- `s` choose a session
- `n` create a new town
- `S` start the full runtime stack
- `C` start the core stack
- `X` stop the stack

Current command set:
- `/help`
- `/status`
- `/new [title]`
- `/sessions`
- `/session <id>`
- `/agent <id>`
- `/shops`
- `/workers`
- `/refresh`
- `/start [full|core]`
- `/stop`

This is still an early operator console, but it is now shaped around actual conversation with the town instead of a static dashboard. The build spec is in [docs/platform-tui.md](docs/platform-tui.md).

## Main Docs

User/operator docs:
- [docs/QUICKSTART.md](docs/QUICKSTART.md)
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)

Architecture docs:
- [docs/runtime-matrix.md](docs/runtime-matrix.md)
- [docs/platform-tui.md](docs/platform-tui.md)
- [docs/model-provider-setup.md](docs/model-provider-setup.md)
- [services/model_broker/README.md](services/model_broker/README.md)
- [docs/memory-boundary-contract.md](docs/memory-boundary-contract.md)
- [docs/pattern-evidence.md](docs/pattern-evidence.md)

Service docs:
- [services/policy/README.md](services/policy/README.md)
- [services/router/README.md](services/router/README.md)
- [services/execution/README.md](services/execution/README.md)
- [services/orchestrator/README.md](services/orchestrator/README.md)
- [services/session/README.md](services/session/README.md)
- [services/catalog/README.md](services/catalog/README.md)
- [services/plugin_host/README.md](services/plugin_host/README.md)
- [services/memory-core/README.md](services/memory-core/README.md)
- [services/memory/README.md](services/memory/README.md)

## Limitations

The project is in strong local-dev shape, but still has normal platform maturity gaps:
- default service supervision is script-managed, not OS-native
- durable memory is not started by default in `bringup`
- dynamic-action approval UX is less mature than the core run approval path
- stronger isolation is still host-dependent

See [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) for the current list.

## License

This project is licensed under the [Umbrella Testing License](LICENSE). Use is limited to express-approved users for testing Emcom Umbrella only.
