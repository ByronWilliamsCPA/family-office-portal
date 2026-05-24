# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""FastAPI application instantiation, middleware registration, and meta routes.

Phase 0 ships the five section stubs, the ``/health`` liveness probe, the admin
surface, and the Cloudflare Access middleware described in ADR-002. The
APScheduler refresh jobs and the live SQLite cache reader land in Phase 1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app import __version__
from app.middleware import CloudflareAccessMiddleware
from app.routes import admin, documents, entities, finances, health, home, portfolio

_REQUIRED_ENV_VARS = (
    "BACKEND_LLC_MANAGER_URL",
    "BACKEND_PP_SECURITY_URL",
    "BACKEND_XERO_CRYPTO_URL",
    "BACKEND_FAMILY_OFFICE_URL",
    "CF_TEAM_DOMAIN",
    "CF_ACCESS_APP_ID",
    "VIEWER_EMAILS",
    "ADMIN_EMAILS",
    "SQLITE_PATH",
)

for _var in _REQUIRED_ENV_VARS:
    if not os.environ.get(_var):
        sys.exit(1)

app: FastAPI = FastAPI(
    title="Family Office Portal",
    description=(
        "Private read-only family estate portal aggregating entity, document, "
        "finance, and portfolio data from four internal backend services behind "
        "a SQLite read-through cache. Runs behind Cloudflare Zero Trust. "
        "Page routes return server-rendered HTML; admin and health routes return JSON. "
        "See docs/planning/tech-spec.md for the full contract."
    ),
    version=__version__,
    contact={"name": "Byron Williams"},
    license_info={"name": "MIT", "identifier": "MIT"},
    openapi_tags=[
        {"name": "health", "description": "Liveness probes for orchestrators."},
        {"name": "home", "description": "Landing dashboard."},
        {
            "name": "documents",
            "description": "Document folder views and proxied previews.",
        },
        {
            "name": "finances",
            "description": "Net worth and asset allocation aggregations.",
        },
        {
            "name": "portfolio",
            "description": "Holdings and performance from pp-security-master.",
        },
        {
            "name": "entities",
            "description": "LLC and trust list and detail views from llc-manager.",
        },
        {
            "name": "admin",
            "description": "Refresh status and manual refresh triggers (Admin role).",
        },
    ],
)

app.add_middleware(CloudflareAccessMiddleware)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(health.router)
app.include_router(home.router)
app.include_router(documents.router)
app.include_router(finances.router)
app.include_router(portfolio.router)
app.include_router(entities.router)
app.include_router(admin.router)
