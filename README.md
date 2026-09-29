# family-office-portal

Secure family estate portal -- consolidated view of entities, finances, documents,
and portfolio, aggregated from `llc-manager`, `xero_crypto`, `pp-security-master`,
and `family_office` backends.

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
- Backend services accessible: `llc-manager`, `pp-security-master`, `xero_crypto`,
  `family_office`

## Setup

```bash
# Install dependencies
uv sync --extra dev

# Copy and configure environment variables
cp .env.example .env
# Edit .env with your backend URLs, CF credentials, and authorized email lists

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
missing or if `AUTHENTIK_JWKS_URL` is not `https://`. See
`docs/planning/tech-spec.md` for full documentation.

| Variable | Description |
| --- | --- |
| `BACKEND_LLC_MANAGER_URL` | Base URL for llc-manager service |
| `BACKEND_PP_SECURITY_URL` | Base URL for pp-security-master service |
| `BACKEND_XERO_CRYPTO_URL` | Base URL for xero_crypto service |
| `BACKEND_FAMILY_OFFICE_URL` | Base URL for family_office service |
| `AUTHENTIK_JWKS_URL` | `https://` JWKS endpoint of the Authentik provider |
| `AUTHENTIK_ISSUER` | Expected `iss` claim of the Authentik provider |
| `AUTHENTIK_AUDIENCE` | Expected `aud` claim (the provider's client ID) |
| `SQLITE_PATH` | Absolute path to the SQLite cache database |

Optional variables:

| Variable | Default | Description |
| --- | --- | --- |
| `FO_ADMIN_GROUP` | `fo-admin` | Authentik group granted the Admin role |
| `FO_VIEWER_GROUP` | `fo-viewer` | Authentik group granted the Viewer role |
| `AUTHENTIK_JWKS_CACHE_SECONDS` | `600` | JWKS cache lifetime in seconds |

## Architecture

Key design decisions are documented as ADRs in `docs/architecture/adr/`:

- [ADR-001](docs/architecture/adr/adr-001-frontend-rendering-architecture.md) -- server-rendered HTML with HTMX
- [ADR-002](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md) -- Cloudflare Zero Trust authentication (superseded by ADR-005)
- [ADR-003](docs/architecture/adr/adr-003-backend-data-aggregation.md) -- SQLite read-through cache
- [ADR-005](docs/architecture/adr/adr-005-authentication-authentik-forward-auth.md) -- Authentik forward-auth authentication

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines, the
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards, and
[CHANGELOG.md](CHANGELOG.md) for release history. This is a private family tool;
external contributions are not accepted.

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability reporting policy.

## License

[MIT](LICENSE) -- Copyright (c) 2026 Byron Williams
