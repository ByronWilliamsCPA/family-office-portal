# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Unit tests for the Phase 0 route stubs.

These tests cover the public surface enriched in the OpenAPI / Postman PR.
They assert status codes, content types, and the JSON shape of each typed
response. They do not exercise upstream backends or middleware (none exist
at Phase 0); subsequent phases will layer integration and resilience tests
on top of this baseline.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def client() -> AsyncClient:
    """Return an HTTPX client wired to the FastAPI app via ASGI transport."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_health_returns_ok(client: AsyncClient) -> None:
    """``/health`` returns the static liveness payload."""
    async with client as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "family-office-portal"}


async def test_home_returns_html(client: AsyncClient) -> None:
    """``/`` returns an HTML placeholder dashboard."""
    async with client as ac:
        response = await ac.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Family Office Portal" in response.text


async def test_documents_index_returns_html(client: AsyncClient) -> None:
    """``/documents`` returns an HTML placeholder folder view."""
    async with client as ac:
        response = await ac.get("/documents")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_documents_search_returns_empty_hits(client: AsyncClient) -> None:
    """``/documents/search`` returns a typed JSON hits list."""
    async with client as ac:
        response = await ac.get("/documents/search", params={"q": "anything"})
    assert response.status_code == 200
    assert response.json() == {"hits": []}


async def test_documents_search_rejects_empty_query(client: AsyncClient) -> None:
    """``/documents/search`` 422s when ``q`` is empty (min_length=1)."""
    async with client as ac:
        response = await ac.get("/documents/search", params={"q": ""})
    assert response.status_code == 422


async def test_document_preview_returns_pdf(client: AsyncClient) -> None:
    """``/documents/{id}/preview`` returns a PDF media type."""
    async with client as ac:
        response = await ac.get("/documents/doc_1/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


async def test_document_download_returns_octet_stream(client: AsyncClient) -> None:
    """``/documents/{id}/download`` returns an octet-stream media type."""
    async with client as ac:
        response = await ac.get("/documents/doc_1/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"


async def test_finances_returns_html(client: AsyncClient) -> None:
    """``/finances`` returns an HTML placeholder page."""
    async with client as ac:
        response = await ac.get("/finances")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_portfolio_returns_html(client: AsyncClient) -> None:
    """``/portfolio`` returns an HTML placeholder page."""
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_entities_index_returns_html(client: AsyncClient) -> None:
    """``/entities`` returns an HTML placeholder list page."""
    async with client as ac:
        response = await ac.get("/entities")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_entity_detail_returns_html(client: AsyncClient) -> None:
    """``/entities/{id}`` returns an HTML placeholder detail page."""
    async with client as ac:
        response = await ac.get("/entities/ent_42")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_admin_refresh_status_returns_empty_entries(
    client: AsyncClient,
) -> None:
    """``/admin/refresh-status`` returns a typed JSON entries list."""
    async with client as ac:
        response = await ac.get("/admin/refresh-status")
    assert response.status_code == 200
    assert response.json() == {"entries": []}


async def test_admin_refresh_trigger_default_body(client: AsyncClient) -> None:
    """``POST /admin/refresh/{service}`` accepts an empty JSON body."""
    async with client as ac:
        response = await ac.post("/admin/refresh/entities", json={})
    assert response.status_code == 202
    assert response.json() == {
        "service": "entities",
        "scheduled": True,
        "forced": False,
    }


async def test_admin_refresh_trigger_with_force(client: AsyncClient) -> None:
    """``POST /admin/refresh/{service}`` honours the ``force`` flag."""
    async with client as ac:
        response = await ac.post(
            "/admin/refresh/holdings",
            json={"force": True},
        )
    assert response.status_code == 202
    assert response.json() == {
        "service": "holdings",
        "scheduled": True,
        "forced": True,
    }


async def test_admin_refresh_trigger_rejects_bad_body(client: AsyncClient) -> None:
    """``POST /admin/refresh/{service}`` 422s on non-coercible ``force``."""
    async with client as ac:
        response = await ac.post(
            "/admin/refresh/positions",
            json={"force": {"nested": "object"}},
        )
    assert response.status_code == 422
