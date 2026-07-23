# Umbrella Memory Boundary Contract (v0.1)

## 1) Purpose

Umbrella uses two memory layers with distinct ownership:

- Layer A: `memory-core` for operational runtime memory.
- Layer B: `services/memory` for durable knowledge memory.

These layers are complementary and are not interchangeable.

## 2) Hard Boundary

### Layer A: memory-core (operational plane)

Owns:

- run-time coordination state
- agent handoff context
- short/medium-lived shared values (`agent|team|global`)
- execution-time hot read/write path

Must not own:

- graph relationships (nodes/edges)
- authoritative long-term history
- archival knowledge records

### Layer B: services/memory (knowledge plane)

Owns:

- durable nodes/edges/events
- query/search surfaces over durable knowledge
- versioned (`etag`) conflict-aware updates
- audit/history-grade records

Honesty note on search: the shipped `POST /v1/nodes/search` is **token-level
BM25 ranking** over node title and content text (`services/memory/search.py`),
scored in-process over the candidate set (no external index). It can search
several namespaces at once (`namespaces: [...]`) and ranks by term relevance.
There is still no embedding/semantic search and no pagination today; embeddings
are roadmap work — see `docs/COMPLETION_PLAN.md` (WS10).

Must not own:

- hot-path orchestration scratch state
- per-step transient runtime coordination keys

## 3) Source Of Truth

- Ephemeral coordination key-values: `memory-core`
- Durable facts/knowledge graph: `services/memory`
- Cross-agent short-lived context: `memory-core`
- Long-term relationships/timeline/history: `services/memory`

## 4) Allowed Flows

1. Runtime flow (default): orchestrator/execution use `memory-core`.
2. Promotion flow (explicit): `memory-core` datum is promoted to durable node memory.
3. Query flow (knowledge): analysis/tools query `services/memory`.
4. Backfill flow (explicit): node memory provides hydration payload that is intentionally written to `memory-core`.

## 5) Prohibited Flows

- Writing durable facts only to `memory-core` and assuming permanence.
- Using `services/memory` as per-step runtime scratch.
- Hidden bidirectional auto-sync.
- Dual-write with no designated owner.
- Blocking orchestrator hot path on graph/search queries.

## 6) Consistency Contract

- `memory-core`: operational consistency, latest useful runtime value.
- `services/memory`: durable consistency with versioned conflict handling.
- Cross-layer consistency is explicit and asynchronous through promotion/backfill operations.
- Hidden cross-layer writes are disallowed.

## 7) Promotion Rule

Promote from `memory-core` into `services/memory` when any condition is true:

- data is needed beyond the current run/session
- data is required for audit/explanation
- data represents stable fact/policy/relationship/decision
- data is needed for future discovery through node search (BM25-ranked; see Layer B note above)

Otherwise keep it in `memory-core`.

## 8) Ownership Contract (API Use)

- Active execution/orchestration path: `memory-core` APIs.
- Knowledge tooling/analytics: `services/memory` APIs.
- Policy may gate both layers but MUST preserve ownership boundaries.

## 9) Naming Conventions

- `memory-core` keys: run/agent/task oriented (`run:<id>:...`, `agent:<id>:...`)
- `services/memory` node IDs: stable domain IDs (`fact:*`, `policy:*`, `decision:*`, `artifact:*`)
- Identifier schemes are not interchangeable across layers.

## 10) Explicit Operations

- Promotion endpoint: `POST /v1/promotions` in `services/memory`
- Promotion queue/DLQ endpoints:
  - `POST /v1/promotions/queue`
  - `POST /v1/promotions/process-queue`
  - `POST /v1/promotions/replay-dlq`
  - `GET /v1/promotions/dlq`
- Hydration payload endpoint: `POST /v1/hydrations/payload` in `services/memory`
  - requires `context.phase` set to `bootstrap` or `resume`
- Observability endpoints:
  - `GET /v1/memory/boundary/stats`
  - `GET /v1/memory/boundary/metrics` (Prometheus-style SLO export)
- Tooling:
  - `scripts/tools/memory-promote`
  - `scripts/tools/memory-hydrate`

These operations are explicit by design and are the only supported cross-layer pathways.
