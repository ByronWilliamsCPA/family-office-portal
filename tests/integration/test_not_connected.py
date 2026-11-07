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
from datetime import datetime, timezone
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

# Finances and Portfolio never say "not connected" to a primary user: their
# main content comes from the balance feed, and the backend that is not
# connected only adds per-asset or per-security detail (see
# ``test_portfolio_accounts.py`` and the digital currency tests below).
BALANCE_FED_PAGES = {"/finances", "/portfolio"}
NOT_CONNECTED_PAGES = [p for p in PAGES if p[0] not in BALANCE_FED_PAGES]


@pytest.mark.parametrize(("path", "url_var"), NOT_CONNECTED_PAGES)
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


@pytest.mark.parametrize(("path", "url_var"), NOT_CONNECTED_PAGES)
async def test_blank_url_is_treated_as_not_connected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str, url_var: str
) -> None:
    """An empty URL (as a compose file may pass) is the same as unset."""
    monkeypatch.setenv(url_var, "")
    async with client as ac:
        response = await ac.get(path)
    assert response.status_code == 200
    assert NOT_CONNECTED in response.text


@pytest.mark.parametrize("path", sorted(BALANCE_FED_PAGES))
async def test_balance_fed_pages_never_say_not_connected(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """With the detail backend unset or blank these pages stay plain, never failed."""
    monkeypatch.delenv("BACKEND_PP_SECURITY_URL")
    monkeypatch.setenv("BACKEND_XERO_CRYPTO_URL", "")
    async with client as ac:
        response = await ac.get(path)
    assert response.status_code == 200
    assert NOT_CONNECTED not in response.text


def _seed_digital_currency(path: Path, *, with_account: bool) -> None:
    """Cache one made-up digital currency position and, optionally, account.

    Args:
        path: SQLite file to seed.
        with_account: Also cache a Digital currency account balance.
    """
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES ('p1', 'Example Coin', 2, 200.0, ?)",
            (now,),
        )
        if with_account:
            conn.execute(
                "INSERT INTO account_balances (account_id, account_name, "
                "entity_id, category, source, value_cents, as_of, fetched_at, "
                "currency) VALUES ('crypto:acct-1', 'Example Wallet', "
                "'11111111-2222-4333-8444-555555555555', 'Digital currency', "
                "'crypto_tracker', 700000, '2026-09-26', ?, 'USD')",
                (now,),
            )


async def test_digital_currency_box_explains_totals_include_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
) -> None:
    """Unconnected detail plus digital currency in the totals says exactly that."""
    monkeypatch.delenv("BACKEND_XERO_CRYPTO_URL")
    _seed_digital_currency(tmp_db_path, with_account=True)
    async with client as ac:
        html = (await ac.get("/finances")).text
    assert "Digital currency" in html
    assert (
        "The account totals above include digital currency. "
        "A list of each coin or token is not available yet." in html
    )
    assert NOT_CONNECTED not in html
    assert "Example Coin" not in html
    assert "11111111-2222-4333-8444-555555555555" not in html


async def test_digital_currency_box_without_any_digital_accounts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no digital currency in the totals the box does not claim there is."""
    monkeypatch.delenv("BACKEND_XERO_CRYPTO_URL")
    async with client as ac:
        html = (await ac.get("/finances")).text
    assert "A list of each coin or token is not available yet." in html
    assert "totals above include digital currency" not in html
    assert NOT_CONNECTED not in html


async def test_digital_currency_box_unchanged_when_connected(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Connected, the per-asset table shows and the new wording does not."""
    _seed_digital_currency(tmp_db_path, with_account=True)
    async with client as ac:
        html = (await ac.get("/finances")).text
    assert "Example Coin" in html
    assert "not available yet" not in html


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
