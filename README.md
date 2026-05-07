# family-office-portal

Secure family estate portal -- consolidated view of entities, finances, documents,
and portfolio, aggregated from `llc-manager`, `xero_crypto`, `pp-security-master`,
and `family_office` backends.

## Overview

A private, read-only web application built with Python/FastAPI and server-rendered
Jinja2 templates with HTMX partial updates. All data flows through a SQLite
read-through cache populated by scheduled refresh jobs. Authentication is handled
entirely by Cloudflare Zero Trust at the network edge.

Five sections: Home, Documents, Finances, Portfolio, Entities.

## Prerequisites

- Python 3.12+
- [UV](https://docs.astral.sh/uv/) package manager
- Cloudflare Zero Trust application configured (see ADR-002)
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

# Generate secrets baseline (required for detect-secrets hook)
detect-secrets scan > .secrets.baseline
git add .secrets.baseline
```

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

All variables are required at startup. See `docs/planning/tech-spec.md` for full
documentation.

| Variable | Description |
| --- | --- |
| `BACKEND_LLC_MANAGER_URL` | Base URL for llc-manager service |
| `BACKEND_PP_SECURITY_URL` | Base URL for pp-security-master service |
| `BACKEND_XERO_CRYPTO_URL` | Base URL for xero_crypto service |
| `BACKEND_FAMILY_OFFICE_URL` | Base URL for family_office service |
| `CF_TEAM_DOMAIN` | Cloudflare team domain for JWT key fetching |
| `CF_ACCESS_APP_ID` | Cloudflare Access application ID (audience claim) |
| `VIEWER_EMAILS` | Comma-separated list of viewer email addresses |
| `ADMIN_EMAILS` | Comma-separated list of admin email addresses |
| `SQLITE_PATH` | Absolute path to the SQLite cache database |

## Architecture

Key design decisions are documented as ADRs in `docs/architecture/adr/`:

- [ADR-001](docs/architecture/adr/adr-001-frontend-rendering-architecture.md) -- server-rendered HTML with HTMX
- [ADR-002](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md) -- Cloudflare Zero Trust authentication
- [ADR-003](docs/architecture/adr/adr-003-backend-data-aggregation.md) -- SQLite read-through cache

## Contributing

See [CONTRIBUTING.md](https://github.com/ByronWilliamsCPA/.github/blob/main/CONTRIBUTING.md)
for contribution guidelines. This is a private family tool; external contributions
are not accepted.

## Security

See [SECURITY.md](SECURITY.md) for the vulnerability reporting policy.

## License

[MIT](LICENSE) -- Copyright (c) 2026 Byron Williams
