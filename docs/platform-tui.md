# Platform TUI Design

## Purpose

Umbrella needs a dedicated terminal UI for operators and for direct interaction with the town model.

The current platform is powerful but fragmented:
- `umbrella-manage` is for service lifecycle
- `umbrellactl` is for plans and memory-core operations
- the Umbrella-native runtime is mostly accessed through raw HTTP endpoints

That is enough for contracts and engineering work, but it is not the right front door for:
- conversing with the `mayor`
- inspecting a live town session
- watching runtime selection and delegation happen in real time
- managing shops, workers, sub-agents, and turns without hand-writing JSON

The TUI should be the operator console and the first usable front end for the Umbrella-native runtime.

## Current Direction

The first dashboard-heavy TUI slice was the wrong shape.

The platform TUI is now being rebuilt around the actual Removed interaction model:
- one primary transcript
- one primary composer
- thin status/footer controls
- slash commands for operator actions
- session and target selection as lightweight prompts, not the main screen

That means `Town Hall` is the default surface, not `Home`.

The current implementation now talks to the session service through a real server-side conversation endpoint:
- `POST /v1/sessions/{id}/converse`
- `GET /v1/runtime/model-provider`
- `POST /v1/runtime/model-provider`
- `POST /v1/runtime/model-provider/test`
- `GET /v1/model-broker/health`
- the TUI no longer owns mayor/originator conversation policy itself
- the mayor can reply directly in a fresh town and can still delegate to workers when they exist

## Product Position

This TUI is not just a thin wrapper around existing CLI commands.

It should act as:
- the operator cockpit for the whole platform
- the primary interaction surface for the Umbrella-native runtime
- the first point of contact for the `mayor`

It should not try to replace:
- low-level scripts used in contracts
- the OpenAPI surfaces
- `umbrellactl` for simple scripted automation

Instead, it should sit above them and make the platform legible.

## Design Goals

1. Make the `mayor` conversationally reachable.
2. Show the town model directly:
   - mayor
   - originator
   - workers
   - shops
   - sub-agents
   - turns
   - delegations
3. Expose runtime identity clearly:
   - `native`
   - `umbrella-agent-runtime`
   - `removed`
4. Make approvals, failures, and policy denials visible without digging through JSON files.
5. Preserve keyboard-first operator flow.
6. Require no browser.
7. Stay stdlib-friendly where possible, but prioritize a good terminal UX over ideological purity.

## Recommended Implementation Choice

Use `textual`.

Why:
- the platform now has enough state density that plain `curses` will become painful
- we need panes, lists, logs, forms, and streaming refresh
- the runtime model benefits from composable views and keyboard bindings
- Textual is good at building a serious terminal application instead of a one-shot menu

Do not build the first version in raw `curses`.

If dependency avoidance becomes critical later, a simpler fallback CLI can exist beside the TUI. The main operator interface should still be `textual`.

## Entry Point

Add a new top-level script:
- `scripts/umbrella-tui`

Also add an installable wrapper if desired:
- `umbrella-tui`

The TUI should start in one of two modes:

1. `platform mode`
- service overview
- runtime overview
- active sessions and runs

2. `town mode`
- focused interaction with one town session
- direct conversation with the mayor

Suggested launch examples:

```bash
python3 scripts/umbrella-tui
```

```bash
python3 scripts/umbrella-tui --session-id session-123
```

## Core Information Architecture

The TUI should be chat-first, with secondary operator surfaces around it.

### 1. Town Hall

Purpose:
- converse with the mayor and inspect the current town

This is the primary screen.

Layout:
- center pane: conversation transcript
- bottom input/composer interaction
- right pane: town state, agents, shops, and service/runtime summary

Right pane should show:
- mayor package and shop
- originator package and shop
- workers and shops
- sub-agents
- available actions in selected shop
- heartbeat and liveness state

Primary interaction:
- `Enter` to send a message to the current target
- `/` to open slash-command mode
- `Tab` to cycle targets
- `/model` to inspect broker-backed model status
- `/model setup` to configure the default broker connection
- `/model glm5` to apply the recommended Z.ai `glm-5-turbo` general-chat preset
- `/model test` to test the configured backend
- the preferred live provider path is `zai`

Actions:
- send message
- start turn
- ask originator to create a worker
- hand off to selected shop
- compact conversation

