# Umbrella Plugin Host Service

HTTP service for process-separated invocation of enabled catalog actions.

This is the second phase of the plugin/skills runtime:
- resolve dynamic actions through the catalog
- recheck catalog compatibility, trust, and lifecycle state at invocation time
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

- invocation is rejected if the catalog item is disabled, incompatible, untrusted under the catalog's signature mode, or in a failed lifecycle state
- the host uses the plugin install root as `cwd`
- the host provides a per-invocation scratch directory under `control-plane/observability/plugin-host/scratch/`
- inherited environment is denied by default except for a small runtime baseline and manifest `envAllowlist`
- the runtime baseline injects `UMBRELLA_ROOT` (the umbrella tree root) and `UMBRELLA_CATALOG_URL`; skills must resolve repo paths from `UMBRELLA_ROOT` instead of walking parents of their own file so they keep working when installed under `control-plane/extensions/`
- invoke responses carry a `policyWarnings` list naming any declared isolation the host does not enforce for that runtime (see Sandbox Honesty below)
- manifest `executionPolicy.maxInputBytes`, `maxOutputBytes`, and `maxRuntimeSec` are enforced by the host
- `runtime: container` plugins fail closed unless `docker` or `podman` is available
- container plugins run with the install root mounted at `/plugin` and the scratch dir mounted at `/scratch`

## Sandbox Honesty

Read this before trusting a plugin you did not write.

- For `runtime: shell` and `runtime: python`, the manifest fields
  `executionPolicy.fs`, `executionPolicy.network`, and
  `executionPolicy.isolationProfile` are **validated and recorded, not
  enforced**. Values outside the allowlists (`fs`: `scratch-only` |
  `install-root`; `network`: `none` | `http-outbound`) are rejected, but an
  accepted value does not restrict the process: shell and python plugins run
  as ordinary local subprocesses with the host's full filesystem and network
  access, under the same user as the plugin-host service. The real controls
  on this path are the filtered environment, timeout, and input/output size
  caps listed above.
- Only `runtime: container` enforces isolation: the network is always
  disabled (`--network none` is hardcoded, regardless of the manifest
  `network` value), the install root is mounted read-only unless
  `fs: install-root` (then read-write), and `fs: scratch-only` or
  `isolationProfile: container-restricted` adds a read-only root filesystem.
- If a plugin needs to be contained, ship it as `runtime: container`.
  Enforcing (or further downgrading) the declared policy for shell/python
  plugins is tracked in [docs/COMPLETION_PLAN.md](../../docs/COMPLETION_PLAN.md) (WS8).
