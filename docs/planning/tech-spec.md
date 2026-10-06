# Technical Implementation Spec: Family Office Estate Portal

**Note (2026-09-28)**: Documents now come from llc-manager. Where this document
says otherwise, `CLAUDE.md` takes precedence.

> **Status**: Draft
> **Version**: 1.2 | **Updated**: 2026-09-29

## TL;DR

Python/FastAPI server rendering Jinja2 templates with HTMX and Tailwind CSS, backed
by a SQLite read-through cache populated by a scheduled refresher that calls four backend
services (`llc-manager`, `pp-security-master`, `xero_crypto`, `family_office`). Authentik
forward auth at the reverse proxy handles login; the portal validates the signed JWT it
forwards (ADR-005).

## 1. Technology Stack

### Core

- **Language**: Python 3.12 (primary development and deployment runtime); minimum supported runtime is 3.10. CI matrix covers 3.10-3.14. Do not use stdlib additions from 3.11+ (e.g. `tomllib`, `Self`, `ExceptionGroup`, `asyncio.timeout`) in application code; BasedPyright type checking is pinned to 3.12.
- **Package Manager**: UV
- **Web Framework**: FastAPI (with Starlette's `Jinja2Templates` for server-side rendering)
- **Template Engine**: Jinja2 (partials for HTMX responses; full pages for initial loads)
- **CSS**: Tailwind CSS v3 (compiled at build time via `tailwindcss` CLI; no Node.js runtime)
- **Partial Updates**: HTMX v2 (loaded as a static asset, no npm)
- **Charts**: Chart.js v4 (vendored static asset; used in Finances and Portfolio sections)
- **Task Scheduler**: APScheduler v3 (in-process; scheduled refresh jobs per backend)

### Code Quality

- **Linter**: Ruff
- **Type Checker**: BasedPyright (strict mode)
- **Formatter**: ruff format (88 chars)
- **Testing**: pytest + pytest-asyncio + httpx (async test client)

### Data Layer

- **Cache Database**: SQLite (see [ADR-003](../architecture/adr/adr-003-backend-data-aggregation.md))
- **ORM**: None; raw SQL via `aiosqlite` for async reads during page render
- **Cache write**: synchronous during refresh job (APScheduler context)

### Infrastructure

- **CI/CD**: GitHub Actions
- **Authentication**: Authentik forward auth behind Traefik (see [ADR-005](../architecture/adr/adr-005-authentication-authentik-forward-auth.md), which supersedes ADR-002)
- **Container**: Docker (single container; SQLite volume-mounted)

## 2. Architecture

### Pattern

Server-rendered monolith with a background refresh scheduler. See [ADR-001](../architecture/adr/adr-001-frontend-rendering-architecture.md).

### Component Diagram

```text
  Browser (HTMX + Tailwind + Chart.js)
          │
          │ HTTPS
          ▼
  ┌───────────────────────────────────┐
  │  Traefik + Authentik outpost      │
  │  (forward auth, signs the JWT)    │
  └───────────────────┬───────────────┘
                      │ X-authentik-jwt header on all requests
                      ▼
  ┌───────────────────────────────────┐
  │      FastAPI Portal Server        │
  │  ┌────────────────────────────┐   │
  │  │  Authentik JWT Middleware  │   │
  │  │  (validates JWT, sets role)│   │
  │  ├────────────────────────────┤   │
  │  │  Route Handlers            │   │
  │  │  /  /documents /finances   │   │
  │  │  /portfolio  /entities     │   │
  │  ├────────────────────────────┤   │
  │  │  Jinja2 Templates          │   │
  │  │  (full pages + partials)   │   │
  │  └──────────┬─────────────────┘   │
  └─────────────┼─────────────────────┘
                │ aiosqlite reads
                ▼
  ┌─────────────────────────────────────┐
  │           SQLite Cache              │
  │  entities | holdings | positions    │
  │  documents | refresh_log            │
  └─────────────────────────────────────┘
                ▲
                │ APScheduler refresh jobs (HTTP calls)
  ┌─────────────┴──────────────────────────────────┐
  │              Backend Services                   │
  │                                                 │
  │  llc-manager      → entities/compliance/dates   │
  │  pp-security-master → holdings/performance      │
  │  xero_crypto      → crypto positions (USD)      │
  │  family_office    → document metadata           │
  └─────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Purpose | Key Functions |
| --- | --- | --- |
| Authentik JWT Middleware | Auth enforcement and role extraction | `AuthentikAuthMiddleware`, `authenticate`, `validate_authentik_jwt`, `role_from_groups` |
| Route Handlers | Map URL paths to template contexts | `home_route`, `documents_route`, `finances_route`, `portfolio_route`, `entities_route` |
| Cache Reader | Async SQLite reads for template context | `get_entities`, `get_holdings`, `get_positions`, `get_documents` |
| Refresh Scheduler | Periodic HTTP calls to backends; writes to SQLite | `refresh_entities`, `refresh_holdings`, `refresh_positions`, `refresh_documents` |
| Staleness Checker | Compare `fetched_at` to threshold; set display flag | `is_stale(dataset, threshold_hours)` |
| Jinja2 Templates | Render HTML pages and HTMX partials | `templates/` directory |

## 3. Data Model

### Cache Tables (SQLite)

```sql
-- Entities from llc-manager
CREATE TABLE entities (
    id          TEXT PRIMARY KEY,          -- llc-manager entity UUID
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,             -- 'LLC' | 'Trust'
    state       TEXT NOT NULL,
    agent       TEXT,
    status      TEXT NOT NULL,             -- 'current' | 'due_soon' | 'overdue'
    next_date   TEXT,                      -- ISO8601 date string
    fetched_at  TEXT NOT NULL              -- ISO8601 datetime
);

-- Holdings from pp-security-master (investment portfolio)
CREATE TABLE holdings (
    id              TEXT PRIMARY KEY,
    security_name   TEXT NOT NULL,         -- plain English name, e.g. "Apple Inc."
    sector          TEXT,
    current_value   REAL,
    allocation_pct  REAL,
    gain_loss       REAL,
    fetched_at      TEXT NOT NULL
);

-- Portfolio performance timeseries from pp-security-master
CREATE TABLE performance (
    date        TEXT NOT NULL,             -- ISO8601 date
    total_value REAL,
    benchmark   REAL,                     -- S&P 500 equivalent
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (date)
);

-- Crypto positions from xero_crypto
CREATE TABLE positions (
    id              TEXT PRIMARY KEY,
    asset           TEXT NOT NULL,         -- 'BTC', 'ETH', etc.
    quantity        REAL,
    usd_value       REAL,
    fetched_at      TEXT NOT NULL
);

-- Document metadata from family_office / document backend
-- NOTE: actual files are NOT stored in SQLite; only metadata and proxy URL
CREATE TABLE documents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,             -- 'Estate Planning' | 'LLCs' | 'Trusts' |
                                           --   'Tax Returns' | 'Insurance' |
                                           --   'Personal records' | 'Other'
    added_at    TEXT NOT NULL,             -- ISO8601 from source system
    modified_at TEXT,
    proxy_url   TEXT NOT NULL,             -- portal proxy path for download/preview
    fetched_at  TEXT NOT NULL
);

