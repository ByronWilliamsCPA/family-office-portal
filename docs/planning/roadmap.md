# Development Roadmap: Family Office Estate Portal

**Note (2026-09-28)**: Documents now come from llc-manager. Where this document
says otherwise, `CLAUDE.md` takes precedence.

> **Status**: Active | **Updated**: 2026-11-04

## TL;DR

Most of the code for a first release is built and merged: the sign-in check, the
five sections, balance intake and totals, document browsing with inline preview
and download, document search, and the question panel. The owner decided on
2026-11-03 that the initial release includes all of it: Home, Documents with
search, Finances, Portfolio, Entities, and the question panel for everyone.
What stands between that code and a portal the two primary users can open on a
tablet is deployment (including the search stack, the model route and the
Portfolio backends), checks against real data and real hardware, and a short
list of gaps. The first wave of repository work merged on 2026-11-04: the
Portfolio refresh jobs are scheduled, the question panel has a per-user rate
limit, every page has resilience tests, a browser suite checks every page at
tablet size, and the stale documentation was corrected. What is left in this
repository is the entity status labels (A5, blocked on B3) and the release
housekeeping (A9). The operations runbook (A8) merged the same day. The section
"Path to the initial release" lists everything in order.

Phase numbers are stable identifiers, not a sequence. Phases 0 to 3 are built in
code; Portfolio still needs its backends connected. Phase 4 (resilience and
polish) is mostly built: what remains is the status labels, the release
housekeeping and the checks on the real tablet.

## Progress log

| Date | Pull request | What merged |
| --- | --- | --- |
| 2026-11-04 | #70 | Roadmap rewritten to the code, with the path to the initial release |
| 2026-11-04 | #71 | `.env.example` listing every setting by name, with a guard test (A1) |
| 2026-11-04 | #72 | README: chat, search and indexer settings, proxy example fixed (A2) |
| 2026-11-04 | #73 | `py` vulnerability entry resolved; ignore flags removed from both security workflows (A4) |
| 2026-11-04 | #74 | Holdings and positions refresh jobs scheduled; Portfolio empty-state sentence fixed (A11) |
| 2026-11-04 | #75 | Page-resilience tests, 45 of them: stale cache and failing backend on every page (A6) |
| 2026-11-04 | #76 | Documentation drift fixed in the tech spec, the plan's cadences and a docstring (A3) |
| 2026-11-04 | #77 | Per-user question rate limit and one question in flight per user (A10) |
| 2026-11-04 | #78 | Playwright tablet and accessibility suite, 11 templates raised to 44 pixel tap targets (A7) |
| 2026-11-04 | #79 | Operations runbook in `docs/operations/runbook.md` (A8) |

## Where the code stands (2026-11-04)

| Area | State | Remaining |
| --- | --- | --- |
| Sign-in check (ADR-005) | Built; fails closed; one key-authenticated exemption for the balance intake | Decode a real header from the deployed proxy |
| Home | Built: balances, trend, sources, coming up, recent documents, question panel | B3 (llc-manager connected); B4 (collector delivery) |
| Documents | Built: folders, name search, inline preview, download through the file proxy (ADR-007), confidential documents hidden from Viewers | B3 (llc-manager connected); open a PDF in the tablet's browser; time a large file |
| Finances | Built on delivered balances: totals, per owner, cash, 90-day trend, manual marks page for admins | Totals exclude home, vehicles and loans (stated on the page); real collector delivery |
| Entities | List and detail built; the "Next date" column is blank for every entity today because llc-manager returns no `next_date` (verified in its schema, 2026-11-04) | Green, yellow and red status and "due within 60 days" labels, which need llc-manager backend work first (A5) |
| Portfolio | Built: holdings table from cache only; refresh jobs scheduled every 2 hours; in the initial release | B7 (backends connected); C9; no performance or sector charts |
| Document search and indexer (ADR-006, ADR-008) | Built; off until its settings exist; in the initial release | Search stack deployed (B5) |
| Question panel (ADR-009) | Built; off unless configured; admins only by default; in the initial release for everyone; per-user rate limit and one question at a time (6 a minute, 60 an hour by default) | Search (B5), model route and instructions file (B6), the real check of the model-down message (C8) |
| Image and CI | CI builds, smoke-tests, signs and pushes an image from `main` | No deployment definition lives in this repository |
| Tests | Unit, integration and fuzz tests; coverage gate in CI; page-resilience tests for every page; a Playwright suite at 1024x768 with accessibility checks, opt-in locally and in a CI job that is not a required check | Checks on the real tablet (C5); make the browser job required if wanted |

