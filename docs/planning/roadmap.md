# Development Roadmap: Family Office Estate Portal

> **Status**: Active | **Updated**: 2026-05-06

## TL;DR

Five phases from scaffold to production-ready portal; Phase 1 integrates the most mature
backend (`llc-manager`) to prove the cache pattern before Phase 3 tackles the alpha-status
`pp-security-master`. Estimated 9-10 weeks to Phase 4 completion.

## Timeline Overview

```text
Phase 0: Foundation       ████░░░░░░░░░░░░░░░░ (1 week)   - Scaffold, auth, CI
Phase 1: Entities         ░░░░████████░░░░░░░░ (2 weeks)  - llc-manager integration
Phase 2: Documents        ░░░░░░░░░░░░████░░░░ (1.5 weeks)- family_office integration
Phase 3: Finances + Port. ░░░░░░░░░░░░░░░░████ (3 weeks)  - xero_crypto + pp-security-master
Phase 4: Polish           ░░░░░░░░░░░░░░░░░░░░ (2 weeks)  - Resilience, E2E, release
```

## Milestones

| Milestone | Target | Status | Dependencies |
| --- | --- | --- | --- |
| M0: Dev environment and auth shell | Week 1 | Planned | None |
| M1: Entities section live | Week 3 | Planned | M0, llc-manager API contract |
| M2: Documents section live | Week 4.5 | Planned | M1, family_office API contract |
| M3: Finances + Portfolio live | Week 7.5 | Planned | M2, xero_crypto + pp-security-master API contracts |
| M4: Portal production-ready | Week 9.5 | Planned | M3 |

---

## Phase 0: Foundation (Week 1)

### Objective

Establish the development environment, project scaffold, CI pipeline, and Cloudflare
Zero Trust integration. At the end of this phase, a logged-in admin user can reach all
five section shells (empty pages, correct navigation, no data yet).

### Deliverables

