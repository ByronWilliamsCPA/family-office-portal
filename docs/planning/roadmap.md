# Development Roadmap: Family Office Estate Portal

**Note (2026-09-28)**: Documents now come from llc-manager. Where this document
says otherwise, `CLAUDE.md` takes precedence.

> **Status**: Active | **Updated**: 2026-11-03

## TL;DR

Most of the code for a first release is built and merged: the sign-in check, the
five sections, balance intake and totals, document browsing with inline preview
and download, document search, and an optional question panel. What stands
between that code and a portal the two primary users can open on a tablet is
mostly deployment, checks against real data and real hardware, and a short list
of small gaps. The section "Path to the initial release" lists them in order.

Phase numbers are stable identifiers, not a sequence. Phases 0 to 2 are built in
code. Phase 3 is half built: Finances works on delivered balances, Portfolio
waits for a backend. Phase 4 (resilience and polish) has barely started and is
where most remaining repository work sits.

## Where the code stands (2026-11-03)

| Area | State | Remaining |
| --- | --- | --- |
| Sign-in check (ADR-005) | Built; fails closed; one key-authenticated exemption for the balance intake | Decode a real header from the deployed proxy |
| Home | Built: balances, trend, sources, coming up, recent documents, question panel | None for the initial release |
| Documents | Built: folders, name search, inline preview, download through the file proxy (ADR-007), confidential documents hidden from Viewers | Open a PDF in the tablet's browser; time a large file |
| Finances | Built on delivered balances: totals, per owner, cash, 90-day trend, manual marks page for admins | Totals exclude home, vehicles and loans (stated on the page); real collector delivery |
| Entities | List and detail built | Green, yellow and red status and "due within 60 days" labels |
| Portfolio | Holdings table from cache only | Refresh jobs exist but are not scheduled; no performance or sector charts |
| Document search and indexer (ADR-006, ADR-008) | Built; off until its settings exist | Search stack deployed, only if search is in the first release |
| Question panel (ADR-009) | Built; off unless configured; admins only by default | Model route and instructions file, only if the panel is in the first release |
| Image and CI | CI builds, smoke-tests, signs and pushes an image from `main` | No deployment definition lives in this repository |
| Tests | Unit, integration and fuzz tests; coverage gate in CI | No resilience page tests, no browser tests, no accessibility checks |

## Milestones

| Milestone | Status | Notes |
| --- | --- | --- |
| M0: Dev environment and auth shell | Code done | Needs the proxy and identity provider wiring in homelab-infra and a real header check |
| M1: Entities section live | Partly done | List and detail work; status labels missing; needs llc-manager connected |
| M2: Documents section live | Code done | Needs llc-manager connected and the real-file checks below |
| M3: Finances and Portfolio live | Finances code done; Portfolio deferred | Finances needs the collector delivering; Portfolio needs its backend |
| M4: Initial release available | Open | See the path below |
| M5: Production-ready (two weeks unattended) | Open | Follows M4 |

---

## Initial release

**Proposed scope, for the owner to confirm.** The initial release is the portal
two tablet users can open at an address under `*.williamshome.family`, sign in
to, and use without help for the four jobs they care about most:

- Home with account totals and the most recent documents
- Documents: find a document and open or download it
- Finances: account totals from delivered balances
- Entities: list and detail

Out of the initial release, shown as "Not connected yet" or left switched off:

- Portfolio holdings and performance (waits for its backend to serve data)
- Crypto positions as a separate figure (same reason)
- Document search and the question panel, unless the owner puts them in scope
  (see decisions O1 and O2 below)

## Path to the initial release

### A. Work in this repository

Each item is one small pull request.

