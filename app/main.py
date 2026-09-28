# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""FastAPI application: startup checks, lifespan, middleware, and routes.

Identity comes from Authentik forward auth (ADR-004). Route handlers read
only from the SQLite cache (ADR-003); the APScheduler refresh jobs fill it.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

from app import __version__
from app.config import REQUIRED_ENV_VARS, load_settings
from app.db import init_schema
from app.middleware import AuthentikAuthMiddleware
from app.routes import admin, documents, entities, finances, health, home, portfolio
from app.scheduler import build_scheduler
from app.templating import render

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.requests import Request
    from starlette.responses import Response

_missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
if _missing:
    # Names only, never values, so no secret reaches the container log.
    sys.stderr.write(
        "Missing required environment variables: " + ", ".join(_missing) + "\n"
    )
    sys.exit(1)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize the schema and run the refresh scheduler for the app's life.

    Args:
        _app (FastAPI): The application instance.

    Yields:
        None: Control while the application serves requests.
    """
    settings = load_settings()
    init_schema(settings.sqlite_path)
    scheduler = build_scheduler(settings) if settings.scheduler_enabled else None
    if scheduler is not None:
        scheduler.start()  # pyright: ignore[reportUnknownMemberType]  # untyped APScheduler 3
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)  # pyright: ignore[reportUnknownMemberType]


app: FastAPI = FastAPI(
    title="Family Office Portal",
    description=(
        "Private read-only family estate portal aggregating entity, document, "
        "finance, and portfolio data from internal backend services behind a "
        "SQLite read-through cache. Identity comes from Authentik forward auth "
        "(ADR-004). Page routes return server-rendered HTML; admin and health "
        "routes return JSON. See docs/planning/tech-spec.md for the contract."
    ),
    version=__version__,
    contact={"name": "Byron Williams"},
    license_info={"name": "MIT", "identifier": "MIT"},
    lifespan=lifespan,
    openapi_tags=[
        {"name": "health", "description": "Liveness probes for orchestrators."},
        {"name": "home", "description": "Landing dashboard."},
        {"name": "documents", "description": "Document folders, search, and previews."},
        {"name": "finances", "description": "Account totals and digital currency."},
        {"name": "portfolio", "description": "Holdings from pp-security-master."},
        {"name": "entities", "description": "LLC and trust views from llc-manager."},
        {
            "name": "admin",
            "description": "Refresh status and manual refresh triggers (Admin role).",
        },
    ],
)

app.add_middleware(AuthentikAuthMiddleware)


@app.exception_handler(StarletteHTTPException)
async def http_error_page(request: Request, exc: StarletteHTTPException) -> Response:
    """Show plain-English pages to people and JSON to admin and API callers.

    Args:
        request (Request): Current request.
        exc (StarletteHTTPException): The raised HTTP error.

    Returns:
        Response: HTML for page routes, JSON for ``/admin`` and ``/health``.
    """
    path = request.url.path
    if path.startswith(("/admin", "/health")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:  # noqa: PLR2004  # HTTP status
        return render(request, "pages/not_found.html", section="", status_code=404)
    return PlainTextResponse(
        "This is not available right now. Please try again later.",
        status_code=exc.status_code,
    )


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(health.router)
app.include_router(home.router)
app.include_router(documents.router)
app.include_router(finances.router)
app.include_router(portfolio.router)
app.include_router(entities.router)
app.include_router(admin.router)
