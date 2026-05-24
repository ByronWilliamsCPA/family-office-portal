# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Pydantic models for the family office portal API surface.

These models describe the JSON-shaped responses and request bodies used by the
admin and health endpoints. Page routes return server-rendered HTML and do not
have a response_model; see ADR-001 for the rendering decision.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness probe payload returned by ``GET /health``."""

    status: str = Field(description="Service status indicator.", examples=["ok"])
    service: str = Field(
        description="Service name reported by the running process.",
        examples=["family-office-portal"],
    )


class RefreshLogEntry(BaseModel):
    """One row of the refresh history for a backend service."""

    service: str = Field(
        description="Backend service identifier.",
        examples=["llc-manager"],
    )
    last_success_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp of the most recent successful refresh.",
        examples=["2026-05-16T03:00:00Z"],
    )
    last_error_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp of the most recent failed refresh, if any.",
        examples=["2026-05-15T22:14:01Z"],
    )
    last_error_message: str | None = Field(
        default=None,
        description="Truncated error message from the most recent failure.",
        examples=["upstream returned HTTP 500"],
    )
    is_stale: bool = Field(
        description="True when the cached dataset has exceeded its staleness window.",
        examples=[False],
    )


class RefreshStatusResponse(BaseModel):
    """Response payload for ``GET /admin/refresh-status``."""

    entries: list[RefreshLogEntry] = Field(
        description="One entry per backend service tracked by the scheduler.",
    )


class RefreshTriggerRequest(BaseModel):
    """Request body for ``POST /admin/refresh/{service}``."""

    force: bool = Field(
        default=False,
        description=(
            "When true, bypass the staleness check and refresh immediately even "
            "if cached data is fresh."
        ),
        examples=[True],
    )


class RefreshTriggerResponse(BaseModel):
    """Response payload for ``POST /admin/refresh/{service}``."""

    service: str = Field(
        description="Backend service whose refresh job was scheduled.",
        examples=["llc-manager"],
    )
    scheduled: bool = Field(
        description="True when the refresh job was enqueued.",
        examples=[True],
    )
    forced: bool = Field(
        description="True when the staleness check was bypassed.",
        examples=[False],
    )


class DocumentSearchHit(BaseModel):
    """Single search result returned by ``GET /documents/search``."""

    id: str = Field(description="Document identifier.", examples=["doc_4f10"])
    name: str = Field(
        description="Display name of the document.",
        examples=["2025 K-1 Schedule"],
    )
    category: str = Field(description="Document category.", examples=["Taxes"])


class DocumentSearchResponse(BaseModel):
    """Response payload for ``GET /documents/search``."""

    hits: list[DocumentSearchHit] = Field(
        description="Matching documents in ranked order.",
    )