| ID | Work | Why it matters |
| --- | --- | --- |
| A1 | Add `.env.example` listing every setting by name with its default | The README tells readers to copy it; the file does not exist |
| A2 | Update the README: question panel, search and indexer settings, manual marks page, and the trusted proxy range (the README and the Dockerfile disagree) | A wrong proxy range misreads client addresses |
| A3 | Fix documentation drift: document and holdings cadences in this roadmap and the tech spec, the old "family_office" documents row, and a Phase 0 docstring in the routes package | Plans that disagree with the code mislead the next reader |
| A4 | Close the `py` entry in `docs/known-vulnerabilities.md`; the audit of the locked dependencies reports nothing | A stale entry past its reassessment date trips the release gate |
| A5 | Entity status labels: colour plus plain-English text, and "due within 60 days" | Phase 1 acceptance criterion; also needs the compliance fields from llc-manager (see B3) |
| A6 | Resilience tests: each page renders with the cache stale or its backend returning 500, and shows the "last updated" label, never an error page | Phase 4 criterion; today only the refresh jobs are tested for this |
| A7 | Tablet and accessibility checks in a browser: render every page at 1024x768, run an accessibility scan, and keep the result in CI | Phase 4 criterion; the layout has never been viewed at tablet size |
| A8 | Deployment runbook in `docs/`: required settings, one worker, timezone, secrets by name, backup and restore of the SQLite file (the daily balance history is not a cache), key rotation, how to read the admin refresh status | No operations document exists |
| A9 | First release housekeeping: move `[Unreleased]` in the changelog under 0.1.0, tag, and confirm the image tag the deployment pins | Nothing is tagged yet |
| A10 | Chat rate limiting (ADR-009 defers it) | Only if Viewers get the question panel |

A1 to A4 are small and independent; do them first. A7 adds a browser-test
dependency, which is decision O3.

### B. Work outside this repository

| ID | Work | Owner area |
| --- | --- | --- |
| B1 | Identity provider application, the `fo-admin` and `fo-viewer` groups, and the forward-auth route in front of the portal | homelab-infra |
| B2 | Deploy the image: SQLite volume, `DISPLAY_TIMEZONE`, the sign-in secret, the intake key, one worker, trusted proxy range, and a name under `*.williamshome.family` | homelab-infra |
| B3 | llc-manager reachable from the portal with a URL and key; confirm the entity compliance fields and the document type strings it returns | llc-manager, homelab-infra |
| B4 | The balance collector delivers directly to the portal on the internal network (a forward-auth route in front would refuse it); confirm each provider's delivery cadence against the 24-hour staleness threshold | collector service, homelab-infra |
| B5 | Only if search is in scope: vector store, embedding service, chunk files, and the indexer command on a schedule | homelab-infra |
| B6 | Only if the question panel is in scope: model route, instructions file path, `CHAT_ENABLED_FOR` | homelab-infra |

### C. Checks that fakes cannot prove

These need the deployed portal, real data or the real tablet.

| ID | Check | Source of the open item |
| --- | --- | --- |
| C1 | A signed header from the real proxy decodes and maps to the right role | Authentik middleware `#VERIFY` |
| C2 | A PDF opens inline in the browser the tablet actually uses, and the download works without a new window | Document proxy `#VERIFY` |
| C3 | A 50 MiB file streams within the five-minute limit from the tablet | Document proxy `#VERIFY` |
| C4 | The Finances total matches the source of truth to the cent, and migration 2 runs on the real database | Balance intake `#VERIFY` |
| C5 | The page layout, the chart and the tables at 1024x768 on the tablet | Frontend conventions |
| C6 | Both primary users find any document in under two minutes without help | Phase 4 success criterion |

### D. Decisions for the owner (O1 to O4)

| ID | Decision | Recommendation |
| --- | --- | --- |
| O1 | Is document search in the initial release? | No. Ship name search and folders first; search needs the vector store and embedding service deployed (B5) |
| O2 | Is the question panel in the initial release, and for whom? | Leave it off or admin-only until the model route exists and the panel has been tried on real documents |
| O3 | Add a browser-test dependency (Playwright) for A7? | Yes, as a dev-only dependency; it is the only way to check the tablet layout in CI |
| O4 | Is Portfolio out of the initial release? | Yes, shown as "Not connected yet" until its backend serves data |

