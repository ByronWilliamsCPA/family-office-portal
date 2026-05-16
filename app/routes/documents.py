# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Document folder routes (proxy to ``family_office`` backend)."""

from __future__ import annotations

from fastapi import APIRouter, Query, status
from fastapi.responses import HTMLResponse, Response

from app.models import DocumentSearchResponse

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get(
    "",
    summary="List document folders",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
)
async def documents_index() -> HTMLResponse:
    """Render the document folder view.

    Authentication: Viewer or Admin via Cloudflare Access. Data is read from
    the SQLite cache populated by the ``refresh_documents`` job; the route
    never calls the upstream ``family_office`` service directly (ADR-003).

    Returns:
        HTMLResponse: Placeholder folder listing page.
    """
    return HTMLResponse("<h1>Documents</h1>")


@router.get(
    "/search",
    summary="Search documents by name",
    response_model=DocumentSearchResponse,
    status_code=status.HTTP_200_OK,
)
async def documents_search(
    q: str = Query(..., min_length=1, description="Free-text search query."),
) -> DocumentSearchResponse:
    """Return documents whose name matches ``q``.

    Authentication: Viewer or Admin via Cloudflare Access. Designed as an HTMX
    partial in Phase 1; the JSON shape is returned at Phase 0 so the contract
    is OpenAPI-described from the start.

    Args:
        q: Free-text query string; must be at least one character.

    Returns:
        DocumentSearchResponse: Matching documents in ranked order.
    """
    _ = q
    return DocumentSearchResponse(hits=[])


@router.get(
    "/{document_id}/preview",
    summary="Inline document preview",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "PDF preview stream.",
        },
    },
)
async def document_preview(document_id: str) -> Response:
    """Stream a PDF preview proxied from the ``family_office`` backend.

    Authentication: Viewer or Admin via Cloudflare Access. Phase 0 returns an
    empty placeholder; Phase 1 will stream upstream content.

    Args:
        document_id: Opaque document identifier sourced from the cache.

    Returns:
        Response: Placeholder PDF response.
    """
    _ = document_id
    return Response(content=b"", media_type="application/pdf")


@router.get(
    "/{document_id}/download",
    summary="Download original document",
    status_code=status.HTTP_200_OK,
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Binary document download.",
        },
    },
)
async def document_download(document_id: str) -> Response:
    """Stream the original document as a file download.

    Authentication: Viewer or Admin via Cloudflare Access. Phase 0 returns an
    empty placeholder; Phase 1 will stream upstream content with a
    ``Content-Disposition: attachment`` header.

    Args:
        document_id: Opaque document identifier sourced from the cache.

    Returns:
        Response: Placeholder binary response.
    """
    _ = document_id
    return Response(content=b"", media_type="application/octet-stream")
