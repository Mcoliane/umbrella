# Umbrella Policy Service

HTTP service for policy decisions and preflight checks.

## Run

```bash
python3 services/policy/app.py --host 127.0.0.1 --port 8791
```

## Endpoints

- `GET /v1/policy/health`
- `POST /v1/policy/check-command`
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