### Suggested order

1. A1 to A4 (small, no dependencies), and decisions O1 to O4.
2. B1 to B3, then deploy (B2) with Documents, Entities and Home. This is the
   first point the primary users can open the portal.
3. A5 to A9 while the first deployment runs; C1 to C3 as soon as it is up.
4. B4 once the collector can deliver; then C4 and the Finances section is live.
5. C5 and C6 on the tablet; tag 0.1.0 (A9). That is the initial release.
6. Search (B5) and the question panel (B6, A10) afterwards, if scoped in.

---

## Phase 0: Foundation

**Status: built.** Remaining: A1 (`.env.example`), B1 and B2 (deployment).

### Objective

Establish the development environment, project scaffold, CI pipeline, and Authentik
forward-auth integration (ADR-005). At the end of this phase, a signed-in admin
user can reach all five section shells.

### Deliverables

- [x] Project scaffold: `pyproject.toml`, UV, Ruff, BasedPyright, pre-commit
- [x] FastAPI app with Jinja2 templates and Tailwind CSS compiled at build time
- [x] HTMX loaded as static asset; Chart.js vendored
- [x] Authentik JWT middleware validating the signed `X-authentik-jwt` header on
  every non-public request
- [ ] Authentik forward auth configured in homelab-infra (B1)
- [x] SQLite database initialized with versioned migrations
- [x] All five navigation sections render
- [ ] `.env.example` documenting every setting (A1)
- [x] Docker image builds and runs; CI builds, smoke-tests and signs it
- [x] GitHub Actions CI: lint, type-check, test, image build

### Success criteria

- A signed-in admin can reach all five sections in a browser (needs B1, B2)
- The middleware returns 403 for any request without a valid signed token (bad
  signature, wrong `iss` or `aud`, expired, or no portal group) and for non-Admin
  users on `/admin/*` (covered by tests; real header still to check, C1)
- CI passes on `main`
- Local setup documented: clone to running portal in under 20 minutes (needs A1)

## Phase 1: Entities

**Status: mostly built.** Remaining: A5 (status labels), B3.

### Objective

Integrate `llc-manager` to populate the Entities section and the Home page's
compliance items. This was the first backend integration and proves the cache
pattern (ADR-003).

**Backend dependency**: `llc-manager` must expose `GET /api/v1/entities` returning
the entity list with compliance status and dates.

### Deliverables

- [x] APScheduler configured; `refresh_entities` job calling `llc-manager` every
  4 hours
- [x] Entities section: list and per-entity detail
- [ ] Green, yellow and red status labels with plain-English text (A5)
- [x] Home page "coming up" items
- [x] Staleness indicator when the cache is older than 8 hours
- [x] `refresh_log` populated after each refresh run
- [x] Admin refresh-status endpoint

### Success criteria

- Entities section shows correct status labels for all LLCs and trusts (A5, B3)
- Per-entity detail shows name, type, state, registered agent, and next key date
- Staleness label appears when `entities.fetched_at` is older than 8 hours
- `llc-manager` returning 500 does not crash the portal; the section shows the
  last cached data with a label (job tests exist; page tests are A6)

### US-001: View entity compliance at a glance

**As a** primary user
**I want** to see which LLCs and trusts are current and which need attention
**So that** I can tell at a glance whether any action is needed without calling anyone

**Acceptance criteria**:

- [ ] Each entity shows a colour-coded status indicator and plain-English label (A5)
- [ ] "Due within 60 days" entities show a clear label, not just a colour (A5)
- [x] No raw identifiers (EIN, state ID) visible to primary users in list view

## Phase 2: Documents

**Status: built.** Remaining: B3, C2, C3, C6.

### Objective

