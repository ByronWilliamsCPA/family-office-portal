# GitHub Copilot Instructions

This file provides context for GitHub Copilot when working in this repository.

## Project

`family-office-portal` is a FastAPI web application that aggregates financial
and entity data from internal backend services for a single authenticated family.
It runs behind Cloudflare Zero Trust and is read-only from the user's perspective.

## Key conventions

- **Source root**: `app/` (not `src/`)
- **Python**: 3.12+ syntax; use `X | Y` union types (not `Optional[X]`)
- **Package manager**: uv; lock file is `uv.lock`
- **Type checker**: basedpyright strict (not mypy)
- **Linter**: ruff (88 chars, PyStrict-aligned rules)
- **Tests**: pytest with pytest-asyncio; 80% coverage minimum
- **Commits**: conventional commits required (`feat:`, `fix:`, `docs:`, etc.)
- **Comments**: no em-dashes ever; one short line max per comment

## Do not do

- Do not create `.py` files outside `app/`
- Do not use `Optional[X]`; use `X | None`
- Do not add `# type: ignore` without a tracking reference
- Do not use `poetry` or bare `pip install`; use `uv sync`
- Do not bypass pre-commit hooks with `--no-verify`
