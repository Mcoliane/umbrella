# Umbrella Execution Service

HTTP service wrapper over action dispatch. It executes native control-plane actions and Umbrella agent runtime actions.

## Run

```bash
python3 services/execution/app.py --host 127.0.0.1 --port 8794
```

Optional:
- `--memory-core-url http://127.0.0.1:8798`
- `--policy-url http://127.0.0.1:8791`
- `--catalog-url http://127.0.0.1:8786`
- `--plugin-host-url http://127.0.0.1:8785`
- `--mesh-token <token>`

Step actions handled natively by execution-service:
- `memoryWrite`
- `memoryRead`
- `memoryDelete`
- `memoryList`
- `memory.promote`
- `memory.hydrate`

When `--catalog-url` and `--plugin-host-url` are configured, enabled catalog actions are dispatched through `umbrella-agent-runtime`. `plugin-host` is the internal executor for that path.

Execution reads the same capability contract as router from `control-plane/router/runtime-capabilities.json`. That lets it:
- reject unsupported action dispatch with `failureReason: runtime_capability_unsupported`
- preserve `supportedRuntimes`, `actionFamily`, and `runtimeCapability` in results
- expose read-only support introspection through `GET /v1/execution/runtime-support`

Execution responses include dispatch metadata:
- `runtimeRequested`
- `runtimeResolved`
- `runtimeClass`
- `runtimeReason`
- `executorRuntime`

Compatibility aliases:
- `memory.get` -> `skill.memory.get`
- `memory.search` -> `skill.memory.search`
- `memory.link` -> `skill.memory.link`

Execution preserves the original requested action id while resolving these aliases into the Umbrella agent runtime path.

Boundary ownership:
- `memory.promote` and `memory.hydrate` are native platform actions owned by execution-service and the durable memory APIs.

## Endpoints

- `GET /v1/execution/health`
- `GET /v1/execution/runtime-support`
- `POST /v1/execution/submit-step-spec`

Failure responses are structured enough to distinguish:
- `failureCategory` such as `policy`, `validation`, `dependency`, or `runtime`
- `failureSource` such as `policy` or `memory-core`
- `failureReason` such as `execution_policy_denied`, `execution_validation_failed`, `dependency_unavailable`, or `runtime_capability_unsupported`
