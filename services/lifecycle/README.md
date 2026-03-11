# Umbrella Lifecycle Service

HTTP service for run lifecycle validation and terminal reason enforcement.

## Run

```bash
python3 services/lifecycle/app.py --host 127.0.0.1 --port 8793
```

## Endpoints

- `GET /v1/lifecycle/health`
- `GET /v1/lifecycle/model`
- `POST /v1/lifecycle/validate-transition`
- `POST /v1/lifecycle/validate-terminal-reason`
