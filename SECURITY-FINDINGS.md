# Security Findings: OWASP Top 10 (2021) Review

> **Scope**: Pre-Phase-1 (Phase 0 Foundation) audit of the family-office-portal
> repository: configuration, GitHub Actions workflows, project docs, and the
> single application package init.
> **Reviewer**: Automated OWASP Top 10 (2021) review
> **Date**: 2026-05-15
> **Branch**: `claude/owasp-security-hardening-TAfUX`

## Executive Summary

The repository is in Phase 0 (Foundation). No route handlers, middleware,
database queries, HTTP clients, or template rendering code exist yet --
`app/` contains only an `__init__.py` docstring module. As a result, the
runtime application surface has nothing to attack today: there are no
endpoints to bypass authorization on, no query construction to inject into,
no session cookies to misconfigure, and no shell calls to sanitize.

The substantive review surface in Phase 0 is the **supply-chain / CI** layer
and the **configuration that governs Phase 1 code**. Both are in unusually
good shape:

- All GitHub Actions `uses:` references are pinned to 40-character commit
  SHAs (verified across all ten workflows).
- `step-security/harden-runner` (with `egress-policy: audit`) gates the
  large workflows (`ci.yml`, `coverage.yml`, `docs.yml`, `security-analysis.yml`,
  `sonarcloud.yml`).
- A multi-tool security scan (Bandit, pip-audit, OSV-Scanner,
  OWASP Dependency-Check, dependency-review, OpenSSF Scorecard, SonarCloud,
  REUSE) already runs on every PR or on a schedule.
- A pre-commit pipeline runs Bandit, detect-secrets, and TruffleHog locally.

This PR closes three small but real gaps and surfaces three forward-looking
Phase-1 commitments that future code must honor.

## Findings

### F-01 Bandit B101 skip allows `assert` in production code | **Medium**

- **File**: `pyproject.toml:113-115` (before fix)
- **Category**: A04 Insecure Design / A05 Security Misconfiguration
- **Issue**: `[tool.bandit].skips = ["B101"]` disabled the
  assert-in-production check. The accompanying comment claimed that
  "production code blocks via ruff S101", but the `S` (flake8-bandit) rule
  family is not in `[tool.ruff.lint].select`. The result: a future
  `app/` module could ship `assert user_role == "admin"` as a security
  check, and that assertion would be silently stripped under
  `python -O`. Bandit already excludes `tests/` via `exclude_dirs`, so
  reverting the skip costs nothing in tests.
- **Fix applied**: Removed `B101` from `[tool.bandit].skips`. Updated the
  comment to document why the check stays on for `app/`.

### F-02 CI workflow granted unused `pull-requests: write` and `checks: write` | **Low**

- **File**: `.github/workflows/ci.yml:15-18` (before fix)
- **Category**: A05 Security Misconfiguration / GitHub Actions least privilege
- **Issue**: The workflow-level `permissions:` block granted
  `pull-requests: write` and `checks: write` to every job. No step in any
  of the four jobs (`setup-optimized`, `test`, `quality-checks`,
  `ci-gate`) actually posts PR comments or writes check runs -- the
  declared permissions exceeded what was used. Excess `GITHUB_TOKEN`
  scope is a standard supply-chain hardening target (per OpenSSF
  Scorecard).
- **Fix applied**: Tightened workflow-level permissions to
  `contents: read` only. If a future step needs to write to PRs or
  checks, it should grant the specific permission at the job level.

### F-03 `dependency-review.yml` ran without `step-security/harden-runner` | **Low**

- **File**: `.github/workflows/dependency-review.yml:22-24` (before fix)
- **Category**: A05 Security Misconfiguration / Supply chain
- **Issue**: Every other major workflow in `.github/workflows/` starts
  with a `step-security/harden-runner` step in `egress-policy: audit`
  mode. The dependency-review workflow alone omitted it, creating an
  inconsistent egress-monitoring posture: outbound calls from this job
  would not be recorded.
