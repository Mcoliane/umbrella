# Umbrella 0.4 Completion Plan

Produced 2026-07-20 from a full-codebase investigation: seven subsystem deep-reads,
a live run of all 38 contract tests (individually, with isolation re-runs), a
repo-wide incompleteness sweep, and an adversarial verification pass that
re-checked every load-bearing claim against the code. Line references are to the
tree at commit 3e27c0c.

## Definition of Done

"Complete" means the project passes its own gates, per the author's docs:

1. `tests/contract/run-all-contracts.sh` runs to completion and is green.
2. `scripts/control-plane/verify-patterns --require-docs` reports FULL against
   fresh evidence in `docs/pattern-evidence.md`.
3. A stranger on a fresh machine can clone, `./install.sh`, `umbrella-manage
   bringup`, complete a governed run, and hold a real model-backed mayor
   conversation — with no hand-stubbed binaries and no silent fallbacks.
4. Every doc claim, schema, and declared contract either matches shipped
   behavior or is explicitly marked aspirational.

## Current State (measured 2026-07-20)

Contract tests: **20/38 pass in batch; 26/38 in isolation; 32/38 with a
`memory-core-reconcile` stub on PATH.** The 18 failures decompose into:

| Root cause | Tests | Class |
|---|---|---|
| `memory-core-reconcile` binary missing (only existed on the author's old machine; fallback removed in d9003cc) | 6 | Blocks **every** orchestrated run on a fresh machine (`policy_blocked`, exit 3) |
| `manage-service-mesh` shutdown rewrites manifest pids to 0; second shutdown runs `kill 0` (process-group self-kill) | 2 | Real code bug |
| Executable bit missing — committed as mode 100644 | 3 | Committed repo defect (reproduces on every clone) |
| Tests never matched committed config (memory skills not enabled for mayor/originator packages; post-d9003cc failure taxonomy) | 4 | Stale tests / pending governance decision |
| Health-window flakes under batch load (pass in isolation) | 3 | Timing |

Subsystem maturity: control-plane core **prototype**; memory stack **solid MVP**
(but the durable service is never launched); agent runtime **prototype**
(sandbox aspirational, trust default-open); model broker + TUI **prototype**
(silent fallback masks dead model path); config/data layer **prototype**;
ops/install **solid MVP** (CI targets a nonexistent `umbrella0.4/` subtree and
has never run); docs **solid MVP** (honest, with localized rot).

---

## Quick Wins (each ≤ ~1 hour)

1. Remove the 4 phantom entries (deleted in d9003cc) from
   `tests/contract/run-all-contracts.sh:17-19,38`; convert the `set -e`
   fail-fast loop to a per-test PASS/FAIL summary with nonzero exit on failure.
2. `chmod +x` + **commit** the three mode-100644 tests:
   `test-model-broker-service.sh`, `test-model-provider-config.sh`,
   `test-session-converse-provider-configured.sh` (rc=126 on every clone).
3. Guard the shutdown kill loop in `scripts/control-plane/manage-service-mesh`
   (~:287-296) against `pid<=0`; short-circuit when the manifest already has
   `stoppedAt`.
4. `chmod 0600` in `services/runtime_model.py:_write_json` (:27-29), the
   launcher token writes, and `scripts/bootstrap/register-agent`; chmod existing
   secret files; **rotate the live Z.ai key** in
   `control-plane/runtime/model-broker.secrets.json` (world-readable since
   creation — treat as compromised).
5. Mirror the `.gitignore` secret/state exclusions into
   `scripts/dist/build-release.sh:65-71` and `install.sh:79-84`; add a
   post-build assertion that fails if any known secret filename appears in the
   tarball.
6. Fix `/model glm5` to hardcode its preset values the way `save_glm47_preset`
   does (`services/tui/app.py:245-274`) — today it silently keeps the stored
   model, contradicting `docs/model-provider-setup.md:30`.
7. Fix `plugin_host` `do_GET`: the 404 branch is dead code after a `return` in
   the health branch (`services/plugin_host/app.py:329-341`); unknown GET paths
   currently get an empty reply.
8. Switch token compares to `hmac.compare_digest` and log a prominent warning
   when a service boots with auth disabled (`services/memory/auth.py`).
9. Fix the TUI `default_urls` map (`services/tui/client.py`): **six of eleven**
   entries point at the wrong service's default port (policy, plugin-host,
   orchestrator, approval, memory-core, memory).
