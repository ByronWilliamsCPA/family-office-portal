# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Portfolio section route."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app import cache
from app.routes._context import freshness
from app.templating import render

router = APIRouter(tags=["portfolio"])


@router.get(
    "/portfolio",
    summary="Holdings",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def portfolio(request: Request) -> Response:
    """Render cached holdings from pp-security-master.

    Authentication: Viewer or Admin (ADR-005).

    Args:
        request (Request): Current request.

    Returns:
        Response: Rendered portfolio page.
    """
    return render(
        request,
        "pages/portfolio.html",
        section="portfolio",
        holdings=await cache.get_holdings(),
        **(await freshness("holdings")),
    )