Populate the Documents section from `llc-manager` and link documents to entity
detail pages. Documents are the highest-frequency use case for primary users
("where is that document?"). The portal stores metadata only; files stream from
`llc-manager` on request (ADR-007).

### Deliverables

- [x] `refresh_documents` job calling `llc-manager` every 12 hours
- [x] Documents section: folders by category and by entity, name search (HTMX)
- [x] Inline PDF preview through the portal
- [x] File download through the portal
- [x] Confidential documents hidden from Viewers
- [x] Documents on the entity detail page and the most recent on Home
- [ ] Empty-category message checked on a real tablet (C5)

### Success criteria

- A primary user can find any document by category in two clicks or fewer (C6)
- Name search returns results without a full page reload
- PDF preview renders inline; the user does not leave the portal (C2)
- An empty category shows a friendly message, not a blank space

### US-002: Find and open an estate document

**As a** primary user
**I want** to browse documents by category and open a PDF without leaving the portal
**So that** I can answer "where is that document?" without calling anyone

**Acceptance criteria**:

- [ ] All seven categories are visible (Estate Planning, LLCs, Trusts, Tax Returns,
  Insurance, Personal records, Other); categories come from the backend data, so
  confirm the names it sends (B3)
- [ ] PDF opens inline with a visible download button on the tablet (C2)
- [ ] Download works on the tablet without triggering a new browser window (C2)

## Phase 3: Finances and Portfolio

**Status: Finances built on delivered balances; Portfolio deferred.**
Remaining: B4, C4 for Finances; the backend and a scheduled job for Portfolio.

### Objective

Show a single account total and how it is made up, and later the investment
holdings. Balances arrive from a collector through the key-authenticated intake
(ADR-003 and ADR-005 amendments of 2026-10-05); the portal never contacts the
upstream commercial systems. Holdings come from `pp-security-master` (alpha) and
crypto positions from `xero_crypto`, both read through the refresh jobs.

**Alpha risk**: `pp-security-master` may have API instability. The cache layer
(ADR-003) means instability shows as stale data, not user-visible errors.

### Deliverables

- [x] Balance intake, daily history and the 90-day trend
- [x] Finances section: total, per-owner totals, cash accounts, trend, with a
  table beside each chart
- [x] Home page total and breakdown
- [x] Manual marks page for admins
- [x] Staleness label judged per provider (24 hours)
- [x] `refresh_holdings` and `refresh_positions` functions, triggerable by an admin
- [ ] Holdings and positions jobs on a schedule (when the backends serve data)
- [ ] Portfolio performance chart with a market comparison and a sector chart
- [ ] Net worth that includes home, vehicles and loans (not planned for the first
  release; the page says what is left out)

### Success criteria

- The Finances total matches the source of truth to the cent (C4)
- Chart and tables are readable on the tablet at 1024x768 (C5)
- Holdings show plain-English names, not ticker symbols (with the Portfolio work)
- `pp-security-master` returning 500 shows stale data with a timestamp, not an
  error (job tests exist; page tests are A6)

### US-003: Check the account total

**As a** primary user
**I want** to see a single number for our accounts, prominently
**So that** I can answer "how is our money doing?" without calling an advisor

**Acceptance criteria**:

- [x] The total appears on both Home and Finances
- [x] If data is stale, the total shows the cached value and the time
- [ ] A change-from-last-month indicator (not built; the 90-day trend is shown)

## Phase 4: Resilience and polish

**Status: barely started.** Remaining: A5 to A9, C5, C6.

### Objective

Harden the portal against degraded backend states, check every empty and error
state, add browser tests, and prepare production deployment.

### Deliverables