Important note:
- the TUI should stay thin here
- conversation routing now belongs in the session service, which decides direct reply vs orchestration

### 2. Turns

Purpose:
- make delegation and orchestration understandable

Show:
- turn list
- selected turn objective
- plan steps
- dependencies
- retry policy
- reconciliation summary

For each delegation:
- requested target
- resolved shop
- runtime selected
- action id / resolved action id
- state
- result summary

This is where the operator sees the town actually working.

### 3. Shops

Purpose:
- manage the town’s businesses

Show:
- all shops in session
- owner agent
- package/profile
- governed enabled actions
- capability families
- heartbeat status

Actions:
- create worker from package
- create worker from profile
- enable action
- disable action
- inspect shop invocation history

This view should let the operator understand each shop as an operational environment, not just as a label.

### 4. Runs

Purpose:
- expose the older control-plane run model

Show:
- recent orchestrator runs
- state
- failed step id
- runtime breakdown
- approval keys
- failure category/source/reason

Actions:
- open run summary
- open run step details
- jump to approval if blocked

### 5. Services

Purpose:
- operator diagnostics

Show:
- policy
- lifecycle
- router
- scheduler
- memory-core
- memory
- execution
- approval
- orchestrator
- catalog
- plugin-host
- session

For each:
- health
- URL
- runtime role
- last known error if any

Actions:
- refresh health
- tail relevant log file path
- copy endpoint URL

## Slash Commands

The chat surface should expose operator actions through commands instead of pushing users into dashboard navigation first.

Current command set:
- `/help`
- `/status`
- `/new [title]`
- `/sessions`
- `/session <id>`
- `/agent <id>`
- `/shops`
- `/workers`
- `/refresh`
- `/start [full|core]`
- `/stop`

## Dedicated Mayor Conversation Model

The TUI should introduce a deliberate “converse with the mayor” workflow.

The user should not have to manually think in raw API terms like:
- `POST /v1/sessions/{id}/messages`
- `POST /v1/sessions/{id}/turns`
- `POST /v1/sessions/{id}/orchestrate-turn`

Instead, the TUI should handle this as:

1. user types a message to the mayor
2. TUI appends the user message to the session
3. TUI creates a turn when the message implies work
4. TUI either:
   - routes directly to `town-hall`, or
   - has the mayor fan out to workers
5. TUI renders the mayor reconciliation as the mayor’s reply

Without this, the mayor remains infrastructure instead of a directly usable agent.

## Runtime Awareness In The UI

The TUI must make runtime selection explicit everywhere.

Every invocation, delegation, and result row should surface:
- requested runtime
- resolved runtime
- executor runtime
- runtime reason
- action family
- runtime capability

The operator should always be able to answer:
- did this run through `umbrella-agent-runtime`?
- did this fall back to `removed`?
- was this action `native`?
- why?

## Recommended Screen Behavior

### Home Screen

Keyboard:
- `t`: new town
- `o`: open town
- `r`: runs
- `s`: services
- `a`: approvals
- `q`: quit

### Town Hall Screen

Keyboard:
- `enter`: send mayor message
- `n`: new turn
- `o`: ask originator to create worker/shop
- `d`: delegate selected task
- `c`: compact session
- `tab`: rotate panes
- `esc`: back to Home

### Shops Screen

Keyboard:
- `a`: enable action
- `x`: disable action
- `w`: create worker
- `i`: inspect invocation history

### Runs Screen

Keyboard:
- `enter`: open run details
- `p`: open approval context
- `f`: filter by runtime

## MVP Scope

The first TUI release should not try to cover every endpoint.

### MVP

Must include:
- service health dashboard
- create/open session
- mayor conversation pane
- session message history
- turn creation
- orchestrate-turn execution
- worker/shop list
- originator-based worker creation from package
- run summaries
- runtime metadata display

Can wait:
- memory graph browser
- full approval actions from TUI
- package installation flows
- container runtime diagnostics
- durable memory graph editing

## Suggested Internal Architecture

Add a new package:
- `services/tui/`

Suggested layout:
- `services/tui/app.py`
- `services/tui/client.py`
- `services/tui/state.py`
- `services/tui/views/home.py`
- `services/tui/views/town.py`
- `services/tui/views/turns.py`
- `services/tui/views/shops.py`
- `services/tui/views/runs.py`
- `services/tui/views/services.py`

Keep HTTP access in one place:
- `client.py`

