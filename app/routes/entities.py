# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Entities section routes (LLC and trust list and detail views)."""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/entities", tags=["entities"])


@router.get(
    "",
    summary="List entities",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def entities_index() -> HTMLResponse:
    """Render the entity list with compliance status.

    Authentication: Viewer or Admin via Cloudflare Access. Reads the cached
    ``entities`` dataset sourced from the ``llc-manager`` backend.

    Returns:
        HTMLResponse: Placeholder entity list page.
    """
    return HTMLResponse("<h1>Entities</h1>")


@router.get(
    "/{entity_id}",
    summary="Entity detail",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def entity_detail(entity_id: str) -> HTMLResponse:
    """Render a single entity detail view.

    Authentication: Viewer or Admin via Cloudflare Access. Raw identifiers
    are not surfaced to primary users (see CLAUDE.md frontend conventions);
    Phase 1 will render a plain-English summary backed by the cache.

    Args:
        entity_id (str): Opaque entity identifier sourced from the cache.

    Returns:
        HTMLResponse: Placeholder entity detail page.
    """
    _ = entity_id
    return HTMLResponse("<h1>Entity</h1>")
