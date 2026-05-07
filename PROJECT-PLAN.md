# Project Plan: Family Office Estate Portal

> **Status**: Active | **Version**: 1.0.0 | **Updated**: 2026-05-07
>
> Authoritative synthesis of all foundational planning documents. Supersedes
> individual documents when there is a conflict, but the source documents remain
> the canonical reference for their respective domains.
>
> Sources:
> [Project Vision](docs/planning/project-vision.md) |
> [Tech Spec](docs/planning/tech-spec.md) |
> [Roadmap](docs/planning/roadmap.md) |
> [ADR-001](docs/architecture/adr/adr-001-frontend-rendering-architecture.md) |
> [ADR-002](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md) |
> [ADR-003](docs/architecture/adr/adr-003-backend-data-aggregation.md)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Scope](#2-scope)
3. [Architecture Overview](#3-architecture-overview)
4. [Technology Stack](#4-technology-stack)
5. [Phased Development](#5-phased-development)
   - [Phase 0: Foundation](#phase-0-foundation)
   - [Phase 1: Entities](#phase-1-entities)
   - [Phase 2: Documents](#phase-2-documents)
   - [Phase 3: Finances and Portfolio](#phase-3-finances-and-portfolio)
   - [Phase 4: Resilience and Polish](#phase-4-resilience-and-polish)
   - [Phase 5: Ask Feature (Future)](#phase-5-ask-feature-future-not-in-mvp)
6. [Risk Register](#6-risk-register)
7. [Success Metrics](#7-success-metrics)
8. [Definition of Done](#8-definition-of-done)
9. [Phase 0 Checklist](#9-phase-0-checklist)

---

## 1. Executive Summary

The Family Office Estate Portal is a private, read-only web application that gives two
low-proficiency tablet users consolidated visibility into the family estate: documents,
LLC and trust compliance, financial summaries, and investment portfolio performance.

Today the two primary users must navigate multiple professional systems, call advisors,
and log into separate tools (Kubera, Portfolio Performance, Box) to answer basic questions
about their own estate. The portal replaces all of that with one URL, one Cloudflare magic
link login, and five clearly labeled sections presented in plain English.

Key design priorities, in order:

1. **Reliability**: Graceful degradation when any backend is slow or unavailable; no blank
   sections, no unhandled errors visible to primary users.
2. **Simplicity**: Maximum two navigation levels; five top-level sections; plain English
   throughout; standard browser navigation always works.
3. **Security**: Authentication at the network edge via Cloudflare Zero Trust; no password
   management for primary users; read-only portal (no write operations for the Viewer role).

The portal is a read-only consumer of four internal backend services. It never contacts
Kubera, Portfolio Performance, Box, or Google Drive directly. All data flows through a local
SQLite read-through cache populated by a background scheduler.

Estimated delivery: 9-10 weeks across five phases (Phase 5 is future scope, not MVP).

---

## 2. Scope

### In Scope (MVP)

- Home dashboard: net worth summary, upcoming compliance dates, portfolio snapshot, recent
  documents
- Documents section: category folders (6 categories), name search, inline PDF preview,
  download proxy
- Finances section: net worth total, asset allocation chart, account list in USD
- Portfolio section: performance chart vs S&P 500, holdings table (plain English names),
  sector allocation
- Entities section: LLC and trust status list (green/yellow/red), per-entity detail,
  linked documents
- Cloudflare Zero Trust authentication (magic link, 30-day device sessions)
- Two access levels: Viewer (read-only primary routes) and Admin (adds refresh triggers and
  refresh status view)
- Tablet-first responsive layout (1024x768 landscape) with desktop support
- Staleness indicators and graceful degraded states for all six display areas (five sections
  plus home)
- Admin refresh-status view showing per-service last-run time and error count
- Docker container deployment

### Out of Scope

- Ask / Q&A feature: deferred to Phase 5; excluded from MVP
- Direct connections to Kubera, Google Drive, or Portfolio Performance: all data comes
  from backend services, not fetched by the portal
- Any write operations for the Viewer role
- Phone-optimized layout (tablet and desktop only for v1)
- Full-text document search (name search only in v1)
- Database schema, API design, data ingestion pipelines, LLM infrastructure (backend team
  concerns)
- Multi-tenant or public registration

---

## 3. Architecture Overview

Three settled architectural decisions govern all implementation work. These are not open
for reconsideration without a new ADR and explicit approval.

### ADR-001: Server-Rendered HTML (Settled)

**Decision**: HTMX + Jinja2 + Tailwind CSS. No SPA patterns, no React, no Vue.

**Rationale**: Low-proficiency users depend on standard browser affordances (back button,
bookmarks, copy URL) that SPAs frequently break. Server rendering produces a complete,
usable HTML document with no hydration phase and no client-side routing edge cases.
HTMX handles partial updates (search results, chart refreshes) without a JavaScript framework.

**Consequence for implementation**:

- Every page route returns a full HTML document via `TemplateResponse`.
- Partial routes used by HTMX `hx-get` return HTML fragments only (never served as a
  direct browser navigation target).
- Full-page templates live in `templates/pages/`; HTMX partial fragments in
  `templates/partials/`.
- Chart.js v4 is vendored as a static asset; HTMX v2 is loaded as a static asset.
  Neither is loaded from a CDN in production.

See [ADR-001](docs/architecture/adr/adr-001-frontend-rendering-architecture.md) for the full
decision record.

### ADR-002: Cloudflare Zero Trust Authentication (Settled)

**Decision**: Cloudflare Zero Trust with email magic links handles all authentication.
The portal's only responsibility is JWT validation in middleware.

**Rationale**: Primary users cannot reliably manage passwords. Cloudflare sits in front of
the application and validates sessions before any request reaches the portal server. The
login instruction fits in one sentence: "Check your email and tap the link."

**Consequence for implementation**:

- CF JWT middleware validates `CF-Access-JWT-Assertion` on every non-static request.
- JWT signature is verified against Cloudflare public keys fetched at startup from
  `https://<CF_TEAM_DOMAIN>/cdn-cgi/access/certs` (cached with TTL).
- The `aud` claim MUST be validated against `CF_ACCESS_APP_ID`. Skipping this check
  allows tokens issued to other apps in the same Cloudflare tenant. **#CRITICAL**
- Role is determined by mapping the JWT `email` claim against `VIEWER_EMAILS` and
  `ADMIN_EMAILS` env vars.
- No password-based auth, OAuth flows, or session cookies are implemented.

See [ADR-002](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md) for the
full decision record.

### ADR-003: SQLite Read-Through Cache (Settled)

**Decision**: The portal maintains a local SQLite cache populated by a scheduled background
refresher. Route handlers read from SQLite only; they never call backend services directly.

**Rationale**: Live fetching means one slow backend blocks the entire page. The cache
decouples portal availability from backend availability. Staleness indicators tell the user
exactly what they are seeing without blocking the page render.

**Consequence for implementation**:

- All six cache tables must be initialized at startup with WAL mode and busy timeout:
  `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`.
- Four APScheduler jobs run independently: `refresh_entities`, `refresh_holdings`,
  `refresh_positions`, `refresh_documents`.
- Every cached table has a `fetched_at` ISO8601 timestamp column.
- A stale section shows the last cached value plus a "last updated [time]" label. It
  never shows a blank section or unhandled error. **#CRITICAL**
- `pp-security-master` alpha instability surfaces as stale data, not errors. **#ASSUME**
  API contract is unstable; treat all 500 responses as expected.

Refresh cadences from ADR-003:

| Service | Cadence | Staleness threshold |
| --- | --- | --- |
| `llc-manager` | Every 4 hours | 8 hours |
| `pp-security-master` | Every 1 hour | 4 hours |
| `xero_crypto` | Every 2 hours | 4 hours |
| `family_office` | On document events (24-hour poll fallback) | 24 hours |

See [ADR-003](docs/architecture/adr/adr-003-backend-data-aggregation.md) for the full
decision record.

---

## 4. Technology Stack

| Category | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.12 | No 3.13 syntax; strict compliance |
| Package manager | UV | Never use pip or conda directly |
| Web framework | FastAPI | Route handlers return `TemplateResponse` |
| Template engine | Jinja2 (Starlette integration) | Full pages in `pages/`; partials in `partials/` |
| CSS | Tailwind CSS v3 | Compiled at build time via CLI; no Node runtime |
| Partial updates | HTMX v2 | Vendored static asset; not CDN-loaded in production |
| Charts | Chart.js v4 | Vendored static asset; no other chart libraries |
| Scheduler | APScheduler v3 | In-process; four independent refresh jobs |
| Database | SQLite via `aiosqlite` | WAL mode; no ORM; raw parameterized SQL |
| Linter / formatter | Ruff (88-char line length) | Ruff format + ruff check |
| Type checker | BasedPyright (strict mode) | All production code must pass strict checks |
| Testing | pytest + pytest-asyncio + httpx | Async test client for route handler tests |
| Logging | structlog (structured JSON) | Never log financial values, document contents, or email addresses beyond INFO-level auth events |
| CI/CD | GitHub Actions | Lint, type-check, test, Docker build on every push |
| Auth | Cloudflare Zero Trust | Magic link; JWT middleware in portal (see ADR-002) |
| Container | Docker (single container) | SQLite volume-mounted; see Phase 4 for prod image |

### Environment Variables (All Required at Startup)

| Variable | Purpose |
| --- | --- |
| `BACKEND_LLC_MANAGER_URL` | Base URL for `llc-manager` HTTP API |
| `BACKEND_PP_SECURITY_URL` | Base URL for `pp-security-master` HTTP API |
| `BACKEND_XERO_CRYPTO_URL` | Base URL for `xero_crypto` HTTP API |
| `BACKEND_FAMILY_OFFICE_URL` | Base URL for `family_office` HTTP API |
| `CF_TEAM_DOMAIN` | Cloudflare team domain (used to fetch JWT public keys) |
| `CF_ACCESS_APP_ID` | Cloudflare Access Application ID (validated against JWT `aud`) |
| `VIEWER_EMAILS` | Comma-separated viewer-role email addresses |
| `ADMIN_EMAILS` | Comma-separated admin-role email addresses |
| `SQLITE_PATH` | Filesystem path to the SQLite cache database |

The application must call `sys.exit(1)` if any variable is absent. No optional env vars
without a documented default.

---

## 5. Phased Development

### Phase 0: Foundation

**Branch**: `chore/phase-0-foundation`
**Timeline**: Week 1
**Milestone**: M0 - Dev environment and auth shell

#### Goal

Establish the development environment, project scaffold, CI pipeline, and Cloudflare Zero
Trust integration. At phase completion, an authenticated admin user can reach all five
section shells in a browser. No backend data is required; sections render empty states only.

#### Deliverables

- Project scaffold: `pyproject.toml`, UV workspace, Ruff, BasedPyright, pre-commit
- FastAPI application with Jinja2 templates and Tailwind CSS compiled at build time
- HTMX v2 loaded as a static asset; Chart.js v4 vendored
- Cloudflare Zero Trust configured; CF JWT middleware validating all requests
- SQLite database initialized with all six cache tables
  (see [Tech Spec Section 3](docs/planning/tech-spec.md))
- All five navigation sections render (empty state; no backend calls yet)
- `.env.example` documenting all required environment variables
  (see [Tech Spec Section 4](docs/planning/tech-spec.md))
- Docker container builds and runs locally
- GitHub Actions CI: lint, type-check, test, Docker build

#### Git Branch

```text
chore/phase-0-foundation
```

Merge target: `main` via pull request after phase gate passes.

#### Task Breakdown

| Task | Estimated Hours |
| --- | --- |
| Initialize `pyproject.toml` and UV workspace | 1 |
| Configure Ruff, BasedPyright, pre-commit | 2 |
| FastAPI application with Jinja2 template setup | 2 |
| Tailwind CSS build pipeline (no Node runtime) | 1 |
| CF Zero Trust Access policy setup | 2 |
| CF JWT validation middleware | 3 |
| SQLite schema migration (all 6 tables) | 2 |
| Navigation shell templates (5 sections, empty state) | 3 |
| `.env.example` with all required env vars | 1 |
| Dockerfile and `docker-compose.yml` | 2 |
| GitHub Actions CI workflow | 2 |
| **Total** | **21** |

#### Acceptance Criteria

- Authenticated admin user can reach all five sections in a browser.
- CF JWT middleware rejects requests without a valid Cloudflare Access token with HTTP 403.
- CI pipeline passes on `main` branch (lint, type-check, test, Docker build).
- Local setup documented: clone to running portal in under 20 minutes.

#### Quality Gates

- Ruff and BasedPyright strict pass with zero errors.
- Pre-commit hooks pass on all files (`pre-commit run --all-files`).
- Minimum test coverage: 80% line; 95% on CF JWT middleware (critical path).
- Docker image builds successfully and container starts without errors.
- No em-dashes in any committed text file (enforced by `no-em-dash` pre-commit hook).

#### Dependencies

- None (this is the first phase).
- Cloudflare Zero Trust access policy for the family email domain must be configured
  or in progress during this phase. It gates every subsequent phase. **#CRITICAL**

---

### Phase 1: Entities

**Branch**: `feat/phase-1-entities`
**Timeline**: Weeks 2-3
**Milestone**: M1 - Entities section live

#### Goal

Integrate `llc-manager` to populate the Entities section and the home dashboard compliance
widget. This is the first backend integration and proves the cache pattern (ADR-003) before
any alpha-status backend is attempted.

**Backend dependency**: `llc-manager` v0.1.0 must expose `GET /api/v1/entities` returning
the entity list with compliance status and dates. Confirm the API contract and outbound auth
mechanism before Phase 1 begins. **#ASSUME** backend HTTP API is available; CLI-only or
direct-DB access is not acceptable per ADR-003.

#### Deliverables

- APScheduler configured; `refresh_entities` job calling `llc-manager` every 4 hours
- Entities section: status list (green/yellow/red badges), per-entity detail view
- Home dashboard: upcoming compliance dates widget (next 3 deadlines, plain English)
- Staleness indicator on Entities section when cache is older than 8 hours
- `refresh_log` populated with success/error status after each refresh run
- Admin refresh-status view showing `llc-manager` last-refresh time and error count

#### Git Branch

```text
feat/phase-1-entities
```

Merge target: `main` via pull request after phase gate passes.

#### Task Breakdown

| Task | Estimated Hours |
| --- | --- |
| APScheduler setup and `refresh_entities` job | 4 |
| Entity cache reader function (`get_entities`) | 2 |
| Entities list template (status badges) | 3 |
| Entity detail template | 2 |
| Staleness check and display logic (`is_stale`) | 2 |
| Home dashboard compliance widget | 2 |
| Admin refresh-status view | 2 |
| Unit tests: cache reader, staleness checker | 2 |
| Integration tests: refresh job with mocked `llc-manager` | 3 |
| **Total** | **22** |

#### Acceptance Criteria (User Story US-001)

As a primary user, I want to see which LLCs and trusts are current and which need attention,
so that I can tell at a glance whether any action is needed without calling anyone.

- Each entity shows a color-coded status indicator and plain-English label.
- "Due within 60 days" entities show a clear label, not just a color.
- No raw identifiers (EIN, state ID) visible to primary users in list view.
- Entities section shows correct status badges for all LLCs and trusts from `llc-manager`.
- Per-entity detail shows name, type, state, registered agent, and next key date.
- Staleness label appears when `entities.fetched_at` is older than 8 hours.
- `llc-manager` returning 500 does not crash the portal; Entities section shows last
  cached data with a staleness label.
- Home dashboard shows 3 upcoming deadlines in plain English.

#### Quality Gates

- Ruff and BasedPyright strict pass with zero errors.
- Pre-commit hooks pass on all files.
- Unit tests for `get_entities` and `is_stale`: 95% coverage (critical path).
- Integration test: mocked `llc-manager` returning 500 must produce stale data display,
  not an error page.
- Resilience test: portal renders all five sections correctly when `llc-manager` is
  completely unreachable.
- Overall line coverage: 80% minimum.

#### Dependencies

- Requires: Phase 0 complete.
- Requires: `llc-manager` HTTP API contract confirmed with the backend team.
- Blocks: Phase 2 (document links reference entity detail pages).

---

### Phase 2: Documents

**Branch**: `feat/phase-2-documents`
**Timeline**: Weeks 4-4.5
**Milestone**: M2 - Documents section live

#### Goal

Integrate `family_office` document backend to populate the Documents section and link
documents to entity detail pages. Documents are the highest-frequency use case for primary
users ("Where is the estate plan?").

**Backend dependency**: `family_office` must expose `GET /api/v1/documents` returning
document metadata (name, category, date, proxy URL). Confirm API contract and URL stability
before Phase 2 begins. Actual documents are not stored in the portal; only metadata and a
proxy URL are cached in SQLite. **#ASSUME** PDF URLs are accessible via HTTP proxy, not
filesystem paths.

#### Deliverables

- `refresh_documents` job calling `family_office` every 24 hours (poll fallback)
- Documents section: folder view by category (all six categories), name search via HTMX
  partial
- Inline PDF preview (proxied through portal; user does not leave the portal or open a
  new tab)
- File download via portal proxy
- Empty state for empty categories (friendly message, not blank space)
- Home dashboard: three most recently added or modified documents

#### Git Branch

```text
feat/phase-2-documents
```

Merge target: `main` via pull request after phase gate passes.

#### Task Breakdown

| Task | Estimated Hours |
| --- | --- |
| `refresh_documents` scheduler job | 2 |
| Document cache reader (`get_documents` with category filter) | 2 |
| Documents folder template and category navigation | 3 |
| HTMX name-search partial | 3 |
| PDF proxy route (stream from backend URL) | 3 |
| Inline preview and download template | 2 |
| Home dashboard recent-documents widget | 2 |
| Integration tests: document list, search, proxy | 3 |
| **Total** | **20** |

#### Acceptance Criteria (User Story US-002)

As a primary user, I want to browse documents by category and open a PDF without leaving
the portal, so that I can answer "where is [document]?" without calling anyone.

- A primary user can find any document by category in two clicks or fewer.
- All six categories are visible: Estate Planning, LLCs, Trusts, Tax Returns, Insurance,
  Other.
- Name search returns results without a full page reload (HTMX partial response).
- PDF opens inline; user does not leave the portal.
- PDF preview renders with a visible download button.
- Download works on tablet without triggering a new browser window.
- Empty category shows a friendly message, not blank space.
- Home dashboard recent-documents widget shows the three newest documents.

#### Quality Gates

- Ruff and BasedPyright strict pass with zero errors.
- Pre-commit hooks pass on all files.
- PDF proxy route has integration test coverage with mocked backend responses.
- HTMX search partial returns HTML fragment only (not a full page).
- Overall line coverage: 80% minimum.
- Cache staleness: documents older than 24 hours show a staleness label.

#### Dependencies

- Requires: Phase 1 complete.
- Requires: `family_office` HTTP API contract confirmed; proxy URL format and TTL confirmed.
  **#EDGE** If proxy URLs have short TTLs or require re-authorization, document refresh
  cadence may need adjustment.
- Blocks: Phase 3 (home dashboard expects documents widget complete).

---

### Phase 3: Finances and Portfolio

**Branch**: `feat/phase-3-finances-portfolio`
**Timeline**: Weeks 5-7.5
**Milestone**: M3 - Finances and Portfolio live

#### Goal

Integrate `xero_crypto` (v1.0.0, stable) and `pp-security-master` (alpha) to populate the
Finances and Portfolio sections. Both backends contribute to the net worth total, so they
are integrated together.

**Backend dependencies**:

- `xero_crypto` v1.0.0: `GET /api/v1/positions` returning crypto positions in USD.
  Kubera (upstream commercial system) is owned by `xero_crypto`; the portal does not
  contact Kubera directly.
- `pp-security-master` (alpha): `GET /api/v1/portfolio/summary` returning holdings and
  performance timeseries. Portfolio Performance (the desktop application upstream) is owned
  by `pp-security-master`; the portal does not contact it directly.

**Alpha risk**: `pp-security-master` may have API instability. The cache layer (ADR-003)
means instability surfaces as stale data, not user-visible errors. A 4-hour staleness
threshold is acceptable. **#ASSUME** All 500 responses from `pp-security-master` are
expected and must be treated as a stale-data condition, not an application error. **#VERIFY**
before go-live: confirm with `pp-security-master` team what constitutes a partial vs total
failure response so the staleness label is accurate.

**#ASSUME** Net worth aggregation sums `positions.usd_value` (crypto, from `xero_crypto`)
and `holdings` USD values (portfolio, from `pp-security-master`). **#VERIFY** with the
admin whether this formula captures all estate assets or whether additional asset classes
must be added before display.

#### Deliverables

- `refresh_positions` job (`xero_crypto`, every 2 hours)
- `refresh_holdings` job (`pp-security-master`, every 1 hour)
- Finances section: net worth total, asset allocation chart (pie), account list in USD
- Portfolio section: performance chart vs S&P 500 (line, 1-month/6-month/1-year),
  holdings table (plain English names), sector allocation chart
- Net worth calculation: sum of `positions.usd_value` + `holdings` USD values
- All figures displayed in USD; non-USD assets labeled as approximate
- Home dashboard: net worth summary widget and portfolio snapshot widget
- Staleness indicators for both sections (4-hour threshold)

#### Git Branch

```text
feat/phase-3-finances-portfolio
```

Merge target: `main` via pull request after phase gate passes.

#### Task Breakdown

| Task | Estimated Hours |
| --- | --- |
| `refresh_positions` job (`xero_crypto`) | 3 |
| `refresh_holdings` job (`pp-security-master`) | 3 |
| Net worth aggregation function | 2 |
| Finances section template (allocation chart + account list) | 4 |
| Portfolio section template (performance chart + holdings table) | 4 |
| Chart.js integration for line and pie charts | 3 |
| Home dashboard net worth and portfolio widgets | 2 |
| Staleness indicators for both sections | 2 |
| Integration tests: both refresh jobs | 4 |
| E2E: all chart sections render at tablet viewport (1024x768) | 3 |
| **Total** | **30** |

#### Acceptance Criteria (User Story US-003)

As a primary user, I want to see a single net worth number prominently displayed, so that
I can answer "how is our money doing?" without calling an advisor.

- Net worth displays on both the Home and Finances sections.
- Trend indicator shows change from last month with a percent.
- If data is stale, the net worth figure shows the cached value and the timestamp.
- Holdings table shows plain English security names (e.g., "Apple Inc." not "AAPL").
- Performance chart renders with S&P 500 comparison on 1-month, 6-month, and 1-year ranges.
- `pp-security-master` returning 500 shows stale data with timestamp, not an error page.
- Sector allocation chart is readable on tablet at 1024x768.
- Net worth figure matches sum of known asset values (verifiable by admin).

#### Quality Gates

- Ruff and BasedPyright strict pass with zero errors.
- Pre-commit hooks pass on all files.
- Resilience tests: mock each backend returning 500 and assert that the affected section
  shows stale data, not an error page (required per testing requirements in CLAUDE.md).
- `pp-security-master` returning 500 must not affect Entities or Documents sections.
- Overall line coverage: 80% minimum; net worth aggregation function: 95% (financial
  critical path).
- Staleness checker: 95% coverage (critical path).
- Chart.js renders validated at 1024x768 via Playwright.

#### Dependencies

- Requires: Phase 2 complete.
- Requires: Both `xero_crypto` and `pp-security-master` API contracts confirmed.
- Blocks: Phase 4.

---

### Phase 4: Resilience and Polish

**Branch**: `feat/phase-4-resilience-polish`
**Timeline**: Weeks 8-9.5
**Milestone**: M4 - Portal production-ready

#### Goal

Harden the portal against degraded backend states, validate all empty and error states,
complete E2E test coverage, and prepare for production deployment. This phase closes the
gap between "working in development" and "ready for daily use by primary users."

#### Deliverables

- All six degraded states designed and tested (one per section plus home)
- Tablet-first layout validated at 1024x768 landscape and 1366x768 desktop
- Accessibility audit: minimum WCAG 2.1 AA for all pages
- E2E test suite: Playwright covering all five sections in nominal, stale, and empty states
- Test coverage: 80% line overall, 95% on critical paths
- Admin refresh-status view complete (all four services, last-run time, error count)
- Production Docker image validated; deployment runbook written
- OpenSSF required files complete: `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`,
  `README.md`, `LICENSE`

#### Git Branch

```text
feat/phase-4-resilience-polish
```

Merge target: `main` via pull request after phase gate passes.

#### Task Breakdown

| Task | Estimated Hours |
| --- | --- |
| Degraded state templates for all sections | 4 |
| Empty state templates for all sections | 3 |
| Session expiry plain-language prompt | 1 |
| Tablet layout review and fixes (1024x768) | 4 |
| WCAG 2.1 AA accessibility audit and fixes | 4 |
| Playwright E2E suite (all sections, all states) | 6 |
| Coverage gap analysis and unit test completion | 4 |
| Production Dockerfile and `docker-compose.prod.yml` | 2 |
| Deployment runbook | 2 |
| OpenSSF required files (CHANGELOG, SECURITY, CONTRIBUTING, README) | 2 |
| **Total** | **32** |

#### Acceptance Criteria

- Both primary users can locate any document in under 2 minutes without assistance
  (user acceptance test).
- Portal renders correctly with any single backend returning 500.
- All Playwright E2E tests pass at 1024x768 viewport.
- CI pipeline passes including coverage gate (80% line, 95% critical paths).
- Back button navigates correctly across all five sections.
- First meaningful paint under 1.5 seconds on a 10 Mbps connection simulation.
- No JavaScript errors in browser console under normal operation.
- All content is readable with JavaScript disabled.
- System operates without manual intervention for 2 weeks (production monitoring).

#### Quality Gates

- Ruff and BasedPyright strict pass with zero errors.
- Pre-commit hooks pass on all files.
- 80% line coverage overall; 95% on auth middleware, cache reads, and staleness logic.
- All six degraded states have Playwright test coverage.
- WCAG 2.1 AA: zero Level A and Level AA violations in automated audit.
- Docker image builds from a clean environment with no warnings.
- Deployment runbook tested by someone other than the author.
- OpenSSF baseline: all five required files present and complete.
- No unfixed CVEs older than 60 days (`uv run pip-audit` clean).

#### Dependencies

- Requires: Phase 3 complete.

---

### Phase 5: Ask Feature (Future, Not in MVP)

**Branch**: `feat/phase-5-ask-feature` (when planned)
**Timeline**: To be determined after Phase 4 release
**Status**: Explicitly out of MVP scope.

#### Description

Adds a sixth top-level section backed by the `family_office` tax and estate law knowledge
base. Users can ask plain-English questions and receive sourced answers.

This feature is intentionally excluded from v1. The `family_office` backend is active and
provides the knowledge base, but the natural language interface is not required for the
initial release.

#### Design Requirements (Captured for Future Reference)

- Clearly labeled as educational information, not legal or financial advice.
- Source citations with every answer.
- No conversation history stored between sessions (privacy).
- `family_office` backend already active; no new backend required.
- A sixth navigation section requires a phase gate approval before implementation
  (the "exactly five sections" constraint in ADR-001 applies to v1 only).

**Do not begin Phase 5 work without explicit project sponsor approval and a new ADR
addressing the navigation constraint.**

---

## 6. Risk Register

| Risk | Probability | Impact | Mitigation |
| --- | --- | --- | --- |
| `pp-security-master` API contract unstable (alpha) | High | Medium | Cache layer absorbs failures as staleness (ADR-003); Phase 3 integrates this backend last, after the cache pattern is proven in Phases 1-2 |
| `family_office` document proxy URLs not stable | Medium | High | Confirm URL format and TTL with backend team before Phase 2 begins; treat short-lived URLs as a blocking dependency |
| Cloudflare Zero Trust setup delay | Low | High | Begin CF configuration in Phase 0 Week 1; it blocks every subsequent phase; escalate immediately if CF policy is not in place by end of Week 1 |
| Tablet layout issues with Chart.js | Medium | Low | Validate chart rendering at 1024x768 during Phase 3; Chart.js is responsive by default; allocate 3 hours for E2E chart validation |
| Backend API contract disagreements | Medium | Medium | Publish required endpoint shapes (Tech Spec Section 4) to backend teams before Phase 1; treat unconfirmed contracts as Phase blockers |
| SQLite WAL write contention | Low | Medium | `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` initialized at startup (ADR-003); aiosqlite for async reads; synchronous writes only in APScheduler context |
| APScheduler scheduler failure causing all data to go stale | Low | High | Monitor scheduler health; surface a "data refresh paused" admin alert when no refresh has run within the expected window; log all scheduler runs to `refresh_log` |

---

## 7. Success Metrics

From the Project Vision, the portal is successful when:

| Metric | Current State | Target |
| --- | --- | --- |
| Document retrieval | Requires a phone call | Under 2 minutes, unassisted |
| Net worth check | Requires advisor contact | Self-service, no calls |
| Entity compliance visibility | Requires navigating professional tools | At-a-glance from Home section |
| System reliability | N/A (no portal) | 2+ weeks without manual intervention or support request |
| User satisfaction | N/A | Both parents describe portal as easy to use without prompting |

Performance targets (from Tech Spec Section 7):

| Metric | Target |
| --- | --- |
| Page render (from cache) | Under 1 second |
| Document list render (up to 500 documents) | Under 1 second |
| HTMX search response | Under 500 ms |
| Refresh scheduler completion per service | Under 60 seconds |

---

## 8. Definition of Done

A feature is complete when all of the following are true:

- Code reviewed and typed: BasedPyright strict passes with zero errors.
- Tests written and passing: Ruff clean; pytest passing; coverage gates met.
- Staleness and empty states handled: not just the happy path.
- Template renders correctly at 1024x768 tablet viewport.
- No linting errors: `ruff check` clean.
- Merged to `main` with a conventional commit (signed, following Conventional Commits spec).
- Pre-commit hooks pass: `pre-commit run --all-files` with zero failures.
- No em-dashes in any committed text.
- RAD markers present: `#CRITICAL`, `#ASSUME`, `#EDGE` tags with paired `#VERIFY` for any
  production-risk assumption introduced in the feature.

---

## 9. Phase 0 Checklist

Immediate environment setup tasks to begin Phase 0:

- [ ] Create and switch to `chore/phase-0-foundation` branch
- [ ] Verify Python 3.12 is available: `python3.12 --version`
- [ ] Install UV: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] Initialize `pyproject.toml` with project metadata and dependency groups
- [ ] Configure Ruff (linter + formatter, 88-char line length) in `pyproject.toml`
- [ ] Configure BasedPyright in strict mode (`pyrightconfig.json` or `pyproject.toml`)
- [ ] Install and configure pre-commit (including `no-em-dash` hook)
- [ ] Run `pre-commit install` to activate hooks
- [ ] Create `templates/pages/` and `templates/partials/` directory structure
- [ ] Download and vendor HTMX v2 to `static/htmx.min.js`
- [ ] Download and vendor Chart.js v4 to `static/chart.min.js`
- [ ] Set up Tailwind CSS CLI build (no Node runtime)
- [ ] Create `.env.example` with all nine required env vars
- [ ] Initialize SQLite schema with all six tables and WAL mode pragmas
- [ ] Scaffold five empty-state route handlers and templates (Home, Documents, Finances,
  Portfolio, Entities)
- [ ] Implement CF JWT middleware (validate header, signature, `aud` claim, role mapping)
- [ ] Write unit tests for CF JWT middleware to 95% coverage
- [ ] Create `Dockerfile` and `docker-compose.yml`
- [ ] Configure GitHub Actions CI workflow (lint, type-check, test, Docker build)
- [ ] Confirm Cloudflare Zero Trust access policy is in place for family email addresses
  **#CRITICAL**
- [ ] Verify all backend teams have received the required endpoint shapes from
  Tech Spec Section 4

---

*This plan is generated from and traceable to:*

- *[Project Vision and Scope](docs/planning/project-vision.md) v1.0*
- *[Technical Implementation Spec](docs/planning/tech-spec.md) v1.0*
- *[Development Roadmap](docs/planning/roadmap.md) (2026-05-06)*
- *[ADR-001: Frontend Rendering Architecture](docs/architecture/adr/adr-001-frontend-rendering-architecture.md)*
- *[ADR-002: Authentication via Cloudflare Zero Trust](docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md)*
- *[ADR-003: Backend Data Aggregation Pattern](docs/architecture/adr/adr-003-backend-data-aggregation.md)*
