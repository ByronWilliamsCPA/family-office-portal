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
- **async tests**: `asyncio_mode = "auto"` is set; never add `@pytest.mark.asyncio` to individual tests
- **dependencies**: add packages with `uv add <package>`; install dev tools with `uv sync --extra dev`
- **logging**: structlog only; never log financial values, document contents, or email addresses

## Architecture rules

- Route handlers return `TemplateResponse` (server-rendered HTML). Never return a raw dict or `JSONResponse` except for HTMX partials returning HTML fragments.
- Route handlers read from SQLite via `cache.py` only. They never call backend HTTP services directly.
- Auth is handled by Cloudflare Zero Trust at the network edge. Never add password-based auth, OAuth flows, session cookies, or a login view.
- Backend HTTP calls (httpx) belong only in APScheduler refresh jobs in `scheduler.py`.

## Do not do

- Do not create `.py` files outside `app/`
- Do not use `Optional[X]`; use `X | None`
- Do not add `# type: ignore` without a tracking reference
- Do not use `poetry` or bare `pip install`; use `uv sync`
- Do not bypass pre-commit hooks with `--no-verify`
- Do not add `@pytest.mark.asyncio` to individual tests; `asyncio_mode = "auto"` runs them automatically
- Do not load JavaScript from a CDN; only HTMX and Chart.js are permitted, vendored in `static/`
- Do not create a login route, session middleware, or OAuth flow
