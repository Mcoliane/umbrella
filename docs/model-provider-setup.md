# Model Provider Setup

Umbrella conversation goes through the internal `model-broker` service.

That means:
- the TUI exposes one `/model` setup flow
- `skill.chat.respond` no longer talks to provider APIs directly
- the broker owns provider connections and normalized inference
- browser login state is not reused directly

Umbrella is provider-agnostic. It works with **any** OpenAI-compatible
`/chat/completions` endpoint. You supply three things:

- `baseUrl` — the API base, e.g. `https://api.example.com/v1`
- `defaultModel` — the model id the endpoint exposes, e.g. `my-model`
- `apiKey` — the API key/token for that endpoint

There are no provider presets and no built-in commercial backend. Point the
broker at whatever OpenAI-compatible endpoint you run or subscribe to.

## Files

- `control-plane/runtime/model-provider.json`
- `control-plane/runtime/model-provider.secrets.json`
- `control-plane/runtime/model-broker.json`
- `control-plane/runtime/model-broker.secrets.json`
- `control-plane/runtime/model-broker.example.json`

The secrets files and `model-broker.json` are ignored by git: `model-broker.json`
is live runtime state, created on first save. The tracked, documented template is
`model-broker.example.json` — it ships disabled with no connections, and the
runtime initializes from it (with `enabled=false`) when `model-broker.json` is
missing.
`model-provider.json` is a compatibility template. Runtime writes go to broker
config, not back into that tracked file.

## TUI setup

From Town Hall:

- `/model` shows current provider status
- `/model setup` prompts for `baseUrl`, `defaultModel`, and the API key, then
  writes the provider config and key
- `/model test` sends a small test request through the model broker
- `/model use <model>` changes the default model
- `/model disable` disables provider-backed conversation

Fastest path:

1. `/model setup`
2. enter your endpoint `baseUrl`, a `defaultModel`, and paste your API key
3. `/model test`
4. talk to `mayor`

## Configuring via the broker API

Instead of the TUI you can configure a connection directly on the model-broker
service (default `http://127.0.0.1:8782`):

```bash
curl -X POST http://127.0.0.1:8782/v1/connections \
  -H 'Content-Type: application/json' \
  -d '{
        "connectionId": "default",
        "providerId": "openai-compatible",
        "baseUrl": "https://api.example.com/v1",
        "defaultModel": "my-model",
        "apiKey": "sk-...",
        "enabled": true
      }'
```

Test it before relying on it:

```bash
curl -X POST http://127.0.0.1:8782/v1/connections/test \
  -H 'Content-Type: application/json' \
  -d '{ "connectionId": "default" }'
```

## Compatibility config shape

```json
{
  "version": "umbrella.model-provider.v1",
  "enabled": true,
  "provider": {
    "id": "default",
    "type": "openai-compatible",
    "baseUrl": "https://api.example.com/v1",
    "defaultModel": "my-model",
    "timeoutSec": 120
  },
  "agentDefaults": {
    "umbrella.mayor.v1": { "model": "my-model" },
    "umbrella.originator.v1": { "model": "my-model" }
  }
}
```

Secrets:

```json
{
  "apiKey": "sk-..."
}
```

## Broker config shape

```json
{
  "version": "umbrella.model-broker.v1",
  "enabled": true,
  "broker": {
    "url": "http://127.0.0.1:8782",
    "defaultConnectionId": "default",
    "allowFallback": true
  },
  "providers": {
    "openai-compatible": {
      "id": "openai-compatible",
      "type": "openai-compatible",
      "supportsApiKey": true,
      "supportsOAuth": false
    }
  },
  "connections": {
    "default": {
      "providerId": "openai-compatible",
      "baseUrl": "https://api.example.com/v1",
      "defaultModel": "my-model",
      "enabled": true
    }
  }
}
```

## Behavior

- If an enabled connection is configured, `skill.chat.respond` uses the broker.
- If no provider is configured, conversation falls back to deterministic local
  replies.
- Conversation metadata records whether fallback was used.

## Provider story

- active path: any OpenAI-compatible `/chat/completions` endpoint, with API key
  auth, configured through `/model setup` or `POST /v1/connections`
- deferred path: OAuth reuse through an upstream gateway or a separate broker
  connection model

Umbrella runtime calls models as a platform service. That means it should use
API-style provider credentials or a broker-managed gateway, not a borrowed chat
login session.
