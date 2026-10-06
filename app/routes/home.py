# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Home dashboard route."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app import cache
from app.routes._context import (
    balance_breakdown,
    balances_summary,
    include_confidential,
)
from app.templating import render

router = APIRouter(tags=["home"])

_UPCOMING_LIMIT = 5
_RECENT_LIMIT = 5


@router.get(
    "/",
    summary="Home dashboard",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def home(request: Request) -> Response:
    """Render the landing dashboard: totals, upcoming dates, recent documents.

    Authentication: Viewer or Admin (ADR-005).

    Args:
        request (Request): Current request.

    Returns:
        Response: Rendered dashboard page.
    """
    entities = await cache.get_entities()
    upcoming = sorted(
        (e for e in entities if e["next_date"]),
        key=lambda e: str(e["next_date"]),
    )[:_UPCOMING_LIMIT]
    documents = await cache.get_documents(
        include_confidential=include_confidential(request)
    )
    recent = sorted(documents, key=lambda d: str(d["added_at"] or ""), reverse=True)[
        :_RECENT_LIMIT
    ]
    return render(
        request,
        "pages/home.html",
        section="home",
        upcoming=upcoming,
        recent_documents=recent,
        **(await balances_summary()),
        **(await balance_breakdown()),
    )
