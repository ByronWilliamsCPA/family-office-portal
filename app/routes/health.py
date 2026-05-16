# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Liveness probe used by deploy infrastructure and the Newman CI workflow."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.models import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Liveness probe",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
)
async def health() -> HealthResponse:
    """Return a static liveness payload.

    Unauthenticated. Intended for orchestrator probes and the Newman CI
    workflow so polling can begin before any backend services are reachable.

    Returns:
        HealthResponse: Status indicator and service name.
    """
    return HealthResponse(status="ok", service="family-office-portal")
