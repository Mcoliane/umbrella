# Umbrella Scheduler Service

HTTP service for step scheduling decisions.

## Run

```bash
python3 services/scheduler/app.py --host 127.0.0.1 --port 8796
```

## Endpoints

- `GET /v1/scheduler/health`
- `GET /v1/scheduler/config`
- `POST /v1/scheduler/compute-ready`
- `POST /v1/scheduler/next-batch`
