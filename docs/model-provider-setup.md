# Model Provider Setup

Umbrella conversation now goes through the internal `model-broker` service.

That means:
- the TUI still exposes one `/model` setup flow
- `skill.chat.respond` no longer talks to provider APIs directly
- the broker owns provider connections and normalized inference
- browser login state is still not reused directly

## Files

- `control-plane/runtime/model-provider.json`
- `control-plane/runtime/model-provider.secrets.json`
- `control-plane/runtime/model-broker.json`
- `control-plane/runtime/model-broker.secrets.json`

The secrets files are ignored by git.
`model-provider.json` is now a compatibility template. Runtime writes go to broker config, not back into that tracked file.

## TUI setup

From Town Hall:

- `/model` shows current provider status
- `/model setup` writes provider config and API key
- `/model test` sends a small test request
- `/model use <model>` changes the default model
- `/model disable` disables provider-backed conversation

## Compatibility config shape

```json
{
  "version": "umbrella.model-provider.v1",
  "enabled": true,
  "provider": {
    "type": "openai-compatible",
    "baseUrl": "https://api.openai.com/v1",
    "defaultModel": "gpt-4.1-mini",
    "timeoutSec": 20
  },
  "agentDefaults": {
    "umbrella.mayor.v1": { "model": "gpt-4.1" },
    "umbrella.originator.v1": { "model": "gpt-4.1-mini" }
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
    "url": "http://127.0.0.1:8796",
    "defaultConnectionId": "default",
    "allowFallback": true
  },
  "connections": {
    "default": {
      "providerId": "openai-compatible",
      "baseUrl": "https://api.openai.com/v1",
      "defaultModel": "gpt-4.1-mini",
      "enabled": true
    }
  }
}
```

## Behavior

- If a configured broker connection is enabled, `skill.chat.respond` uses the broker.
- If no provider is configured, conversation falls back to deterministic local replies.
- Conversation metadata records whether fallback was used.

## Why not OAuth

Umbrella runtime calls models as a platform service. That means it should use API-style provider credentials or a local compatible gateway, not a borrowed chat login session.
