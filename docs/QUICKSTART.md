# Umbrella0.4 Quickstart (5 Minutes)

## 1) Install

```bash
cd umbrella0.4
./install.sh
source ~/.local/umbrella0.4/env.sh
```

## 2) Start services

```bash
umbrella-manage bringup
umbrella-manage status
```

## 3) Run a smoke plan

```bash
umbrella-runner \
  --plan control-plane/planner/plans/service-mesh-smoke.json \
  --run-id "run-quickstart-$(date +%s)"
```

## 4) Use CLI memory operations

```bash
umbrellactl memory put --namespace team --key hello --value '{"v":"world"}'
umbrellactl memory get --namespace team --key hello
```

## Memory model (short-term vs long-term)

- `memory-core` is the default short-term/working memory used by orchestrated runs and `umbrellactl memory ...`.
- Node memory (`services/memory`, `/v1/nodes`, `/v1/edges`) is intended for longer-term structured knowledge (nodes, links, history).
- Writing to node memory is explicit/opt-in. Agents must call node-memory tools/actions (for example `scripts/tools/memory-put`, `memory.get`, `memory.search`, `memory.link`) when they want to persist long-lived knowledge.

## 5) Shutdown

```bash
umbrella-manage shutdown
```

## What you get out of the box

- Local service mesh lifecycle commands
- Signed agent bootstrap flow (`scripts/bootstrap/register-agent`)
- Approval-gated orchestration and resume support
- Automatic short-term memory via memory-core APIs and CLI helpers
- Explicit long-term node memory APIs/tools for structured knowledge
