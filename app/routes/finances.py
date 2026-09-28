# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Finances section route."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app import cache
from app.routes._context import balances_summary, freshness
from app.templating import render

router = APIRouter(tags=["finances"])


@router.get(
    "/finances",
    summary="Account totals and digital currency",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def finances(request: Request) -> Response:
    """Render account totals and crypto positions from the cache.

    Authentication: Viewer or Admin (ADR-004).

    Args:
        request (Request): Current request.

    Returns:
        Response: Rendered finances page.
    """
    positions_state = await freshness("positions")
    return render(
        request,
        "pages/finances.html",
        section="finances",
        positions=await cache.get_positions(),
        positions_updated=positions_state["updated"],
        positions_stale=positions_state["stale"],
        **(await balances_summary()),
    )
