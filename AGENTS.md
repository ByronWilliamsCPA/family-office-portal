# AGENTS.md

Agentic coding assistants (Codex, GitHub Copilot Workspace, and similar tools)
should read this file before beginning any task in this repository.

## Project overview

`family-office-portal` is a FastAPI web application that aggregates financial
and entity data from internal backend services. It runs behind Cloudflare Zero
Trust and serves a single authenticated family as a read-only portal.

## Source layout

```text
app/           Python package (all application code)
tests/         pytest test suite
docs/          Documentation and ADRs
.github/       CI/CD workflows and GitHub configuration
pyproject.toml uv project manifest and tool configuration
```

## Key conventions

- **Language**: Python 3.12 only (not 3.13 or later), no 3.10/3.11 compatibility shims
- **Package manager**: uv (not pip, not Poetry)
- **Type checker**: basedpyright strict mode (not mypy)
- **Linter/formatter**: ruff
- **Test framework**: pytest with pytest-asyncio and pytest-cov
- **Source root**: `app/` (not `src/`)
- **Commits**: conventional commits (`feat:`, `fix:`, `docs:`, etc.)
- **Pre-commit**: always run `uv run pre-commit run --all-files` before committing

## What NOT to do

- Do not create `.py` files at the repo root; all Python belongs under `app/`
- Do not use `import mypy` or reference mypy configuration; use basedpyright
- Do not add `# type: ignore` or `# noqa` without a tracking reference
- Do not use `poetry` or `pip install` commands; use `uv sync`

## Critical Rules (read before any change)

These rules are non-negotiable, mirrored from CLAUDE.md so non-Claude agents
(Codex, Cursor, Aider) see them without reading Claude-specific files:

- **CF JWT `aud` claim validation** is mandatory. Skipping it is a security
  defect tagged `#CRITICAL`. See ADR-002.
- **Route handlers read from SQLite only.** They must never call backend HTTP
  services directly. See ADR-003.
- **Python 3.12 only.** Do not introduce 3.13 syntax or features.
- **No CDN-loaded assets in production.** HTMX and Chart.js are vendored
  static files; never reference a CDN URL in templates.
- **Never log financial values, document contents, or full email addresses.**
  INFO-level auth events with redacted email are the only allowed PII pattern.

## Global standards

Project-specific rules are in `CLAUDE.md` at the repo root. The maintainer's
multi-tool agent and skill catalog is referenced from the maintainer
workstation; agents on other machines should rely on this file, `CLAUDE.md`,
and `GEMINI.md` for project context.
