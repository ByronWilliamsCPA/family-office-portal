# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Route contract tests: status codes, content types, and JSON shapes.

Every request carries a valid Authentik JWT from the shared fixtures, except
``/health``, which is public.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.scheduler import TRIGGERS

if TYPE_CHECKING:
    from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    """``/health`` is public and returns the liveness payload."""
    async with client as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "family-office-portal"}


async def test_home_returns_html(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Home renders the portal name and the signed-in user."""
    async with client as ac:
        response = await ac.get("/", headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Family Office Portal" in response.text
    assert "Signed in as Viewer" in response.text


async def test_home_shows_total_when_balances_cached(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: object,
) -> None:
    """Home sums cached account balances (stored in cents)."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, category, source, "
            "value_cents, as_of, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "a1",
                    "Brokerage",
                    "Investments",
                    "ibkr",
                    125_000_050,
                    "2026-09-27",
                    now,
                ),
                ("a2", "Operating", "Cash", "xero", 2_500_000, "2026-09-26", now),
            ],
        )
        conn.commit()
    async with client as ac:
        response = await ac.get("/", headers=viewer_headers)
    assert "$1,275,000" in response.text


async def test_documents_index_returns_html(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """The documents page renders with an empty cache."""
    async with client as ac:
        response = await ac.get("/documents", headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_documents_search_returns_html_partial(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Search returns an HTML fragment for HTMX."""
    async with client as ac:
        response = await ac.get("/documents/search?q=tax", headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'No documents match "tax"' in response.text


async def test_documents_search_rejects_empty_query(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """An empty query is a validation error."""
    async with client as ac:
        response = await ac.get("/documents/search?q=", headers=viewer_headers)
    assert response.status_code == 422


async def test_document_preview_unknown_document_is_404(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Previewing a document that is not cached returns 404."""
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 404


async def test_document_download_unknown_document_is_404(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Downloading a document that is not cached returns 404."""
    async with client as ac:
        response = await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert response.status_code == 404


async def test_document_preview_known_document_not_yet_available(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: object,
) -> None:
    """A cached document returns 503 until the file proxy (C5) lands."""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, proxy_url, fetched_at) "
            "VALUES ('doc-1', 'Will.pdf', 'Estate Planning', "
            "'/documents/doc-1/preview', '2026-09-01T00:00:00')"
        )
        conn.commit()
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 503
    assert "not available right now" in response.text


async def test_finances_returns_html(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Finances renders and notes what is not included."""
    async with client as ac:
        response = await ac.get("/finances", headers=viewer_headers)
    assert response.status_code == 200
    assert "not included yet" in response.text


async def test_portfolio_returns_html(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Portfolio renders with an empty cache."""
    async with client as ac:
        response = await ac.get("/portfolio", headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


async def test_entities_index_returns_html(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Entities renders with an empty cache."""
    async with client as ac:
        response = await ac.get("/entities", headers=viewer_headers)
    assert response.status_code == 200


async def test_admin_refresh_status_returns_empty_entries(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """With no refresh history, the status list is empty."""
    async with client as ac:
        response = await ac.get("/admin/refresh-status", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == {"entries": []}


async def test_admin_refresh_status_summarizes_log(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: object,
) -> None:
    """The status shows the latest success and error per service."""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.executemany(
            "INSERT INTO refresh_log (service, status, message, ran_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("llc-manager", "success", None, "2026-09-27T01:00:00+00:00"),
                (
                    "llc-manager",
                    "error",
                    "HTTPStatusError: 500",
                    "2026-09-27T05:00:00+00:00",
                ),
            ],
        )
        conn.commit()
    async with client as ac:
        response = await ac.get("/admin/refresh-status", headers=admin_headers)
    (entry,) = response.json()["entries"]
    assert entry["service"] == "llc-manager"
    assert entry["last_success_at"] == "2026-09-27T01:00:00+00:00"
    assert entry["last_error_message"] == "HTTPStatusError: 500"
    assert entry["is_stale"] is True


async def test_admin_refresh_trigger_default_body(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: object,
) -> None:
    """Triggering a refresh queues the job and echoes the request."""
    calls: list[str] = []
    monkeypatch.setitem(TRIGGERS, "entities", lambda: calls.append("entities"))  # type: ignore[attr-defined]
    async with client as ac:
        response = await ac.post(
            "/admin/refresh/entities", json={}, headers=admin_headers
        )
    assert response.status_code == 202
    assert response.json() == {
        "service": "entities",
        "scheduled": True,
        "forced": False,
    }
    assert calls == ["entities"]


async def test_admin_refresh_trigger_with_force(
    client: AsyncClient,
    admin_headers: dict[str, str],
    monkeypatch: object,
) -> None:
    """``force`` is echoed back."""
    monkeypatch.setitem(TRIGGERS, "documents", lambda: None)  # type: ignore[attr-defined]
    async with client as ac:
        response = await ac.post(
            "/admin/refresh/documents", json={"force": True}, headers=admin_headers
        )
    assert response.status_code == 202
    assert response.json() == {
        "service": "documents",
        "scheduled": True,
        "forced": True,
    }


async def test_admin_refresh_trigger_rejects_bad_body(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """A non-boolean ``force`` is a validation error."""
    async with client as ac:
        response = await ac.post(
            "/admin/refresh/entities",
            json={"force": "yes please"},
            headers=admin_headers,
        )
    assert response.status_code == 422


async def test_stale_section_shows_plain_notice(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: object,
) -> None:
    """A stale dataset shows its last update and a readable warning."""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.execute(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES ('p1', 'BTC', 1.0, 65000.0, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    async with client as ac:
        response = await ac.get("/finances", headers=viewer_headers)
    assert "PM. This may be out of date." in response.text or (
        "AM. This may be out of date." in response.text
    )
