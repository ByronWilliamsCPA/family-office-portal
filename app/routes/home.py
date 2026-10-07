# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Home dashboard route."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app.routes._context import home_context
from app.templating import render

router = APIRouter(tags=["home"])


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
