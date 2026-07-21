# Upgrade Notes

Upgrades are **non-destructive**: `./install.sh` never deletes durable state.
The installer's copy step excludes every state path from deletion, so the
following survive an upgrade or reinstall in place:

- durable knowledge graph (`control-plane/observability/memory-service/memory.db`)
- memory promotion queue/DLQ (`control-plane/observability/memory-boundary/`)
- sessions and shop profiles (`control-plane/observability/sessions/`,
  `control-plane/observability/session-profiles/`)
- catalog registry (`control-plane/observability/catalog/`)
- approvals and resume journal (`control-plane/approvals/`)
- memory-core store (`control-plane/memory-core/store.json`)
- installed extensions (`control-plane/extensions/`)
- run history (`control-plane/observability/runs/`)
- operator-configured provider secrets (`control-plane/runtime/*.secrets.json`)

In addition, when the installer detects an existing install with durable
state, it automatically snapshots that state to
`PREFIX/backups/pre-upgrade-<timestamp>.tar.gz` before copying anything
(skip with `--no-backup`).

## Backup and restore

`scripts/tools/umbrella-backup` snapshots and restores all durable state:

```bash
# Snapshot (uses ~/.umbrella/config.json to find the install):
umbrella-backup create --output ~/umbrella-backup.tar.gz

# Restore (stop services first):
umbrella-manage shutdown
umbrella-backup restore --archive ~/umbrella-backup.tar.gz
umbrella-manage bringup
```

The tarball is written mode 0600 because it can include provider secrets
(`--no-secrets` to exclude them). Run history is excluded by default
(`--include-runs` to add it). The memory database is snapshotted with the
SQLite backup API, so `create` is safe while services are running; `restore`
requires services to be stopped.

## In-place upgrade

1. Stop services:
   - `umbrella-manage shutdown`
2. Re-run the installer from the new release/checkout:
   - `./install.sh --prefix ~/.local/umbrella0.4`
   - This snapshots durable state to `PREFIX/backups/`, copies the new app
     code, and applies any pending schema migrations.
3. Verify:
   - `umbrella-manage bringup`
   - `umbrella-manage status`
   - `umbrella-manage shutdown`

## Upgrade from release artifact

```bash
tar -xzf umbrella0.4-<version>.tar.gz
cd umbrella0.4-<version>
./install.sh --prefix ~/.local/umbrella0.4
```

## Schema migrations

The memory service database is versioned via a `schema_version` table.
Migrations live in `services/memory/db/migrations/` as ordered `NNN_*.sql`
files (`001_init.sql` is the baseline) and are applied by
`scripts/tools/memory-migrate`:

- `./install.sh` runs it automatically on every install/upgrade;
- it is a no-op on an already-current database;
- a pre-versioning database is adopted at version 1 without a reset.

Manual invocation (for example after pulling new code into a dev checkout):

```bash
scripts/tools/memory-migrate --umbrella-root ~/.local/umbrella0.4/app
```

## Interpreter requirement

`runtime/runtime.lock.json` declares the supported Python range (>= 3.9,
stdlib only). `install.sh` enforces `python.minimum` from that file and
aborts with a clear error when `python3` is too old. No virtual environment
is created; the system `python3` is the runtime interpreter.

## Compatibility notes

- Service auth token path remains manifest-based (`auth.tokenPath`).
- Approval-service remains sole authority for approval block/resume.
- Mesh credentials (`bootstrap-secret.txt`, `platform-token.txt`,
  `service-token.txt`) are purged from `PREFIX/app` on upgrade and
  regenerated at bringup; provider secrets are preserved.
- Existing observability run history is preserved; clear it deliberately via
  the reset procedure in `UNINSTALL_RESET.md` if desired.
