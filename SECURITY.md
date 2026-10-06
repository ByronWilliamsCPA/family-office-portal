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
  identity only from that header, verifies its HS256 signature with the
  proxy provider's client secret in `AUTHENTIK_JWT_SECRET` (HS256 is the
  only accepted algorithm), and requires `exp`, `iss` (`AUTHENTIK_ISSUER`)
  and `aud` (`AUTHENTIK_AUDIENCE`) with 10 s leeway for clock skew, so a
  token minted for another Authentik application, an expired token, an
  `alg: none` token, a token signed with any asymmetric algorithm, or one
  signed with a different secret is refused with 403. The plain `X-authentik-username`, `X-authentik-email` and
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
- **SSRF via backend service URLs.** The upstream backend URLs
  (`BACKEND_LLC_MANAGER_URL`, `BACKEND_PP_SECURITY_URL`,
  `BACKEND_XERO_CRYPTO_URL`, `BACKEND_DATA_INGESTOR_URL`) are fixed at
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
`/health`, `/static/` and `POST /api/v1/balances` (exact method and path; the
balance intake route checks its own key instead, see below): HS256 signature with the provider's client secret,
`exp`, `iss` and `aud`, and a non-empty identity claim. It maps the `groups` claim
to `Viewer` or `Admin`, requires `Admin` for `/admin/*`, and returns 403 on
any failure (fail closed). The Authentik and Traefik configuration belongs to
homelab-infra; ADR-005 records the contract the portal relies on. Viewers
cannot see documents marked confidential. No password-based auth, OAuth
flows, or session cookies are implemented in the portal, and none are
planned.

The application never writes to or directly contacts upstream commercial
systems. It writes only to its own SQLite cache: scheduled refresh jobs store
backend data there, and the balance intake route (`POST /api/v1/balances`)
stores account balances that a collector delivers. Page routes only read the
cache. The cache database is never exposed to the network.

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
repository. The portal holds two authentication credentials. The first is
`AUTHENTIK_JWT_SECRET`, the client secret of the portal's Authentik proxy
provider. It is injected into the portal's environment by the deployment
stack (homelab-infra), never committed, and it both signs and verifies the
`X-authentik-jwt` header, so anyone who holds it can mint a token the portal
accepts. Startup refuses a missing value, the stack's placeholder, a value
shorter than 32 characters, or one with leading or trailing whitespace, and
the error never echoes the value. `#CRITICAL`: the proxy provider must stay
single-application and its secret must never be shared with another
application or provider (ADR-005 amendment 2026-09-29). `#VERIFY`: in
Authentik, confirm no other application is bound to the portal's provider.
The `AUTHENTIK_ISSUER` and `AUTHENTIK_AUDIENCE` values are not secret.

The second is `BALANCE_INTAKE_API_KEY`, the shared key a collector sends in
the `X-API-Key` header to `POST /api/v1/balances`, the one route exempt from
the sign-in token check. It is optional: unset or blank disables the route,
which then answers 404. It is injected by the deployment stack, never
committed, and held only by the portal and the collector. Startup refuses a
key shorter than 32 characters, one that is not printable ASCII, or one equal
to `AUTHENTIK_JWT_SECRET`, and the error names the variable, never the value.
The route compares the key in constant time before it reads the body, answers
401 to a missing or wrong key, and never logs the key, submitted values,
account identifiers or balances. `#CRITICAL`: any container on the shared
Traefik network can reach the portal directly, so this key is the only
control on the route and it travels as plain HTTP inside that network
(ADR-005 amendment 2026-10-05). `#VERIFY`: confirm with homelab-infra that no
public route forwards `/api/v1/balances` and that only trusted containers
share the portal's network.

Rotation cadence:

- CI/CD tokens (Codecov, SonarCloud, any future backend API keys): reviewed
  and rotated quarterly, aligned with the same quarterly cadence used for
  unfixed-CVE reassessment above.
- Authentik proxy provider client secret (`AUTHENTIK_JWT_SECRET`): rotated
  in Authentik by homelab-infra, or immediately upon suspected compromise.
  There is no key-set pickup and no hot reload: the portal verifies with the
  value it read at startup, so rotation is two-phase. First the secret is
  changed in Authentik, which signs with the new value at once; then the
  portal is restarted with the new secret in its environment. Between those
  two steps every request gets 403 (fail closed), so do both in one
  maintenance window. Recreating the provider also issues a new client ID,
  so `AUTHENTIK_AUDIENCE` must be updated at the same time.
- Balance intake key (`BALANCE_INTAKE_API_KEY`): rotated quarterly, or
  immediately upon suspected compromise. The portal accepts one key at a
  time, so rotation is two-phase: set the new key in the portal's environment
  and restart it, then give the collector the same key. Between those two
  steps the collector's deliveries get 401 and change nothing, and the
  previously stored balances stay in place. Do both
  steps in one maintenance window, then confirm one delivery succeeds.
- Any secret is rotated immediately, outside the regular cadence, if exposure
  is suspected (e.g., accidental commit, leaked CI log, compromised
  contributor account).
