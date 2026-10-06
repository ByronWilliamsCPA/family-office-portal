# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Documents section routes: folders, name search, preview, and download.

Preview and download stream the file from llc-manager on each request, a
bounded exception to the cached-read rule of ADR-003 recorded in ADR-007.
The folder and search views read the SQLite cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from starlette.responses import Response, StreamingResponse

from app import cache, document_files
from app.config import BackendConfigError, load_settings
from app.routes._context import freshness, include_confidential
from app.templating import render

if TYPE_CHECKING:
    import aiosqlite

router = APIRouter(prefix="/documents", tags=["documents"])

logger = structlog.get_logger(__name__)

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

    When the document service is not connected the fragment says so, as the
    full page does, instead of listing links that could only answer 503.

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
    return render(
        request,
        "partials/document_results.html",
        section="documents",
        query=query,
        documents=results,
    )


_FILE_RESPONSES: dict[int | str, dict[str, str]] = {
    200: {"description": "The file, streamed from the document service"},
    404: {"description": "Document not found, or not visible to this user"},
    502: {"description": "The document service failed or refused the request"},
    503: {"description": "The document service is not connected"},
    504: {"description": "The document service did not answer in time"},
}


async def _stream_document(
    request: Request,
    document_id: str,
    requested: document_files.Disposition,
) -> StreamingResponse:
    """Check visibility in the cache, then stream the file from upstream.

    Args:
        request (Request): Current request.
        document_id (str): Document identifier from the path.
        requested (document_files.Disposition): ``inline`` or ``attachment``.

    Returns:
        StreamingResponse: The file with safe headers.

    Raises:
        HTTPException: 404 when unknown or hidden from this user (no upstream
            request is made), 503 when the document service is not
            connected, or the mapped status of an upstream failure.
    """
    # #CRITICAL: security: visibility is decided from the cache before any
    # upstream request, so a Viewer never causes a confidential file to be
    # fetched. #VERIFY: tests/unit/test_document_files.py proves no request.
    # #EDGE: a document marked confidential upstream stays visible to Viewers
    # until the next successful documents refresh (scheduled every 12 hours).
    # A failed refresh keeps the old cache, so the window lasts until one
    # succeeds; a malformed item is skipped rather than failing the refresh.
    # #VERIFY: after marking a document confidential, trigger
    # ``POST /admin/refresh/documents`` and confirm success in
    # ``/admin/refresh-status``.
    document = await cache.get_document(
        document_id, include_confidential=include_confidential(request)
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    settings = load_settings()
    try:
        connection = settings.backend_connection("llc_manager")
    except BackendConfigError:
        logger.warning("document_file_failed", reason="backend_misconfigured")
        connection = None
    if connection is None:
        logger.info("document_file_failed", reason="backend_not_connected")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        upstream = await document_files.open_upstream(
            connection,
            str(document["id"]),
            timeout_seconds=settings.backend_timeout_seconds,
        )
    except document_files.FileProxyError as exc:
        raise HTTPException(status_code=exc.status_code) from exc
    built = False
    try:
        headers = document_files.response_headers(
            upstream, title=str(document["name"]), requested=requested
        )
        built = True
    finally:
        if not built:
            # Nothing will stream, so nothing else would close the upstream.
            await upstream.aclose()
    return document_files.UpstreamFileResponse(upstream, headers=headers)


@router.get(
    "/{document_id}/preview",
    summary="Inline document preview",
    response_class=StreamingResponse,
    responses=_FILE_RESPONSES,
)
async def document_preview(request: Request, document_id: str) -> StreamingResponse:
    """Show a document in the browser.

    PDFs and images open inline; any other type downloads instead. A link
    may add ``#page=N`` to open a PDF at a page. Authentication: Viewer or
    Admin (ADR-005); Viewers get 404 for confidential documents.

    Args:
        request (Request): Current request.
        document_id (str): Document identifier.

    Returns:
        StreamingResponse: The file, shown inline when its type allows.
    """
    return await _stream_document(request, document_id, "inline")


@router.get(
    "/{document_id}/download",
    summary="Download a document",
    response_class=StreamingResponse,
    responses=_FILE_RESPONSES,
)
async def document_download(request: Request, document_id: str) -> StreamingResponse:
    """Download a document as a file.

    Authentication: Viewer or Admin (ADR-005); Viewers get 404 for
    confidential documents.

    Args:
        request (Request): Current request.
        document_id (str): Document identifier.

    Returns:
        StreamingResponse: The file as an attachment.
    """
    return await _stream_document(request, document_id, "attachment")