10. Update `docs/platform-tui.md:591-599`: the converse endpoint shipped
    (`services/session/app.py:2549-2551`), Option B is the chosen design, and
    the slash-command list is missing every `/model` command.

> Explicitly **not** a quick win: token-gating session/catalog/plugin-host.
> Verification showed the TUI client sends no Authorization on any
> session/catalog/plugin-host data call, and the policy service has no outbound
> token support for its catalog calls — gating alone breaks conversation and
> `authorize-step`. Bundled into WS4 as a multi-day change.

---

## P0 — Make the MVP work end-to-end (~2 focused weeks)

### WS1 — Unblock the out-of-box run pipeline and quality gate

Goal: a fresh clone completes the README quickstart run with no hand-stubbed
binaries, and the author's own contract gate runs to completion.

- **Implement and ship `memory-core-reconcile`** (days). Stdlib tool matching
  the interface `scripts/control-plane/capability-parity-gate:32-48` expects
  (`--strict` validate / `--fix` repair, JSON `{ok}` last line). Change the
  defaults in `services/policy/app.py:670`, `services/orchestrator/app.py:660`,
  and `scripts/umbrellactl:207` to an **absolute path resolved from
  `--umbrella-root`** — the gate resolves non-absolute commands via
  `shutil.which` only, so a repo-relative default still fails. Update
  `test-service-mesh-runner.sh` to run without its stub (:32, :107).
- **Honor `skipDriftLint`/`skipCapabilityParity` end-to-end** (hours). The
  runner and approval service already send them; the orchestrator handler
  (`services/orchestrator/app.py:645-664`) reads neither and policy
  `preflight_all/drift/parity` (`services/policy/app.py:558-601`) accept no
  skip parameters. Close the dead plumbing.
- **Fix mesh shutdown idempotency and failed-bringup orphan leak** (hours).
  Quick win 3 plus a trap in `bringup_impl` so a failed `wait_health` kills
  already-spawned PIDs instead of leaking them under `set -e`; mirror in
  `manage-platform-stack`.
- **Repair the aggregate contract runner** (hours). Quick wins 1-2; also raise
  the ~6s service health window in the test helpers (three flakes).
- **Reconcile the four stale contract tests** (days; pending OQ-11).
  `test-memory-get/search-skill` assume the mayor/originator packages enable
  the memory skills (only `umbrella.programming-agent.v1` does);
  `test-memory-link-skill` assumes `skill.memory.link` is invocable outside
  town-hall; `test-failure-reporting` predates d9003cc semantics
  (`runtime_capability_unsupported` vs `execution_validation_failed`). Either
  enable the skills in the default packages or rewrite the tests to committed
  governance.
- **Run the full gate green and refresh `docs/pattern-evidence.md`** (hours).
  Last full gate 2026-03-13 predates the broker/converse/dispatch changes.

### WS2 — Bring the durable memory layer live by default

Goal: `bringup` starts both memory layers; `memory.promote`/`memory.hydrate`
work out of the box with mesh auth; queued promotions cannot strand.

- **Launch `services/memory` from both launchers and wire execution to it**
  (days). Nothing listens on execution's default `--memory-url` (:8787) today;
  both native boundary actions fail `dependency_unavailable` on every install.
- **Unify durable-memory auth with the mesh token** (hours). The service reads
  only `UMBRELLA_MEMORY_TOKEN` (`services/memory/config.py:18`), which nothing
  sets; execution already sends the mesh token.
