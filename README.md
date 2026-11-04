# family-office-portal

Secure family estate portal -- consolidated view of entities, finances, documents,
and portfolio, aggregated from the `llc-manager`, `pp-security-master`,
`xero_crypto`, and `data-ingestor` backends.

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

# Production, behind Traefik only (the flags match the Dockerfile CMD)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips="<traefik-network-cidr>"
```

Run a single worker: the in-process scheduler must run in exactly one process,
because there is one SQLite writer. Replace `<traefik-network-cidr>` with the range
of the reverse proxy network, so that `X-Forwarded-*` headers are trusted only from
the proxy; never set it to `*`. The container image defaults to a broad private
range; narrow it once the proxy network subnet is pinned.

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

Chat, document search, and the indexer are optional and off by default. With none
of their settings present the app starts normally and the matching pages say they
are not connected. Their settings are listed by name in `.env.example`; values come
from the deployment environment and are never committed.

### Question panel (chat)

The Home page can show a panel where a signed-in user asks a question about the
family's documents and balances. Each question makes two live calls while the
request is open: a document search and the chat model. Design and limits are in
[ADR-009](docs/architecture/adr/adr-009-chat-live-model-call.md).

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_BASE_URL` | unset | Base URL of the chat model service, with no trailing `/v1` (the app adds `/v1/chat/completions`) |
| `LLM_API_KEY` | unset | Bearer key for the chat model service; required when the URL is set; a secret |
| `LLM_MODEL` | unset | Model name sent with each request; left out of the request when empty |
| `LLM_TIMEOUT_SECONDS` | `30` | Longest one model call may take; a number above zero |
| `CHAT_INSTRUCTIONS_PATH` | unset | File holding the chat instructions; keep it outside the repository, readable by the app |
| `CHAT_ENABLED_FOR` | `admin` | Who sees the panel: `admin`, `all` (Admins and Viewers) or `none` |

The panel is connected only when the URL and key form a valid connection and the
instructions file is readable. A URL without a key, or an unusable URL, stops
startup, and the error names the variable. Answering also needs document search
(below); without it the panel says search is not connected yet. A person who is not
in the chosen audience sees no panel.

There is currently no per-user rate limit; the only throttle is a cap of two model
calls at a time across the whole app, so one user can hold both. Keep
`CHAT_ENABLED_FOR` at `admin` until a per-user limit exists, and do not open chat to
a wider group before then.

### Document search and the indexer

Document search finds passages in the family's documents for the question panel. It
uses an embedding service and a Qdrant vector database; see
[ADR-008](docs/architecture/adr/adr-008-document-search.md) for search and
[ADR-006](docs/architecture/adr/adr-006-document-indexer.md) for the indexer.

| Variable | Default | Description |
| --- | --- | --- |
| `EMBED_BASE_URL` | unset | Base URL of the embedding service, with no trailing `/v1` (the app adds `/v1/embeddings`) |
| `EMBED_API_KEY` | unset | Bearer key for the embedding service; required when the URL is set; a secret |
| `EMBEDDING_MODEL` | unset | Embedding model name; required when the URL is set; the service must return 1024-dimensional vectors |
| `EMBED_TIMEOUT_SECONDS` | `60` | Longest one embedding request may take; a number above zero |
| `QDRANT_URL` | unset | Base URL of the Qdrant vector database |
| `QDRANT_API_KEY` | unset | Qdrant key; required when the URL is set; a secret |
| `CHUNKS_DIR` | unset | Read-only directory of chunk-set files; used only by the document indexer |
| `TAX_LAW_PATH` | unset | Tax-law knowledge-base JSON file; used only by the tax-law indexer |

Search is connected only when both the embedding pair and the Qdrant pair are set
and valid. Unlike the backend pairs, a bad value here does not stop startup: the web
process logs a warning that names the variable and treats search as not connected.

Two commands fill the search index. They take no arguments, read the settings above
from the environment, and run from the portal image, never inside the web process:

```bash
# Index the chunk-set files in CHUNKS_DIR
python -m app.retrieval.indexer

# Index the tax-law knowledge base at TAX_LAW_PATH
python -m app.retrieval.tax_law
```

The operator schedules both, off-hours, one run at a time: a run must finish before
the next starts, and nothing else may write to the same Qdrant collections. Exit
status 0 means success, 1 means a file or the knowledge base could not be used, and
2 means the command is not configured, a setting is wrong, or a service refused its
key or stopped answering.

### Accounts valued by hand

Some accounts have no feed, so their value is typed in from statements. The Admin
page at `/admin/manual-marks` lists each such account with its owner, the date of
its value and how old that value is, oldest first, so an Admin can see which
values need updating. Viewers cannot open it; the Finances page links to it for Admins.

## Container image

`Dockerfile` builds a distroless image (DHI Python 3.12) that compiles Tailwind in
the builder stage and runs as UID 65532. `.github/workflows/build-image.yml`
smoke-tests, pushes, and signs `ghcr.io/byronwilliamscpa/family-office-portal`
with `sha-<short>` tags for the homelab-infra stack to pin.

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
