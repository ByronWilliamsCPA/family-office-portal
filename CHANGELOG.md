# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Schema migrations tracked by `PRAGMA user_version`; migration 1 adds a `currency` column to `account_balances` and `balances_daily`
- MVP foundation (M0): pydantic settings with startup fail-fast (`app/config.py`), SQLite schema with WAL and busy timeout including `account_balances` and durable `balances_daily` history (`app/db.py`), async cache readers with allowlisted staleness checks (`app/cache.py`), and APScheduler refresh jobs that log every run to `refresh_log` and keep cached rows on failure (`app/scheduler.py`)
- Authentik forward-auth middleware validating the signed `X-authentik-jwt` header (signature, `iss`, `aud`, `exp`), mapping `fo-viewer`/`fo-admin` groups to roles, and failing closed (ADR-004)
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

### Changed

- Documents now come from llc-manager instead of the family_office repository
- Postman collection asserts `/health` is public and every other route returns 403 without an Authentik identity
- urllib3 bumped 2.6.3 → 2.7.0 in `uv.lock` to resolve CVE-2026-44431 and CVE-2026-44432
- CODEOWNERS moved from repo root to .github/CODEOWNERS
- ADRs migrated from docs/planning/adr/ to docs/architecture/adr/
- LICENSE: added SPDX-License-Identifier header
- SECURITY.md: switched from email reporting to GitHub Private Vulnerability Reporting (PVR) only

### Removed

- Cloudflare Access middleware stub and the `CF_TEAM_DOMAIN`, `CF_ACCESS_APP_ID`, `VIEWER_EMAILS`, `ADMIN_EMAILS`, and `BACKEND_FAMILY_OFFICE_URL` settings (ADR-002 superseded)

### Fixed

- Refresh jobs now follow `{items, total}` paging; a short or runaway page set fails the refresh and keeps the old cache instead of replacing it with partial data
- Refresh jobs no longer overlap: one lock per service plus one process-wide SQLite write lock; the unfinished holdings and positions jobs are no longer scheduled (admins can still trigger them)
- Account totals sum USD rows only and say how many accounts in other currencies are left out; money is formatted from `Decimal` with half-even rounding
- Home and Finances show the oldest balance as-of date and warn when a value is older than 35 days, even if it was fetched recently
- Unknown document types are filed under "Other" instead of "LLCs"
- Startup prints the names of missing required environment variables to stderr before exiting
- The JWKS cache is locked so concurrent requests with a new key ID fetch the key set once
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