-- Refresh job audit log
CREATE TABLE refresh_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT NOT NULL,             -- 'llc-manager' | 'pp-security-master' |
                                           --   'xero_crypto' | 'family_office'
    status      TEXT NOT NULL,             -- 'success' | 'error'
    error_msg   TEXT,
    ran_at      TEXT NOT NULL              -- ISO8601 datetime
);
```

### Relationships

- `entities` rows link to `documents` rows via `category = 'LLCs'` or `'Trusts'`
  filter on the entity name (soft link; not a foreign key)
- `holdings` + `positions` are independent datasets combined in the Finances section

## 4. API Endpoints (Internal Portal Routes)

| Method | Path | Purpose | Auth |
| --- | --- | --- | --- |
| GET | `/` | Home dashboard | Viewer, Admin |
| GET | `/documents` | Document folder view | Viewer, Admin |
| GET | `/documents/search` | Search by name (HTMX partial) | Viewer, Admin |
| GET | `/documents/{id}/preview` | Inline PDF proxy | Viewer, Admin |
| GET | `/documents/{id}/download` | File download proxy | Viewer, Admin |
| GET | `/finances` | Net worth and asset allocation | Viewer, Admin |
| GET | `/portfolio` | Holdings and performance | Viewer, Admin |
| GET | `/entities` | Entity list with status | Viewer, Admin |
| GET | `/entities/{id}` | Entity detail view | Viewer, Admin |
| GET | `/admin/refresh-status` | Per-service refresh log | Admin only |
| POST | `/admin/refresh/{service}` | Trigger manual refresh | Admin only |
| POST | `/api/v1/balances` | Deliver account balances (collector) | `X-API-Key` header, not identity; disabled (404) when `BALANCE_INTAKE_API_KEY` is unset |

### Backend Service Contracts (Required from Backend Teams)

| Service | Expected endpoint | Response shape |
| --- | --- | --- |
| `llc-manager` | `GET /api/v1/entities` | `[{id, name, type, state, agent, status, next_date, ...}]` |
| `pp-security-master` | `GET /api/v1/portfolio/summary` | `{holdings: [...], performance: [...]}` |
| `xero_crypto` | `GET /api/v1/positions` | `[{asset, quantity, usd_value, ...}]` |
| `llc-manager` (documents) | `GET /api/v1/documents` | `{items: [{id, title, category, entity_id, document_type, document_date, is_confidential, created_at, updated_at, ...}], total}` |
| `llc-manager` (document file) | `GET /api/v1/documents/{id}/file` | The file bytes with its `Content-Type`; streamed per request by the preview and download routes (ADR-007) |

Upstream commercial systems -- **Kubera** (net worth aggregation), **Portfolio Performance**
(desktop investment tracker), **Box** (document storage), and **Google Drive** -- are not
contacted by the portal. Each backend service owns its own integration with these systems.

**Outbound auth**: The mechanism by which the portal authenticates to each backend
(API key in header, private network restriction, mTLS) must be confirmed with each `#ASSUME` `#VERIFY`
backend team before Phase 1 begins. The portal sends the key from
`BACKEND_<NAME>_API_KEY` as `X-API-Key` on every request to that backend.

