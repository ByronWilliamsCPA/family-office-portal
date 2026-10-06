# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Balance intake: `POST /api/v1/balances` accepts a delivery of account balances (`{"items": [...], "total": n}`) from a collector on the internal network. It is guarded by its own `X-API-Key` header, compared in constant time with the optional `BALANCE_INTAKE_API_KEY` setting (at least 32 characters of printable ASCII, different from `AUTHENTIK_JWT_SECRET`, never logged or echoed); when the setting is unset the endpoint answers 404 and accepts nothing. The auth middleware lets exactly `POST /api/v1/balances` through to the route and nothing else; any other method or path still needs a signed identity. Bodies are limited to 6 MiB and validated strictly (values are decimal strings with at most 12 digits before the point, a duplicate account or a wrong `total` is refused, errors name the field and never echo a value) and stored in one all-or-nothing transaction, replacing the rows of each provider present in the delivery and keeping the rows of providers that are absent
- Migration 2 adds a nullable `reconciled_through` column to `account_balances`; amounts are rounded half to even to integer cents and stored with their currency
- Daily balance history: every successful delivery, and a new `snapshot_balances_daily` job once at startup and then daily at midday in `DISPLAY_TIMEZONE`, upserts today's row per account into `balances_daily`; a same-day delivery that swaps an account removes the replaced account from that day only, and earlier days are never deleted; the daily job has a one-hour misfire grace and coalesces a late run
- `DISPLAY_TIMEZONE` lookups share one helper that falls back to UTC with a single warning for any unusable name, including one that raises `OSError`, and an unusable name is reported at startup
- Authentik forward-auth JWT middleware (`app/middleware/authentik.py`, ADR-005): identity comes only from the signed `X-authentik-jwt` header, verified with HS256 (the only accepted algorithm) keyed by the proxy provider's client secret in `AUTHENTIK_JWT_SECRET`, and checked for `exp`, `iss` and `aud` with a 10 s leeway for clock skew; the portal makes no outbound request to authenticate; the `fo-admin` / `fo-viewer` groups map to Admin / Viewer, `/admin` and `/admin/*` require Admin, only `/health` and `/static/` are public (both judged on the routed path with any `root_path` stripped), and every failure returns 403 with a reason-only log line
- Required env vars `AUTHENTIK_JWT_SECRET` (at least 32 characters, not the stack placeholder, no leading or trailing whitespace; never echoed in errors or settings `repr`), `AUTHENTIK_ISSUER` and `AUTHENTIK_AUDIENCE`; optional `FO_ADMIN_GROUP` (default `fo-admin`) and `FO_VIEWER_GROUP` (default `fo-viewer`)
- ADR-005 (Authentik forward auth), including the contract the portal expects from homelab-infra and the uvicorn `--proxy-headers --forwarded-allow-ips` guidance
- Middleware test suite (`tests/unit/test_middleware.py`) with HS256 token fixtures and a secret generated at test time in `tests/conftest.py`, covering the module at 100%
- CI: Claude Tier 0 baseline PR review caller (`.github/workflows/claude-baseline-review.yml`), a thin caller of the org reusable in `ByronWilliamsCPA/.github`. Part of the org-wide tiered-pr-review rollout.
- Portal foundation: pydantic settings (`app/config.py`) with optional per-backend API keys (unset sends no `X-API-Key` header), a FastAPI lifespan that initializes the schema and runs the scheduler, SQLite schema with WAL and busy timeout including `account_balances` and durable `balances_daily` history (`app/db.py`), async cache readers with allowlisted staleness checks (`app/cache.py`), and APScheduler refresh jobs that log every run to `refresh_log` and keep cached rows on failure (`app/scheduler.py`)
- Schema migrations tracked by `PRAGMA user_version`; migration 1 adds a `currency` column to `account_balances` and `balances_daily`
- Server-rendered pages for Home, Documents, Finances, Portfolio, and Entities with plain-English freshness labels, HTMX name search, confidential-document filtering for Viewers, and an HTML not-found page
- Distroless Dockerfile with Tailwind CLI build stage and GHCR build, smoke-test, and signing workflow (`.github/workflows/build-image.yml`)
- Vendored HTMX 2.0.4 (0BSD) with REUSE annotation
- Phase 0 FastAPI application skeleton in `app/main.py`: title, description, version, contact, `CloudflareAccessMiddleware` registration, and a `GET /health` liveness probe returning `{"status": "ok"}`
- Cloudflare Access middleware pass-through stub in `app/middleware/cloudflare_access.py` per ADR-002; full JWT validation deferred to Phase 1
- Phase 0 health smoke test (`tests/test_health.py`) exercising the endpoint via httpx `ASGITransport`
- Lightweight security workflow (`.github/workflows/security.yml`) running Bandit + pip-audit on every pull request and push to main, installed via uv, with hardened egress allowlist
- OpenAPI schema export script (`scripts/export_openapi.py`) and committed placeholder (`docs/api/openapi.json`)
- Coverage gate: `[tool.coverage.report] fail_under = 80` in `pyproject.toml`
- Initial project scaffold: FastAPI application with Cloudflare Zero Trust auth
- Pre-commit hooks: ruff, basedpyright, bandit, detect-secrets, interrogate, pydoclint, commitizen, yamllint, markdownlint, no-em-dash (SHA-pinned)
- Foundation files: README, LICENSE, SECURITY, CODEOWNERS, CLAUDE.md, AGENTS.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, GOVERNANCE.md
- pyproject.toml with uv dependency management, PyStrict-aligned Ruff config, basedpyright strict mode, and pytest-asyncio auto mode
- CHANGELOG.md, docs/known-vulnerabilities.md, docs/planning/project-vision.md, docs/planning/roadmap.md, docs/planning/tech-spec.md
- Architecture Decision Records: ADR-001 (frontend rendering), ADR-002 (Cloudflare Zero Trust auth), ADR-003 (backend data aggregation) under docs/architecture/adr/
- CI pipeline: main CI workflow with setup, test, quality-checks, and ci-gate jobs (ci.yml)
- Security analysis workflow: CodeQL, dependency review, Bandit, pip-audit, OSV Scanner, OWASP Dependency-Check (security-analysis.yml)
- PR validation workflow calling org reusable python-ci.yml (pr-validation.yml)
- Documentation quality workflow: ruff lint, interrogate docstring coverage (docs.yml)
- REUSE FSFE compliance workflow (reuse.yml)
- SonarCloud analysis workflow with SHA-pinned actions (sonarcloud.yml)
- OpenSSF Scorecard workflow (scorecard.yml)
- Codecov coverage upload workflow (codecov.yml)
- Python compatibility matrix workflow (python-compatibility.yml)
- Renovate automated dependency update configuration
- GitHub Copilot instructions file (.github/copilot-instructions.md)
- sonar-project.properties for SonarCloud configuration
- .codecov.yml for Codecov configuration
- Route stubs for all five portal sections (home, documents, finances, portfolio, entities) plus admin refresh triggers and liveness probe; each endpoint returns the correct typed response or HTML placeholder
- Pydantic response models for all typed endpoints: `HealthResponse`, `DocumentSearchResponse`, `RefreshStatusResponse`, `RefreshTriggerRequest`, `RefreshTriggerResponse` in `app/models.py`
- OpenAPI enrichment: per-route `summary`, `status_code`, `responses`, and `tags` on every route decorator; app-level metadata (title, description, version, contact, license, tags) in the `FastAPI()` constructor
- Postman collection generator script (`scripts/generate_postman.py`) that builds a Newman-compatible collection from the live OpenAPI schema
- Committed Postman collection (`docs/api/postman-collection.json`) generated from the Phase 0 route stubs
- Newman contract-testing CI workflow (`.github/workflows/postman-api-tests.yml`): starts the FastAPI app in background, waits for `/health`, runs Newman, uploads JUnit report as an artifact
- Unit test suite (`tests/unit/test_routes.py`) covering all 15 Phase 0 route endpoints: status codes, content types, and JSON shapes; shared `client` fixture in `tests/conftest.py`
- Document file proxy (ADR-007): `/documents/{id}/preview` and `/documents/{id}/download` now stream the file from llc-manager `GET /api/v1/documents/{id}/file` with the backend `X-API-Key`, instead of answering 503. The confidential check runs against the cache first, so a Viewer asking for a confidential or unknown document gets 404 and no upstream request is made. PDFs and images preview inline; every other type downloads. The content type comes from an allowlist (anything else is `application/octet-stream`), `Content-Disposition` uses an RFC 6266 `filename*` with control and format characters replaced, and responses send `nosniff`, `private, no-store`, `frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN` and `Cross-Origin-Resource-Policy: same-origin`. Files over 50 MiB are refused, a whole response may run at most five minutes, redirects are not followed, timeouts map to 504 and other upstream failures to 502, and upstream bodies never reach the browser. Document lists gain a Download link, and a `document_url` template filter builds preview links that can open a PDF at `#page=N`

