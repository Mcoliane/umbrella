## Summary

<!-- 1–3 sentences. What does this change and why? -->

## Scope

- Dispatch path(s) touched: `native` / `umbrella-agent-runtime`
- Services touched:
- Capability contract changes (`control-plane/router/runtime-capabilities.json`): yes / no
- Doc updates:

## Test plan

- [ ] `./tests/contract/run-all-contracts.sh` passes
- [ ] `./scripts/control-plane/verify-patterns --umbrella-root . --require-docs` passes
- [ ] New behavior has a dedicated contract test under `tests/contract/`
- [ ] No new absolute user paths, secrets, or generated artifacts in tracked files

## DCO

- [ ] All commits are `Signed-off-by:` per [CONTRIBUTING.md](../CONTRIBUTING.md)

## Notes for reviewers
