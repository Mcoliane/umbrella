# Umbrella Plugin Host Service

HTTP service for isolated invocation of enabled catalog actions.

This is the second phase of the plugin/skills runtime:
- resolve dynamic actions through the catalog
- recheck catalog compatibility and lifecycle state at invocation time
- execute plugin entrypoints in a separate process boundary
- optionally execute `runtime: container` plugins through `docker` or `podman`
- enforce manifest-driven execution policy for timeout, input size, output size, env allowlist, and scratch directory setup
- normalize results and failures back into the execution contract

## Run

```bash
python3 services/plugin_host/app.py --host 127.0.0.1 --port 8785 --catalog-url http://127.0.0.1:8786
```

Optional container runtime:

```bash
python3 services/plugin_host/app.py \
  --catalog-url http://127.0.0.1:8786 \
  --container-runtime auto
```

## Endpoints

- `GET /v1/plugin-host/health`
- `POST /v1/plugin-host/invoke`

## Execution Controls

- invocation is rejected if the catalog item is disabled, incompatible, or in a failed lifecycle state
- the host uses the plugin install root as `cwd`
- the host provides a per-invocation scratch directory under `control-plane/observability/plugin-host/scratch/`
- inherited environment is denied by default except for a small runtime baseline and manifest `envAllowlist`
- manifest `executionPolicy.maxInputBytes`, `maxOutputBytes`, and `maxRuntimeSec` are enforced by the host
- `runtime: container` plugins fail closed unless `docker` or `podman` is available
- container plugins run with the install root mounted at `/plugin` and the scratch dir mounted at `/scratch`
