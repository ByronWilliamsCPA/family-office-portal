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

## Global standards

Global agent and skill conventions live at `~/.claude/CLAUDE.md`.
Project-specific rules are in `CLAUDE.md` at the repo root.
