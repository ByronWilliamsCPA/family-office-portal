# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Admin routes (refresh status and manual triggers)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status

from app.models import (
    RefreshStatusResponse,
    RefreshTriggerRequest,
    RefreshTriggerResponse,
)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/refresh-status",
    summary="Per-service refresh log",
    status_code=status.HTTP_200_OK,
)
async def refresh_status() -> RefreshStatusResponse:
    """Return the most recent refresh outcome per backend service.

    Authentication: Admin only via Cloudflare Access. Reads the ``refresh_log``
    table populated by the APScheduler jobs.

    Returns:
        RefreshStatusResponse: One entry per tracked backend.
    """
    return RefreshStatusResponse(entries=[])


@router.post(
    "/refresh/{service}",
    summary="Trigger a manual refresh",
    status_code=status.HTTP_202_ACCEPTED,
    responses={422: {"description": "Validation error"}},
)
async def trigger_refresh(
    service: Literal["entities", "holdings", "positions", "documents"],
    body: RefreshTriggerRequest,
) -> RefreshTriggerResponse:
    """Enqueue an out-of-band refresh for the named backend service.

    Authentication: Admin only via Cloudflare Access. Phase 0 returns a stub
    response; Phase 1 will dispatch to the APScheduler job for the matching
    service (``entities``, ``holdings``, ``positions``, ``documents``).

    Args:
        service (Literal["entities", "holdings", "positions", "documents"]):
            Backend service identifier to refresh.
        body (RefreshTriggerRequest): Trigger options; set ``force`` to
            bypass the staleness check.

    Returns:
        RefreshTriggerResponse: Confirmation of the scheduling decision.
    """
    return RefreshTriggerResponse(
        service=service,
        scheduled=True,
        forced=body.force,
    )
