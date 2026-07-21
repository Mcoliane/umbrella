# Umbrella 0.4

Umbrella is a local control plane for agent runtimes.

It provides:
- orchestration
- policy and approval gates
- lifecycle and terminal-state validation
- hot-path and durable memory layers
- capability-aware action dispatch
- an Umbrella-native agent runtime for shops, sessions, skills, and sub-agents

## Status

Current project state:
- the control plane is real and usable
- the Umbrella-native runtime is real and usable
- action dispatch is capability-based

You can run your own agents under `umbrella-agent-runtime` without rebuilding the governance, routing, and memory plumbing.

## Dispatch Model

Umbrella dispatches actions along two paths, classified by the capability contract:

`native`
- first-party platform and memory-boundary actions
- examples: `memory.promote`, `memory.hydrate`, `memoryWrite`, `memoryRead`, `memoryDelete`, `memoryList`

`umbrella-agent-runtime`
- the Umbrella-native agent runtime
- owns sessions, shops, sub-agents, catalog-managed skills, and plugin-host-backed execution
- owns server-side conversation through `POST /v1/sessions/{id}/converse`
- uses an internal `model-broker` service for model access
- prefers `Z.ai` as the primary live model backend
- model-provider config in `control-plane/runtime/model-provider.json`
- broker routing and connections in `control-plane/runtime/model-broker.json`

The capability contract lives in [`control-plane/router/runtime-capabilities.json`](control-plane/router/runtime-capabilities.json) and is the source of truth for action-family ownership, compatibility aliases, and supported runtimes for **action-based steps** (steps submitted through `/v1/execution/submit-step-spec`).

One path exists outside the capability contract: raw command steps. A plan step that carries a `command` field is sent to `/v1/execution/submit-command`, which runs it directly with `/bin/sh -c` on the host. This is a deliberate trusted-operator escape hatch — see the dispatch flow below for exactly what does and does not gate it.

## What Umbrella Is For

Umbrella is the layer that decides:
- what is allowed to run
- which dispatch path should execute it
- what approvals are required
- how state transitions are validated
- how run results are summarized
- how short-term and long-term memory boundaries are enforced

## Architecture

**See [ARCHITECTURE.md](ARCHITECTURE.md) for the full architecture — topology, dispatch flow, the town model, the code agent, and design decisions, with diagrams.**

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

High-level flow for action-based steps:
1. a run or session action enters Umbrella
2. router resolves dispatch path and capability metadata
3. execution calls `policy` (`/v1/policy/authorize-step`) and rejects the step if policy denies it
4. execution dispatches to either `native` or `umbrella-agent-runtime`
5. orchestrator/session persist results and summaries

Raw command steps are different, and it is important to be honest about this:
- a plan step with a `command` field goes to `/v1/execution/submit-command` and runs via `/bin/sh -c` on the host
- **policy is not consulted on this path** — there is no `authorize-step` call for raw commands
- the only gates are the mesh bearer token on the execution service and any `requiresApproval` flag the plan itself declares on the step
- treat raw command plans as trusted-operator input: anyone who can submit one has shell access as the service user

This posture is tracked in [docs/COMPLETION_PLAN.md](docs/COMPLETION_PLAN.md) (WS9, OQ-2).

## Umbrella Agent Runtime

`umbrella-agent-runtime` is the Umbrella-native runtime path.

It includes:
- catalog-managed skills and plugins
- direct conversational skill routing through `skill.chat.respond`
- plugin-host execution boundary
- town/session runtime
- shop-scoped action governance
- turn orchestration with dependency graphs and retries
- sub-agents and assignments
- compatibility aliases for migrated memory actions

Implemented primarily through:
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

Agent packages are source-controlled defaults for Umbrella-native agents.

They define:
- role and title defaults
- shop defaults
- default enabled actions
- capability-family metadata

Current built-in packages:
- `umbrella.mayor.v1`
- `umbrella.originator.v1`
- `umbrella.programming-agent.v1`

The civic packages include `skill.chat.respond`, so a fresh town can answer directly before any worker shop exists.

They live in [`control-plane/runtime/agent-packages.json`](control-plane/runtime/agent-packages.json). The session service uses them to stamp out tuned agents and shops.

## Memory Model

Umbrella has two memory layers.

`memory-core`
- short-term operational memory
- used for active runs and CLI memory operations

`memory`
- durable node/edge/event knowledge memory
- used for explicit long-term structured knowledge

Boundary actions (owned by `native`):
- `memory.promote`
- `memory.hydrate`

Important operational note:
- `umbrella-manage bringup` starts both memory layers: `memory-core` and the
  durable `memory` service, mesh-token-authenticated, with state under
  `control-plane/observability/`

See [docs/memory-boundary-contract.md](docs/memory-boundary-contract.md) for the boundary rules.

## What Runs Natively vs Where

Owned by `native`:
- `memoryWrite`
- `memoryRead`
- `memoryDelete`
- `memoryList`
- `memory.promote`
- `memory.hydrate`

Owned by `umbrella-agent-runtime`:
- `skill.chat.respond`
- `skill.memory.get`
- `skill.memory.search`
- `skill.memory.link`
- session/shop/sub-agent actions

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

The cleanest path is:
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

A working terminal UI exists at:
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
- the fastest Z.ai path is `/model glm5`, which presets the general endpoint and `glm-5-turbo`

Current controls:
- `Enter` send a message to the current target
- `/` open slash-command mode
- `/model`, `/model setup`, `/model glm5`, `/model test`, `/model use <model>`, `/model disable`
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

The build spec is in [docs/platform-tui.md](docs/platform-tui.md).

## Main Docs

User/operator docs:
- [docs/QUICKSTART.md](docs/QUICKSTART.md)
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)

Architecture docs:
- [ARCHITECTURE.md](ARCHITECTURE.md)
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
- dynamic-action approval UX is less mature than the core run approval path
- stronger isolation is still host-dependent

See [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) for the current list.

## License

Copyright (C) 2026 Emcom Umbrella Contributors

Umbrella is free software: you can redistribute it and/or modify it under the terms of the **GNU Affero General Public License, version 3** (AGPL-3.0) as published by the Free Software Foundation. See [LICENSE](LICENSE) for the full text.

Umbrella is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

If you run a modified version of Umbrella on a server that users interact with over a network, AGPL-3.0 requires you to offer those users access to the corresponding source. See §13 of the license.

"Emcom" and "Umbrella" names and marks are not licensed under AGPL-3.0; the AGPL covers the source code only.
