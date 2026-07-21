# Troubleshooting

## Installer fails with `python3 is required`

- Install Python 3.11+ and re-run `./install.sh`.

## `PermissionError: [Errno 1] Operation not permitted` during tests

- This is typically a sandbox/local-port bind restriction.
- Re-run tests in a normal local shell session.

## `umbrella-manage status` reports `manifest_not_found`

- Bring up first:
  - `umbrella-manage bringup`
- Or pass explicit manifest path:
  - `umbrella-manage status --manifest <path>`

## `401 UNAUTHORIZED` from services

- Ensure runner/CLI uses the mesh token generated during bringup.
- All mesh services are token-gated, including the durable memory service, session, catalog, and plugin-host.
- The token is written next to the service manifest as `service-token.txt` (default installed location: `~/.local/umbrella0.4/app/control-plane/runtime/service-token.txt`).
- For installed usage, prefer `umbrellactl` and `umbrella-runner` wrappers.

## CLI command not found after install

- Run:
  - `source ~/.local/umbrella0.4/env.sh`
- Or open a new shell.

## Service does not shut down cleanly

- Run:
  - `umbrella-manage shutdown`
- If needed, delete stale manifest and restart:
  - `rm -f ~/.local/umbrella0.4/app/control-plane/runtime/service-manifest.json`
  - `umbrella-manage bringup`
- The manifest lives under `<umbrella-root>/control-plane/runtime/` (or under `$UMBRELLA_RUNTIME_ROOT/control-plane/runtime/` if that variable is set); for a source checkout that is `<repo>/control-plane/runtime/service-manifest.json`.
