# Umbrella Router Service

HTTP service for action routing decisions.

When `--catalog-url` is configured, router will classify matching catalog actions as `umbrella-agent-runtime` before falling back to static prefix rules. `plugin-host` remains an internal executor used by the Umbrella-native runtime path.

Compatibility aliases such as `memory.get`, `memory.search`, and `memory.link` resolve to the corresponding catalog skills while preserving the original requested action id in routing metadata.

Native platform ownership rules match exact actions, such as routing `memory.promote` and `memory.hydrate` to the `native` dispatch path.

Router consults a capability contract in `control-plane/router/runtime-capabilities.json`. That contract is the source of truth for:
- action-family ownership per dispatch path
- compatibility aliases
- supported dispatch paths for a resolved action
- capability metadata

## Run

```bash
python3 services/router/app.py --host 127.0.0.1 --port 8795
```

## Endpoints

- `GET /v1/router/health`
- `GET /v1/router/config`
- `GET /v1/router/runtime-capabilities`
- `POST /v1/router/route-step`
- `POST /v1/router/reroute-step`
