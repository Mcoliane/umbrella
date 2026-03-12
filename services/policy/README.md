# Umbrella Policy Service

HTTP service for agent capability decisions and preflight checks.

Static policy rules live in [control-plane/policy/multi-agent-policy.json](/Users/coolfriend/Desktop/Emcom_umbrella0.4/control-plane/policy/multi-agent-policy.json). Runtime agent registrations are written separately under [control-plane/observability/policy/agent-registry.json](/Users/coolfriend/Desktop/Emcom_umbrella0.4/control-plane/observability/policy/agent-registry.json), so normal service activity does not dirty the repo-tracked seed policy file.

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

Multi-agent policy gates:
- `external_agent_registration`
- `tool_capability_claims`

By default, privileged actions (`memoryWrite`, `memoryDelete`, `memoryList`) require:
- registered agent
- required capability claim