### Environment Variables (`.env.example`)

| Variable | Purpose |
| --- | --- |
| `AUTHENTIK_JWT_SECRET` | Client secret of the Authentik proxy provider, the HS256 key for `X-authentik-jwt`; injected by the deployment stack, never committed; at least 32 characters, not the stack placeholder, no leading or trailing whitespace |
| `AUTHENTIK_ISSUER` | Exact expected `iss` claim (e.g. `https://auth.example.com/application/o/family-office-portal/`) |
| `AUTHENTIK_AUDIENCE` | Expected `aud` claim: the Authentik provider's client ID |
| `SQLITE_PATH` | Filesystem path to the SQLite cache database (e.g. `/data/portal.db`) |

All variables above are required at startup; the application writes the missing variable's
name to stderr and exits with status 1 if any is absent, and also exits 1 if
`AUTHENTIK_JWT_SECRET` is shorter than 32 characters, equals the stack placeholder, or
has leading or trailing whitespace (the error names the variable, never its value).

Optional variables, each with a documented default:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FO_ADMIN_GROUP` | `fo-admin` | Authentik group granted the Admin role |
| `FO_VIEWER_GROUP` | `fo-viewer` | Authentik group granted the Viewer role; must differ from `FO_ADMIN_GROUP` |
| `BACKEND_TIMEOUT_SECONDS` | `10` | Timeout for outbound refresh-job calls |
| `DISPLAY_TIMEZONE` | `UTC` | IANA time zone for "last updated" labels |
| `SCHEDULER_ENABLED` | `true` | Start the refresh scheduler at startup; `false` for template work without backends |
| `BALANCE_INTAKE_API_KEY` | unset | Shared key for `POST /api/v1/balances`, compared in constant time with the `X-API-Key` header; at least 32 characters, never logged or echoed; unset disables the endpoint (404) |

Each backend is an optional pair of variables. Set both to connect it:

| Variable pair | Backend | Rule |
| --- | --- | --- |
| `BACKEND_LLC_MANAGER_URL`, `BACKEND_LLC_MANAGER_API_KEY` | `llc-manager` | Optional pair |
| `BACKEND_PP_SECURITY_URL`, `BACKEND_PP_SECURITY_API_KEY` | `pp-security-master` | Optional pair |
| `BACKEND_XERO_CRYPTO_URL`, `BACKEND_XERO_CRYPTO_API_KEY` | `xero_crypto` | Optional pair |
| `BACKEND_DATA_INGESTOR_URL`, `BACKEND_DATA_INGESTOR_API_KEY` | `data-ingestor` | Optional pair; settings and startup checks only, no client calls yet |

Rules for every pair:

- A URL set with its key unset, empty or whitespace: the application exits 1 and
  the error names the key variable.
- A key set with no URL: allowed; startup logs one info line.
- No URL: the backend is "not connected". Its scheduled refresh jobs log one
  line and make no outbound call, and pages that show its data say "Not connected
  yet" instead of an error.
- Every request to a connected backend carries `X-API-Key`; there is no path that
  sends one without it.

## 5. Security

### Authentication

Authentik forward auth behind Traefik -- see [ADR-005](../architecture/adr/adr-005-authentication-authentik-forward-auth.md)
(supersedes [ADR-002](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md)).
Login, passwordless sign-in and session length are Authentik's job, configured in
homelab-infra.

The Authentik JWT middleware (`app/middleware/authentik.py`) validates every request
except `/health` and `/static/`:

1. The signed `X-authentik-jwt` header is present. The plain `X-authentik-username`,
   `X-authentik-email` and `X-authentik-groups` headers are never read.
2. The header's `alg` is `HS256` (the only accepted algorithm) and the signature is
   verified with the proxy provider's client secret in `AUTHENTIK_JWT_SECRET`. An
   Authentik proxy provider cannot keep a signing key, so its token carries no `kid`
   and there is no key set to fetch; the portal makes no outbound request to
   authenticate anything.
3. The signature verifies, and `exp`, `iss` and `aud` are present, with `iss` equal to
   `AUTHENTIK_ISSUER`, `aud` containing `AUTHENTIK_AUDIENCE`, and `exp` in the future
   (10 s leeway for clock skew). The `aud` check prevents accepting tokens minted for other Authentik
   applications.
4. `preferred_username` (or `sub`) is a non-empty string.

Public and admin paths are judged on the routed path (any ASGI `root_path` prefix
stripped), and `/admin` matches only on a segment boundary. Any failure returns a
plain 403, and the log records only a reason category, never the token. The portal
needs its own single-application Authentik provider so that its `aud` is unique and
its client secret, which both signs and verifies the token, is never shared (ADR-005
contract item 13 and the ADR-005 amendment of 2026-09-29).

### Balance intake

`POST /api/v1/balances` is the one route that does not use the signed identity
token. A collector on the internal network sends the shared key in `X-API-Key`.

- The middleware lets exactly `POST /api/v1/balances` through to the route
  (routed path, any `root_path` stripped). Every other method and path still
  needs a valid token, and a token sent to this route is ignored.
- The key is the optional `BALANCE_INTAKE_API_KEY` setting (at least 32
  characters of printable ASCII, different from `AUTHENTIK_JWT_SECRET`),
  compared with `hmac.compare_digest`. It is checked before the
  body is read. A wrong or missing key is a 401. When the setting is unset the
  endpoint is disabled and answers 404.
- The body is limited to 6 MiB (enough for the largest valid delivery) and
  2000 rows, and values are decimal strings
  with at most 12 digits before the point. A validation error names the row
  and field but never echoes a submitted value, and nothing is stored on any
  error.
- The key, submitted values, account identifiers and balances are never logged.
- The collector must reach the portal directly on the internal network, because
  a forward-auth route in front of the portal would refuse it. See the ADR-005
  amendment of 2026-10-05.

### Authorization

Role-based: Viewer (read-only; all primary portal routes) vs Admin (adds refresh triggers
and refresh status view). Role determined by the JWT `groups` claim: membership of
`FO_ADMIN_GROUP` (default `fo-admin`) grants Admin, `FO_VIEWER_GROUP` (default
`fo-viewer`) grants Viewer, and a token with neither is refused. Every `/admin/*` path
requires Admin. The principal (username, email, name, role) is stored on
`request.state.principal`.

### Data Protection

- **In Transit**: TLS terminated at Traefik; internal backend service calls over HTTPS
  or trusted private network
- **At Rest**: SQLite file on the portal host; no PII beyond email addresses and financial
  summaries; disk encryption at host level is the operator's responsibility
- **Sensitive Data**: Account numbers and full legal identifiers from backends are stored
  in the cache only if required for display; log sanitization must exclude financial values

## 6. Error Handling

### Strategy

Graceful degradation: stale data with a label is always preferred over an empty section
or an error message visible to primary users. Backend errors during refresh are logged to
`refresh_log` and surfaced only in the Admin refresh-status view.

### Staleness Thresholds

| Dataset | Stale after | Display label |
| --- | --- | --- |
| Entities (`llc-manager`) | 8 hours | "last updated [time]" |
| Holdings/Performance (`pp-security-master`) | 4 hours | "last updated [time]" |
| Crypto positions (`xero_crypto`) | 4 hours | "last updated [time]" |
| Documents (`family_office`) | 24 hours | "last updated [time]" |

### Logging

- **Format**: Structured JSON via `structlog`
- **Levels**: DEBUG (dev), INFO (refresh events), WARNING (stale threshold breach),
  ERROR (backend unreachable, JWT validation failure)
- **Never log**: financial values, document contents, email addresses beyond INFO-level
  auth events

## 7. Performance Requirements

| Metric | Target | Measurement |
| --- | --- | --- |
| Page render (from cache) | < 1 second | Playwright timing on 10 Mbps tablet sim |
| Document list render | < 1 second for up to 500 documents | Playwright timing |
| HTMX search response | < 500 ms | Network tab, search input delay |
| Refresh scheduler | Completes within 60 seconds per service | `refresh_log.ran_at` delta |

## 8. Testing Strategy

### Coverage Target

- Minimum: 80% line coverage
- Critical paths (auth middleware, cache reads, staleness logic): 95%

### Test Types

- **Unit**: Cache reader functions, staleness checker, JWT validation middleware,
  template context builders
- **Integration**: Full page renders with SQLite fixture data; HTMX partial responses;
  refresh scheduler with mocked backend HTTP responses
- **E2E**: Playwright at 1024x768 (tablet landscape); cover all five sections in
  nominal state, stale state, and empty state

## Related Documents

- [Project Vision](./project-vision.md)
- [ADR-001: Frontend Rendering](../architecture/adr/adr-001-frontend-rendering-architecture.md)
- [ADR-002: Authentication (superseded)](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md)
- [ADR-005: Authentication, Authentik forward auth](../architecture/adr/adr-005-authentication-authentik-forward-auth.md)
- [ADR-003: Backend Data Aggregation](../architecture/adr/adr-003-backend-data-aggregation.md)
- [Development Roadmap](./roadmap.md)