## Milestones

| Milestone | Status | Notes |
| --- | --- | --- |
| M0: Dev environment and auth shell | Code done | Needs the proxy and identity provider wiring in homelab-infra and a real header check |
| M1: Entities section live | Partly done | List and detail work; status labels missing; needs llc-manager connected |
| M2: Documents section live | Code done | Needs llc-manager connected, the search stack (B5) and the real-file and real-search checks below |
| M3: Finances and Portfolio live | Finances code done; Portfolio code done, jobs scheduled (A11, 2026-11-04) | Finances needs the collector delivering (B4); Portfolio **at risk**: the two backends do not serve what the jobs call (B7, see "Findings of 2026-11-04") |
| M3a: Question panel live for everyone | Code done, rate limit included (A10, 2026-11-04) | Needs search (B5), the model route (B6) and the panel checks below |
| M4: Initial release available | Open | See the path below |
| M5: Production-ready (two weeks unattended) | Open | Follows M4 |

---

## Initial release

**Scope confirmed by the owner on 2026-11-03.** The initial release is the portal
two tablet users can open at an address under `*.williamshome.family`, sign in
to, and use without help for the jobs they care about most:

- Home with account totals, the most recent documents and the question panel
- Documents: find a document by folder, by name or by what it says, and open or
  download it
- Finances: account totals from delivered balances
- Portfolio: holdings and crypto positions from their backends
- Entities: list and detail
- The question panel, available to every signed-in user (`CHAT_ENABLED_FOR=all`)

Two dependencies follow from this scope:

- The question panel cannot work without document search: with search off, its
  answer says so. B5 is therefore on the critical path for both.
- Portfolio shows data only when `pp-security-master` and `xero_crypto` are
  connected (B7); the refresh jobs are scheduled (A11, done). If a backend is
  down, the section shows its last cached figures with a "last updated" label,
  never an error. **Portfolio is at risk for the initial release (2026-11-04):**
  read from source, `pp-security-master` serves no HTTP API and `xero_crypto`
  serves no positions endpoint, so the jobs have nothing to call until the owner
  chooses a way forward (see B7).

Still outside the initial release:

- Portfolio performance and sector charts
- Net worth that includes home, vehicles and loans (the Finances page says what
  is left out)
- A change-from-last-month indicator

## Path to the initial release

### A. Work in this repository

Each item is one small pull request.

