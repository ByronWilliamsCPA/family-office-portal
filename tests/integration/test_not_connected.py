# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Pages for a backend that is not connected show a plain "not connected yet".

A backend is not connected when its ``BACKEND_<NAME>_URL`` is unset. Pages
that show its data must answer 200 with a plain sentence, never an error page
and never an empty list that looks like a failure.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from tests.unit.chat_fakes import random_key, write_instructions

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

NOT_CONNECTED = "Not connected yet"

# (page path, URL variable of the backend that feeds it)
PAGES = [
    ("/entities", "BACKEND_LLC_MANAGER_URL"),
    ("/documents", "BACKEND_LLC_MANAGER_URL"),
    ("/finances", "BACKEND_XERO_CRYPTO_URL"),
    ("/portfolio", "BACKEND_PP_SECURITY_URL"),
    ("/", "BACKEND_LLC_MANAGER_URL"),
]


@pytest.mark.parametrize(("path", "url_var"), PAGES)
async def test_page_says_not_connected_when_url_is_unset(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str, url_var: str
) -> None:
    """With the backend URL unset the page renders a plain not-connected note."""
    monkeypatch.delenv(url_var)
    async with client as ac:
        response = await ac.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert NOT_CONNECTED in response.text


@pytest.fixture
def chat_connected(
    portal_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Connect chat too, since Home shows its panel to Admins.

    Args:
        portal_env: Applied first, because it clears the chat settings.
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Per-test directory for the synthetic instructions file.
    """
    del portal_env
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", random_key())
    monkeypatch.setenv("CHAT_INSTRUCTIONS_PATH", str(write_instructions(tmp_path)))


@pytest.mark.usefixtures("chat_connected")
@pytest.mark.parametrize(("path", "url_var"), PAGES)
async def test_page_has_no_not_connected_note_when_connected(
    client: AsyncClient, path: str, url_var: str
) -> None:
    """With the URL set (and an empty cache) the page does not claim otherwise."""
    del url_var
    async with client as ac:
        response = await ac.get(path)
    assert response.status_code == 200
    assert NOT_CONNECTED not in response.text


@pytest.mark.parametrize(("path", "url_var"), PAGES)
async def test_blank_url_is_treated_as_not_connected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str, url_var: str
) -> None:
    """An empty URL (as a compose file may pass) is the same as unset."""
    monkeypatch.setenv(url_var, "")
    async with client as ac:
        response = await ac.get(path)
    assert response.status_code == 200
    assert NOT_CONNECTED in response.text


async def test_unconnected_backend_does_not_affect_other_pages(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disconnecting llc-manager leaves the portfolio page as it was."""
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert NOT_CONNECTED not in response.text


async def test_unconnected_entities_page_hides_old_cached_rows(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
) -> None:
    """Rows cached before a backend was disconnected are not shown as current."""
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO entities (id, name, fetched_at) "
            "VALUES ('e1', 'Old Holdings LLC', '2026-01-01T00:00:00+00:00')"
        )
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        response = await ac.get("/entities")
    assert NOT_CONNECTED in response.text
    assert "Old Holdings LLC" not in response.text


async def test_unconnected_documents_page_has_no_search_form(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no document source the page offers no search box."""
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        response = await ac.get("/documents")
    assert 'role="search"' not in response.text
