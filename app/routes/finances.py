# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Finances section route (net worth and asset allocation)."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["finances"])


@router.get(
    "/finances",
    summary="Net worth and asset allocation",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def finances() -> HTMLResponse:
    """Render the finances dashboard.

    Authentication: Viewer or Admin via Cloudflare Access. Aggregates the
    ``holdings`` dataset (``pp-security-master``) with the ``positions``
    dataset (``xero_crypto``) from the SQLite cache.

    Returns:
        HTMLResponse: Placeholder finances page.
    """
    return HTMLResponse("<h1>Finances</h1>")
