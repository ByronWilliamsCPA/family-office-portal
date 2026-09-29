# Security Policy

## Scope

This repository contains the Family Office Estate Portal, a private read-only
web application that aggregates financial and entity data from internal backend
services. It handles non-public financial information and operates behind
Authentik forward-auth access controls.

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

- **Forged or foreign identity token.** Authentik forward auth behind Traefik
  authenticates users and forwards a signed `X-authentik-jwt` header
  (ADR-005, which supersedes ADR-002). `app/middleware/authentik.py` takes
  identity only from that header, verifies its RS256 signature against the
  https JWKS at `AUTHENTIK_JWKS_URL`, and requires `exp`, `iss`
  (`AUTHENTIK_ISSUER`) and `aud` (`AUTHENTIK_AUDIENCE`) with zero leeway, so a
  token minted for another Authentik application, an expired token, an
  `alg: none` or HS256 token, or one signed by an unknown key is refused with
  403. The plain `X-authentik-username`, `X-authentik-email` and
  `X-authentik-groups` headers are never read, because anything that reaches
  the app could set them. `#CRITICAL`: the middleware must stay the only
  source of identity. `#VERIFY`: `tests/unit/test_middleware.py` covers each
  of these cases and must stay at or above 95% coverage.
- **Direct access that bypasses Traefik.** The signed-JWT check means a
  request that skips Traefik still needs a valid token, but it could replay
  one captured within its lifetime. `#ASSUME`: homelab-infra publishes no host
  port for the portal and lets only Traefik reach it (ADR-005 contract item
  12). `#VERIFY`: from a host outside the Traefik network, a direct request to
  the portal's container address must fail to connect.
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

Authentik forward auth behind Traefik handles sign-in and sessions and
forwards a signed `X-authentik-jwt` header (ADR-005). The in-app
`AuthentikAuthMiddleware` validates that token on every request except
`/health` and `/static/`: RS256 signature against the provider's JWKS, `exp`,
`iss` and `aud`, and a non-empty identity claim. It maps the `groups` claim
to `Viewer` or `Admin`, requires `Admin` for `/admin/*`, and returns 403 on
any failure (fail closed). The Authentik and Traefik configuration belongs to
homelab-infra; ADR-005 records the contract the portal relies on. No
password-based auth, OAuth flows, or session cookies are implemented in the
portal, and none are planned.

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
secrets (CI tokens such as `CODECOV_TOKEN` and `SONAR_TOKEN`) are stored
exclusively in GitHub Actions repository secrets, never committed to the
repository. The portal needs no authentication credential of its own. The
`AUTHENTIK_JWKS_URL`, `AUTHENTIK_ISSUER` and `AUTHENTIK_AUDIENCE` values are
public, and the signing key stays inside Authentik, managed by homelab-infra.

Rotation cadence:

- CI/CD tokens (Codecov, SonarCloud, any future backend API keys): reviewed
  and rotated quarterly, aligned with the same quarterly cadence used for
  unfixed-CVE reassessment above.
- Authentik token signing key: rotated in Authentik by homelab-infra, or
  immediately upon suspected compromise. The portal picks up the new key from
  the JWKS on its next refetch (an unknown `kid` triggers one, rate-limited to
  once per 30 seconds), so rotation needs no portal change or restart.
- Any secret is rotated immediately, outside the regular cadence, if exposure
  is suspected (e.g., accidental commit, leaked CI log, compromised
  contributor account).
