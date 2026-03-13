# Runtime Matrix

Umbrella is runtime-agnostic at the control-plane layer. It does not require every runtime to expose the same action list. Instead, the platform reasons about:
- runtime identity
- runtime capability families
- action-family ownership
- explicit unsupported-capability behavior

## Runtime Classes

`native`
- first-party Umbrella control-plane and memory-boundary actions
- examples:
  - `memory.promote`
  - `memory.hydrate`
  - `memoryWrite`
  - `memoryRead`
  - `memoryDelete`
  - `memoryList`

`umbrella-agent-runtime`
- Umbrella-native agent runtime for shops, sessions, sub-agents, and catalog-managed skills
- implemented through session + catalog + plugin-host
- examples:
  - `skill.memory.get`
  - `skill.memory.search`
  - `skill.memory.link`
  - session/shop/sub-agent invocations

`removed`
- supported alternate runtime with its own compatibility action families
- examples:
  - `bootstrap.prepare`
  - `bootstrap.compile`
  - `mirror.verify`
  - `validation.canonical_entry_consistency`
  - `dist.fresh_install_sim`
  - `audit.uniqueness_vs_vanilla`

## Required Runtime Contract

Any runtime integrated with Umbrella should support:
- step submission
- normalized results
- timeout handling
- normalized failure reporting
- policy/approval context propagation
- heartbeat/result/cancel semantics where applicable

This is the shared substrate. It is separate from runtime-specific action families.

## Capability Families

`native`
- `control-plane.native-actions`
- `memory.boundary`

`umbrella-agent-runtime`
- `catalog.skill.invoke`
- `session.turn.invoke`
- `shop.action.invoke`
- `delegation.invoke`
- `subagent.assignment.invoke`
- `plugin-host.execution`
- compatibility aliases:
  - `memory.get -> skill.memory.get`
  - `memory.search -> skill.memory.search`
  - `memory.link -> skill.memory.link`

`removed`
- `legacy.adapter.actions`
- `removed.compatibility`

## Ownership Model

Owned by `native`
- platform and boundary actions that should stay first-party

Owned by `umbrella-agent-runtime`
- catalog-managed skills
- town hall / originator / shop actions
- session and sub-agent execution

Owned by `removed`
- retained compatibility families that Umbrella does not require from every runtime

## Unsupported Capability Behavior

If a runtime is explicitly requested for an action it does not support:
- router and execution resolve support through the runtime capability contract
- execution returns `failureReason: runtime_capability_unsupported` when reroute is disabled
- capability-aware reroute can move a request to another supported runtime when configured

This is intentional. Runtime agnosticism in Umbrella is capability-based, not parity-based.

## Current Status

Implemented:
- formal runtime capability contract in `control-plane/router/runtime-capabilities.json`
- capability-aware routing metadata in router
- capability-aware enforcement in execution
- runtime support introspection:
  - `GET /v1/router/runtime-capabilities`
  - `GET /v1/execution/runtime-support`
- per-step runtime metadata persisted through execution/session flows

Not required for `umbrella-agent-runtime` completeness:
- direct parity with Removed compatibility families like `bootstrap.*`, `mirror.*`, `validation.*`, `dist.*`, and `audit.*`

Umbrella Agent Runtime is considered complete enough when it supports:
- catalog-managed skills
- session/shop/sub-agent execution
- delegation and turn orchestration
- plugin-host-backed isolated skill execution
- capability-aware routing and runtime metadata
- compatibility aliases for migrated memory actions
