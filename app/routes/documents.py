# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Documents section routes: folders, name search, preview, and download."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, NoReturn

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app import cache
from app.routes._context import freshness, include_confidential
from app.templating import render, templates

if TYPE_CHECKING:
    import aiosqlite

router = APIRouter(prefix="/documents", tags=["documents"])

OptionalSearch = Annotated[
    str | None, Query(max_length=200, description="Optional name search.")
]
RequiredSearch = Annotated[
    str, Query(min_length=1, max_length=200, description="Text to find in names.")
]


def _group_by_category(
    documents: list[aiosqlite.Row],
) -> list[tuple[str, list[aiosqlite.Row]]]:
    groups: dict[str, list[aiosqlite.Row]] = {}
    for doc in documents:
        groups.setdefault(str(doc["category"]), []).append(doc)
    return sorted(groups.items())


@router.get(
    "",
    summary="Document folders",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def documents_index(
    request: Request,
    q: OptionalSearch = None,
) -> Response:
    """Render documents grouped by category, with optional name search.

    Works without JavaScript: the search form submits here with ``q``.
    Authentication: Viewer or Admin (ADR-005). Viewers never see
    confidential documents.

    Args:
        request (Request): Current request.
        q (OptionalSearch): Optional name search text.

    Returns:
        Response: Rendered documents page.
    """
    admin = include_confidential(request)
    documents = await cache.get_documents(include_confidential=admin)
    query = (q or "").strip()
    results = (
        await cache.search_documents(query, include_confidential=admin) if query else []
    )
    return render(
        request,
        "pages/documents.html",
        section="documents",
        categories=_group_by_category(documents),
        query=query,
        documents=results,
        **(await freshness("documents")),
    )


@router.get(
    "/search",
    summary="Search documents by name (HTMX partial)",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def documents_search(
    request: Request,
    q: RequiredSearch,
) -> Response:
    """Return the search results fragment used by the Documents page.

    Args:
        request (Request): Current request.
        q (RequiredSearch): Text to find in document names.

    Returns:
        Response: HTML fragment listing matching documents.
    """
    query = q.strip()
    results = await cache.search_documents(
        query, include_confidential=include_confidential(request)
    )
    return templates.TemplateResponse(
        request,
        "partials/document_results.html",
        {"query": query, "documents": results},
    )


async def _require_document(request: Request, document_id: str) -> aiosqlite.Row:
    documents = await cache.get_documents(
        include_confidential=include_confidential(request)
    )
    for doc in documents:
        if doc["id"] == document_id:
            return doc
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.get(
    "/{document_id}/preview",
    summary="Inline document preview",
    response_model=None,
    responses={
        404: {"description": "Document not found"},
        503: {"description": "Not yet available"},
    },
)
async def document_preview(request: Request, document_id: str) -> NoReturn:
    """Show a document inline.

    The planned file proxy to llc-manager is not built yet; until it is, a known
    document returns 503.

    Args:
        request (Request): Current request.
        document_id (str): Document identifier.

    Raises:
        HTTPException: 404 when unknown or not visible, 503 until the file proxy exists.
    """
    await _require_document(request, document_id)
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


@router.get(
    "/{document_id}/download",
    summary="Download a document",
    response_model=None,
    responses={
        404: {"description": "Document not found"},
        503: {"description": "Not yet available"},
    },
)
async def document_download(request: Request, document_id: str) -> NoReturn:
    """Download a document.

    The planned file proxy to llc-manager is not built yet; until it is, a known
    document returns 503.

    Args:
        request (Request): Current request.
        document_id (str): Document identifier.

    Raises:
        HTTPException: 404 when unknown or not visible, 503 until the file proxy exists.
    """
    await _require_document(request, document_id)
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