That client should wrap:
- session endpoints
- catalog endpoints
- execution/runtime support endpoints
- router runtime-capabilities endpoint
- orchestrator health and summary reads
- approval endpoints
- service manifest reads where useful

## Data Sources

The TUI should prefer live service APIs first.

Primary sources:
- `session`
- `catalog`
- `execution`
- `router`
- `orchestrator`
- `approval`

Secondary sources:
- service manifest under `control-plane/runtime/service-manifest.json`
- session JSON under `control-plane/observability/sessions/`
- run summaries under `control-plane/observability/runs/`

Use direct file reads only when no API exists or for richer local diagnostics.

## Mayor Conversation Transport

There are two reasonable approaches.

### Option A: Thin Conversation Layer In TUI

The TUI itself:
- records the user message
- decides whether to open a turn
- submits a simple orchestrate-turn request
- renders reconciliation as the mayor reply

Pros:
- fastest path
- no server changes required

Cons:
- mayor behavior lives partly in the client

### Option B: Add A Session Endpoint For Mayor Conversation

Example:
- `POST /v1/sessions/{id}/converse`

This endpoint would:
- append message
- create or extend a turn
- decide the orchestration path
- return the mayor’s response and any generated work

Pros:
- cleaner product model
- logic belongs server-side

Cons:
- more backend work before the TUI lands

Recommendation:
- ship TUI MVP with Option A
- move to Option B once the interaction pattern stabilizes

## Visual Style

This should not look like a generic sysadmin dashboard.

Direction:
- civic operations console
- town hall as the center
- strong hierarchy
- restrained but distinct color cues

Suggested palette:
- warm neutral base
- muted green for healthy
- amber for approvals/stale
- red for failures
- steel blue for `native`
- copper or gold accent for `town-hall`
- slate or teal accent for `umbrella-agent-runtime`
- gray/red accent for `removed`

Typography is terminal-bound, so the main distinction will come from:
- layout
- borders
- title bars
- concise labels

## Phase Plan

### Phase 1: TUI Skeleton

Deliver:
- app shell
- Home view
- Services view
- session list/open/create

Success criterion:
- operator can see platform health and open an existing town

### Phase 2: Town Hall MVP

Deliver:
- mayor conversation pane
- session messages
- create turn
- orchestrate-turn from UI
- render mayor reconciliation

Success criterion:
- operator can practically “talk to the mayor”

### Phase 3: Shop And Originator Controls

Deliver:
- shop list
- create worker from package/profile
- enable/disable shop actions
- inspect worker/sub-agent state

Success criterion:
- originator and shop management are usable without raw JSON

### Phase 4: Run + Runtime Visibility

Deliver:
- run summaries
- runtime breakdowns
- invocation/delegation details
- explicit runtime filter

Success criterion:
- operator can explain where work ran and why

### Phase 5: Approval + Diagnostics

Deliver:
- approval queue pane
- blocked run drilldown
- service/log diagnostics

Success criterion:
- TUI is a credible operator console, not just a session toy

## Non-Goals

The first TUI should not:
- reimplement every CLI command
- replace contract tests
- become a general graph editor
- hide runtime differences
- pretend every runtime has identical capabilities

## Recommended First Build Slice

Build the smallest slice that proves the product direction:

1. launch TUI
2. detect core services
3. create/open town session
4. show mayor/originator/workers in panes
5. post a message
6. open a turn
7. run a simple mayor-orchestrated action
8. render the mayor summary as the reply

If that feels good, the rest of the TUI will have a solid foundation.

## Current Implementation Status

Implemented now:
- `scripts/umbrella-tui`
- stdlib curses-based shell under `services/tui/`
- full-stack lifecycle launcher:
  - `scripts/control-plane/manage-platform-stack`
- Home view:
  - service health
  - platform start/stop controls
  - session list
  - runtime classes
  - agent package list
- Town view:
  - mayor/originator/session summary
  - shop list
  - message transcript
  - create town
  - talk to the mayor or another agent/shop
  - mayor path can orchestrate worker shops and return the reconciliation summary as the reply

Not implemented yet:
- dedicated turn creation and orchestration controls
- originator worker creation from inside the TUI
- approvals view
- runs view
- service log drilldown
- a server-side `converse` endpoint

So the current build satisfies the initial shell and town-entry slice, but not the full MVP defined above.
