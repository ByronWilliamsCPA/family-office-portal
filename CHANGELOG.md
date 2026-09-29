# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Authentik forward-auth JWT middleware (`app/middleware/authentik.py`, ADR-005): identity comes only from the signed `X-authentik-jwt` header, verified with RS256 against the https JWKS at `AUTHENTIK_JWKS_URL` (TTL cache, rate-limited refetch on an unknown `kid`) and checked for `exp`, `iss` and `aud` with zero leeway; the `fo-admin` / `fo-viewer` groups map to Admin / Viewer, `/admin/*` requires Admin, only `/health` and `/static/` are public, and every failure returns 403 with a reason-only log line
- Required env vars `AUTHENTIK_JWKS_URL` (must be `https://`), `AUTHENTIK_ISSUER` and `AUTHENTIK_AUDIENCE`; optional `FO_ADMIN_GROUP` (default `fo-admin`), `FO_VIEWER_GROUP` (default `fo-viewer`) and `AUTHENTIK_JWKS_CACHE_SECONDS` (default `600`)
- ADR-005 (Authentik forward auth), including the contract the portal expects from homelab-infra and the uvicorn `--proxy-headers --forwarded-allow-ips` guidance
- Middleware test suite (`tests/unit/test_middleware.py`) with RS256 token and JWKS fixtures in `tests/conftest.py`, covering the module at 100%
- CI: Claude Tier 0 baseline PR review caller (`.github/workflows/claude-baseline-review.yml`), a thin caller of the org reusable in `ByronWilliamsCPA/.github`. Part of the org-wide tiered-pr-review rollout.
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

- Authentication moves from Cloudflare Zero Trust Access to Authentik forward auth behind Traefik; ADR-002 is marked superseded by ADR-005 and kept as history
- Startup now writes the name of a missing required env var to stderr before exiting 1, and exits 1 on an invalid Authentik configuration
- Test fixtures: `cf_env` is renamed `portal_env`, and the shared `client` fixture sends an Admin token (`anon_client` sends none)
- Postman contract tests: protected requests send `X-authentik-jwt: {{authentikJwt}}` and expect 403 when the variable is empty, which is how the Newman CI run exercises them; the `apiKey` collection variable is gone
- CLAUDE.md, AGENTS.md, GEMINI.md, Copilot instructions, README, SECURITY.md, SECURITY-FINDINGS.md (F-06 resolved), PROJECT-PLAN.md, tech spec, roadmap and project vision now describe Authentik
- urllib3 bumped 2.6.3 → 2.7.0 in `uv.lock` to resolve CVE-2026-44431 and CVE-2026-44432
- CODEOWNERS moved from repo root to .github/CODEOWNERS
- ADRs migrated from docs/planning/adr/ to docs/architecture/adr/
- LICENSE: added SPDX-License-Identifier header
- SECURITY.md: switched from email reporting to GitHub Private Vulnerability Reporting (PVR) only

### Removed

- Cloudflare Access pass-through stub `app/middleware/cloudflare_access.py` and its `CloudflareAccessMiddleware` registration
- Env vars `CF_TEAM_DOMAIN`, `CF_ACCESS_APP_ID`, `VIEWER_EMAILS` and `ADMIN_EMAILS`

### Fixed

- Dependencies: refreshed `uv.lock` (targeted `uv lock --upgrade-package`, not a blanket upgrade, so the dev toolchain such as ruff stays pinned) to clear pip-audit findings on `cryptography` (PYSEC-2026-3552/3553/3554, GHSA-537c-gmf6-5ccf), `msgpack` (PYSEC-2026-3625), `pip` (PYSEC-2026-3721), `pydantic-settings` (GHSA-4xgf-cpjx-pc3j), and `starlette` (PYSEC-2026-248, PYSEC-2026-249), and `anyio` 4.13.0 -> 4.14.2 (CVE-2026-63374, CVE-2026-64847); these were the pip-audit failures cascading into `Code Quality Checks`, `Bandit + pip-audit`, `Multi-Tool Security Scan`, and this PR's own `Security Gate Validation` job
- CI: the required `Security Gate Validation` check never reported on pull requests because it was produced by `_security-gate.yml`, a `workflow_call`-only reusable workflow invoked from `security-analysis.yml` via `uses:`. A reusable-workflow caller job can only ever emit a `<caller job name> / <inner job name>` context (here, `Security Analysis / Security Gate Validation`), never the bare inner job name the org ruleset requires; the check sat "Expected" forever and blocked every PR. Replaced the caller/callee indirection with a single normal job (`security-gate-validation`, `name: Security Gate Validation`) inlined directly in `security-analysis.yml`, and removed the now-unused `_security-gate.yml`. The gate now also requires `detect-changes` to succeed, so a failed change-detection job can no longer skip every scan and pass the gate with no scan run
- CI: required-check workflows (`ci.yml`, `pr-validation.yml`, `reuse.yml`, `security-analysis.yml`) now trigger on `merge_group`. The default branch enforces a merge queue, and without this trigger no required check ever reported on a queue entry, so every queued PR timed out and was dropped. `dependency-review.yml` deliberately stays off `merge_group` because `actions/dependency-review-action` needs a `pull_request` payload; `pr-validation.yml` supplies the shared `Dependency & Standards Validation` context in the queue
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
