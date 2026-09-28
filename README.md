# family-office-portal

Secure family estate portal -- consolidated view of entities, finances, documents,
and portfolio, aggregated from the `llc-manager`, `xero_crypto`, and
`pp-security-master` backends.

## Overview

A private, read-only web application built with Python/FastAPI and server-rendered
Jinja2 templates with HTMX partial updates. All data flows through a SQLite
read-through cache populated by scheduled refresh jobs. Sign-in is handled by
Authentik forward auth behind Pangolin and Traefik on the homelab (ADR-004); the
portal validates the signed `X-authentik-jwt` header.

Five sections: Home, Documents, Finances, Portfolio, Entities.

## Prerequisites

- Python 3.12 (only; 3.13 is not yet supported)
- [UV](https://docs.astral.sh/uv/) package manager
- An Authentik forward-auth proxy provider for the portal, with groups `fo-viewer`
  and `fo-admin` (see ADR-004)
- Backend services reachable on the private network: `llc-manager`,
  `pp-security-master`, `xero_crypto`

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

# Production
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

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

Required at startup (the app exits with status 1 if any is missing; see
`app/config.py`):

| Variable | Description |
| --- | --- |
| `BACKEND_LLC_MANAGER_URL` | Base URL for llc-manager (entities and documents) |
| `BACKEND_LLC_MANAGER_API_KEY` | API key sent to llc-manager |
| `BACKEND_PP_SECURITY_URL` | Base URL for pp-security-master |
| `BACKEND_PP_SECURITY_API_KEY` | API key sent to pp-security-master |
| `BACKEND_XERO_CRYPTO_URL` | Base URL for xero_crypto |
| `BACKEND_XERO_CRYPTO_API_KEY` | API key sent to xero_crypto |
| `AUTHENTIK_JWKS_URL` | JWKS URL of the portal's Authentik proxy provider |
| `AUTHENTIK_ISSUER` | Expected `iss` claim of `X-authentik-jwt` |
| `AUTHENTIK_AUDIENCE` | Expected `aud` claim (the provider's client ID) |
| `SQLITE_PATH` | Path to the SQLite database (back it up; it holds daily balance history) |

Optional:

| Variable | Default | Description |
| --- | --- | --- |
| `FO_VIEWER_GROUP` | `fo-viewer` | Authentik group granting Viewer |
| `FO_ADMIN_GROUP` | `fo-admin` | Authentik group granting Admin |
| `DISPLAY_TIMEZONE` | `UTC` | IANA time zone for "last updated" labels |
| `SCHEDULER_ENABLED` | `true` | Set `false` for template work without backends |
| `BACKEND_TIMEOUT_SECONDS` | `10` | Outbound request timeout |
| `JWKS_CACHE_SECONDS` | `600` | How long Authentik signing keys are reused |

## Container image

`Dockerfile` builds a distroless image (DHI Python 3.12) that compiles Tailwind in
the builder stage and runs as UID 65532. `.github/workflows/build-image.yml`
smoke-tests, pushes, and signs `ghcr.io/byronwilliamscpa/family-office-portal`
with `sha-<short>` tags for the homelab-infra stack to pin.

## Architecture

Key design decisions are documented as ADRs in `docs/architecture/adr/`:

- [ADR-001](docs/architecture/adr/adr-001-frontend-rendering-architecture.md) -- server-rendered HTML with HTMX
- [ADR-002](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md) -- Cloudflare Zero Trust authentication (superseded)
- [ADR-004](docs/architecture/adr/adr-004-authentication-authentik-forward-auth.md) -- Authentik forward auth
- [ADR-003](docs/architecture/adr/adr-003-backend-data-aggregation.md) -- SQLite read-through cache

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines, the
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards, and
[CHANGELOG.md](CHANGELOG.md) for release history. This is a private family tool;
external contributions are not accepted.

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability reporting policy.

## License

[MIT](LICENSE) -- Copyright (c) 2026 Byron Williams
