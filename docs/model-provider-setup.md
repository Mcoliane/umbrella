# Model Provider Setup

Umbrella conversation now goes through the internal `model-broker` service.

That means:
- the TUI still exposes one `/model` setup flow
- `skill.chat.respond` no longer talks to provider APIs directly
- the broker owns provider connections and normalized inference
- browser login state is still not reused directly
- the preferred live provider is now `Z.ai`

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
- `/model glm47` applies the recommended Z.ai coding preset
- `/model test` sends a small test request
- `/model use <model>` changes the default model
- `/model disable` disables provider-backed conversation

Fastest path for Z.ai:

1. `/model glm47`
2. paste your Z.ai key
3. `/model test`
4. talk to `mayor`

## Compatibility config shape

```json
{
  "version": "umbrella.model-provider.v1",
  "enabled": true,
  "provider": {
    "type": "zai",
    "baseUrl": "https://api.z.ai/api/coding/paas/v4",
    "defaultModel": "glm-4.7",
    "timeoutSec": 20
  },
  "agentDefaults": {
    "umbrella.mayor.v1": { "model": "glm-4.7" },
    "umbrella.originator.v1": { "model": "glm-4.5-air" }
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
  "providers": {
    "zai": {
      "type": "zai"
    }
  },
  "connections": {
    "default": {
      "providerId": "zai",
      "baseUrl": "https://api.z.ai/api/coding/paas/v4",
      "defaultModel": "glm-4.7",
      "enabled": true
    }
  }
}
```

## Behavior

- If a configured `zai` broker connection is enabled, `skill.chat.respond` uses the broker.
- If no provider is configured, conversation falls back to deterministic local replies.
- Conversation metadata records whether fallback was used.

## Current provider story

- preferred active path: `Z.ai` with API key auth
- secondary compatibility path: `openai-compatible`
- deferred path: OpenAI OAuth reuse through an upstream gateway or separate broker connection model

Umbrella runtime calls models as a platform service. That means it should use API-style provider credentials or a broker-managed gateway, not a borrowed chat login session.
