# Umbrella Catalog Service

HTTP service for skill/plugin discovery, validation, managed install/update flows, and lifecycle state.

This is the first phase of a plugin/skills runtime:
- scan local `skills/` and `plugins/` trees for manifests
- validate manifests against the catalog contract
- maintain local and managed install state with explicit lifecycle rows
- install bundle artifacts into `control-plane/extensions/`
- verify bundle checksums before a managed install becomes usable
- optionally verify detached bundle signatures against trusted public keys
- track installed versions for managed catalog items
- expose a central action catalog for future policy/router/session integration

## Run

```bash
python3 services/catalog/app.py --host 127.0.0.1 --port 8786
```

Optional signature enforcement:

```bash
python3 services/catalog/app.py \
  --signature-mode require-signature \
  --trusted-key-dir control-plane/policy/trusted-signing-keys
```

Bundle signature format:
- `SIGNATURE.json`
  - `keyId`
  - `algorithm` (`sha256-rsa`)
  - `signedFile` (`CHECKSUMS.json`)
- `SIGNATURE`
  - detached OpenSSL signature over `CHECKSUMS.json`

## Endpoints

- `GET /v1/catalog/health`
- `GET /v1/catalog/items`
- `GET /v1/catalog/items/{id}`
- `GET /v1/catalog/items/{id}/versions`
- `GET /v1/catalog/actions`
- `POST /v1/catalog/refresh`
- `POST /v1/catalog/install-local`
- `POST /v1/catalog/install-bundle`
- `POST /v1/catalog/update`
- `POST /v1/catalog/uninstall`
- `POST /v1/catalog/items/enable`
- `POST /v1/catalog/items/disable`

## Runtime State

- Runtime registry path:
  - `control-plane/observability/catalog/registry.json`
- Managed extension install root:
  - `control-plane/extensions/`

## Schemas

- Manifest schema:
  - `services/catalog/schemas/manifest.schema.json`
- Catalog entry schema:
  - `services/catalog/schemas/catalog-entry.schema.json`
- Action invocation contract:
  - `services/catalog/schemas/action-invocation.schema.json`