- **Fix applied**: Added the standard pinned `harden-runner` step (same
  SHA used elsewhere in the repo) before the checkout.

### F-04 Tech spec example backend URL uses `http://` | **Low (informational)**

- **File**: `docs/planning/tech-spec.md:222`
- **Category**: A02 Cryptographic Failures (transport)
- **Issue**: The environment-variable table shows
  `BACKEND_LLC_MANAGER_URL` with the example `http://llc-manager:8000`.
  This is documentation of an internal service URL on a private Docker
  network (service name `llc-manager`, not externally resolvable). The
  Security section of the same spec (line 254) states "internal backend
  service calls over HTTPS or trusted private network", which permits
  plaintext on a trusted segment. **No production code path is affected
  yet** -- no HTTP client exists. Treat as a `#VERIFY` item for Phase 1
  deployment: confirm the actual production deployment either uses
  HTTPS end-to-end or runs all four backends on a network segment that
  rejects external traffic.
- **Recommended fix**: At Phase 1, either change the example to
  `https://...` or add an inline note that plaintext is acceptable only
  on a confirmed-isolated subnet. No code change required now.

### F-05 No application security headers will be set by default | **Medium (forward-looking)**

- **File**: `SECURITY.md:59-60` (acknowledged); will land in Phase 1 `app/main.py`
- **Category**: A05 Security Misconfiguration
- **Issue**: The project's own `SECURITY.md` already notes: "Content
  Security Policy headers beyond FastAPI defaults are not yet
  implemented. Header hardening is planned before production
  deployment." FastAPI/Starlette do **not** set `Content-Security-Policy`,
  `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`, or
  `Strict-Transport-Security` by default. Because the portal renders
  sensitive financial HTML, missing headers leave the app exposed to
  clickjacking, MIME-sniffing, and -- if Cloudflare ever stops adding
  HSTS at the edge -- downgrade attacks.
- **Recommended fix**: Phase 1 must add a Starlette middleware (or
  `starlette.middleware.trustedhost.TrustedHostMiddleware` plus a
  custom response-header middleware) that sets at minimum:
  `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'`
  (HTMX is vendored so no inline-script allowance is needed),
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin`,
  `Strict-Transport-Security: max-age=31536000; includeSubDomains`. No
  code change is possible in Phase 0 -- there is no `app/main.py` yet --
  but this finding must be addressed in the Phase 1 acceptance criteria.

### F-06 CF JWT `aud` claim validation is unimplemented | **Critical (forward-looking)**

- **File**: To be created at `app/middleware/` in Phase 1
- **Category**: A01 Broken Access Control / A07 Identification and
  Authentication Failures
- **Issue**: `CLAUDE.md` and `docs/architecture/adr/adr-002-...md` both
  flag this as a `#CRITICAL` requirement: when the CF JWT middleware
  is written, it MUST validate the `aud` claim against
  `CF_ACCESS_APP_ID`. Without it, tokens issued to any other
  application in the same Cloudflare tenant would be accepted. There
  is currently no code to fix, so this is a forward-looking gate.
- **Recommended fix**: When implementing
  `app/middleware/cf_jwt.py` in Phase 1, the validation order must be:
  (1) header present; (2) signature verifies against keys fetched from
  `https://<CF_TEAM_DOMAIN>/cdn-cgi/access/certs` and cached with TTL;
  (3) `aud` claim equals `CF_ACCESS_APP_ID`; (4) `exp` / `nbf` checked;
  (5) `email` claim mapped to `Viewer` or `Admin`. A failure at any
  step must return 403 with no information disclosure. Unit-test
  coverage must be 95 percent per `CLAUDE.md`.

## OWASP Top 10 (2021) Coverage Summary

