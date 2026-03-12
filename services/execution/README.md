# Umbrella Execution Service

HTTP service wrapper over runtime adapter contract calls.

## Run

```bash
python3 services/execution/app.py --host 127.0.0.1 --port 8794
```

Optional:
- `--memory-core-url http://127.0.0.1:8798`
- `--policy-url http://127.0.0.1:8791`
- `--mesh-token <token>`

Step actions handled natively by execution-service:
- `memoryWrite`
- `memoryRead`
- `memoryDelete`
- `memoryList`

## Endpoints

- `GET /v1/execution/health`
- `POST /v1/execution/submit-step-spec`
- `POST /v1/execution/submit-command`
- `POST /v1/execution/heartbeat`
- `POST /v1/execution/result`
- `POST /v1/execution/cancel`
- `POST /v1/execution/compensate`

Failure responses are structured enough to distinguish:
- `failureCategory` such as `policy`, `validation`, `dependency`, or `runtime`
- `failureSource` such as `policy`, `memory-core`, or `adapter`
- `failureReason` such as `execution_policy_denied`, `execution_validation_failed`, `dependency_unavailable`, or `execution_runtime_failed`
