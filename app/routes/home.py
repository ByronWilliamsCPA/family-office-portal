# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Home dashboard route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app import cache
from app.chat.service import chat_panel_state
from app.routes._context import (
    balance_breakdown,
    balances_summary,
    include_confidential,
)
from app.templating import render

router = APIRouter(tags=["home"])

_UPCOMING_LIMIT = 5
_RECENT_LIMIT = 5


async def home_context(request: Request) -> dict[str, Any]:
    """Build the Home page context: balances, dates, documents and chat state.

    Args:
        request (Request): Current request.

    Returns:
        dict[str, Any]: Template values for ``pages/home.html``.
    """
    entities = await cache.get_entities()
    upcoming = sorted(
        (e for e in entities if e["next_date"]),
        key=lambda e: str(e["next_date"]),
    )[:_UPCOMING_LIMIT]
    admin = include_confidential(request)
    documents = await cache.get_documents(include_confidential=admin)
    recent = sorted(documents, key=lambda d: str(d["added_at"] or ""), reverse=True)[
        :_RECENT_LIMIT
    ]
    return {
        "upcoming": upcoming,
        "recent_documents": recent,
        "chat": chat_panel_state(is_admin=admin),
        **(await balances_summary()),
        **(await balance_breakdown()),
    }


@router.get(
    "/",
    summary="Home dashboard",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def home(request: Request) -> Response:
    """Render the landing dashboard: totals, dates, documents and chat.

    Authentication: Viewer or Admin (ADR-005).

    Args:
        request (Request): Current request.

    Returns:
        Response: Rendered dashboard page.
    """
    return render(
        request, "pages/home.html", section="home", **(await home_context(request))
    )