| OWASP Item | Phase 0 Status | Phase 1 Gate |
| --- | --- | --- |
| A01 Broken Access Control | No routes exist; nothing to bypass | F-06 (CF JWT `aud`) |
| A02 Cryptographic Failures | No secrets in source. F-04 (http example) | Confirm internal TLS at deploy |
| A03 Injection | No SQL/shell code yet; CLAUDE.md mandates parameterized queries | Code review enforces |
| A04 Insecure Design | F-01 fixed (assert-in-production now blocked) | Threat model in Phase 1 |
| A05 Security Misconfiguration | F-02, F-03 fixed; F-05 forward-looking | Add security-headers middleware |
| A06 Vulnerable Components | pip-audit, OSV, OWASP-DC, dependency-review all wired | Maintain `known-vulnerabilities.md` |
| A07 AuthN Failures | No app-level auth (Cloudflare at edge per ADR-002) | F-06 |
| A08 Software/Data Integrity | All Actions pinned to SHA; harden-runner in audit mode | Maintain on every PR |
| A09 Logging Failures | structlog mandated; CLAUDE.md forbids logging financial values or emails beyond INFO auth events | Enforced via code review |
| A10 SSRF | No HTTP client yet; APScheduler refresher will use closed URL allowlist (env-configured) | Validate at Phase 1 |

## GitHub Actions Hardening Review

| Workflow | Pinned to SHA | `harden-runner` | Least-priv permissions |
| --- | --- | --- | --- |
| `ci.yml` | Yes | Yes (every job) | **Tightened in this PR** (was over-broad) |
| `coverage.yml` | Yes | Yes | Read-only |
| `dependency-review.yml` | Yes | **Added in this PR** | `contents: read, pull-requests: write` (required for PR comment) |
| `docs.yml` | Yes | Yes | `contents: read` |
| `pr-validation.yml` | Yes (reusable workflow) | (inherited) | Job-level explicit |
| `python-compatibility.yml` | Yes (reusable workflow) | (inherited) | `contents: read` |
| `reuse.yml` | Yes (reusable workflow) | (inherited) | `contents: read` |
| `scorecard.yml` | Yes (reusable workflow) | (inherited) | Job-level explicit |
| `security-analysis.yml` | Yes | Yes (every job) | `read-all` at workflow, job-level overrides |
| `sonarcloud.yml` | Yes | Yes | `contents: read, pull-requests: write` (needed by Sonar PR decoration) |

No unpinned actions were found. No mutable-tag references (`@v1`, `@main`)
remain. The OpenSSF Scorecard workflow already monitors and re-checks this
posture weekly.

## Changes Applied in This PR

| Change | File | Rationale |
| --- | --- | --- |
| Tighten CI workflow permissions to `contents: read` | `.github/workflows/ci.yml` | F-02 -- remove unused `pull-requests: write` and `checks: write` |
| Add `step-security/harden-runner` step | `.github/workflows/dependency-review.yml` | F-03 -- consistent egress-audit posture across workflows |
| Remove `B101` from Bandit `skips` | `pyproject.toml` | F-01 -- assert-in-production gap; tests still excluded via `exclude_dirs` |
| This findings document | `SECURITY-FINDINGS.md` | OWASP audit record |

## Items Deferred to Phase 1

- F-05: response-header middleware (CSP, XFO, XCTO, HSTS, Referrer-Policy).
- F-06: CF JWT validation middleware with mandatory `aud` claim check.
- F-04 (verification): confirm production deployment uses HTTPS for all
  backend calls, or document the trusted-network assumption.
- Add CSRF-not-applicable note once routes exist (the portal is read-only,
  but any future POST/PUT endpoint must use Starlette's
  `SessionMiddleware` + token, or rely on Cloudflare-issued service tokens).

## Verification Steps

1. `uv run bandit -r app -c pyproject.toml` -- still passes (no `app/` code
   uses `assert`).
2. `uv run ruff check .` -- unchanged rule set; no new violations.
3. `uv run pre-commit run --all-files` -- all hooks pass.
4. CI workflow runs: jobs succeed with reduced token scope (artifact upload
   only requires `contents: read`).
5. `dependency-review.yml` job: still passes; harden-runner logs outbound
   calls in audit mode.