- [ ] Project scaffold: `pyproject.toml`, UV workspace, Ruff, BasedPyright, pre-commit
- [ ] FastAPI app with Jinja2 templates and Tailwind CSS compiled at build time
- [ ] HTMX loaded as static asset; Chart.js vendored
- [ ] Cloudflare Zero Trust configured; CF JWT middleware validating all requests
- [ ] SQLite database initialized with all cache tables (see [Tech Spec §3](./tech-spec.md#3-data-model))
- [ ] All five navigation sections render (empty state, no backend calls yet)
- [ ] `.env.example` documenting all required environment variables (see [Tech Spec §4](./tech-spec.md#4-api-endpoints-internal-portal-routes))
- [ ] Docker container builds and runs locally
- [ ] GitHub Actions CI: lint, type-check, test, Docker build

### Success Criteria

- Authenticated admin user can reach all five sections in a browser
- CF JWT middleware rejects requests without a valid Cloudflare Access token
- CI pipeline passes on `main` branch
- Local setup documented: clone → running portal in < 20 minutes

### Tasks

| Task | Est. Hours | Status |
| --- | --- | --- |
| Initialize pyproject.toml and UV workspace | 1 | Planned |
| Configure Ruff, BasedPyright, pre-commit | 2 | Planned |
| FastAPI app with Jinja2 template setup | 2 | Planned |
| Tailwind CSS build pipeline (no Node runtime) | 1 | Planned |
| CF Zero Trust Access policy setup | 2 | Planned |
| CF JWT validation middleware | 3 | Planned |
| SQLite schema migration (all 6 tables) | 2 | Planned |
| Navigation shell templates (5 sections, empty) | 3 | Planned |
| `.env.example` with all required env vars | 1 | Planned |
| Dockerfile and docker-compose | 2 | Planned |
| GitHub Actions CI workflow | 2 | Planned |

---

## Phase 1: Entities (Weeks 2-3)

### Objective

Integrate `llc-manager` to populate the Entities section and the home dashboard's
compliance widget. This is the first backend integration and proves the cache pattern
(ADR-003) before any alpha-status backend is attempted.

**Backend dependency**: `llc-manager` v0.1.0 must expose `GET /api/v1/entities`
returning entity list with compliance status and dates. Confirm API contract before
Phase 1 begins.

### Deliverables

- [ ] APScheduler configured; `refresh_entities` job calling `llc-manager` every 4 hours
- [ ] Entities section: status list (green/yellow/red), per-entity detail view
- [ ] Home dashboard: upcoming compliance dates widget (next 3 deadlines)
- [ ] Staleness indicator on Entities section when cache is > 8 hours old
- [ ] `refresh_log` populated with success/error status after each refresh run
- [ ] Admin refresh-status view showing `llc-manager` last-refresh time

### Success Criteria

- Entities section shows correct status badges for all LLCs and trusts from `llc-manager`
- Per-entity detail shows name, type, state, registered agent, and next key date
- Staleness label appears when `entities.fetched_at` is > 8 hours old
- `llc-manager` returning 500 does not crash the portal; Entities section shows
  last cached data with a staleness label
- Home dashboard shows 3 upcoming deadlines in plain English

### User Stories

#### US-001: View entity compliance at a glance

**As a** primary user
**I want** to see which LLCs and trusts are current and which need attention
**So that** I can tell at a glance whether any action is needed without calling anyone

**Acceptance Criteria**:

- [ ] Each entity shows a color-coded status indicator and plain-English label
- [ ] "Due within 60 days" entities show a clear label, not just a color
- [ ] No raw identifiers (EIN, state ID) visible to primary users in list view

**Tasks**:

| Task | Est. Hours | Status |
| --- | --- | --- |
| APScheduler setup and refresh_entities job | 4 | Planned |
| Entity cache reader function | 2 | Planned |
| Entities list template (status badges) | 3 | Planned |
| Entity detail template | 2 | Planned |
| Staleness check and display logic | 2 | Planned |
| Home dashboard compliance widget | 2 | Planned |
| Admin refresh-status view | 2 | Planned |
| Unit tests: cache reader, staleness checker | 2 | Planned |
| Integration tests: refresh job with mocked llc-manager | 3 | Planned |

### Dependencies

- Requires: Phase 0 complete; `llc-manager` HTTP API contract confirmed
- Blocks: Phase 2 (documents link to entity detail pages)

---

## Phase 2: Documents (Weeks 4-4.5)

### Objective

Integrate `family_office` document backend to populate the Documents section and link
documents to entity detail pages. Documents are the highest-frequency use case for
primary users ("where is [specific document]?").

**Backend dependency**: `family_office` must expose `GET /api/v1/documents` returning
document metadata (name, category, date, proxy URL). Confirm API contract before Phase 2.

**Note on document storage**: Documents themselves are not stored in the portal.
The portal stores only metadata and a proxy URL. Actual files live in the backend's
storage (Box integration handled by `family_office`; not a portal concern).

### Deliverables

- [ ] `refresh_documents` job calling `family_office` every 24 hours
- [ ] Documents section: folder view by category, name search (HTMX partial)
- [ ] Inline PDF preview (proxied through portal; no new tab)
- [ ] File download via portal proxy
- [ ] Empty state for empty categories
- [ ] Home dashboard: three most recently added/modified documents

### Success Criteria

- A primary user can find any document by category in two clicks or fewer
- Name search returns results without a full page reload (HTMX partial response)
- PDF preview renders inline; user does not leave the portal
- Empty category shows a friendly message, not a blank space
- Home dashboard recent-documents widget shows the three newest documents

### User Stories

#### US-002: Find and open an estate document

**As a** primary user
**I want** to browse documents by category and open a PDF without leaving the portal
**So that** I can answer "where is [document]?" without calling anyone

**Acceptance Criteria**:

- [ ] All six categories are visible (Estate Planning, LLCs, Trusts, Tax Returns,
  Insurance, Other)
- [ ] PDF opens inline with a visible download button
- [ ] Download works on tablet without triggering a new browser window

**Tasks**:

| Task | Est. Hours | Status |
| --- | --- | --- |
| refresh_documents scheduler job | 2 | Planned |
| Document cache reader (with category filter) | 2 | Planned |
| Documents folder template and category nav | 3 | Planned |
| HTMX name-search partial | 3 | Planned |
| PDF proxy route (stream from backend URL) | 3 | Planned |
| Inline preview and download template | 2 | Planned |
| Home dashboard recent-documents widget | 2 | Planned |
| Integration tests: document list, search, proxy | 3 | Planned |

### Dependencies

- Requires: Phase 1 complete; `family_office` HTTP API contract confirmed
- Blocks: Phase 3 (home dashboard needs documents widget to be complete)

---

## Phase 3: Finances and Portfolio (Weeks 5-7.5)

### Objective

Integrate `xero_crypto` (v1.0.0, stable) and `pp-security-master` (alpha) to populate
the Finances and Portfolio sections. These are integrated together because they both
feed into the Finances section's net worth total.

**Backend dependencies**:

- `xero_crypto` v1.0.0: `GET /api/v1/positions` returning crypto positions in USD.
  Kubera (upstream commercial system) is owned by `xero_crypto`; the portal does not
  contact Kubera directly.
- `pp-security-master` (alpha): `GET /api/v1/portfolio/summary` returning holdings and
  performance timeseries. Portfolio Performance (the desktop application upstream) is
  owned by `pp-security-master`; the portal does not contact it directly.

**Alpha risk**: `pp-security-master` may have API instability. The cache layer (ADR-003)
means instability shows as stale data, not user-visible errors. A 4-hour staleness
threshold is acceptable given alpha status.

### Deliverables

- [ ] `refresh_positions` job (xero_crypto, every 2 hours)
- [ ] `refresh_holdings` job (pp-security-master, every 1 hour)
- [ ] Finances section: net worth total, asset allocation chart, account list
- [ ] Portfolio section: performance chart vs S&P 500, holdings table, sector chart
- [ ] Net worth calculation: sum of `positions.usd_value` + `holdings` USD values
- [ ] All figures in USD; non-USD assets labeled as approximate
- [ ] Home dashboard: net worth summary widget + portfolio snapshot widget
- [ ] Staleness indicators for both sections (4-hour threshold)

### Success Criteria

- Net worth figure matches sum of known asset values (verifiable by admin)
- Holdings table shows plain English security names (e.g. "Apple Inc." not "AAPL")
- Performance chart renders with S&P 500 comparison on 1-month, 6-month, 1-year ranges
- `pp-security-master` returning 500 shows stale data with timestamp, not an error
- Sector allocation chart is readable on tablet at 1024x768

### User Stories

#### US-003: Check net worth

**As a** primary user
**I want** to see a single net worth number prominently displayed
**So that** I can answer "how is our money doing?" without calling an advisor

**Acceptance Criteria**:

- [ ] Net worth displays on both Home and Finances sections
- [ ] Trend indicator shows change from last month with a percent
- [ ] If data is stale, the net worth figure shows the cached value and the timestamp

**Tasks**:

| Task | Est. Hours | Status |
| --- | --- | --- |
| refresh_positions job (xero_crypto) | 3 | Planned |
| refresh_holdings job (pp-security-master) | 3 | Planned |
| Net worth aggregation function | 2 | Planned |
| Finances section template (chart + account list) | 4 | Planned |
| Portfolio section template (perf chart + holdings) | 4 | Planned |
| Chart.js integration for line and pie charts | 3 | Planned |
| Home dashboard net worth + portfolio widgets | 2 | Planned |
| Staleness indicators (Finances + Portfolio) | 2 | Planned |
| Integration tests: both refresh jobs | 4 | Planned |
| E2E: all chart sections render at tablet viewport | 3 | Planned |

### Dependencies

- Requires: Phase 2 complete; both API contracts confirmed
- Blocks: Phase 4

---

## Phase 4: Resilience and Polish (Weeks 8-9.5)

### Objective

Harden the portal against degraded backend states, validate all empty and error states,
complete E2E test coverage, and prepare for production deployment.

### Deliverables

- [ ] All six degraded states designed and tested (one per section + home)
- [ ] Tablet-first layout validated at 1024x768 landscape and 1366x768 desktop
- [ ] Accessibility audit: minimum WCAG 2.1 AA for all pages
- [ ] E2E test suite: Playwright covering all five sections in nominal, stale, and empty states
- [ ] Test coverage ≥ 80% line, ≥ 95% on critical paths
- [ ] Admin refresh-status view complete (all four services, last-run time, error count)
- [ ] Production Docker image validated; deployment runbook written
- [ ] CHANGELOG, SECURITY.md, CONTRIBUTING.md, README complete (OpenSSF baseline)

### Success Criteria

- Both primary users can locate any document in < 2 minutes without assistance
  (user acceptance test)
- Portal renders correctly with any single backend returning 500
- All Playwright E2E tests pass at 1024x768 viewport
- CI pipeline passes including coverage gate
- System operates without manual intervention for 2 weeks (production monitoring)

### Tasks

| Task | Est. Hours | Status |
| --- | --- | --- |
| Degraded state templates for all sections | 4 | Planned |
| Empty state templates for all sections | 3 | Planned |
| Session expiry plain-language prompt | 1 | Planned |
| Tablet layout review and fixes (1024x768) | 4 | Planned |
| WCAG 2.1 AA accessibility audit and fixes | 4 | Planned |
| Playwright E2E suite (all sections, all states) | 6 | Planned |
| Coverage gap analysis and unit test completion | 4 | Planned |
| Production Dockerfile and docker-compose.prod.yml | 2 | Planned |
| Deployment runbook | 2 | Planned |
| OpenSSF required files (README, SECURITY, etc.) | 2 | Planned |

---

## Phase 5: Ask Feature (Future -- Not in MVP)

Planned for a future release. Adds a sixth top-level section backed by the `family_office`
tax and estate law knowledge base. Users can ask plain-English questions and receive
sourced answers.

Key design requirements (captured here for future reference):

- Clearly labeled as educational information, not legal or financial advice
- Source citations with every answer
- No conversation history stored between sessions (privacy)
- `family_office` backend already active and provides the knowledge base

---

## Risk Register

| Risk | Probability | Impact | Mitigation |
| --- | --- | --- | --- |
| `pp-security-master` API contract unstable (alpha) | High | Medium | Cache layer absorbs failures as staleness; Phase 3 integrates this last |
| `family_office` document proxy URLs not stable | Medium | High | Confirm URL format and TTL with backend team before Phase 2 |
| Cloudflare Zero Trust setup delay | Low | High | Begin CF configuration in Phase 0 Week 1; it gates everything |
| Tablet layout issues with Chart.js | Medium | Low | Validate chart rendering at 1024x768 during Phase 3; Chart.js is responsive by default |
| Backend API contract disagreements | Medium | Medium | Publish required endpoint shapes (Tech Spec §4) to backend teams before Phase 1 |

## Definition of Done

A feature is complete when:

- [ ] Code reviewed and typed (BasedPyright strict passes)
- [ ] Tests written and passing (Ruff, pytest)
- [ ] Staleness and empty states handled (not just the happy path)
- [ ] Template renders correctly at 1024x768 tablet viewport
- [ ] No linting errors (`ruff check` clean)
- [ ] Merged to `main` with a conventional commit

## Related Documents

- [Project Vision](./project-vision.md)
- [Technical Spec](./tech-spec.md)
- [ADR-001: Frontend Architecture](../architecture/adr/adr-001-frontend-rendering-architecture.md)
- [ADR-002: Authentication](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md)
- [ADR-003: Backend Data Aggregation](../architecture/adr/adr-003-backend-data-aggregation.md)
