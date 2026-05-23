# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 0 FastAPI application skeleton in `app/main.py`: title, description, version, contact, `CloudflareAccessMiddleware` registration, and a `GET /health` liveness probe returning `{"status": "ok"}`
- Cloudflare Access middleware pass-through stub in `app/middleware/cloudflare_access.py` per ADR-002; full JWT validation deferred to Phase 1
- Phase 0 health smoke test (`tests/test_health.py`) exercising the endpoint via httpx `ASGITransport`
- Lightweight security workflow (`.github/workflows/security.yml`) running Bandit + pip-audit on every pull request and push to main, installed via uv, with hardened egress allowlist
- OpenAPI schema export script (`scripts/export_openapi.py`) and committed placeholder (`docs/api/openapi.json`)
- Coverage gate: `[tool.coverage.report] fail_under = 80` in `pyproject.toml`
- Initial project scaffold: FastAPI application with Cloudflare Zero Trust auth
- Pre-commit hooks: ruff, basedpyright, bandit, detect-secrets, interrogate, darglint, commitizen, yamllint, markdownlint, no-em-dash (SHA-pinned)
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

### Changed

- urllib3 bumped 2.6.3 → 2.7.0 in `uv.lock` to resolve CVE-2026-44431 and CVE-2026-44432
- CODEOWNERS moved from repo root to .github/CODEOWNERS
- ADRs migrated from docs/planning/adr/ to docs/architecture/adr/
- LICENSE: added SPDX-License-Identifier header
- SECURITY.md: switched from email reporting to GitHub Private Vulnerability Reporting (PVR) only

### Fixed

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
