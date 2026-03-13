# Model Provider Setup

Umbrella conversation uses an OpenAI-compatible backend through platform config, not browser login state.

## Files

- `control-plane/runtime/model-provider.json`
- `control-plane/runtime/model-provider.secrets.json`

The secrets file is ignored by git.

## TUI setup

From Town Hall:

- `/model` shows current provider status
- `/model setup` writes provider config and API key
- `/model test` sends a small test request
- `/model use <model>` changes the default model
- `/model disable` disables provider-backed conversation

## Config shape

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

## Behavior

- If a configured provider is enabled, `skill.chat.respond` uses it.
- If no provider is configured, conversation falls back to deterministic local replies.
- Conversation metadata records whether fallback was used.

## Why not OAuth

Umbrella runtime calls models as a platform service. That means it should use API-style provider credentials or a local compatible gateway, not a borrowed chat login session.
