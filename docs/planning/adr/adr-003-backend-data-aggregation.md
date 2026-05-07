# ADR-003: Backend Data Aggregation Pattern

> **Status**: Accepted
> **Date**: 2026-05-06

## TL;DR

The portal will maintain a local read-through cache (SQLite) and call each backend service
on a scheduled refresh cadence, rather than fetching live data on every page request.
This decouples the portal's availability from the availability of any single backend.

## Context

### Problem

The portal aggregates data from four backend services at different maturity levels:

| Service | What it provides | Maturity |
| --- | --- | --- |
| `llc-manager` | LLC and trust entities, compliance dates, ownership | v0.1.0 (stable) |
| `pp-security-master` | Investment holdings, classifications, performance | Alpha |
| `xero_crypto` | Crypto portfolio positions and reconciliation | v1.0.0 (stable) |
| `family_office` | Tax and estate law knowledge base (future Q&A) | Active |

The primary users have near-zero tolerance for blank screens or errors. If `pp-security-master`
is down or slow (expected given its alpha status), the Finances and Portfolio sections must
still show something useful. At the same time, the portal must never connect directly to
upstream commercial systems -- Kubera, Portfolio Performance (desktop app), Box, or Google
Drive -- because those connections are owned and managed by the backend services.

### Constraints

- **Technical**: Four backend services; two are pre-stable; portal must degrade gracefully
  when any one is unavailable
- **Business**: Primary users must never see an unhandled error or blank section

### Significance

This decision shapes every section's resilience behavior and the entire data flow through
the portal. Switching from live-fetch to cached-fetch mid-project requires rewriting all
data access patterns.

## Decision

**We will use a local read-through cache (SQLite) populated by a scheduled background
refresher that calls each backend service independently, because this decouples page
rendering from backend availability and enables accurate staleness reporting.**

### Rationale

Live fetching (calling a backend on every page request) means one slow backend blocks the
entire page or section. A local cache means page renders are always fast: the portal
reads from SQLite, never from a remote service during a user request. Staleness
indicators ("last updated 3 hours ago") tell the user exactly what they're seeing
without blocking the page.

## Options Considered

### Option 1: Local Read-Through Cache + Scheduled Refresh ✓

**Pros**:

- Page renders always complete quickly regardless of backend availability
- Staleness is explicit and measurable (cache timestamp vs current time)
- Each backend can fail independently without affecting other sections
- Alpha-status backends (`pp-security-master`) can be refreshed on a conservative
  cadence without risk of cascading failures

**Cons**:

- Data is never real-time; there is always some lag between a backend update and the
  portal display (acceptable for this use case -- users check occasionally, not live)
- Additional local storage component (SQLite) to maintain

### Option 2: Live Fetch on Every Page Request

**Pros**:

- Data is always current

**Cons**:

- One slow backend blocks the entire page; a network timeout shows a blank section
  or unhandled error to the user
- No way to show "last updated" without a prior cached value
- Alpha-status backends make this unreliable from day one

### Option 3: Edge Cache (Cloudflare Cache Rules)

**Pros**:

- No server-side storage

**Cons**:

- Cache invalidation requires Cloudflare API calls from backend services (coupling)
- No backend-specific staleness granularity; one cache TTL for all data

## Consequences

### Positive

- Portal availability is independent of backend availability
- Staleness indicators are accurate (based on the cache timestamp, not a guess)
- `pp-security-master` alpha instability is contained to the Portfolio/Finances sections
  and surfaces as "stale data" rather than an error
- No direct connection from the portal to Kubera, Portfolio Performance, Box, or
  Google Drive -- all commercial system integrations stay in the backend services

### Trade-offs

- Refresh scheduler failure means all data goes stale; mitigation: monitor scheduler
  health and surface a "data refresh paused" admin alert when refresh has not run
  within expected window
- SQLite write contention during refresh is possible if refresh runs while a user
  is reading; mitigation: initialize the database with `PRAGMA journal_mode=WAL` and
  `PRAGMA busy_timeout=5000`. WAL mode allows multiple concurrent async readers
  (`aiosqlite`) while the synchronous refresh writer (APScheduler) commits within a
  standard transaction -- no staging tables or table-rename tricks required

### Technical Debt

- If real-time compliance alerts are ever required (e.g., push notification when an
  LLC filing deadline passes today), the polling model will need extension with a
  webhook or SSE endpoint -- design the cache schema to include a `notified_at`
  column on compliance deadline rows

## Implementation

### Components Affected

1. **Portal data layer**: All route handlers read from SQLite cache; no direct backend
   calls during user requests
2. **Refresh scheduler**: Background job (e.g., APScheduler or a cron-triggered
   FastAPI startup task) that calls each backend service on its own cadence:
   - `llc-manager`: every 4 hours (stable, compliance dates change infrequently)
   - `pp-security-master`: every 1 hour (market data; degraded state expected in alpha)
   - `xero_crypto`: every 2 hours (crypto prices; stable API)
   - `family_office`: on document add/update events (knowledge base, not time-series)
3. **Staleness display layer**: Every cached dataset records `fetched_at`; templates
   compare `fetched_at` to current time and render staleness label if > threshold

### Backend Service Contract

Each backend must expose an HTTP endpoint (not CLI or direct DB access) that returns
the portal's required data in JSON. Current assumptions:

| Service | Expected endpoint | Data returned |
| --- | --- | --- |
| `llc-manager` | `GET /api/entities` | Entity list with compliance status and dates |
| `pp-security-master` | `GET /api/portfolio/summary` | Holdings, performance, sector allocation |
| `xero_crypto` | `GET /api/positions` | Crypto positions and USD conversion |
| `family_office` | `GET /api/documents` | Document metadata list |

These endpoint contracts must be validated with each backend team before Phase 1 begins.

### Testing Strategy

- Unit: Cache read/write with fixture data; staleness threshold logic
- Integration: Refresh scheduler with mocked backend responses; confirm data lands in
  SQLite correctly and `fetched_at` is updated
- Resilience: Mock a backend returning 500; confirm that section shows stale data
  with appropriate label, not an error page

## Validation

### Success Criteria

- [ ] Portal pages render in < 1 second when all backend services are unreachable
  (serving from cache)
- [ ] Staleness label appears on any section whose data is older than the configured
  threshold
- [ ] `pp-security-master` returning 500 does not affect the Entities or Documents sections
- [ ] Admin view shows per-service refresh status and last successful refresh time

### Review Schedule

- Initial: End of Phase 1 (first backend integration complete)
- Ongoing: If staleness reports are consistently > 24 hours, revisit refresh cadence

## Related

- [ADR-001](./adr-001-frontend-rendering-architecture.md): Server-rendered templates
  read from the cache layer, which is populated by the refresher
- [Tech Spec](../tech-spec.md#3-data-model): SQLite cache schema
- [Project Vision](../project-vision.md#44-resilient-to-data-freshness): Resilience
  requirement driving this decision
- [Roadmap](../roadmap.md): Phase-by-phase backend integration order
