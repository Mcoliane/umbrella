# Umbrella Plugin Host Service

HTTP service for process-separated invocation of enabled catalog actions.

This is the second phase of the plugin/skills runtime:
- resolve dynamic actions through the catalog
- recheck catalog compatibility, trust, and lifecycle state at invocation time
- execute plugin entrypoints in a separate process boundary
- enforce manifest-driven execution policy for timeout, input size, output size, env allowlist, and scratch directory setup
- normalize results and failures back into the execution contract

## Run

```bash
python3 services/plugin_host/app.py --host 127.0.0.1 --port 8785 --catalog-url http://127.0.0.1:8786
```

## Endpoints

- `GET /v1/plugin-host/health`
- `POST /v1/plugin-host/invoke`
- `POST /v1/plugin-host/cancel`

## Execution Controls

- invocation is rejected if the catalog item is disabled, incompatible, untrusted under the catalog's signature mode, or in a failed lifecycle state
- the host uses the plugin install root as `cwd`
- the host provides a per-invocation scratch directory under `control-plane/observability/plugin-host/scratch/`
- inherited environment is denied by default except for a small runtime baseline and manifest `envAllowlist`
- the runtime baseline injects `UMBRELLA_ROOT` (the umbrella tree root) and `UMBRELLA_CATALOG_URL`; skills must resolve repo paths from `UMBRELLA_ROOT` instead of walking parents of their own file so they keep working when installed under `control-plane/extensions/`
- every invoke response carries a `policyWarnings` entry naming the declared isolation the host does not enforce — which is all of it (see Sandbox Honesty below)
- manifest `executionPolicy.maxInputBytes`, `maxOutputBytes`, and `maxRuntimeSec` are enforced by the host
- plugins are spawned in their own process group so `POST /v1/plugin-host/cancel` can terminate the whole tree, not just the direct child

## Sandbox Honesty

Read this before trusting a plugin you did not write.

- **This service does not sandbox anything.** It gives a plugin its own
  process, not its own container. The manifest fields `executionPolicy.fs`,
  `executionPolicy.network`, and `executionPolicy.isolationProfile` are
  **validated and recorded, not enforced**. Values outside the allowlists
  (`fs`: `scratch-only` | `install-root`; `network`: `none` |
  `http-outbound`) are rejected, but an accepted value does not restrict the
  process: plugins run as ordinary local subprocesses with the host's full
  filesystem and network access, under the same user as the plugin-host
  service.
- The real controls are the filtered environment, the scratch working
  directory, the timeout, and the input/output size caps listed above. Every
  invoke response carries a `policyWarnings` entry saying so, and
  `GET /v1/plugin-host/health` reports `isolation: none`.
- A `runtime: container` path backed by `docker`/`podman` used to exist here
  and was removed. Three reasons, none of them "containers don't work":
  nothing shipped ever used it; its default entrypoint was constructed wrong,
  so any manifest that did not set `container.command` explicitly had never
  run; and it expanded the plugin env into `-e KEY=VALUE` argv entries, which
  are echoed back in the `command` response field and persisted into session
  transcripts — leaking any allowlisted secret to disk. Removing it was
  cheaper than fixing all three for a feature with no users.
- To be clear about what is and is not possible: `docker` and `podman` run on
  both supported platforms, so restoring this path is a small change if
  running untrusted plugins ever becomes a goal. What is not possible is
  *implementing* containment in-tree — namespaces and cgroups are Linux kernel
  features with no standard-library binding and no macOS equivalent, and the
  one portable stdlib primitive (`resource.setrlimit`) caps resource use
  without restricting filesystem or network access. So the choice is an
  external container runtime or nothing; for now the project carries neither.
- **Trust, not confinement, is the boundary here.** A plugin you install is a
  program you have decided to run as yourself. The catalog's signature and
  checksum verification, and the approval gate on the action, are the controls
  that actually matter — treat them accordingly.
