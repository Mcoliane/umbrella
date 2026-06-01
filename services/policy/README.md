# Umbrella Policy Service

HTTP service for agent capability decisions and preflight checks.

Static policy rules live in [`control-plane/policy/multi-agent-policy.json`](../../control-plane/policy/multi-agent-policy.json). Runtime agent registrations are written separately under `control-plane/observability/policy/agent-registry.json`, so normal service activity does not dirty the repo-tracked seed policy file.

When `--catalog-url` is configured, policy can also resolve dynamic catalog actions, enforce their declared capability requirements, and compute an effective action-policy descriptor for approval, scope, delegation, and sub-agent use.

## Run

```bash
python3 services/policy/app.py --host 127.0.0.1 --port 8791
```

## Endpoints

- `GET /v1/policy/health`
- `POST /v1/policy/preflight/drift-lint`
- `POST /v1/policy/preflight/capability-parity`
- `POST /v1/policy/preflight/all`
- `POST /v1/policy/agents/register`
- `POST /v1/policy/authorize-step`

Effective action policy:
- `actionClass`
- `riskClass`
- `approvalMode`
- `memoryAccess`
- `networkAccess`
- `fsAccess`
- `processAccess`
- `identityScope`
- `delegationAllowed`
- `subAgentAllowed`

Catalog `policyHints` can propose these values. Policy merges those hints with platform defaults and action-specific overrides before returning or enforcing the decision.

Multi-agent policy gates:
- `external_agent_registration`
- `tool_capability_claims`

By default, privileged actions (`memoryWrite`, `memoryDelete`, `memoryList`) require:
- registered agent
- required capability claim
