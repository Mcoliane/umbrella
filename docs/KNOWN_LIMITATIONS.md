# Known Limitations

- Local process supervision is script-managed; no built-in launchd/systemd units yet.
- Contract tests may fail in restricted sandbox environments due to local socket bind limits.
- Runtime dependency lock currently pins Python tooling only; service code is standard-library based.
- No hosted update channel yet; upgrades are manual via installer/release artifacts.
- No GUI onboarding flow; CLI-first experience.
