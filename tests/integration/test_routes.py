# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Integration tests for portal route handlers.

Spec contract (tech-spec §4 endpoint table + ``CLAUDE.md`` + ADR-005):

* All non-public routes require a valid ``X-authentik-jwt`` token;
  unauthenticated requests get **403** (per ADR-005, fail closed).
* ``/admin/*`` routes require role=Admin; Viewer requests get 403.
* Routes return ``TemplateResponse`` (HTML), not JSON, except HTMX partial
  routes which return HTML fragments.
* All five sections render even when their backing dataset is empty (graceful
  degradation -- no blank screens for primary users).
* ``/health`` is a public liveness endpoint that bypasses the auth middleware
  (uptime probes, not user content; documented JSON exception).

Phase gating: the section-route tests skip until ``app.main`` has the Phase 1
routes mounted. ``/health`` works in Phase 0/A.
"""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
import pytest

if importlib.util.find_spec("app.main") is None:
    pytest.skip("app.main not implemented yet", allow_module_level=True)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _phase1_routes_present() -> bool:
    """True iff section routes (e.g. ``/documents``) are mounted on app.main.

    # noqa
    """
    try:
        main = importlib.import_module("app.main")
    except SystemExit:
        # Startup fail-fast fired because collection runs without the env
        # vars; that behavior itself only exists once Phase 1 is in place.
        return True
    paths = {getattr(r, "path", "") for r in main.app.routes}
    return "/documents" in paths


phase1 = pytest.mark.skipif(
    not _phase1_routes_present(),
    reason="Phase 1 section routes not yet mounted on app.main",
)


# --------------------------------------------------------------------------- #
# Test client + auth fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(
    portal_env: dict[str, str],
    tmp_db_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an opened ``httpx.AsyncClient`` bound to a freshly loaded app.

    Unlike the shared ``client`` fixture this one sends no identity header;
    tests pass ``viewer_headers`` or ``admin_headers`` (``tests/conftest.py``)
    explicitly.

    # noqa
    """
    del portal_env
    main = importlib.import_module("app.main")

    if importlib.util.find_spec("app.db") is not None:
        db = importlib.import_module("app.db")
        if hasattr(db, "init_schema"):
            db.init_schema(str(tmp_db_path))

    importlib.reload(main)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as ac:
        yield ac


# --------------------------------------------------------------------------- #
# Public endpoints (Phase 0 / A; no JWT required)
# --------------------------------------------------------------------------- #


async def test_health_endpoint_is_public(
    client: httpx.AsyncClient,
) -> None:
    """``/health`` returns 200 with status and service fields, and bypasses auth.

    Uptime-probe endpoint, documented JSON exception to the HTML-only rule.

    # noqa
    """
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "family-office-portal"}


# --------------------------------------------------------------------------- #
# Auth enforcement (every primary route requires a valid JWT)
# --------------------------------------------------------------------------- #

PRIMARY_ROUTES = ["/", "/documents", "/finances", "/portfolio", "/entities"]


@phase1
@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_unauthenticated_request_is_rejected(
    client: httpx.AsyncClient, path: str
) -> None:
    """A request to any primary route without a JWT is rejected with 403.

    ADR-005: portal middleware returns 403 (not 401) for missing/invalid JWT.

    # noqa
    """
    response = await client.get(path)
    assert response.status_code == 403


@phase1
@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_invalid_jwt_is_rejected(client: httpx.AsyncClient, path: str) -> None:
    """A request bearing a malformed JWT is rejected with 403.

    # noqa
    """
    response = await client.get(path, headers={"X-authentik-jwt": "garbage.token.here"})
    assert response.status_code == 403


@phase1
@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_authenticated_viewer_can_load_primary_routes(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    path: str,
) -> None:
    """A Viewer-role JWT successfully loads each primary route.

    # noqa
    """
    response = await client.get(path, headers=viewer_headers)
    assert response.status_code == 200


@phase1
@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_primary_routes_return_html(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    path: str,
) -> None:
    """Primary route responses carry an HTML content-type, not JSON.

    # noqa
    """
    response = await client.get(path, headers=viewer_headers)
    assert "text/html" in response.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# Admin-only authorization
# --------------------------------------------------------------------------- #


@phase1
async def test_admin_route_rejects_viewer(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A Viewer-role JWT cannot access /admin/refresh-status.

    # noqa
    """
    response = await client.get("/admin/refresh-status", headers=viewer_headers)
    assert response.status_code == 403


@phase1
async def test_admin_route_accepts_admin(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """An Admin-role JWT successfully loads /admin/refresh-status.

    # noqa
    """
    response = await client.get("/admin/refresh-status", headers=admin_headers)
    assert response.status_code == 200


@phase1
async def test_admin_refresh_trigger_rejects_viewer(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A Viewer-role JWT cannot trigger a manual refresh.

    # noqa
    """
    response = await client.post("/admin/refresh/llc-manager", headers=viewer_headers)
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Graceful degradation: empty cache must not error
# --------------------------------------------------------------------------- #


@phase1
@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_empty_cache_renders_without_error(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    path: str,
) -> None:
    """Per ADR-003: a stale or empty section must show the last cached value
    plus a 'last updated' label -- never an unhandled error or blank section.

    # noqa
    """
    response = await client.get(path, headers=viewer_headers)
    assert response.status_code == 200
    assert response.text  # non-empty body


# --------------------------------------------------------------------------- #
# Section content shows up when cache is populated
# --------------------------------------------------------------------------- #


@phase1
async def test_entities_route_shows_seeded_entity_name(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A seeded entity name appears in the /entities response body.

    # noqa
    """
    fetched = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, state, status, next_date, "
            "fetched_at) VALUES ('ent-1', 'Cascade Holdings LLC', 'LLC', 'WY', "
            "'current', '2026-12-01', ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/entities", headers=viewer_headers)
    assert response.status_code == 200
    assert "Cascade Holdings LLC" in response.text


@phase1
async def test_documents_route_shows_seeded_document_name(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A seeded document name appears in the /documents response body.

    # noqa
    """
    fetched = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, added_at, proxy_url, "
            "fetched_at) VALUES ('doc-1', '2025 Tax Return.pdf', 'Tax Returns', "
            "'2026-04-15T00:00:00', '/documents/doc-1/download', ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/documents", headers=viewer_headers)
    assert response.status_code == 200
    assert "2025 Tax Return.pdf" in response.text


@phase1
async def test_portfolio_route_shows_seeded_holding_name(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A seeded holding name appears in the /portfolio response body.

    # noqa
    """
    fetched = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES "
            "('hold-1', 'Vanguard Total Stock Market', 'Diversified', "
            "100000.0, 12.5, ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/portfolio", headers=viewer_headers)
    assert response.status_code == 200
    assert "Vanguard Total Stock Market" in response.text


# --------------------------------------------------------------------------- #
# Single-resource routes
# --------------------------------------------------------------------------- #


@phase1
async def test_entity_detail_route_returns_200_for_existing_id(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A GET on /entities/{id} for an existing entity returns 200.

    # noqa
    """
    fetched = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, state, status, next_date, "
            "fetched_at) VALUES ('ent-detail', 'Detail LLC', 'LLC', 'NV', "
            "'current', '2026-11-15', ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/entities/ent-detail", headers=viewer_headers)
    assert response.status_code == 200


@phase1
async def test_entity_detail_route_returns_404_for_unknown_id(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A GET on /entities/{id} for an unknown entity returns 404.

    # noqa
    """
    response = await client.get("/entities/nonexistent", headers=viewer_headers)
    assert response.status_code == 404


@phase1
async def test_documents_search_returns_html_partial(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """Per ``CLAUDE.md``: HTMX partial routes return HTML fragments, not full pages.

    # noqa
    """
    response = await client.get("/documents/search?q=tax", headers=viewer_headers)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# Header spoofing and confidential documents (ADR-005)
# --------------------------------------------------------------------------- #


@phase1
async def test_plain_authentik_headers_are_not_trusted(
    client: httpx.AsyncClient,
) -> None:
    """Unsigned identity headers alone never grant access.

    # noqa
    """
    response = await client.get(
        "/",
        headers={
            "X-authentik-username": "admin",
            "X-authentik-groups": "fo-admin",
            "X-authentik-email": "admin@example.com",
        },
    )
    assert response.status_code == 403


def _seed_confidential_docs(path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO documents (id, name, category, is_confidential, proxy_url, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "doc-open",
                    "Operating Agreement.pdf",
                    "LLCs",
                    0,
                    "/documents/doc-open/preview",
                    now,
                ),
                (
                    "doc-secret",
                    "Private Letter.pdf",
                    "LLCs",
                    1,
                    "/documents/doc-secret/preview",
                    now,
                ),
            ],
        )
        conn.commit()


@phase1
async def test_viewer_does_not_see_confidential_documents(
    client: httpx.AsyncClient,
    tmp_db_path: Path,
    viewer_headers: dict[str, str],
) -> None:
    """Viewers see open documents only, in lists and search.

    # noqa
    """
    _seed_confidential_docs(tmp_db_path)
    page = await client.get("/documents", headers=viewer_headers)
    search = await client.get("/documents/search?q=pdf", headers=viewer_headers)
    preview = await client.get("/documents/doc-secret/preview", headers=viewer_headers)
    assert "Operating Agreement.pdf" in page.text
    assert "Private Letter.pdf" not in page.text
    assert "Private Letter.pdf" not in search.text
    assert preview.status_code == 404


@phase1
async def test_admin_sees_confidential_documents(
    client: httpx.AsyncClient,
    tmp_db_path: Path,
    admin_headers: dict[str, str],
) -> None:
    """Admins see every document.

    # noqa
    """
    _seed_confidential_docs(tmp_db_path)
    page = await client.get("/documents", headers=admin_headers)
    assert "Private Letter.pdf" in page.text


@phase1
async def test_unknown_page_shows_plain_english_not_found(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A missing page renders an HTML page, not raw JSON.

    # noqa
    """
    response = await client.get("/entities/nope", headers=viewer_headers)
    assert response.status_code == 404
    assert "could not find that page" in response.text