### Changed

- **BREAKING**: each backend is now an optional pair, `BACKEND_<NAME>_URL` plus `BACKEND_<NAME>_API_KEY`, for `LLC_MANAGER`, `PP_SECURITY`, `XERO_CRYPTO` and a new `DATA_INGESTOR` (settings and startup checks only; no client calls it yet). `BACKEND_FAMILY_OFFICE_URL` is removed: it was required but never read. A URL set with an unset, empty or whitespace key now stops startup with an error naming the key variable, where it used to start and send requests with no `X-API-Key` header; the refresh client can no longer send a request without that header. A key with no URL is allowed and logged at info level. A backend with no URL is "not connected": its refresh jobs skip with one log line and no outbound call, and its pages show "Not connected yet" (ADR-003 amendment 2026-09-30). `AUTHENTIK_JWT_SECRET`, `AUTHENTIK_ISSUER`, `AUTHENTIK_AUDIENCE` and `SQLITE_PATH` stay required
- **BREAKING**: forward-auth token validation switches from an asymmetric signature checked against a fetched key set to HS256 keyed by the proxy provider's client secret, because an Authentik proxy provider cannot keep a signing key and signs `X-authentik-jwt` with its client secret (ADR-005 amendment 2026-09-29). The required env var `AUTHENTIK_JWT_SECRET` replaces `AUTHENTIK_JWKS_URL`, and `AUTHENTIK_JWKS_CACHE_SECONDS` is removed; a deployment that still sets only `AUTHENTIK_JWKS_URL` exits 1 at startup. Rotating the secret now needs a portal restart with the new value, and requests get 403 between the Authentik change and that restart
- CI: the Newman contract-test job generates `AUTHENTIK_JWT_SECRET` at runtime (or uses the Actions secret of that name), so no secret literal is committed
- Authentication moves from Cloudflare Zero Trust Access to Authentik forward auth behind Traefik; ADR-002 is marked superseded by ADR-005 and kept as history
- The documents refresh reads the documents contract fields only: `title`, `category`, `entity_id`, `document_type`, `document_date`, `is_confidential`, `created_at` and `updated_at`. The legacy `name`, `added_at` and `modified_at` fallbacks and the guess of a category from `document_type` are removed; a missing or unknown category is filed under "Other". `is_confidential` now fails closed: only a JSON `false` shows a document to Viewers, and a missing, null or non-boolean flag hides it. An item without a usable ID or title, or with a repeated ID, is skipped and logged by ID while the rest of the refresh still lands, so one bad item cannot freeze confidentiality changes; categories the portal does not know are counted in one `document_category_unknown` warning per refresh
- Startup now writes the name of a missing required env var to stderr before exiting 1, and exits 1 on an invalid Authentik configuration
- Test fixtures: `cf_env` is renamed `portal_env`, and the shared `client` fixture sends an Admin token (`anon_client` sends none)
- Postman contract tests: protected requests send `X-authentik-jwt: {{authentikJwt}}` and expect 403 when the variable is empty, which is how the Newman CI run exercises them; the `apiKey` collection variable is gone
- CLAUDE.md, AGENTS.md, GEMINI.md, Copilot instructions, README, SECURITY.md, SECURITY-FINDINGS.md (F-06 resolved), PROJECT-PLAN.md, tech spec, roadmap and project vision now describe Authentik
- Documents now come from llc-manager instead of the family_office backend
- urllib3 bumped 2.6.3 → 2.7.0 in `uv.lock` to resolve CVE-2026-44431 and CVE-2026-44432
- CODEOWNERS moved from repo root to .github/CODEOWNERS
- ADRs migrated from docs/planning/adr/ to docs/architecture/adr/
- LICENSE: added SPDX-License-Identifier header
- SECURITY.md: switched from email reporting to GitHub Private Vulnerability Reporting (PVR) only

