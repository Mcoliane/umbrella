# Uninstall / Reset

> **WARNING — durable state lives under `PREFIX/app`.** The install prefix
> (default `~/.local/umbrella0.4`) contains your durable knowledge graph
> (`memory.db`), sessions, shop profiles, catalog registry, approvals, and
> provider secrets. Any procedure below marked **DESTRUCTIVE** permanently
> deletes some or all of that state. **Take a backup first**:
>
> ```bash
> umbrella-backup create --output ~/umbrella-backup.tar.gz
> ```
>
> Restore later with
> `umbrella-backup restore --archive ~/umbrella-backup.tar.gz`
> (see `docs/UPGRADE.md` for details).

## Full uninstall (DESTRUCTIVE)

Deletes the app **and all durable state**: knowledge graph, town state
(sessions/shop profiles), catalog registry, approvals, provider secrets,
and any automatic pre-upgrade backups stored under `PREFIX/backups/`.

```bash
umbrella-backup create --output ~/umbrella-backup.tar.gz   # keep a copy
umbrella-manage shutdown || true
rm -rf ~/.local/umbrella0.4
rm -f ~/.umbrella/config.json
```

Remove this line from your shell profile if present:

```bash
source "$HOME/.local/umbrella0.4/env.sh"
```

## Reset runtime state only (safe)

Clears service manifests, mesh tokens, and logs under `PREFIX/runtime`.
Does **not** touch durable data — the knowledge graph, sessions, profiles,
catalog, and approvals are unaffected; tokens are regenerated at bringup.

```bash
umbrella-manage shutdown || true
rm -rf ~/.local/umbrella0.4/runtime
mkdir -p ~/.local/umbrella0.4/runtime
umbrella-manage bringup
```

## Reset observability runs only (deletes run history)

Deletes orchestrated run records. Other durable state is unaffected.

```bash
rm -rf ~/.local/umbrella0.4/app/control-plane/observability/runs/*
```

## Reset durable data (DESTRUCTIVE — wipes the knowledge graph and town state)

Only do this to deliberately start from a blank town. Back up first.

```bash
umbrella-backup create --output ~/umbrella-backup.tar.gz   # keep a copy
umbrella-manage shutdown || true
APP=~/.local/umbrella0.4/app
rm -rf "$APP/control-plane/observability/memory-service" \
       "$APP/control-plane/observability/memory-boundary" \
       "$APP/control-plane/observability/sessions" \
       "$APP/control-plane/observability/session-profiles" \
       "$APP/control-plane/observability/catalog" \
       "$APP/control-plane/memory-core/store.json" \
       "$APP/control-plane/approvals"/*.json \
       "$APP/control-plane/approvals/resume-journal"
umbrella-manage bringup
```
