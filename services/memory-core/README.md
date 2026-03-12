# Umbrella Memory Core Service

Standalone shared memory service for umbrella-hosted agent memory.

Role:
- short-term operational memory (hot runtime plane)
- default memory path for orchestrator/execution and `umbrellactl memory ...`

## Run

```bash
python3 services/memory-core/app.py --host 127.0.0.1 --port 8798
```

## Endpoints

- `GET /v1/memory-core/health`
- `POST /v1/memory-core/put`
- `POST /v1/memory-core/get`
- `POST /v1/memory-core/delete`
- `POST /v1/memory-core/list`

Namespaces:
- `agent`
- `team`
- `global`

Store path:
- `control-plane/memory-core/store.json`
