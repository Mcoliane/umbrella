# Umbrella Router Service

HTTP service for runtime routing decisions.

When `--catalog-url` is configured, router will classify matching catalog actions as `umbrella-agent-runtime` before falling back to static prefix rules. `plugin-host` remains an internal executor used by the Umbrella-native runtime path.

Legacy compatibility aliases such as `memory.get`, `memory.search`, and `memory.link` resolve to the corresponding catalog skills while preserving the original requested action id in routing metadata.

Native platform ownership rules can also match exact actions, such as routing `memory.promote` and `memory.hydrate` to the `native` runtime instead of the Removed adapter.

Router now also consults a runtime capability contract in `control-plane/router/runtime-capabilities.json`. That contract is the source of truth for:
- action-family ownership per runtime
- compatibility aliases
- supported runtimes for a resolved action
- capability-aware reroute metadata

This lets Umbrella stay runtime-agnostic without requiring `umbrella-agent-runtime` and `removed` to expose identical action families.

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
