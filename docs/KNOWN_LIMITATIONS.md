# Known Limitations

- Local process supervision is script-managed; no built-in launchd/systemd units yet.
- Contract tests may fail in restricted sandbox environments due to local socket bind limits.
- Runtime dependency lock currently pins Python tooling only; service code is standard-library based.
- No hosted update channel yet; upgrades are manual via installer/release artifacts.
- No GUI onboarding flow; CLI-first experience.
- `umbrella-manage bringup` includes `memory-core` in the default mesh, but does not automatically start the node-memory service (`services/memory`).
- There is no automatic promotion/sync from short-term `memory-core` entries to long-term node memory; agents must write node memory explicitly.
- The plugin/skills runtime is implemented, but stronger isolation is still host-dependent: process isolation is enforced directly, while container isolation is optional and requires local `docker` or `podman`.
- Approval for dynamic actions is policy-aware, but first-class human approval UX is still centered on run/step approval flows rather than a dedicated interactive skill-approval surface.
- Shop profiles and runtime session state are persisted under generated observability paths; they are not yet promoted into a more formal operator-managed configuration model.