- [ ] Degraded and empty states tested on every page (A6)
- [ ] Tablet layout verified at 1024x768 landscape and 1366x768 (A7, C5)
- [ ] Accessibility check, at least WCAG 2.1 AA, for all pages (A7)
- [ ] Browser test suite covering all sections in nominal, stale and empty states (A7)
- [x] Test coverage gate in CI (80% overall)
- [x] Admin refresh-status endpoint for all services
- [x] Production image built, smoke-tested and signed in CI
- [ ] Deployment runbook (A8)
- [ ] Changelog under a version, tag, and the OpenSSF baseline files reviewed (A9)
  (LICENSE, SECURITY.md, CONTRIBUTING.md and README exist)
- [ ] Plain-language prompt when the session expires

### Success criteria

- Both primary users locate any document in under two minutes without help (C6)
- The portal renders correctly with any single backend returning 500 (A6)
- All browser tests pass at the 1024x768 viewport
- CI passes including the coverage gate
- The system runs two weeks without manual intervention (M5)

## Phase 5: Ask

**Status: first version built, off unless configured.**

A first version shipped as a question panel on the Home page, not a sixth
section (ADR-009). It answers from the cached balances and the internal document
search with a live chat model call, stores nothing, and is off for Viewers unless
`CHAT_ENABLED_FOR` allows it. The sixth-section design stays a later option.

Design requirements kept for later work:

- Clearly labelled as educational information, not legal or financial advice
- Source citations with every answer
- No conversation history stored between sessions (privacy)

Remaining: B5 and B6 to turn it on, A10 before Viewers use it, and the decisions
O1 and O2.

---

## Risk register

| Risk | Probability | Impact | Mitigation |
| --- | --- | --- | --- |
| Deployment wiring (identity provider, proxy, trusted range) delays the first release | Medium | High | Do B1 and B2 first and check a real header (C1) before anything else |
| The collector cannot reach the portal directly, because a forward-auth route is in front of it | Medium | High | Deliver on the internal network to the exact intake path (B4) |
| `llc-manager` entity or document fields differ from what the pages expect | Medium | Medium | Confirm the fields and type strings (B3) before relying on A5 |
| `pp-security-master` API unstable (alpha) | High | Low | Portfolio is outside the initial release; the cache absorbs failures as staleness |
| PDF preview misbehaves in the tablet's browser | Medium | High | Test on the real tablet early (C2); download remains as the fallback |
| The SQLite file is lost and the daily balance history with it | Low | High | Backup and restore in the runbook (A8); the file is not a cache |
| Tablet layout problems that tests on a desktop never show | Medium | Medium | Browser checks at 1024x768 (A7) and a look on the tablet (C5) |
| A provider stops delivering and its last rows stay | Low | Low | Accepted limitation: documented in the balance storage code; a retire action can be added if it happens |

## Definition of done

A feature is complete when:

- [ ] Code reviewed and typed (BasedPyright strict passes)
- [ ] Tests written and passing (Ruff, pytest)
- [ ] Staleness and empty states handled (not just the happy path)
- [ ] Template renders correctly at 1024x768 tablet viewport
- [ ] No linting errors (`ruff check` clean)
- [ ] Merged to `main` with a conventional commit

## Related documents

- [Project Vision](./project-vision.md)
- [Technical Spec](./tech-spec.md)
- [ADR-001: Frontend Architecture](../architecture/adr/adr-001-frontend-rendering-architecture.md)
- [ADR-002: Authentication (superseded)](../architecture/adr/adr-002-authentication-cloudflare-zero-trust.md)
- [ADR-003: Backend Data Aggregation](../architecture/adr/adr-003-backend-data-aggregation.md)
- [ADR-005: Authentication, Authentik forward auth](../architecture/adr/adr-005-authentication-authentik-forward-auth.md)
- [ADR-006: Document indexer](../architecture/adr/adr-006-document-indexer.md)
- [ADR-007: Live document file proxy](../architecture/adr/adr-007-live-document-file-proxy.md)
- [ADR-008: Document search](../architecture/adr/adr-008-document-search.md)
- [ADR-009: Chat live model call](../architecture/adr/adr-009-chat-live-model-call.md)
