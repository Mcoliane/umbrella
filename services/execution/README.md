# Umbrella Execution Service

HTTP service wrapper over runtime dispatch. It can execute native control-plane actions, Umbrella agent runtime actions, or legacy Removed adapter actions.

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

When `--catalog-url` and `--plugin-host-url` are configured, enabled catalog actions are handled as `umbrella-agent-runtime` actions. `plugin-host` is the internal executor for that runtime path rather than the public runtime identity.

Execution now reads the same runtime capability contract as router from `control-plane/router/runtime-capabilities.json`. That lets it:
- reject unsupported runtime/action combinations with `failureReason: runtime_capability_unsupported`
- preserve `supportedRuntimes`, `actionFamily`, and `runtimeCapability` in results
- expose read-only support introspection through `GET /v1/execution/runtime-support`

Execution responses include runtime selection metadata:
- `runtimeRequested`
- `runtimeResolved`
- `runtimeClass`
- `runtimeReason`
- `executorRuntime`

Legacy compatibility aliases:
- `memory.get` -> `skill.memory.get`
- `memory.search` -> `skill.memory.search`
- `memory.link` -> `skill.memory.link`

Execution preserves the original requested action id while resolving these aliases into the Umbrella agent runtime path.

Boundary ownership:
- `memory.promote` and `memory.hydrate` are native platform actions owned by execution-service and the durable memory APIs
- they no longer rely on the Removed adapter for normal execution

## Endpoints

- `GET /v1/execution/health`
- `GET /v1/execution/runtime-support`
- `POST /v1/execution/submit-step-spec`
- `POST /v1/execution/submit-command`
- `POST /v1/execution/heartbeat`
- `POST /v1/execution/result`
- `POST /v1/execution/cancel`
- `POST /v1/execution/compensate`

Failure responses are structured enough to distinguish:
- `failureCategory` such as `policy`, `validation`, `dependency`, or `runtime`
- `failureSource` such as `policy`, `memory-core`, or `adapter`
- `failureReason` such as `execution_policy_denied`, `execution_validation_failed`, `dependency_unavailable`, or `execution_runtime_failed`
