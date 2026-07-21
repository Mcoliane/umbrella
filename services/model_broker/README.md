# Model Broker

The model broker is Umbrella's internal model gateway.

Run it directly:

```bash
python3 services/model_broker/app.py --host 127.0.0.1 --port 8782
```

The default port is `8782`.

Configuration lives in `control-plane/runtime/model-broker.json` (live runtime
state, not tracked by git) with API keys in
`control-plane/runtime/model-broker.secrets.json`. The tracked, documented
template is `control-plane/runtime/model-broker.example.json`; it ships
disabled with no connections. When the runtime file is missing, the broker
initializes from that template with `enabled=false`.

It owns:
- provider connection storage
- connection testing
- normalized chat inference for `skill.chat.respond`

It does not own:
- town/session conversation policy
- transcript persistence
- agent orchestration

Current endpoints:
- `GET /v1/model-broker/health`
- `GET /v1/providers`
- `GET /v1/connections`
- `GET /v1/models`
- `POST /v1/connections`
- `POST /v1/connections/test`
- `POST /v1/chat/respond`

Current provider support:
- `openai-compatible` — any OpenAI-compatible `/chat/completions` endpoint

The broker is intended to be the only service that talks to provider APIs directly.
`skill.chat.respond` now calls the broker and falls back locally only when the broker or a connection is unavailable.
