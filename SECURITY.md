# Security Policy

## Scope

This repository contains the Family Office Estate Portal, a private read-only
web application that aggregates financial and entity data from internal backend
services. It handles non-public financial information and operates behind
Cloudflare Zero Trust access controls.

## Supported Versions

Only the current `main` branch is actively supported.

| Version | Supported |
| --- | --- |
| `main` | Yes |
| All others | No |

## Reporting a Vulnerability

Do not open a public GitHub issue for security vulnerabilities.

**Use GitHub Private Vulnerability Reporting (PVR):**
[https://github.com/ByronWilliamsCPA/family-office-portal/security/advisories/new](https://github.com/ByronWilliamsCPA/family-office-portal/security/advisories/new)

Include in your report:

- A description of the vulnerability
- Steps to reproduce or a proof-of-concept
- The potential impact (confidentiality, integrity, availability)
- Any suggested mitigations

You will receive an acknowledgment within 72 hours and a resolution update
within 14 days.

### Acknowledgement Policy

Reporters who follow this private disclosure process will be credited in the
published security advisory unless they explicitly request anonymity. We do
not currently operate a paid bug bounty program.

## Security Surface

This section names the repo-specific attack vectors for the Family Office
Estate Portal and the mitigation currently in place or planned for each. It
distinguishes what exists today, in Phase 0, from what Phase 1 adds, and is
kept alongside the generic reporting process above (OpenSSF Best Practices
Badge / OSSF-010: a security policy must describe the project's actual
attack surface, not only how to file a report).

- **Cloudflare Access JWT `aud` bypass.** Authentication happens at the
  Cloudflare Zero Trust network edge: Access issues the
  `CF-Access-JWT-Assertion` header and forwards only authenticated traffic to
  the application (ADR-002). At Phase 0, `app/middleware/cloudflare_access.py`
  is a pass-through stub: it does not itself verify the JWT signature or the
  `aud` claim, and `tests/unit/test_middleware.py` skips until the Phase 1
  validation API exists. Until Phase 1 ships, the application has no
  application-level check that a token issued to a different app in the same
  Cloudflare tenant is rejected; it relies solely on the edge configuration.
  `#CRITICAL`: implement signature and `aud` validation in
  `app/middleware/cloudflare_access.py` before any route depends on the
  `Viewer`/`Admin` role split. `#VERIFY`: a fixture token with a foreign `aud`
  must return 403 once Phase 1 validation lands, per the negative tests
  planned in `tests/unit/test_middleware.py`.
- **SQL injection via the SQLite read-through cache.** `app/cache.py` and
  `app/db.py` do not exist yet at Phase 0; no route handler queries SQLite.
  When Phase 1 introduces them, every query must be parameterized (no ORM,
  no string-built SQL), per `CLAUDE.md` "Data layer rules".
- **Path/identifier injection via opaque cache keys.** `document_id` and
  `entity_id` path parameters are intended to be treated strictly as opaque
  cache lookup keys, never interpolated into filesystem paths, shell
  commands, or SQL strings; the Phase 0 placeholder handlers already discard
  these values without using them. `tests/fuzz/test_route_input_fuzz.py`
  fuzzes these path parameters (Hypothesis) with adversarial input,
  including path-traversal sequences, to assert the app never raises an
  unhandled exception on malformed input at this pre-cache-lookup stage.
- **SSRF via backend service URLs.** The four upstream backend URLs
  (`BACKEND_LLC_MANAGER_URL`, `BACKEND_PP_SECURITY_URL`,
  `BACKEND_XERO_CRYPTO_URL`, `BACKEND_FAMILY_OFFICE_URL`) are fixed at
  process startup from environment variables; no request path accepts a
  user-supplied URL that is then fetched server-side.
- **Sensitive data exposure to low-proficiency primary users.** Financial
  values, raw identifiers (EIN, state IDs, UUIDs), and document contents must
  be restricted from list/detail views and from logs above INFO-level auth
  events once real data exists (see `CLAUDE.md` "Logging" and "Frontend
  conventions"). Phase 0 routes return placeholder content with no real
  financial data, so this control has nothing to protect yet; it becomes
  load-bearing starting Phase 1.
- **Dependency vulnerabilities.** Any Python dependency can introduce a
  known CVE; see "Dependency Scanning" below for the mitigation.

## Security Architecture

Authentication happens entirely at the Cloudflare Zero Trust network edge:
Access enforces identity and issues the `CF-Access-JWT-Assertion` header
before any request reaches the application (ADR-002). At Phase 0, the
in-app `CloudflareAccessMiddleware` is a pass-through stub: it does not
verify the JWT signature, the `aud` claim, or map the `email` claim to a
role; every request that reaches the app is accepted. Phase 1 replaces the
stub with the full validation pipeline described in "Authentication rules"
in `CLAUDE.md`. No password-based auth, OAuth flows, or session cookies are
implemented, and none are planned.

The application is designed to be read-only: it will never write to or
directly contact upstream commercial systems. Phase 1 routes all backend
data through an internal SQLite cache populated by scheduled refresh jobs;
at Phase 0, `app/cache.py` and `app/db.py` do not exist, and route handlers
return static placeholder content instead of cached data. The cache
database, once it exists, is never exposed to the network.

## Known Limitations

- The `pp-security-master` backend is alpha-status; its API contract is
  unstable. Stale data from this backend is surfaced with a timestamp
  rather than shown as an error.
- Content Security Policy headers beyond FastAPI defaults are not yet
  implemented. Header hardening is planned before production deployment.

## Dependency Scanning

Dependencies are scanned with `uv run pip-audit` before each release.
Known unfixed CVEs are documented in `docs/known-vulnerabilities.md` and
reviewed quarterly. No vulnerability older than 60 days may be left without
reassessment.

## Secret Management and Rotation

This repository holds no application secrets in source control. Runtime
secrets (CI tokens such as `CODECOV_TOKEN`, `SONAR_TOKEN`, and the Cloudflare
Access application credentials referenced by `CF_TEAM_DOMAIN` /
`CF_ACCESS_APP_ID`) are stored exclusively in GitHub Actions repository
secrets or the Cloudflare Zero Trust dashboard, never committed to the
repository.

Rotation cadence:

- CI/CD tokens (Codecov, SonarCloud, any future backend API keys): reviewed
  and rotated quarterly, aligned with the same quarterly cadence used for
  unfixed-CVE reassessment above.
- Cloudflare Access service credentials: rotated per Cloudflare's own
  recommended schedule, or immediately upon suspected compromise or
  contributor offboarding.
- Any secret is rotated immediately, outside the regular cadence, if exposure
  is suspected (e.g., accidental commit, leaked CI log, compromised
  contributor account).