| ID | Work | Why it matters |
| --- | --- | --- |
| A1 | **Done (#71).** `.env.example` lists every setting by name with its default; a guard test fails when a setting is missing from it | The README tells readers to copy it |
| A2 | **Done (#72).** README covers the question panel, search and indexer settings and the manual marks page, and its proxy example now carries the one-worker flag and a placeholder for the proxy network range instead of a fixed narrower range. The Dockerfile still ships a broader private range with the note to narrow it once the proxy network range is pinned (B2) | A wrong proxy range misreads client addresses; a second worker runs every scheduled job twice |
| A3 | **Done (#76 and #74).** The tech spec no longer names `family_office` (documents come from llc-manager; the four backends are llc-manager, pp-security-master, xero_crypto and data-ingestor), the stale "stubs at Phase 0" docstring is corrected, and the plan's cadences state the 2 hour interval for holdings and positions | Plans that disagree with the code mislead the next reader |
| A4 | **Done (#73).** The advisory behind the `py` entry (PYSEC-2022-42969) was withdrawn, so the audit no longer reports it against the locked version. The ignore flags are removed from both security workflows and the entry in `docs/known-vulnerabilities.md` is marked resolved, so no overdue entry remains to trip the release gate | A stale entry past its reassessment date trips the release gate |
| A5 | **Open, blocked on llc-manager backend work (verified 2026-11-04).** Entity status labels: colour plus plain-English text, and "due within 60 days" | Phase 1 acceptance criterion. llc-manager's API serves no compliance fields: they exist only in its database models, with no route that returns them. See "Findings of 2026-11-04" for the fields available today and the ones A5 needs |
| A6 | **Done (#75).** 45 page tests: each page renders with the cache stale or its backend returning 500 and shows the "last updated" label, never an error page | Phase 4 criterion |
| A7 | **Done except two parts (#78).** Playwright is a dev-only extra. The suite (`-m e2e`, `tests/e2e/`) checks every page at 1024x768 (layout, text clipping, headings, labels, tap targets of 44 pixels, JavaScript-off readability, the back button, the two-click rule, axe-core accessibility), and 11 templates were raised to 44 pixel tap targets. Left: the CI job (`e2e.yml`) is not a required check, and 1366x768 and the real tablet are still checked by hand (C5) | Phase 4 criterion |
| A8 | **Done (#79).** Deployment runbook at `docs/operations/runbook.md`: required settings, one worker, timezone, secrets by name, backup and restore of the SQLite file (the daily balance history is not a cache), scheduling the indexer, key rotation, how to read the admin refresh status. Written from the code and the Dockerfile; the backup, restore and scheduling examples have not been run on the real deployment (its "Not yet proven" list stays open) | The runbook is only as good as its first real run |
| A9 | First release housekeeping: move `[Unreleased]` in the changelog under 0.1.0, tag, and confirm the image tag the deployment pins | Nothing is tagged yet |
| A10 | **Done (#77).** Per-user rate limit for the question panel (`CHAT_RATE_LIMIT_PER_MINUTE` 6 and `CHAT_RATE_LIMIT_PER_HOUR` 60 by default) and one question in flight per user, recorded in ADR-009 ("Rate limiting" and "Accepted limits"). The model-down message is a plain sentence and is covered by tests; the check with the real service stopped stays in C8 | Required: the panel is on for everyone (O2) |
| A11 | **Done (#74).** `refresh_holdings` and `refresh_positions` are in `JOBS` on a 2 hour interval, and the Portfolio empty-state sentence is fixed | Required: Portfolio is in the release (O4) |

A1 to A4, A6, A7, A8, A10 and A11 are done. A5 waits for llc-manager backend
work (B3 and the findings below), and A9 comes last.

### B. Work outside this repository

| ID | Work | Owner area |
| --- | --- | --- |
| B1 | Identity provider application, the `fo-admin` and `fo-viewer` groups, and the forward-auth route in front of the portal. The definitions are already authored in the infrastructure repository (verified by reading it, 2026-11-04); what remains is mostly manual work by the owner on the live systems, which this repository cannot see | homelab-infra |
| B2 | Deploy the image: SQLite volume, `DISPLAY_TIMEZONE`, the sign-in secret, the intake key, one worker, trusted proxy range, and a name under `*.williamshome.family`. The stack definition is already authored in the infrastructure repository and deployment status is not verified from this repository. Gaps found by reading it: the proxy network is created by hand with no pinned subnet and the portal stack passes a broad Docker private range as the trusted proxy range, so the range cannot be narrowed until the owner pins the subnet; the real portal hostname differs from the roadmap's example and needs owner confirmation | homelab-infra |
| B3 | llc-manager reachable from the portal with a URL and key; confirm the entity compliance fields and the document type strings it returns | llc-manager, homelab-infra |
| B4 | The balance collector delivers to the portal through a separate internal route that requires a client certificate and accepts only `POST` on the exact intake path, with no forward auth (verified by reading the infrastructure repository, 2026-11-04; see the ADR-005 amendment of that date). The collector stack and the route are authored; whether they are live is not verified, and what remains is mostly manual work by the owner. Confirm each provider's delivery cadence against the 24-hour staleness threshold (see the risk register) | collector service, homelab-infra |
| B5 | Search stack: vector store, embedding service, chunk files, the indexer command on a schedule, and the tax-law command, both run from the same image and kept off the web process (ADR-006). Required for search and for the question panel. The definitions are authored in the infrastructure repository; deployment status is not verified from here. Gaps found: the vector store is off by default (its replica count defaults to zero until the owner sets it); the scheduled jobs run from a sidecar loop with a daily time and a trigger file, not from cron (see the runbook); and nothing yet writes the chunk sets: the data-ingestor stack that would is authored, but its own README says no image is published yet | homelab-infra, data_ingestor |
| B6 | Question panel: model route, the instructions file kept outside the repository, and `CHAT_ENABLED_FOR=all`. The model route and the portal wiring are authored in the infrastructure repository; whether the route is live is not verified from here, and what remains is mostly manual work by the owner | homelab-infra |
| B7 | **Blocked, owner decision needed (verified in code, 2026-11-04).** pp-security-master serves no HTTP API and xero_crypto serves no positions endpoint, so the Portfolio refresh jobs have nothing to call. Base URLs and keys alone will not fix this. Two ways forward are listed under "Findings of 2026-11-04"; neither is chosen | owner, pp-security-master, xero_crypto |

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
| C7 | Search: index one real document, then find it by what it says | Document search `#VERIFY` (ADR-008) |
| C8 | Question panel: one real question answered with a source; an out-of-scope question is refused; with the model service stopped, a plain-English message appears and no error page | Question panel `#VERIFY` (ADR-009) |
| C9 | Portfolio: the holdings and crypto figures match the source systems; a backend returning 500 shows the last figures with a "last updated" label (cannot start until B7 is unblocked) | Portfolio data `#VERIFY` |
| C10 | Entities and Home: confirm that individual and household entries do not appear on the Entities list or in Home's "coming up" list. llc-manager's entity types include both kinds (verified), and the portal applies no entity-type filter to those two pages (verified); whether llc-manager's list returns them is not verified | Entity cache `#VERIFY` |

### D. Decisions (made 2026-11-03)

| ID | Decision | Consequence |
| --- | --- | --- |
| O1 | Document search is in the initial release | B5 (vector store, embedding service, scheduled indexer, tax-law commands off the web process) is on the critical path; search is also a prerequisite of the question panel |
| O2 | The question panel is in the initial release, for everyone (`CHAT_ENABLED_FOR=all`) | B5 and B6 (model route, instructions file outside the repository) are required; A10 (per-user rate limiting) becomes required; verify the behaviour when the model service is down |
| O3 | Playwright is added as a dev-only dependency | A7 proceeds: tablet size 1024x768, back button, readability with JavaScript off, accessibility basics |
| O4 | Portfolio is in the initial release | A11 (schedule the holdings and positions jobs, fix the empty-state sentence) and B7 (connect pp-security-master and xero_crypto) are required; pp-security-master stays alpha, so its 500 responses must show as stale data, never as errors. **Update 2026-11-04:** B7 is blocked because the backends do not serve what the jobs call; Portfolio in the initial release is at risk until the owner chooses a way forward |

### Findings of 2026-11-04 (backends and infrastructure)

Two read-only investigations compared this repository with the backend
repositories and with the infrastructure repository. "Verified in code" means
read at the named source; "not verified against a running system" means nothing
was contacted. The backend heads read were the latest fetched on 2026-11-04.

**llc-manager entities (A5).** Verified in code. The entity list returns identity
and formation data: legal name, entity type, formation state, formation date,
active flag, addresses, bank account references and similar. It returns no
compliance fields. They exist only in database models that no route serves:
registrations (annual report due date, next renewal date, expiration date, good
standing flag, status), tax filings (due date, extended due date, filed date,
status) and registered agents. A5 needs a due date and a status per entity, so
llc-manager needs backend work first (a read-only compliance endpoint, or
computed fields on the entity). The status vocabulary the portal expects
(`current`, `due_soon`, `overdue`) has to be agreed with llc-manager's owner.
Deriving "due soon" in the portal from document dates is not an option: formation
document dates are not due dates. Until then the entity "Next date" column is blank
for every entity, and Home's "coming up" list is empty, because llc-manager returns
no `next_date`.

**Portfolio (B7, O4).** Verified in code. The holdings job calls
`/api/v1/portfolio/summary` on pp-security-master, which has no HTTP service at
its current head. The positions job calls `/api/v1/positions` on xero_crypto,
which has no such route (its routes sit under a different prefix) and no
`X-API-Key` scheme. Options for the owner, not chosen here:

1. Add the endpoints to the backends (and the key scheme to xero_crypto), then
   keep the jobs as they are.
2. Feed Portfolio some other way, for example through the balance collector, and
   change this repository's jobs to match.

**Deployment (B1, B2, B4, B5, B6).** The infrastructure repository already holds
the definitions for the portal stack, the identity application, the collector,
the model route and the intake route. Not verified against a running system:
that any of it is deployed or live. Remaining work is mostly manual steps by the
owner on the live systems (creating networks and host directories, setting
variables, copying configuration files, creating users). Gaps are listed in B2
and B5 above.

### Suggested order

1. A1 to A4 (done).
2. B1 to B3, then deploy (B2) with Documents, Entities and Home. This is the
   first point the primary users can open the portal.
3. A5 and A9 while the first deployment runs (A6, A7 and A8 are done); C1 to C3 as soon as it is up.
4. B5 (search stack) and B6 (model route); the rate limit (A10) is in place
   before the panel is switched on for everyone; then C7 and C8.
5. B7 (Portfolio backends; A11 is done); then C9.
6. B4 once the collector can deliver; then C4 and the Finances section is live.
7. C5 and C6 on the tablet; tag 0.1.0 (A9). That is the initial release.

Steps 4 to 6 are independent of each other and can run in parallel.

---

## Phase 0: Foundation

**Status: built.** Remaining: B1 and B2 (deployment).

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
- [x] `.env.example` documenting every setting (A1)
- [x] Docker image builds and runs; CI builds, smoke-tests and signs it
- [x] GitHub Actions CI: lint, type-check, test, image build

### Success criteria

- A signed-in admin can reach all five sections in a browser (needs B1, B2)
- The middleware returns 403 for any request without a valid signed token (bad
  signature, wrong `iss` or `aud`, expired, or no portal group) and for non-Admin
  users on `/admin/*` (covered by tests; real header still to check, C1)
- CI passes on `main`
- Local setup documented: clone to running portal in under 20 minutes (A1 and A2 are done; the timing is not measured)

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
  last cached data with a label (job tests and page tests exist)

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

**Status: Finances built on delivered balances; Portfolio built, jobs scheduled;
both in the initial release.**
Remaining: B4, C4 for Finances; B7 and C9 for Portfolio.

### Objective

Show a single account total and how it is made up, and the investment
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
- [x] Holdings and positions jobs on a 2 hour schedule (A11)
- [ ] Holdings and positions backends connected (B7)
- [ ] Portfolio performance chart with a market comparison and a sector chart
  (not in the initial release)
- [ ] Net worth that includes home, vehicles and loans (not planned for the first
  release; the page says what is left out)

### Success criteria

- The Finances total matches the source of truth to the cent (C4)
- Chart and tables are readable on the tablet at 1024x768 (C5)
- Holdings show plain-English names, not ticker symbols (with the Portfolio work)
- Portfolio figures match the source systems (C9)
- `pp-security-master` returning 500 shows stale data with a timestamp, not an
  error (job tests and page tests exist)

### US-003: Check the account total

**As a** primary user
**I want** to see a single number for our accounts, prominently
**So that** I can answer "how is our money doing?" without calling an advisor

**Acceptance criteria**:

- [x] The total appears on both Home and Finances
- [x] If data is stale, the total shows the cached value and the time
- [ ] A change-from-last-month indicator (not built; the 90-day trend is shown)

## Phase 4: Resilience and polish

**Status: mostly built.** Remaining: A5, A9, the plain-language session prompt, and C5 to C9.

### Objective

Harden the portal against degraded backend states, check every empty and error
state, add browser tests, and prepare production deployment.

### Deliverables

- [x] Degraded and empty states tested on every page (A6)
- [ ] Tablet layout verified at 1024x768 landscape and 1366x768 (1024x768 is automated in A7; 1366x768 and the real tablet remain, C5)
- [x] Accessibility check, at least WCAG 2.1 AA, for all pages (A7: axe-core in the browser suite; a person still looks on the tablet, C5)
- [x] Browser test suite (Playwright, dev-only) covering all sections, the back button and JavaScript-off readability (A7); the CI job is not a required check
- [x] Question panel rate limit and model-down behaviour (A10; the real-service check is C8)
- [x] Test coverage gate in CI (80% overall)
- [x] Admin refresh-status endpoint for all services
- [x] Production image built, smoke-tested and signed in CI
- [x] Deployment runbook (A8, #79; its commands are not yet run on the real deployment)
- [ ] Changelog under a version, tag, and the OpenSSF baseline files reviewed (A9)
  (LICENSE, SECURITY.md, CONTRIBUTING.md and README exist)
- [ ] Plain-language prompt when the session expires

### Success criteria

- Both primary users locate any document in under two minutes without help (C6)
- The portal renders correctly with any single backend returning 500 (A6, tested)
- All browser tests pass at the 1024x768 viewport (A7)
- CI passes including the coverage gate
- The system runs two weeks without manual intervention (M5)

## Phase 5: Ask

**Status: first version built, off unless configured; in the initial release for
everyone.**

A first version shipped as a question panel on the Home page, not a sixth
section (ADR-009). It answers from the cached balances and the internal document
search with a live chat model call and stores nothing. It is off for Viewers
unless `CHAT_ENABLED_FOR` allows it; the initial release sets
`CHAT_ENABLED_FOR=all`. The sixth-section design stays a later option.

Design requirements kept for later work:

- Clearly labelled as educational information, not legal or financial advice
- Source citations with every answer
- No conversation history stored between sessions (privacy)

The per-user rate limit (A10) is done. Remaining: B5 (search; without it the answer
says search is off) and B6 to turn it on, and the checks in C8.

---

## Risk register

| Risk | Probability | Impact | Mitigation |
| --- | --- | --- | --- |
| Deployment wiring (identity provider, proxy, trusted range) delays the first release | Medium | High | Do B1 and B2 first and check a real header (C1) before anything else |
| The collector cannot reach the portal directly, because a forward-auth route is in front of it | Medium | High | Deliver on the internal network to the exact intake path (B4) |
| `llc-manager` entity or document fields differ from what the pages expect | Medium | Medium | Confirm the fields and type strings (B3) before relying on A5 |
| `pp-security-master` API unstable (alpha) | High | Medium | Portfolio is in the initial release; the cache absorbs failures and the section shows stale data with a "last updated" label (A6 tests it, #75). Superseded in part by the next row |
| Portfolio has no data source: pp-security-master serves no HTTP API and xero_crypto serves no positions endpoint (verified in code, 2026-11-04) | High | High | The owner chooses a way forward (B7). Until then the section shows no figures; it still falls back to a "last updated" label rather than an error |
| The 24-hour balances staleness threshold has no margin against a daily collector: freshness is the oldest delivery among providers, so one provider that is late turns the whole Finances section stale (verified in the cache code; collector cadence from its documentation and stack defaults) | Medium | Medium | Owner option: raise the threshold (for example to 36 or 48 hours), or run the collector twice a day. Not decided |
| Question panel enabled for everyone before a rate limit exists | Low | Medium | Mitigated: the per-user limit and one question at a time are in code (A10, #77). The limiter is in memory, so it assumes one worker, which the Dockerfile sets |
| Search stack not deployed | Medium | High | B5 is on the critical path; without it search is off and the question panel cannot answer from documents |
| PDF preview misbehaves in the tablet's browser | Medium | High | Test on the real tablet early (C2); download remains as the fallback |
| The SQLite file is lost and the daily balance history with it | Low | High | Backup and restore in the runbook (A8, #79; not yet run on the real deployment); the file is not a cache |
| Tablet layout problems that tests on a desktop never show | Medium | Medium | Browser checks at 1024x768 are built (A7, #78) but run in a non-required CI job; a look on the tablet is still needed (C5) |
| A provider stops delivering and its last rows stay | Low | Low | Accepted limitation: documented in the balance storage code; a retire action can be added if it happens |

## Definition of done

A feature is complete when:

- [ ] Code reviewed and typed (BasedPyright strict passes)
- [ ] Tests written and passing (Ruff, pytest)
- [ ] Staleness and empty states handled (not just the happy path)
- [ ] Template renders correctly at 1024x768 tablet viewport
- [ ] Behaviour when a backend or the model service is down is a plain-English message or a stale label, never an error page
- [ ] No linting errors (`ruff check` clean)
- [ ] Merged to `main` with a conventional commit

The initial release (M4) is done when:

- [ ] Every item in A1 to A11 and B1 to B7 is finished
- [ ] Checks C1 to C9 have passed on the deployed portal, with real data and the
  real tablet
- [ ] Both primary users can find a document, read the account total, see their
  holdings and ask a question without help
- [ ] `0.1.0` is tagged and the deployment pins that image tag (A9)

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
