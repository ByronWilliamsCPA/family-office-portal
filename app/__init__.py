# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Family office portal: private estate view backed by Cloudflare Zero Trust.

This module exposes the FastAPI application instance ``app`` used by uvicorn
and by the test client. Application features (routes, scheduler, cache) are
added incrementally from Phase 1 onward; Phase A ships only the health probe
and the Cloudflare Access middleware stub so the CI scaffold has something
real to import and exercise.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.middleware import CloudflareAccessMiddleware

__version__ = "0.1.0"

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

    Returns:
        A dictionary with a single ``status`` key set to ``"ok"``.
    """
    return {"status": "ok"}
