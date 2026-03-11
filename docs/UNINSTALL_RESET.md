# Uninstall / Reset

## Full uninstall

```bash
umbrella-manage shutdown || true
rm -rf ~/.local/umbrella0.4
rm -f ~/.umbrella/config.json
```

Remove this line from your shell profile if present:

```bash
source "$HOME/.local/umbrella0.4/env.sh"
```

## Reset runtime state only

```bash
umbrella-manage shutdown || true
rm -rf ~/.local/umbrella0.4/runtime
mkdir -p ~/.local/umbrella0.4/runtime
umbrella-manage bringup
```

## Reset observability runs only

```bash
rm -rf ~/.local/umbrella0.4/app/control-plane/observability/runs/*
```