- **Anchor boundary tree and DB path to `--umbrella-root`; heal the split-brain
  trees** (days). `store.py:31` derives the boundary root from `db_path`;
  `config.py:19-20` resolves the default DB against CWD. Consequences already
  visible: 5 promotions stranded in `/memory-boundary` since 2026-03-13, and a
  second divergent tree under `control-plane/observability/memory-boundary`.
  **Also give every memory contract test an explicit `--db-path`** — the suite
  currently mutates the live repo DB and DLQ (verified: the poison-DLQ replay
  counter advanced during the investigation's own test batch).
- **Make promotion processing automatic and DLQ-safe** (days). In-service
  drain, enqueue-time payload validation, max-attempts/parked state for
  `replay_promotion_dlq` (`store.py:559-604` retries poison entries forever),
  tmp+rename writes, don't unlink queue entries when the DLQ write failed.
- **Fix the TUI service map** (hours; part of quick win 9).
- Also: resolve the **scheduler/model-broker default-port collision** (both
  default to 8796: `services/scheduler/app.py:172`,
  `services/model_broker/app.py:538`).

### WS3 — Make the mayor conversation truthful and configurable

Goal: a stranger reaches a real model-backed conversation, is told plainly when
the deterministic fallback answered instead, and `/model` behaves as documented.

- **Surface model provenance in the TUI** (hours). The skill emits
  `providerUsed`/`fallbackUsed` and session persists them; the TUI renders
  neither (`services/tui/app.py:618-649`) — a user with a bad key holds an
  entire fake keyword-stub conversation believing the product works. This is
  the flagship UX defect.
- **Fix the `/model glm5` preset and base-URL truth** (hours; quick win 6 plus
  reconciling `api.z.ai/api/paas/v4` in docs vs `api.z.ai/api/coding/paas/v4`
  in `tui/app.py:14`).
- **Split broker config into a tracked default-disabled template and a
  gitignored runtime file** (days). A fresh clone currently ships
  `control-plane/runtime/model-broker.json` with `enabled=true`, `glm-4.7`, and
  no key; runtime saves dirty the git tree.
- **Propagate provider error bodies through broker errors** (hours).
  `test_connection`/`chat_respond` reduce everything to `HTTP 401`
  (`services/model_broker/app.py:311-320,440-441`); `list_models` swallows all
  exceptions.
- **Route `/model test` through the broker** (days). It currently bypasses the
  broker entirely (`services/runtime_model.py:436-489`), so a passing test
  proves nothing about the path the mayor uses.

### WS4 — Close default-open auth and stop secret leakage

Goal: default bringup leaves no unauthenticated endpoint; secrets are 0600; no
build path can ship credentials.

- **Token-gate session, catalog, and plugin-host — as one bundled change**
  (days, not hours). Prerequisites verified missing: the TUI client sends no
  Authorization on any session/catalog data call; `services/policy`'s outbound
  catalog requests (`app.py:207-235`) have no token support (a gated catalog
  would 500 every `authorize-step`); the launcher never passes the mesh token
  to plugin-host, so plugin-host→catalog breaks too. Land the three spawns, the
  TUI client auth, policy outbound auth, and plugin-host outbound auth
  together; re-run `test-policy-catalog-gates` and `test-platform-tui-*`.
- **Enforce 0600 on all secrets; rotate the exposed key** (hours; quick win 4).
- **Make release builds secret-free** (hours; quick win 5).
- **Make fail-open auth visible; constant-time compares** (hours; quick win 8).
- **Stop passing the mesh token on subprocess argv** (hours).
  `services/approval/app.py:307-308` exposes it in process listings.

---

## P1 — Harden into a robust platform

### WS5 — State integrity, crash safety, honest orchestrator semantics

- **Shared atomic-persistence helper (tmp + `os.replace` + per-store lock)
  adopted by session, orchestrator, catalog, approval, policy** (week+). Only
  memory-core/memory do this correctly today; everything else is unlocked
  read-modify-write JSON under `ThreadingHTTPServer` — first parallel workload
  silently loses writes.
- **Run durability and crash reconciliation** (week+). `run.json` is written at
  init and RUNNING but not per step-transition; a crashed orchestrator strands
  runs invisibly. Flush atomically on every transition; reconcile orphaned
  RUNNING runs at startup; add cancel or strip `step.cancel`/`heartbeat` from
  the declared contract (OQ-3).
- **Make resume a real resume** (days). `start_run` re-initializes every step
  READY regardless of `resumeBlocked` — approved resumes re-execute completed
  side effects, including memory writes.
- **Implement bounded retry or strip the dead retry contract** (days; OQ-3).
  `RETRYING`/`retryPolicy` are declared in config and unreachable in code.
- **Per-step and per-run wall-clock budgets** (days). Command-step timeouts map
  to the `timeout` terminal reason already; the gap is run-level budgets and
  stalled downstream calls.
- **Give the router real authority or remove it from the loop** (days; OQ-4).
  Its decision is computed then discarded (`orchestrator/app.py:502`).
- Taxonomy cleanup: add or remove `command_failed` (emitted by every failed
  `submit-command` run) and `orchestrator_error` — both absent from
  `run-lifecycle.json`.

### WS6 — Resurrect CI and make the quality gates self-honest

- **Rewrite both workflows for the standalone layout** (days). They filter on
  and invoke `umbrella0.4/**` paths that don't exist; neither has ever passed.
  Add an exec-bit lint.
- **Fix `verify-patterns` to check the filesystem** (hours). It greps test
  names from the runner's text (:123-134) — this is how it certified FULL
  coverage over a gate that cannot run.
- **Align the Python/reproducibility claims** (days). Lock says >=3.11,<3.12;
  development artifacts show 3.14; the suite passes on 3.9.6; the installer
  venv is created and never used.
- **Fix or delete the Dockerfile** (days; OQ-7). As shipped: container exits
  after bringup, fossilized ports, no `.dockerignore` (secrets/tmp baked into
  layers).

### WS7 — TUI operator cockpit to its own MVP

Blocked on OQ-5 (curses vs Textual). Approvals screen, runs screen with runtime
metadata, turn-orchestration and worker-creation flows (six dead `TuiClient`
methods are pre-built plumbing), scrollback, non-blocking status refresh, an
installed `umbrella-tui` wrapper.

### WS8 — Plugin trust chain and relocatable skill packaging

- Relocatable skill contract: plugin-host injects `UMBRELLA_ROOT` etc.; shipped
  skills' `parents[3]` root resolution breaks under managed install into
  `control-plane/extensions/`.
- Enforce trust modes: `require-checksum` is dead config; scan-root manifests
  auto-enable (`catalog/app.py:382`); `install-local` records
  `checksumVerified:false`; SIGNATURE.json can point at any file.
- Fix the `--scan-root` argparse bug: `action='append'` with a non-empty
  default means operators can never remove the default auto-scan roots.
- Enforce or honestly downgrade sandbox claims (`fs`/`network`/
  `isolationProfile` are validated strings never enforced for shell/python;
  container path hardcodes `--network none`).
- Remove or implement the phantom `http` runtime (catalog installs it;
  plugin-host raises `unsupported plugin runtime`).
- Correctness debt: do_GET 404 (quick win 7), action-id collision policy,
  chat-respond's 4096-byte output cap truncating model JSON into parse failures.

### WS9 — Documentation and contract truth reconciliation

Most tasks are hours each and need not wait on WS5: rewrite
`platform-tui.md`'s status/decision sections; correct README's router
authority (:39) and "policy authorizes the action" (:69-74; `submit-command`
bypasses policy entirely — OQ-2); honesty notes for deletion-compaction
(`session/README.md:25`) and substring-scan "semantic search"
(`memory-boundary-contract.md:34,80`); publish the complete effective policy
(the committed `multi-agent-policy.json` is a partial subset of code-enforced
defaults); enforce or rewrite the orphaned `step-spec.schema.json` (violated by
both committed plans); refresh `KNOWN_LIMITATIONS.md` as P0 lands; require
evidence refresh in any PR touching `services/`.

### WS13 — Upgrade and data safety *(added by verification pass)*

The documented upgrade path is destructive: `install.sh:79-84` uses
`rsync -a --delete` into `$PREFIX/app` and **all durable state lives inside**
(sessions, shop profiles, catalog registry, `memory.db`, approvals) — only
`observability/runs` is excluded. No schema versioning exists beyond a
hardcoded `001_init.sql`; no backup/restore command. For a product whose pitch
includes a durable knowledge graph, upgrades must preserve state: move state
out of the rsync blast radius (or exclude it), add a migration mechanism and a
backup command, and fix `UNINSTALL_RESET.md`'s wipe footgun.

---

## P2 — Expand toward the documented vision

### WS10 — Memory layer maturation
Edge read/traverse APIs + referential integrity (edges are write-only; deletes
leave dangling edges); SQLite FTS5 with ranking/pagination replacing the
substring scan; retention enforcement + event-log lifecycle; promotion
idempotency (key plumbed everywhere, deduped nowhere); memory-core TTL/eviction
and size limits; durable-service GET robustness (malformed query params crash
requests).

### WS11 — Model provider expansion and broker generalization
Real adapter interface (the zai and openai_compatible provider files are
byte-identical); Anthropic adapter + `/model anthropic` preset; native OpenAI
preset; persona-free inference endpoint with usage accounting (the broker
currently only serves the town persona contract); resolve the dead OAuth
schema fields (OQ-6).

### WS12 — Operational maturity
Consolidate the two duplicated launchers (fixes currently must land twice);
watchdog/launchd/systemd supervision; runtime-state retention and GC (~292MB /
19.5k unpruned files, dominated by plugin-host scratch); restore or retire the
dead hash-chained audit log (old runs have chains, new runs don't — silent
audit regression; OQ-10); real session lifecycle (list/close/delete, genuine
summarizing compaction); telemetry beyond one counter file (re-enable request
logging, per-service counters modeled on the memory boundary's existing
JSON/Prometheus export).

---

## Open Questions (owner decisions)

1. **`memory-core-reconcile` semantics** — what invariants should it
   check/repair, or should the capability-parity preflight be replaced/dropped?
2. **`submit-command` posture** — documented trusted-operator escape hatch, or
   routed through policy/approval so README:69-74 is literally true?
3. **Resilience contract honesty** — implement retry/heartbeat/step.cancel, or
   strip them from the declared contract? (Declared-but-dead is the only wrong
   state.)
4. **Router's role** — obey `route-step`, make it validation-only, or fold into
   execution per the d9003cc two-path direction?
5. **TUI framework** — bless shipped curses (amending platform-tui.md), or port
   to Textual as the spec mandates (ends the stdlib-only property)?
6. **Provider strategy** — is a single generic OpenAI-compatible connection the
   0.4 scope, or do dedicated Anthropic/OpenAI adapters belong in this release?
7. **Distribution targets** — are Docker images and release tarballs real
   deliverables, or delete them for 0.4 in favor of clone + install.sh?
8. **Threat model** — single-user localhost, or same-host multi-user (moves
   per-service credentials, operator identity, and enforced sandboxing up from
   P2)? SECURITY.md currently implies the stronger posture.
9. **Session/shop state location** — promote out of
   `control-plane/observability/` now, or document the wipe footgun for 0.4?
10. **Tamper-evident audit log** — re-wire into the orchestrator, or delete?
11. **Memory-skill governance** — enable `skill.memory.get/search` in the
    mayor/originator packages and widen `skill.memory.link` scope (what the
    four never-green tests assume), or keep the restrictive committed policy
    and rewrite the tests?

## Sequencing

```
Quick wins ──► WS1 ──► WS5 ──► WS9 (final claims), WS12
           └─► WS2 ──► WS10
           └─► WS3 ──► WS7 (with WS1), WS11
           └─► WS4 ──► WS8
WS6 after WS1 (CI over a failing gate is red noise)
WS13 independent; land before any public release
```

P0 ≈ two focused weeks to an honest, stranger-installable MVP. P1 is what makes
"robust" true. P2 is expansion the docs already promise.
