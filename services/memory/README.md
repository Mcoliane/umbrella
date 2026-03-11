# Umbrella Memory Service

Umbrella-hosted shared memory service (v1).

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
