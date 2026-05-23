# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""FastAPI application instantiation, middleware registration, and meta routes.

Phase 0 ships only the ``/health`` liveness probe and the Cloudflare Access
middleware stub described in ADR-002. Application content routes, the
APScheduler, and the SQLite cache layer land in Phase 1.
"""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.middleware import CloudflareAccessMiddleware

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


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Return a static liveness indicator.

    ``/health`` is the documented JSON exception to the project's
    HTML-only response rule; it exists for uptime probes, not user content.

    Returns:
        A dictionary with a single ``status`` key set to ``"ok"``.
    """
    return {"status": "ok"}
