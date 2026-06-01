# Contributing to Umbrella

Thanks for your interest in Umbrella. This project is licensed under [AGPL-3.0](LICENSE) and accepts contributions under the same terms.

## Ground rules

- By submitting a contribution you agree it is licensed under AGPL-3.0.
- Security-sensitive issues: do **not** open a public issue. See [SECURITY.md](SECURITY.md).

## Developer Certificate of Origin (DCO)

Every commit must be signed off:

```
git commit -s -m "your message"
```

This appends a `Signed-off-by: Your Name <you@example.com>` line that certifies you wrote the patch or have the right to submit it under AGPL-3.0. Full text: <https://developercertificate.org/>.

## Development setup

1. Install:
   ```bash
   ./install.sh
   source ~/.local/umbrella0.4/env.sh
   ```
2. Bring up the default mesh:
   ```bash
   umbrella-manage bringup
   umbrella-manage status
   ```
3. Run the contract suite before opening a PR:
   ```bash
   ./tests/contract/run-all-contracts.sh
   ```
4. Verify pattern docs are in sync:
   ```bash
   ./scripts/control-plane/verify-patterns --umbrella-root . --require-docs
   ```

See [docs/INSTALL.md](docs/INSTALL.md) and [docs/QUICKSTART.md](docs/QUICKSTART.md) for more.

## Pull request checklist

- [ ] Commits are signed off (`-s`).
- [ ] New behavior has a contract test under `tests/contract/`.
- [ ] Touched services have their README updated if endpoints/flags changed.
- [ ] No absolute user paths or secrets in tracked files.
- [ ] `run-all-contracts.sh` passes locally.

## Scope guidance

- **`native`** runtime owns platform and memory-boundary actions.
- **`umbrella-agent-runtime`** owns catalog skills, sessions, shops, sub-agents.
- New action families should declare their dispatch-path ownership in [`control-plane/router/runtime-capabilities.json`](control-plane/router/runtime-capabilities.json).

## Reporting bugs / requesting features

Use the GitHub issue templates under `.github/ISSUE_TEMPLATE/`.
