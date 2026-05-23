# Technical Implementation Spec: Family Office Estate Portal

> **Status**: Draft
> **Version**: 1.1 | **Updated**: 2026-05-23

## TL;DR

Python/FastAPI server rendering Jinja2 templates with HTMX and Tailwind CSS, backed
by a SQLite read-through cache populated by a scheduled refresher that calls four backend
services (`llc-manager`, `pp-security-master`, `xero_crypto`, `family_office`). Authentication
is handled entirely by Cloudflare Zero Trust at the network edge.

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
- **Authentication**: Cloudflare Zero Trust (see [ADR-002](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md))
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
  │     Cloudflare Zero Trust         │
  │  (magic link auth, JWT issuance)  │
  └───────────────────┬───────────────┘
                      │ CF-Access-JWT header on all requests
                      ▼
  ┌───────────────────────────────────┐
  │      FastAPI Portal Server        │
  │  ┌────────────────────────────┐   │
  │  │  CF JWT Middleware         │   │
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
| CF JWT Middleware | Auth enforcement and role extraction | `validate_cf_jwt`, `get_role_from_email` |
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
                                           --   'Tax Returns' | 'Insurance' | 'Other'
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

### Backend Service Contracts (Required from Backend Teams)

| Service | Expected endpoint | Response shape |
| --- | --- | --- |
| `llc-manager` | `GET /api/v1/entities` | `[{id, name, type, state, agent, status, next_date, ...}]` |
| `pp-security-master` | `GET /api/v1/portfolio/summary` | `{holdings: [...], performance: [...]}` |
| `xero_crypto` | `GET /api/v1/positions` | `[{asset, quantity, usd_value, ...}]` |
| `family_office` | `GET /api/v1/documents` | `[{id, name, category, added_at, url, ...}]` |

Upstream commercial systems -- **Kubera** (net worth aggregation), **Portfolio Performance**
(desktop investment tracker), **Box** (document storage), and **Google Drive** -- are not
contacted by the portal. Each backend service owns its own integration with these systems.

**Outbound auth**: The mechanism by which the portal authenticates to each backend
(API key in header, private network restriction, mTLS) must be confirmed with each `#ASSUME` `#VERIFY`
backend team before Phase 1 begins. Configure via `BACKEND_*_API_KEY` or equivalent
env vars once the mechanism is decided.

### Environment Variables (`.env.example`)

| Variable | Purpose |
| --- | --- |
| `BACKEND_LLC_MANAGER_URL` | Base URL for `llc-manager` HTTP API (e.g. `http://llc-manager:8000`) |
| `BACKEND_PP_SECURITY_URL` | Base URL for `pp-security-master` HTTP API |
| `BACKEND_XERO_CRYPTO_URL` | Base URL for `xero_crypto` HTTP API |
| `BACKEND_FAMILY_OFFICE_URL` | Base URL for `family_office` HTTP API |
| `CF_TEAM_DOMAIN` | Cloudflare team domain (used to fetch JWT public keys at startup) |
| `CF_ACCESS_APP_ID` | Cloudflare Access Application ID (validated against JWT `aud` claim) |
| `VIEWER_EMAILS` | Comma-separated list of viewer-role email addresses |
| `ADMIN_EMAILS` | Comma-separated list of admin-role email addresses |
| `SQLITE_PATH` | Filesystem path to the SQLite cache database (e.g. `/data/portal.db`) |

All variables are required at startup; the application must fail fast if any are absent.

## 5. Security

### Authentication

Cloudflare Zero Trust (magic link) -- see [ADR-002](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md).

CF JWT middleware must validate three things on every request:

1. `CF-Access-JWT-Assertion` header is present
2. JWT signature verified against Cloudflare team domain public keys (fetched from
   `https://<CF_TEAM_DOMAIN>/cdn-cgi/access/certs` at startup; cached with TTL)
3. `aud` claim matches `CF_ACCESS_APP_ID` env var -- prevents accepting tokens issued
   to other applications in the same Cloudflare tenant

### Authorization

Role-based: Viewer (read-only; all primary portal routes) vs Admin (adds refresh triggers
and refresh status view). Role determined by JWT `email` claim mapped against
`VIEWER_EMAILS` and `ADMIN_EMAILS` environment variable lists.

### Data Protection

- **In Transit**: TLS enforced by Cloudflare; internal backend service calls over HTTPS
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
- [ADR-002: Authentication](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md)
- [ADR-003: Backend Data Aggregation](../architecture/adr/adr-003-backend-data-aggregation.md)
- [Development Roadmap](./roadmap.md)
