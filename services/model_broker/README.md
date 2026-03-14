# Model Broker

The model broker is Umbrella's internal model gateway.

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
- `openai-compatible`

The broker is intended to be the only service that talks to provider APIs directly.
`skill.chat.respond` now calls the broker and falls back locally only when the broker or a connection is unavailable.
