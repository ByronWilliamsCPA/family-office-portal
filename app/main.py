# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""FastAPI application instantiation, middleware registration, and meta routes.

Phase 0 ships only the ``/health`` liveness probe and the Cloudflare Access
middleware stub described in ADR-002. Application content routes, the
APScheduler, and the SQLite cache layer land in Phase 1.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from app import __version__
from app.middleware import CloudflareAccessMiddleware

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
        "Private, read-only family estate portal aggregating entity, document, "
        "finance, and portfolio data from internal backend services. Runs behind "
        "Cloudflare Zero Trust."
    ),
    version=__version__,
    contact={
        "name": "Byron Williams",
        "email": "byronawilliams@gmail.com",
    },
)

app.add_middleware(CloudflareAccessMiddleware)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Return a static liveness indicator.

    ``/health`` is the documented JSON exception to the project's
    HTML-only response rule; it exists for uptime probes, not user content.

    Returns:
        A dictionary with a single ``status`` key set to ``"ok"``.
    """
    return {"status": "ok"}
