# family-office-portal

Secure family estate portal -- consolidated view of entities, finances, documents,
and portfolio, aggregated from the `llc-manager`, `xero_crypto`, and
`pp-security-master` backends.

## Overview

A private, read-only web application built with Python/FastAPI and server-rendered
Jinja2 templates with HTMX partial updates. All data flows through a SQLite
read-through cache populated by scheduled refresh jobs. Authentik forward auth at
the reverse proxy handles login; the app only validates the signed JWT it forwards
(ADR-005).

Five sections: Home, Documents, Finances, Portfolio, Entities.

## Prerequisites

- Python 3.12 (only; 3.13 is not yet supported)
- [UV](https://docs.astral.sh/uv/) package manager
- Authentik proxy provider and Traefik forward auth configured by homelab-infra
  (contract in ADR-005)
- Backend services, each optional and connected when its URL and key are set:
  `llc-manager`, `pp-security-master`, `xero_crypto`, `data-ingestor`

## Setup

```bash
# Install dependencies
uv sync --extra dev

# Copy and configure environment variables
cp .env.example .env
# Edit .env with the Authentik settings, SQLITE_PATH, and any backend URL and key pairs

# Install pre-commit hooks
pre-commit install
pre-commit install --hook-type commit-msg
```

The `.secrets.baseline` file is committed to the repository and updated only when
new secrets are intentionally introduced. Do not regenerate it on first checkout.

## Running

```bash
# Development server (auto-reload)
uv run uvicorn app.main:app --reload --port 8000

# Production, behind Traefik only (replace the range with the Traefik network)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips 172.20.0.0/16
```

In production the portal must be reachable only through Traefik with Authentik
forward auth in front of it; see ADR-005 for the full homelab-infra contract.

## Testing

```bash
# Run full test suite with coverage
uv run pytest

# Run a specific test file
uv run pytest tests/test_auth.py -v

# Type checking
uv run basedpyright

# Linting
uv run ruff check .

# Dependency audit
uv run pip-audit
```

## Environment Variables

The variables below are required at startup; the app exits with status 1 if any is
missing or if `AUTHENTIK_JWT_SECRET` is unusable (shorter than 32 characters, the
stack placeholder, or padded with whitespace). See
`docs/planning/tech-spec.md` for full documentation.

| Variable | Description |
| --- | --- |
| `AUTHENTIK_JWT_SECRET` | Client secret of the Authentik proxy provider (HS256 key for `X-authentik-jwt`); a secret, never commit it |
| `AUTHENTIK_ISSUER` | Expected `iss` claim of the Authentik provider |
| `AUTHENTIK_AUDIENCE` | Expected `aud` claim (the provider's client ID) |
| `SQLITE_PATH` | Absolute path to the SQLite cache database |

Backends are optional, in pairs. Set both variables of a pair to connect a backend:

| Variable | Description |
| --- | --- |
| `BACKEND_LLC_MANAGER_URL` / `BACKEND_LLC_MANAGER_API_KEY` | llc-manager base URL and the key sent to it as `X-API-Key` |
| `BACKEND_PP_SECURITY_URL` / `BACKEND_PP_SECURITY_API_KEY` | pp-security-master base URL and key |
| `BACKEND_XERO_CRYPTO_URL` / `BACKEND_XERO_CRYPTO_API_KEY` | xero_crypto base URL and key |
| `BACKEND_DATA_INGESTOR_URL` / `BACKEND_DATA_INGESTOR_API_KEY` | data-ingestor base URL and key (settings only; no client calls it yet) |

A URL with an unset, empty or whitespace key stops startup, and the error names the
key variable. A key with no URL is allowed and logged at info level. A backend with
no URL is "not connected": its refresh jobs skip with one log line and make no
outbound call, and its pages show "Not connected yet".

Other optional variables, each with a documented default:

| Variable | Default | Description |
| --- | --- | --- |
| `FO_ADMIN_GROUP` | `fo-admin` | Authentik group granting Admin |
| `FO_VIEWER_GROUP` | `fo-viewer` | Authentik group granting Viewer |
| `DISPLAY_TIMEZONE` | `UTC` | IANA time zone for "last updated" labels |
| `SCHEDULER_ENABLED` | `true` | Set `false` for template work without backends |
| `BACKEND_TIMEOUT_SECONDS` | `10` | Outbound request timeout |
| `BALANCE_INTAKE_API_KEY` | unset | Key a collector sends in `X-API-Key` to deliver balances to `POST /api/v1/balances`; at least 32 characters; unset disables the endpoint (it answers 404) |

## Container image

`Dockerfile` builds a distroless image (DHI Python 3.12) that compiles Tailwind in
the builder stage and runs as UID 65532. `.github/workflows/build-image.yml`
smoke-tests, pushes, and signs `ghcr.io/byronwilliamscpa/family-office-portal`
with `sha-<short>` tags for the homelab-infra stack to pin.

Optional variables:

| Variable | Default | Description |
| --- | --- | --- |
| `FO_ADMIN_GROUP` | `fo-admin` | Authentik group granted the Admin role |
| `FO_VIEWER_GROUP` | `fo-viewer` | Authentik group granted the Viewer role |

## Architecture

Key design decisions are documented as ADRs in `docs/architecture/adr/`:

- [ADR-001](docs/architecture/adr/adr-001-frontend-rendering-architecture.md) -- server-rendered HTML with HTMX
- [ADR-002](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md) -- Cloudflare Zero Trust authentication (superseded by ADR-005)
- [ADR-003](docs/architecture/adr/adr-003-backend-data-aggregation.md) -- SQLite read-through cache
- [ADR-005](docs/architecture/adr/adr-005-authentication-authentik-forward-auth.md) -- Authentik forward-auth authentication
- [ADR-006](docs/architecture/adr/adr-006-document-indexer.md) -- document indexer as a scheduled command

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines, the
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards, and
[CHANGELOG.md](CHANGELOG.md) for release history. This is a private family tool;
external contributions are not accepted.

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability reporting policy.

## License

[MIT](LICENSE) -- Copyright (c) 2026 Byron Williams
