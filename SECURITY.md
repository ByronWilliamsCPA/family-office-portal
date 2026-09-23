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
Estate Portal and the mitigation currently in place for each. It is kept
alongside the generic reporting process above (OpenSSF Best Practices
Badge / OSSF-010: a security policy must describe the project's actual
attack surface, not only how to file a report).

- **Cloudflare Access JWT `aud` bypass.** The CF JWT middleware (ADR-002)
  validates the JWT signature and the `aud` claim against
  `CF_ACCESS_APP_ID`. Skipping the `aud` check would let a token issued to a
  different app in the same Cloudflare tenant authenticate here; this is
  tracked as a `#CRITICAL` in `app/middleware/cloudflare_access.py` and
  covered by negative tests in `tests/unit/test_middleware.py`.
- **SQL injection via the SQLite read-through cache.** Route handlers and
  refresh jobs use raw SQL (no ORM) against the cache database. Every query
  is parameterized; string-built SQL is not permitted anywhere in
  `app/cache.py` or `app/db.py`.
- **Path/identifier injection via opaque cache keys.** `document_id` and
  `entity_id` path parameters are treated strictly as opaque cache lookup
  keys; they are never interpolated into filesystem paths, shell commands,
  or SQL strings. `tests/fuzz/test_route_input_fuzz.py` fuzzes these path
  parameters (Hypothesis) with adversarial input, including path-traversal
  sequences, to assert the app never raises an unhandled exception on
  malformed input.
- **SSRF via backend service URLs.** The four upstream backend URLs
  (`BACKEND_LLC_MANAGER_URL`, `BACKEND_PP_SECURITY_URL`,
  `BACKEND_XERO_CRYPTO_URL`, `BACKEND_FAMILY_OFFICE_URL`) are fixed at
  process startup from environment variables; no request path accepts a
  user-supplied URL that is then fetched server-side.
- **Sensitive data exposure to low-proficiency primary users.** Financial
  values, raw identifiers (EIN, state IDs, UUIDs), and document contents are
  restricted from list/detail views and from logs above INFO-level auth
  events (see `CLAUDE.md` "Logging" and "Frontend conventions").
- **Dependency vulnerabilities.** Any Python dependency can introduce a
  known CVE; see "Dependency Scanning" below for the mitigation.

## Security Architecture

Authentication is handled entirely by Cloudflare Zero Trust at the network
edge. The application validates Cloudflare Access JWTs on every non-static
request and maps the `email` claim to `Viewer` or `Admin` role. No
password-based auth, OAuth flows, or session cookies are implemented.

The application is read-only: it never writes to or directly contacts upstream
commercial systems. All backend data flows through an internal SQLite cache
populated by scheduled refresh jobs. The cache database is never exposed to
the network.

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
