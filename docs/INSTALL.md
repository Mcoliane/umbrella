# Install Guide

## Prerequisites

- macOS or Linux shell environment
- `python3` (3.11.x recommended)
- `rsync`, `tar`, `shasum`

## Standard install

```bash
./install.sh
source ~/.local/umbrella0.4/env.sh
```

## Custom prefix

```bash
./install.sh --prefix /opt/umbrella0.4
source /opt/umbrella0.4/env.sh
```

## Skip health check (CI packaging use)

```bash
./install.sh --skip-health-check
```

## Verify install

```bash
umbrellactl --help
umbrella-manage bringup
umbrella-manage status
umbrella-manage shutdown
```
