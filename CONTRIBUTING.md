# Contributing to family-office-portal

Thank you for your interest in contributing to the family-office-portal project.
This is a private family estate portal; contributions are limited to authorized
collaborators. The guidelines below apply to all changes.

## Reporting Bugs

Open a GitHub Issue at <https://github.com/ByronWilliamsCPA/family-office-portal/issues>.
Include a clear description, steps to reproduce, expected behavior, and actual behavior.
Attach relevant log output where possible (redact any financial values or personal data
before pasting).

## Reporting Security Vulnerabilities

Do not file a public issue for security vulnerabilities. Follow the process described
in [SECURITY.md](SECURITY.md) to report privately through GitHub's private vulnerability
reporting interface.

## Code of Conduct

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md). Report unacceptable behavior to the
maintainer through the SECURITY.md private reporting channel.

## Pull Request Guidelines

- Branch from `main` using the naming convention `feature/<short-description>` or
  `fix/<short-description>`.
- Link your PR to the relevant issue with "Closes #ISSUE-NUMBER" in the PR description.
- Keep commits atomic; one logical change per commit.

## Commit Message Format

All commits must follow Conventional Commits:

```text
<type>(<scope>): <subject>
```

Where `<type>` is one of: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`.

Example: `fix(middleware): validate aud claim before returning 200`

## Code Style

- Python 3.12 only.
- Formatting and linting: `uv run ruff format .` and `uv run ruff check --fix .`
- Type checking: `uv run basedpyright`
- Run `uv run pre-commit run --all-files` before every commit; the hooks enforce
  formatting, type safety, and commit message standards.

## Testing Requirements

- Overall line coverage must remain at or above 80%.
- Critical paths (CF JWT middleware in `app/middleware/`, cache reads and staleness
  logic in `app/cache.py`) must remain at or above 95% coverage.
- Run `uv run pytest` locally before opening a PR. CI will run unit and integration
  test suites on every push.
- New features must include unit tests; resilience scenarios (backend 500s,
  stale data) must include integration coverage per ADR-003.

## Local Development Setup

```bash
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
uv run pytest
```

See `CLAUDE.md` for the full development command reference.
