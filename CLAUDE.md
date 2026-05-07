# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Status**: Active | **Version**: 1.1.0 | **Updated**: 2026-05-07
>
> Project-specific rules for the family-office-portal FastAPI application.
> Global standards are in `~/.claude/CLAUDE.md` and apply everywhere.
> Rules here extend or override the global standard for this project only.

## Development commands

```bash
# Install all dependencies (including dev extras)
uv sync --extra dev

# Run the development server with auto-reload
uv run uvicorn app.main:app --reload --port 8000

# Run the full test suite with coverage
uv run pytest

# Run a single test file
uv run pytest tests/test_auth.py -v

# Type check
uv run basedpyright

# Lint (with auto-fix)
uv run ruff check --fix .

# Format
uv run ruff format .

# Dependency vulnerability scan
uv run pip-audit

# Run all pre-commit hooks against every file (required before committing)
uv run pre-commit run --all-files

# Compile Tailwind CSS (run once; use --watch during active template development)
tailwindcss -i static/css/input.css -o static/css/output.css --minify
```

## Source layout

The Python package is `app/`. All source files live under it; do not create top-level
`.py` files outside `app/`.

```text
app/
  main.py              # FastAPI app instantiation, lifespan, middleware registration
  middleware/          # CF JWT validation middleware
  routes/              # One module per section (home, documents, finances, portfolio, entities)
  cache.py             # Async SQLite readers called by route handlers
  scheduler.py         # APScheduler setup; refresh job definitions
  db.py                # SQLite connection factory, schema init (WAL + busy_timeout)
templates/
  pages/               # Full-page Jinja2 templates (browser-navigable URLs)
  partials/            # HTMX fragment templates (not navigable directly)
static/
  htmx.min.js          # Vendored; do not load from CDN
  chart.umd.min.js     # Vendored Chart.js v4
  css/                 # Tailwind output
tests/
  conftest.py          # SQLite fixture DB, httpx AsyncClient
```

Component-to-file mapping from `docs/planning/tech-spec.md`:

| Component | Location | Notes |
| --- | --- | --- |
| CF JWT Middleware | `app/middleware/` | Validates header, signature, `aud` claim, maps role |
| Route Handlers | `app/routes/` | Return `TemplateResponse`; read SQLite via `cache.py` |
| Cache Reader | `app/cache.py` | Async `aiosqlite` reads; called by routes |
| Refresh Scheduler | `app/scheduler.py` | Sync writes; calls backend services via `httpx` |
| Staleness Checker | `app/cache.py` | `is_stale(dataset, threshold_hours)` |

## Project context

This is a private, read-only family estate portal. Two low-proficiency primary
users view it on tablets. Reliability and plain-English presentation are the top
priorities. The portal is a read-only consumer of four backend services; it never
writes to or contacts upstream commercial systems directly.

**Current phase**: Phase 0 (Foundation) -- all tasks "Planned"; no application code exists yet.
Phase 0 goal: scaffold, auth middleware, CI pipeline, and five empty section shells.

Key documents to read before making architectural or data-model decisions:

- `docs/planning/tech-spec.md` -- canonical stack, schema, endpoints, env vars
- `docs/architecture/adr/adr-001-frontend-rendering-architecture.md` -- server-rendered
  HTML is a settled decision; do not propose SPA patterns
- `docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md` -- auth is at
  the network edge; do not add application-level password handling
- `docs/architecture/adr/adr-003-backend-data-aggregation.md` -- all data flows through
  the SQLite read-through cache; route handlers never call backend services directly
- `docs/planning/roadmap.md` -- current phase and acceptance criteria

## Tech stack conventions

- **Language**: Python 3.12 only. Do not introduce 3.13 syntax or features.
- **Package management**: UV. Never use pip or conda directly. Use `uv run` for
  tool invocations and `uv add` for dependencies.
- **Web framework**: FastAPI with Starlette's `Jinja2Templates`. Route handlers
  return `TemplateResponse`; they do not return JSON unless the route is an HTMX
  partial returning an HTML fragment.
- **Templates**: Jinja2 in `templates/`. Full-page templates in `templates/pages/`;
  HTMX partial fragments in `templates/partials/`. Never return a partial from a
  route that a browser may navigate to directly.
- **Tailwind**: Compiled at build time via the `tailwindcss` CLI binary. No Node.js
  runtime; no `npm run`. Do not add PostCSS plugins or Node dependencies.
- **HTMX**: Loaded as a static asset (`static/htmx.min.js`). Do not load HTMX from
  a CDN in production templates.
- **Charts**: Chart.js v4 vendored in `static/`. Do not add other chart libraries.
- **Scheduler**: APScheduler v3 configured in-process at FastAPI startup. All four
  refresh jobs (`refresh_entities`, `refresh_holdings`, `refresh_positions`,
  `refresh_documents`) run on independent cadences.
- **Database**: SQLite via `aiosqlite` for async reads in route handlers; synchronous
  writes in APScheduler refresh jobs. Initialize with `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=5000`. No ORM; use raw SQL with parameterized queries.
- **HTTP client**: `httpx` for outbound calls in APScheduler refresh jobs. Use
  `httpx.Client` (synchronous) inside scheduler jobs; `httpx.AsyncClient` in tests.
  Backend auth mechanism (API key vs private network) is unconfirmed; confirm with
  each backend team before Phase 1. #ASSUME
- **Logging**: `structlog` in structured JSON format. Never log financial values,
  document contents, or email addresses beyond INFO-level auth events.

## Authentication rules

Cloudflare Zero Trust handles authentication at the network edge (ADR-002). The
portal's only auth responsibility is JWT validation in middleware.

The CF JWT middleware must:

1. Require the `CF-Access-JWT-Assertion` header on every non-static request.
2. Validate the JWT signature against Cloudflare public keys fetched from
   `https://<CF_TEAM_DOMAIN>/cdn-cgi/access/certs` at startup (cache with TTL).
3. Validate the `aud` claim against `CF_ACCESS_APP_ID`. Skipping this check allows
   tokens issued to other apps in the same Cloudflare tenant -- a security gap
   documented in ADR-002. #CRITICAL
4. Map the `email` claim to `Viewer` or `Admin` role via `VIEWER_EMAILS` and
   `ADMIN_EMAILS` env vars.

Never implement password-based auth, OAuth flows, or session cookies.

## Data layer rules

- Route handlers read from SQLite only. They never call backend HTTP services.
- Refresh jobs (APScheduler) call backend services and write to SQLite. They never
  serve HTTP responses.
- Every cached dataset has a `fetched_at` ISO8601 timestamp column.
- Staleness thresholds (from tech-spec.md):
  - `entities` (llc-manager): 8 hours
  - `holdings` / `performance` (pp-security-master): 4 hours
  - `positions` (xero_crypto): 4 hours
  - `documents` (family_office): 24 hours
- A stale section must show the last cached value plus a "last updated [time]" label.
  Never show a blank section or an unhandled error to a primary user.
- `pp-security-master` is alpha-status. Treat its 500 responses as expected; surface
  as stale data, not as errors in user-visible templates. #ASSUME API contract unstable

## Environment variables

All environment variables in `docs/planning/tech-spec.md` section 4 are required at
startup. The application must call `sys.exit(1)` if any are absent. Do not add
optional env vars without a documented default.

Required: `BACKEND_LLC_MANAGER_URL`, `BACKEND_PP_SECURITY_URL`,
`BACKEND_XERO_CRYPTO_URL`, `BACKEND_FAMILY_OFFICE_URL`, `CF_TEAM_DOMAIN`,
`CF_ACCESS_APP_ID`, `VIEWER_EMAILS`, `ADMIN_EMAILS`, `SQLITE_PATH`.

## Frontend conventions

- **Target viewport**: 1024x768 landscape (tablet). Design for this first.
- **Navigation**: exactly five top-level sections (Home, Documents, Finances,
  Portfolio, Entities). Do not add sub-menus or a sixth section without a phase
  gate approval.
- **Navigation depth**: maximum two levels. A primary user must never be more than
  two clicks from any content.
- **Plain English**: no raw identifiers (EIN, state IDs, UUIDs) visible to primary
  users in list or detail views.
- **Back button**: must always work. Never use `history.pushState` patterns that break
  standard browser navigation.
- **JavaScript**: HTMX and Chart.js only. All content must be readable with JavaScript
  disabled (HTMX degrades to full-page reload; this is acceptable).

## Testing requirements

Coverage targets override global defaults for critical paths:

| Scope | Minimum |
| --- | --- |
| Overall line coverage | 80% |
| Critical paths (auth middleware, cache reads, staleness logic) | 95% |

Test types required:

- **Unit**: cache reader functions, staleness checker, JWT validation middleware,
  template context builders.
- **Integration**: full page renders with SQLite fixture data; HTMX partial responses;
  refresh scheduler with mocked backend HTTP responses.
- **Resilience**: mock each backend returning 500 and assert that the affected section
  shows stale data, not an error page.

`asyncio_mode = "auto"` is set in `pyproject.toml`, so every `async def test_*`
function runs automatically. Do not add `@pytest.mark.asyncio` to individual tests.
Use `httpx.AsyncClient` (already a project dependency) as the FastAPI test client.

## Model Selection

| Task type | Model | When |
| --- | --- | --- |
| Architecture, planning, ADRs | Opus 4.7 | Multi-step decisions, deep code review |
| Standard development | Sonnet 4.6 | Most coding and editing |
| Read-only exploration | Haiku 4.5 | File scanning, quick lookups |

Use Haiku for the built-in `Explore` subagent (file scanning, structure mapping).
Use Opus when reasoning about CF JWT middleware security or SQLite WAL concurrency.

## Response-Aware Development (RAD)

Tag assumptions that could cause production failures using `#CRITICAL`, `#ASSUME`,
and `#EDGE` markers paired with `#VERIFY` instructions. Mandatory categories:

- **Timing**: APScheduler cadences and staleness threshold alignment
- **External resources**: backend service availability; `pp-security-master` alpha
  status is a standing `#ASSUME`
- **Data integrity**: SQLite WAL concurrency between async readers and sync writer
- **Security**: CF JWT `aud` claim validation; any bypass is a `#CRITICAL`
- **Financial**: net worth aggregation logic; any rounding or currency assumption
  is an `#ASSUME` requiring `#VERIFY`

Full tagging syntax: `~/.claude/docs/response-aware-development.md`

## Cross-references

Global rules that apply without modification:

| Rule | Applies to |
| --- | --- |
| `~/.claude/rules/python.md` | All `.py` files |
| `~/.claude/rules/testing.md` | All `tests/` files |
| `~/.claude/rules/git-workflow.md` | All branches and commits |
| `~/.claude/rules/writing.md` | All `.md` files, docstrings, comments |
| `~/.claude/rules/pre-commit.md` | Pre-commit hook checklist |

Project-specific rules to create as development progresses:

- `.claude/rules/templates.md` -- Jinja2 partial vs full-page conventions (create
  before Phase 1 templates are written)
- `.claude/rules/cache-layer.md` -- SQLite reader/writer patterns, fixture conventions
  (create before Phase 1 data layer)
- `.claude/rules/refresh-jobs.md` -- APScheduler job structure, error handling,
  `refresh_log` write conventions (create before first refresh job)
