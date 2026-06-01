---
name: Feature request
about: Propose a new capability or change in behavior
title: "[feature] "
labels: enhancement
assignees: ''
---

## Problem

<!-- What user/operator/agent need is unmet today? -->

## Proposed solution

<!-- Sketch the smallest change that solves the problem. -->

## Runtime ownership

Which runtime should own this?

- [ ] `native` (platform / memory boundary)
- [ ] `umbrella-agent-runtime` (catalog / session / shop / sub-agent)
- [ ] `removed` (compatibility action family)
- [ ] Cross-cutting (control plane / router / policy / approval)

## Alternatives considered

## Impact

- New action families:
- New capabilities to declare in `control-plane/router/runtime-capabilities.json`:
- Doc updates needed in `docs/runtime-matrix.md`:
- Contract test additions:
