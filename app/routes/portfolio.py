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
from app.routes._context import freshness, portfolio_accounts
from app.templating import render

router = APIRouter(tags=["portfolio"])


@router.get(
    "/portfolio",
    summary="Accounts and holdings",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def portfolio(request: Request) -> Response:
    """Render investment-type accounts, plus holdings when pp-security is connected.

    The account list comes from the cached balance feed and needs no backend.
    The per-security table is shown by the template only when the
    pp-security-master backend is connected and holdings are cached. Reads
    SQLite only.

    Authentication: Viewer or Admin (ADR-005).

    Args:
        request (Request): Current request.

    Returns:
        Response: Rendered portfolio page.
    """
    holdings_state = await freshness("holdings")
    return render(
        request,
        "pages/portfolio.html",
        section="portfolio",
        holdings=await cache.get_holdings(),
        holdings_updated=holdings_state["updated"],
        holdings_stale=holdings_state["stale"],
        **(await portfolio_accounts()),
    )
