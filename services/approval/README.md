# Umbrella Approval Service

HTTP service for approval artifact lifecycle.

## Run

```bash
python3 services/approval/app.py --host 127.0.0.1 --port 8792
```

## Endpoints

- `GET /v1/approval/health`
- `GET /v1/approval/{approval_key}`
- `GET /v1/approval/{approval_key}/run-status`
- `POST /v1/approval/{approval_key}/request`
- `POST /v1/approval/{approval_key}/approve`
- `POST /v1/approval/{approval_key}/deny`
- `POST /v1/approval/resume`
- `GET /v1/approval/resume-journal?runId=<runId>[&approvalKey=<approvalKey>]`
- `GET /v1/approval/resume-journal/{runId}/{approvalKey}/{idempotencyKey}`

`/v1/approval/resume` requires:
- `plan`
- `runId`
- `approvalKey` (must already be `APPROVED`)

Optional:
- `idempotencyKey`
- `orchestratorUrl`

If `idempotencyKey` is set, approval-service journals the first resume result at:
- `control-plane/approvals/resume-journal/<runId>__<approvalKey>__<idempotencyKey>.json`

Repeated resume calls with the same `(runId, approvalKey, idempotencyKey)` return the journaled result and do not invoke the runner again.

Use the resume journal read endpoints to inspect replay history without reading files directly.

`/v1/approval/{approval_key}/run-status` returns:
- `PENDING`
- `BLOCKED`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
