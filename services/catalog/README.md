# Umbrella Catalog Service

HTTP service for skill/plugin discovery, validation, managed install/update flows, and lifecycle state.

This is the first phase of a plugin/skills runtime:
- scan local `skills/` and `plugins/` trees for manifests
- validate manifests against the catalog contract (`shell`, `python`, and
  `container` runtimes; `http` is rejected because plugin-host has no HTTP
  dispatch)
- maintain local and managed install state with explicit lifecycle rows
- install bundle artifacts into `control-plane/extensions/`
- verify bundle checksums (every installed file must be listed in
  `CHECKSUMS.json`) before a managed install becomes usable
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
  - `signedFile` (must be `CHECKSUMS.json`; any other value is rejected so the
    signature always covers the checksum manifest)
- `SIGNATURE`
  - detached OpenSSL signature over `CHECKSUMS.json`

## Trust Model

`--signature-mode` gates enable and invocation, not just install:
- `permissive` (default): every registered item may be enabled.
- `require-checksum`: only installs with a verified `CHECKSUMS.json` are
  trusted. Scan-discovered and `install-local` items carry no verification,
  so they are registered and listed but cannot be enabled or invoked.
- `require-signature`: same as `require-checksum`, but a verified detached
  signature is required.

Each catalog entry reports its gate as `trust: {ok, signatureMode, reason}`.
`install-local` records `checksumVerified: false` / `signatureStatus:
not-present` honestly — it performs no verification.

### Scan roots and `--trusted-scan-root`

`--scan-root` is repeatable and **replaces** the default scan roots
(`skills`, `plugins`) when given; omitting it keeps the defaults.

Dropping a manifest into a scanned directory does not produce an enabled
executable. A scan-discovered manifest may honor its `defaultEnabled: true`
only when it sits under a trusted scan root, declared with the repeatable
`--trusted-scan-root` flag (default: `skills`, the first-party tree shipped
with Umbrella). Manifests discovered anywhere else — including the default
`plugins` root — are registered but stay disabled until an operator enables
them explicitly via `POST /v1/catalog/items/enable`. Pass
`--trusted-scan-root ''` to trust no scan root at all.

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
