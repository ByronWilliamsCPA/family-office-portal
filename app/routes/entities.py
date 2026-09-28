# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Entities section routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app import cache
from app.routes._context import freshness, include_confidential
from app.templating import render

router = APIRouter(tags=["entities"])


@router.get(
    "/entities",
    summary="Entity list",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def entities_index(request: Request) -> Response:
    """Render every cached LLC and trust.

    Authentication: Viewer or Admin (ADR-005).

    Args:
        request (Request): Current request.

    Returns:
        Response: Rendered entity list.
    """
    return render(
        request,
        "pages/entities.html",
        section="entities",
        entities=await cache.get_entities(),
        **(await freshness("entities")),
    )


@router.get(
    "/entities/{entity_id}",
    summary="Entity detail",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    responses={404: {"description": "Entity not found"}},
)
async def entity_detail(request: Request, entity_id: str) -> Response:
    """Render one entity with its documents.

    Authentication: Viewer or Admin (ADR-005).

    Args:
        request (Request): Current request.
        entity_id (str): Entity identifier.

    Returns:
        Response: Rendered entity page.

    Raises:
        HTTPException: 404 when the entity is not cached.
    """
    entity = await cache.get_entity(entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    documents = await cache.get_documents(
        include_confidential=include_confidential(request), entity_id=entity_id
    )
    return render(
        request,
        "pages/entity_detail.html",
        section="entities",
        entity=entity,
        documents=documents,
    )
