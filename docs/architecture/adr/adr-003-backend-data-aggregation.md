# ADR-003: Backend Data Aggregation Pattern

> **Status**: Accepted
> **Date**: 2026-05-06
> **Amended**: 2026-09-30 (optional, keyed backend connections; see "Amendment 2026-09-30" below)
> **Amended**: 2026-10-05 (balance intake writer; see "Amendment 2026-10-05" below)
> **Related**: [ADR-007](adr-007-live-document-file-proxy.md) records a bounded exception:
> document preview and download stream the file from llc-manager per request

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
| `pp-security-master` | Investment holdings, classifications, performance | Alpha `#ASSUME` (contract unstable, `#VERIFY` before Phase 1) |
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
| `llc-manager` | `GET /api/v1/entities` | Entity list with compliance status and dates |
| `pp-security-master` | `GET /api/v1/portfolio/summary` | Holdings, performance, sector allocation |
| `xero_crypto` | `GET /api/v1/positions` | Crypto positions and USD conversion |
| `family_office` | `GET /api/v1/documents` | Document metadata list |

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
- [Tech Spec](../../planning/tech-spec.md#3-data-model): SQLite cache schema
- [Project Vision](../../planning/project-vision.md): Resilience
  requirement driving this decision
- [Roadmap](../../planning/roadmap.md): Phase-by-phase backend integration order
- [ADR-006](./adr-006-document-indexer.md): Document indexer; the document-search
  carve-out from the SQLite-only read rule

## Amendment 2026-09-30: optional, keyed backend connections

> **Status**: Accepted
> **Amends**: the original text above, which is left intact for the record.
> Where the two differ, this amendment takes precedence.

### What changed

The original text assumed four backends that are all required at startup, and
named `family_office` as the fourth. Neither holds now:

- `family_office` is not a backend of the portal. Document metadata comes from
  `llc-manager` (`GET /api/v1/documents`), and no portal code ever read
  `BACKEND_FAMILY_OFFICE_URL`, so the variable is removed. The `family_office`
  row in the tables above, its refresh cadence, and its `GET /api/v1/documents`
  contract no longer apply.
- Backends are connected one at a time as they ship, so none is required to
  start the portal.

### Decision

Each backend is an optional pair of environment variables,
`BACKEND_<NAME>_URL` and `BACKEND_<NAME>_API_KEY`, for `LLC_MANAGER`,
`PP_SECURITY`, `XERO_CRYPTO` and `DATA_INGESTOR`. The `DATA_INGESTOR` pair is
configuration only for now: the portal validates it at startup, but no client
calls that service yet.

| Situation | Behavior |
| --- | --- |
| URL set, key unset, empty or whitespace | Startup fails; the error names the key variable |
| URL set, key set | Backend is connected |
| URL unset, key set | Backend is not connected; startup logs one info line |
| URL unset, key unset | Backend is not connected |

A backend that is not connected has no outbound traffic and no error state:

- Its scheduled refresh jobs log one line (`refresh_skipped_not_connected`),
  make no outbound call, and write nothing to `refresh_log`.
- Pages that show its data render a plain "Not connected yet" note instead of
  an error or an empty list.
- Cached rows from an earlier connection are not shown as current.

The refresh client can no longer send a request without its key. The jobs take
a `BackendConnection` (URL and key), which cannot be built with a blank value;
the `X-API-Key` header is always set from it. The earlier behavior of sending
the request with no key header when the key was unset is removed.

### Consequences of the amendment

- A deployment must set both variables of a pair, or neither. Configuration
  that sets only a URL, which used to start and send unauthenticated requests,
  now fails at startup.
- `BACKEND_FAMILY_OFFICE_URL` is ignored if still set; remove it from the
  deployment.
- Local template work needs no backend variables at all.
- #ASSUME each backend checks `X-API-Key`. #VERIFY with each backend before
  its URL and key are set in the deployment.

## Amendment 2026-10-05: the balance intake is the one HTTP writer

> **Status**: Accepted
> **Amends**: the original text and the 2026-09-30 amendment above, which are
> left intact for the record. Where they differ, this amendment takes
> precedence for account balances.

### What changed

Route handlers read SQLite only, and writes belong to the scheduled refresh
jobs. Account balances are delivered to the portal by a collector instead of
being fetched by it, so the portal cannot pull them on a schedule.

### Decision

- `POST /api/v1/balances` is the single HTTP handler allowed to write. This
  amendment does not change how page routes read data. Authentication for
  this route is described in the ADR-005 amendment of the same date.
- The write goes through the same process-wide write lock as the scheduler's
  writes, in a worker thread so the event loop is never blocked, in a single
  all-or-nothing transaction. Any failure inside the transaction rolls the
  whole delivery back, and a database failure answers 503 with a fixed
  message.
- Per-provider replace semantics: a provider is the part of an account
  identifier before its first colon. Each provider present in a delivery has
  its stored rows replaced by the delivered rows, and a provider absent from
  the delivery keeps its previous rows. Every stored row keeps its own
  delivery time (`fetched_at`), so staleness can be judged per provider. The
  balance pages on Home and Finances, and the admin refresh status for
  balances, judge the balances section by the oldest provider's latest
  delivery (the oldest stored `fetched_at`, since one delivery gives all of a
  provider's rows the same time), so one provider that stops reporting shows
  the existing "may be out of date" label.
- The same transaction upserts today's row per account into `balances_daily`,
  and removes today's rows for accounts that the delivery replaced away, so the
  day's total matches the headline total. History for earlier days is never
  deleted. A scheduled job also snapshots once at startup and then daily at
  midday in `DISPLAY_TIMEZONE`; a fixed local time, unlike a 24 hour
  interval, cannot skip a local date when daylight saving time changes.
- Amounts are decimal strings, stored as integer cents (rounded half to
  even) with their currency. At most 12 digits before the decimal point are
  accepted, so a full delivery of the largest value cannot overflow SQLite's
  64-bit sum.

### Consequences of the amendment

- There is no way yet to retire a provider that has stopped reporting: its last
  rows stay stored and keep counting in the totals and the trend, and the
  balances section and its admin refresh status stay labelled out of date.
  #ASSUME the owner will decide whether a retire action or an age limit is
  wanted. #VERIFY before any provider is switched off for good.
- The "route handlers never write" rule now has this one named exception, which
  is covered by tests for authentication, atomicity and the per-provider
  behavior.
