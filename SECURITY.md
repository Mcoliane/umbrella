# Security Policy

## Reporting a Vulnerability

Umbrella is a control plane for agent runtimes — vulnerabilities here can affect policy enforcement, approval gating, and memory boundaries. Please report responsibly.

**Do not open a public GitHub issue for security problems.**

Preferred channels (in order):

1. **GitHub Security Advisory** — open a private advisory at
   <https://github.com/Mcoliane/Emcom_umbrella0.4/security/advisories/new>
2. **Email** — `colianem@gmail.com` with subject line `[umbrella-security]`.

Please include:
- Affected version (see [`VERSION`](VERSION)) and commit hash if known.
- A description of the issue and the impact you believe it has.
- Reproduction steps or a proof-of-concept.
- Any suggested mitigations.

## Scope

In scope:
- Control-plane services under `services/` (policy, router, execution, orchestrator, approval, lifecycle, scheduler, session, catalog, plugin_host, memory, memory-core, model_broker).
- Memory boundary enforcement (`memory.promote` / `memory.hydrate` flows).
- Plugin-host isolation paths.
- Service-mesh auth token handling.
- Bootstrap and install flows (`install.sh`, `umbrella-manage`).

Out of scope:
- Issues only reproducible against modified forks.
- Findings against third-party model providers configured through the broker.
- Denial of service requiring privileged local access.

## Response Expectations

- Acknowledgment within 5 business days.
- Initial assessment within 14 days.
- Fix or mitigation plan communicated before any public disclosure.

We will credit reporters in release notes unless asked not to.

## Supported Versions

Only the latest tagged release on `main` receives security fixes.
