# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Admin routes (refresh status and manual triggers). Admin role only."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, status

from app import cache
from app.models import (
    RefreshLogEntry,
    RefreshStatusResponse,
    RefreshTriggerRequest,
    RefreshTriggerResponse,
)
from app.scheduler import TRIGGERS

router = APIRouter(prefix="/admin", tags=["admin"])

# refresh_log service name -> cached dataset it fills.
_SERVICE_DATASETS: dict[str, str] = {
    "llc-manager": "entities",
    "pp-security-master": "holdings",
    "xero_crypto": "positions",
    "llc-manager-documents": "documents",
}


@router.get(
    "/refresh-status",
    summary="Per-service refresh log",
    status_code=status.HTTP_200_OK,
)
async def refresh_status() -> RefreshStatusResponse:
    """Return the latest success and failure per backend service.

    Authentication: Admin only (ADR-005). Services that have never run are
    omitted.

    Returns:
        RefreshStatusResponse: One entry per service that has run.
    """
    entries: dict[str, RefreshLogEntry] = {}
    for row in await cache.get_refresh_log():
        service = str(row["service"])
        entry = entries.get(service)
        if entry is None:
            dataset = _SERVICE_DATASETS.get(service)
            stale = (
                await cache.is_stale(dataset, cache.STALENESS_HOURS[dataset])
                if dataset
                else False
            )
            entry = RefreshLogEntry(service=service, is_stale=stale)
            entries[service] = entry
        if row["status"] == "success" and entry.last_success_at is None:
            entry.last_success_at = str(row["ran_at"])
        if row["status"] == "error" and entry.last_error_at is None:
            entry.last_error_at = str(row["ran_at"])
            entry.last_error_message = row["message"]
    return RefreshStatusResponse(entries=list(entries.values()))


@router.post(
    "/refresh/{service}",
    summary="Trigger a manual refresh",
    status_code=status.HTTP_202_ACCEPTED,
    responses={422: {"description": "Validation error"}},
)
async def trigger_refresh(
    service: Literal["entities", "holdings", "positions", "documents"],
    body: RefreshTriggerRequest,
    background: BackgroundTasks,
) -> RefreshTriggerResponse:
    """Run the named refresh job after the response is sent.

    Authentication: Admin only (ADR-005).

    Args:
        service (Literal["entities", "holdings", "positions", "documents"]):
            Dataset to refresh.
        body (RefreshTriggerRequest): Trigger options. Refreshes always run
            now; ``force`` is echoed for compatibility.
        background (BackgroundTasks): FastAPI background task queue.

    Returns:
        RefreshTriggerResponse: Confirmation that the job was queued.
    """
    background.add_task(TRIGGERS[service])
    return RefreshTriggerResponse(service=service, scheduled=True, forced=body.force)
