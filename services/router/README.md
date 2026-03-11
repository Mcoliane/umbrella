# Umbrella Router Service

HTTP service for runtime routing decisions.

## Run

```bash
python3 services/router/app.py --host 127.0.0.1 --port 8795
```

## Endpoints

- `GET /v1/router/health`
- `GET /v1/router/config`
- `POST /v1/router/route-step`
- `POST /v1/router/reroute-step`
