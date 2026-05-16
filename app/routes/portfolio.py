# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Portfolio section route (holdings and performance)."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["portfolio"])


@router.get(
    "/portfolio",
    summary="Holdings and performance",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def portfolio() -> HTMLResponse:
    """Render the portfolio page.

    Authentication: Viewer or Admin via Cloudflare Access. Reads the cached
    ``holdings`` and ``performance`` datasets sourced from the
    ``pp-security-master`` backend.

    Returns:
        HTMLResponse: Placeholder portfolio page.
    """
    return HTMLResponse("<h1>Portfolio</h1>")
