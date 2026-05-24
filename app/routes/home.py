# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Home dashboard route."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["home"])


@router.get(
    "/",
    summary="Home dashboard",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def home() -> HTMLResponse:
    """Render the landing dashboard.

    Authentication: Viewer or Admin via Cloudflare Access (see ADR-002). Phase 0
    returns a static HTML placeholder; Phase 1 will swap in a Jinja2 template
    that aggregates the five sections.

    Returns:
        HTMLResponse: Placeholder dashboard page.
    """
    return HTMLResponse("<h1>Family Office Portal</h1>")
