# Umbrella Session Service

HTTP service for first-class town/session state, message history, shop-scoped action governance and invocation, and delegation.

This session model treats the first-contact agent as the mayor of the town:
- the mayor runs the `town hall`
- the `originator` runs the `originator studio` and can create new agents and shops
- worker agents each run their own `shop`
- `sub-agents` are runtime assignment-bearing instances of those worker shops inside a town session
- each shop owns its governed actions/skills, including explicit enable/disable state
- reusable `shop profiles` let the originator stamp out standard shops with stable defaults
- source-controlled `agent packages` let the originator stamp out full runtime-tuned workers with default role, shop, skills, runtime identity, and capability-family metadata
- the mayor can open turns, fan work out by `shopId`, `shopProfileId`, or agent `role`, and reconcile results into a mayor summary
- orchestration plans can declare `dependsOn` and `inputBindings` so later shops can consume earlier shop outputs
- dependency graphs can declare `onDependencyFailure` with `skip`, `fail-fast`, or `continue`
- turn orchestration metadata can set default `onDependencyFailure` and `retryBudget`, with per-step overrides
- sessions, agents, and shops carry heartbeat state so stale or expired towns can be detected explicitly
- the session can compact older conversation history into retained summary records
- invocations and delegations persist runtime selection metadata so the town record shows whether work ran through the Umbrella agent runtime or another supported runtime
- legacy compatibility aliases such as `memory.get`, `memory.search`, and `memory.link` can be invoked from a shop when the corresponding `skill.*` action is enabled; the invocation ledger preserves both the requested and resolved action ids

## Run

```bash
python3 services/session/app.py --host 127.0.0.1 --port 8784 --catalog-url http://127.0.0.1:8786 --execution-url http://127.0.0.1:8794
```

## Endpoints

- `GET /v1/session/health`
- `GET /v1/shop-profiles`
- `GET /v1/shop-profiles/{id}`
- `POST /v1/shop-profiles`
- `GET /v1/agent-packages`
- `GET /v1/agent-packages/{id}`
- `POST /v1/sessions`
- `GET /v1/sessions/{id}`
- `POST /v1/sessions/{id}/heartbeat`
- `POST /v1/sessions/{id}/agents/{agentId}/heartbeat`
- `GET /v1/sessions/{id}/sub-agents`
- `POST /v1/sessions/{id}/sub-agents`
- `POST /v1/sessions/{id}/sub-agents/{subAgentId}/heartbeat`
- `POST /v1/sessions/{id}/assignments`
- `GET /v1/sessions/{id}/assignments/{assignmentId}`
- `POST /v1/sessions/{id}/originations`
- `POST /v1/sessions/{id}/workers`
- `POST /v1/sessions/{id}/shops/{shopId}/actions/enable`
- `POST /v1/sessions/{id}/shops/{shopId}/actions/disable`
- `POST /v1/sessions/{id}/turns`
- `POST /v1/sessions/{id}/delegations`
- `POST /v1/sessions/{id}/orchestrate-turn`
- `POST /v1/sessions/{id}/messages`
- `POST /v1/sessions/{id}/invoke-action`
- `POST /v1/sessions/{id}/compact`

## Runtime State

- Session records:
  - `control-plane/observability/sessions/`
- Shop profiles:
  - `control-plane/observability/session-profiles/`
- Agent packages:
  - `control-plane/runtime/agent-packages.json`

## Liveness

- sessions persist `lastHeartbeatAt`, `heartbeatTtlSec`, `heartbeatStatus`, and `lastSeenBy`
- agents and shops persist `lastHeartbeatAt`, `heartbeatStatus`, and `lastSeenBy`
- sub-agents persist `lastHeartbeatAt`, `heartbeatStatus`, and `lastSeenBy`
- `GET /v1/sessions/{id}` computes `heartbeatStatus` as `healthy`, `stale`, or `expired`
- expired sessions remain readable, but new execution work is rejected until a heartbeat refreshes the town

## Sub-Agents

- a `worker/shop` is the stable business in town
- a `sub-agent` is a runtime instance backed by one of those existing workers/shops
- assignments attach objectives to sub-agents and execute through the existing delegation path
- assignment records persist `resultRefs` for the generated turn, delegation, and invocation

## Agent Packages

- agent packages are source-controlled runtime defaults, not per-session generated state
- they define:
  - preferred runtime
  - role/title defaults
  - default shop identity
  - default enabled skills
  - capability-family metadata for the shop
- the foundational civic packages are:
  - `umbrella.mayor.v1`
  - `umbrella.originator.v1`
- worker packages such as `umbrella.programming-agent.v1` sit underneath them
- package-based originations can omit most worker/shop fields and let the package fill them in