### Removed

- Cloudflare Access pass-through stub `app/middleware/cloudflare_access.py` and its `CloudflareAccessMiddleware` registration
- Env vars `CF_TEAM_DOMAIN`, `CF_ACCESS_APP_ID`, `VIEWER_EMAILS` and `ADMIN_EMAILS`
- CI: deleted `.github/workflows/dependency-review.yml` and the inline `actions/dependency-review-action` step (and its now-empty `dependency-security` job) in `security-analysis.yml`. GitHub now bills Advanced Security (Code Security), so the dependency-review action no longer functions on this repo. This supersedes the earlier `harden-runner` hardening entry for `dependency-review.yml` under Fixed, and that file's `merge_group` exemption note, since the file no longer exists.
- CI: removed the `github/codeql-action/upload-sarif` step from the `owasp-dependency-check` job in `security-analysis.yml` for the same reason; that job's existing `Upload OWASP Report` artifact step already covers the same `reports/` directory (OWASP Dependency-Check's `format: ALL` output includes the SARIF file), so no replacement step was needed. Pruned the job's now-unused `security-events: write` permission.
- CI: disabled `upload-sarif` in `scorecard.yml` (org-level `python-scorecard.yml` reusable workflow input) for the same reason; the reusable workflow already uploads the same SARIF file unconditionally as the `scorecard-results` artifact.
- What remains active: Bandit and pip-audit (in both `security.yml` on every pull request and push targeting `main`, and `security-analysis.yml`'s change-aware `security-scanning` job), the OSV Scanner job, and the OWASP Dependency-Check tool itself (still runs; only its SARIF-to-Security-tab upload was removed, its artifact report remains).
- What is no longer enforced in CI: the diff-based dependency vulnerability review on pull requests (`fail-on-severity` high, and moderate on security-relevant changes) and dependency license enforcement (the `deny-licenses` GPL-2.0 and GPL-3.0 list and the `allow-licenses` allow-list). No remaining job checks the licenses of dependencies; REUSE compliance covers this repository's own files only.
- `security-analysis.yml`'s aggregating `Security Gate Validation` job (required org status check) was updated to stop referencing the removed `dependency-security` job's result; `pr-validation.yml`'s separate `Dependency & Standards Validation` gate (also a required org status check) is untouched and continues to run on every PR independent of the deleted `dependency-review.yml` file.

### Fixed

- Dependencies: refreshed `uv.lock` (targeted `uv lock --upgrade-package`, not a blanket upgrade, so the dev toolchain such as ruff stays pinned) to clear pip-audit findings on `cryptography` (PYSEC-2026-3552/3553/3554, GHSA-537c-gmf6-5ccf), `msgpack` (PYSEC-2026-3625), `pip` (PYSEC-2026-3721), `pydantic-settings` (GHSA-4xgf-cpjx-pc3j), and `starlette` (PYSEC-2026-248, PYSEC-2026-249), and `anyio` 4.13.0 -> 4.14.2 (CVE-2026-63374, CVE-2026-64847); these were the pip-audit failures cascading into `Code Quality Checks`, `Bandit + pip-audit`, `Multi-Tool Security Scan`, and this PR's own `Security Gate Validation` job
- CI: the required `Security Gate Validation` check never reported on pull requests because it was produced by `_security-gate.yml`, a `workflow_call`-only reusable workflow invoked from `security-analysis.yml` via `uses:`. A reusable-workflow caller job can only ever emit a `<caller job name> / <inner job name>` context (here, `Security Analysis / Security Gate Validation`), never the bare inner job name the org ruleset requires; the check sat "Expected" forever and blocked every PR. Replaced the caller/callee indirection with a single normal job (`security-gate-validation`, `name: Security Gate Validation`) inlined directly in `security-analysis.yml`, and removed the now-unused `_security-gate.yml`. The gate now also requires `detect-changes` to succeed, so a failed change-detection job can no longer skip every scan and pass the gate with no scan run
- CI: required-check workflows (`ci.yml`, `pr-validation.yml`, `reuse.yml`, `security-analysis.yml`) now trigger on `merge_group`. The default branch enforces a merge queue, and without this trigger no required check ever reported on a queue entry, so every queued PR timed out and was dropped. `dependency-review.yml` deliberately stays off `merge_group` because `actions/dependency-review-action` needs a `pull_request` payload; `pr-validation.yml` supplies the shared `Dependency & Standards Validation` context in the queue
- Refresh jobs now follow `{items, total}` paging; a short or runaway page set fails the refresh and keeps the old cache instead of replacing it with partial data
- Refresh jobs no longer overlap: one lock per service plus one process-wide SQLite write lock; the unfinished holdings and positions jobs are no longer scheduled (admins can still trigger them)
- Account totals sum USD rows only and say how many accounts in other currencies are left out; money is formatted from `Decimal` with half-even rounding
- Home and Finances show the oldest balance as-of date and warn when a value is older than 35 days, even if it was fetched recently
- Unknown document types are filed under "Other" instead of "LLCs"
- CI: SonarCloud quality gate now evaluates correctly after passing the project version (read dynamically from `pyproject.toml`) to the scan action; without a project version the quality gate returned `NONE` and the gate action failed
- CI: placeholder test `assert True` removed so the function body is just its existing docstring, resolving SonarCloud rule S5914 (constant boolean expression in assertion); pytest still collects and passes the function
- CI: OpenSSF Scorecard workflow now sets `publish-results: false` to prevent OIDC token mismatch when running as a callee reusable workflow (the token resolves to the .github repo, not the calling repo)
- CI: pip-audit invocation now passes `--ignore-vuln PYSEC-2022-42969` to honor the project's documented exemption in `docs/known-vulnerabilities.md` (transitive `py@1.11.0` via `interrogate`, dev-only, mitigation accepted); the OpenSSF release gate still blocks releases for any documented entry older than 60 days
- Security: removed `B101` from `[tool.bandit].skips`; assert-in-production check now active for `app/` (tests remain excluded via `exclude_dirs`); closing the gap where a future `assert` used as a security guard would be silently stripped under `python -O`
- Security: tightened `.github/workflows/ci.yml` workflow-level permissions from `pull-requests: write, checks: write` to `contents: read` only; no step in any of the four CI jobs uses the removed scopes
- Security: added `step-security/harden-runner` (egress-policy: audit) to `.github/workflows/dependency-review.yml`, bringing it into line with every other major workflow in the repo
- Security: upgraded transitive `urllib3` 2.6.3 -> 2.7.0 (via `uv lock --upgrade-package urllib3`), closing CVE-2026-44431 and CVE-2026-44432; `urllib3` is a dev-only dependency pulled in by `pip-audit` via `cachecontrol -> requests`
- Security: upgraded transitive `idna` 3.13 -> 3.16 (via `uv lock --upgrade-package idna`), closing CVE-2026-45409; `idna` is pulled in by `requests` (dev) and `httpx` (runtime)
- Security: upgraded transitive `starlette` 1.0.0 -> 1.0.1 (via `uv lock --upgrade-package starlette`), closing PYSEC-2026-161; `starlette` is a runtime dependency pulled in by `fastapi`
- Security: added `step-security/harden-runner` to the `ci-gate` job in `.github/workflows/ci.yml` for consistent egress-audit coverage across every job in the workflow
- Security: added `step-security/harden-runner` to `.github/workflows/postman-api-tests.yml`; every CI job in the repo now has egress-audit coverage
- Security: pinned `newman` to `6.2.1` (was `@6` range) in the Postman API tests workflow to prevent unintentional minor-version drift during CI
- Removed PII (`email` claim `byronawilliams@gmail.com`) from the OpenAPI `contact` block in `app/main.py` and the committed `docs/api/openapi.json`
