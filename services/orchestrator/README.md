# Umbrella Orchestrator Service

Control-plane orchestration service that owns run initialization, scheduling, dispatch, and terminalization.

## Run

```bash
python3 services/orchestrator/app.py --host 127.0.0.1 --port 8797
```

## Endpoints

- `GET /v1/orchestrator/health`
- `GET /v1/orchestrator/runs/{run_id}/summary`
- `POST /v1/orchestrator/runs/start`

`/v1/orchestrator/runs/start` accepts service URLs and run metadata and executes one run to terminal state.

Run summaries persist structured failure details when available, including:
- `failureCategory`
- `failureSource`
- `failureMessage`
- `failedStepId`
- `blockedStepId`

Run summaries also persist runtime telemetry when available, including:
- `runtimeBreakdown`
- `runtimeRequested`
- `runtimeResolved`
- `executorRuntime`
- `actionFamily`
- `runtimeCapability`

Resume protection:
- `resumeBlocked=true` is rejected unless `caller` is `approval-service`.
