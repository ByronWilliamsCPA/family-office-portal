# GitHub Copilot Instructions

This file provides context for GitHub Copilot when working in this repository.

## Project

`family-office-portal` is a FastAPI web application that aggregates financial
and entity data from internal backend services for a single authenticated family.
It runs behind Authentik forward auth and is read-only from the user's perspective.

## Key conventions

- **Source root**: `app/` (not `src/`)
- **Python**: 3.12 only (not 3.13+); use `X | Y` union types (not `Optional[X]`)
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
- Route handlers read from SQLite via `cache.py` only. They never call backend HTTP services directly, except the document file proxy for preview and download (`app/document_files.py`, ADR-007), which checks visibility in the cache first, and document search (`app/retrieval/search.py`, ADR-008), which embeds the query and queries Qdrant during the request, and the question panel (`app/chat/`, ADR-009), which also calls the chat model during the request.
- Auth is handled by Authentik forward auth at the reverse proxy; the app only validates the signed `X-authentik-jwt` header (ADR-005). Never add password-based auth, OAuth flows, session cookies, or a login view.
- Backend HTTP calls (httpx) belong only in APScheduler refresh jobs in `scheduler.py`, plus the document file proxy in `app/document_files.py` (ADR-007), document search in `app/retrieval/search.py` (ADR-008), and the chat model client in `app/chat/client.py` (ADR-009).

## Do not do

- Do not create `.py` files outside `app/`
- Do not use `Optional[X]`; use `X | None`
- Do not add `# type: ignore` without a tracking reference
- Do not use `poetry` or bare `pip install`; use `uv sync`
- Do not bypass pre-commit hooks with `--no-verify`
- Do not add `@pytest.mark.asyncio` to individual tests; `asyncio_mode = "auto"` runs them automatically
- Do not load JavaScript from a CDN; only HTMX and Chart.js are permitted, vendored in `static/`
- Do not create a login route, session middleware, or OAuth flow
