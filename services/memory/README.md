# Umbrella Memory Service

Umbrella-hosted shared memory service (v1).

Role:
- durable long-term knowledge memory (node/edge/event plane)
- explicit/opt-in usage for knowledge capture, search, and relationship modeling

## Run

```bash
python3 services/memory/app.py --host 127.0.0.1 --port 8787
```

## Auth

Set `UMBRELLA_MEMORY_TOKEN` to require bearer auth.

## Data

Default sqlite DB path:
- `control-plane/observability/memory-service/memory.db`

## API

OpenAPI spec:
- `services/memory/openapi.v1.yaml`

Cross-layer explicit operations:
- `POST /v1/promotions` (promote memory-core datum to durable node memory)
- `POST /v1/promotions/queue` (enqueue promotion)
- `POST /v1/promotions/process-queue` (process queue; failures moved to DLQ)
- `POST /v1/promotions/replay-dlq` (retry DLQ entries)
- `GET /v1/promotions/dlq` (inspect failed promotions)
- `POST /v1/promotions/sweep` (nominate town-conversation knowledge recurring across >=2 towns; side-effect-free — this service never writes long-term nodes from a sweep, session curates nominations through Layer 2 distillation first)
- `POST /v1/promotions/mark-swept` (retire nominated sources after session has processed them)
- `POST /v1/hydrations/payload` (build explicit hydration payload for memory-core writes)
  - requires `context.phase` of `bootstrap` or `resume`
- `GET /v1/memory/boundary/stats` (queue/DLQ/processed observability)
- `GET /v1/memory/boundary/metrics` (Prometheus-style SLO metrics export)
