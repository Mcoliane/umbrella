# Upgrade Notes

## In-place upgrade

1. Stop services:
   - `umbrella-manage shutdown`
2. Replace app directory with new release contents.
3. Re-run installer:
   - `./install.sh --prefix ~/.local/umbrella0.4`
4. Verify:
   - `umbrella-manage bringup`
   - `umbrella-manage status`
   - `umbrella-manage shutdown`

## Upgrade from release artifact

```bash
tar -xzf umbrella0.4-<version>.tar.gz
cd umbrella0.4-<version>
./install.sh --prefix ~/.local/umbrella0.4
```

## Compatibility notes

- Service auth token path remains manifest-based (`auth.tokenPath`).
- Approval-service remains sole authority for approval block/resume.
- Existing observability run history can be preserved or cleared based on ops policy.
