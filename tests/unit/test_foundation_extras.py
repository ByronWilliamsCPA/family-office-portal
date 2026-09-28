# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003
"""Extra coverage for cache filters, key handling, templating, and lifespan."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization

from app import cache, db
from app.middleware import authentik
from app.routes._context import AS_OF_WARN_DAYS, balances_summary
from app.templating import friendly_date, friendly_time, money, templates


@pytest.fixture
def seeded_db(tmp_db_path: Path, portal_env: dict[str, str]) -> Path:
    """Schema with two entities and three documents.

    # noqa
    """
    del portal_env
    db.init_schema(str(tmp_db_path))
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, fetched_at) VALUES ('e1', 'Alpha LLC', ?)",
            (now,),
        )
        conn.executemany(
            "INSERT INTO documents (id, name, category, entity_id, is_confidential, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("d1", "100% Owned Agreement.pdf", "LLCs", "e1", 0, now),
                ("d2", "Minutes_2025.pdf", "LLCs", "e1", 1, now),
                ("d3", "Umbrella Policy.pdf", "Insurance", None, 0, now),
            ],
        )
        conn.commit()
    return tmp_db_path


async def test_get_entity_found_and_missing(seeded_db: Path) -> None:
    """``get_entity`` returns the row or None.

    # noqa
    """
    del seeded_db
    found = await cache.get_entity("e1")
    assert found is not None
    assert found["name"] == "Alpha LLC"
    assert await cache.get_entity("missing") is None


async def test_get_documents_filters_confidential_and_entity(seeded_db: Path) -> None:
    """Viewers get non-confidential rows; entity filter narrows results.

    # noqa
    """
    del seeded_db
    viewer = {r["id"] for r in await cache.get_documents()}
    admin = {r["id"] for r in await cache.get_documents(include_confidential=True)}
    entity = {
        r["id"]
        for r in await cache.get_documents(include_confidential=True, entity_id="e1")
    }
    assert viewer == {"d1", "d3"}
    assert admin == {"d1", "d2", "d3"}
    assert entity == {"d1", "d2"}


async def test_search_documents_escapes_like_wildcards(seeded_db: Path) -> None:
    """``%`` and ``_`` in a query are literal characters, not wildcards.

    # noqa
    """
    del seeded_db
    percent = {r["id"] for r in await cache.search_documents("100%")}
    underscore = {
        r["id"] for r in await cache.search_documents("_", include_confidential=True)
    }
    assert percent == {"d1"}
    assert underscore == {"d2"}


async def test_last_fetched_at_rejects_unknown_dataset(seeded_db: Path) -> None:
    """Dataset names are allowlisted.

    # noqa
    """
    del seeded_db
    with pytest.raises(ValueError, match="Unknown dataset"):
        await cache.last_fetched_at("entities; DROP TABLE entities")


async def test_last_fetched_at_keeps_timezone(seeded_db: Path) -> None:
    """Timezone-aware timestamps round-trip with their offset.

    # noqa
    """
    del seeded_db
    latest = await cache.last_fetched_at("entities")
    assert latest is not None
    assert latest.tzinfo is not None


def test_sqlite_path_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sqlite_path`` fails loudly without ``SQLITE_PATH``.

    # noqa
    """
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    with pytest.raises(RuntimeError, match="SQLITE_PATH"):
        db.sqlite_path()


# --------------------------------------------------------------------------- #
# Authentik key handling
# --------------------------------------------------------------------------- #


def test_fetch_authentik_jwks_calls_url() -> None:
    """The real fetcher GETs the JWKS URL and returns its JSON.

    # noqa
    """
    response = MagicMock()
    response.json.return_value = {"keys": []}
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = response
    with patch("httpx.Client", return_value=client):
        document = authentik.fetch_authentik_jwks("https://auth.test/jwks/", 5.0)
    client.get.assert_called_once_with("https://auth.test/jwks/")
    assert document == {"keys": []}


@pytest.mark.usefixtures("portal_env", "patched_jwks")
def test_token_without_kid_is_rejected(
    rsa_key_pair: tuple[Any, Any],
) -> None:
    """A token with no ``kid`` header cannot be matched to a key.

    # noqa
    """
    private, _ = rsa_key_pair
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    token = pyjwt.encode({"sub": "x"}, pem, algorithm="RS256")
    with pytest.raises(authentik.AuthError, match="no key ID"):
        authentik.authenticate(token)


@pytest.mark.usefixtures("portal_env")
def test_unknown_kid_triggers_refetch_after_interval(
    patched_jwks: dict[str, int],
    jwt_factory: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new ``kid`` refetches keys once the minimum interval has passed.

    # noqa
    """
    authentik.authenticate(jwt_factory())
    monkeypatch.setattr(authentik, "_MIN_REFRESH_INTERVAL_SECONDS", -1.0)
    with pytest.raises(authentik.AuthError):
        authentik.authenticate(jwt_factory(kid="rotated-key"))
    assert patched_jwks["calls"] == 2


# --------------------------------------------------------------------------- #
# Templating filters
# --------------------------------------------------------------------------- #


def test_money_formats_whole_dollars() -> None:
    """Amounts show thousands separators, no cents, and a sign.

    # noqa
    """
    assert money(1250000.4) == "$1,250,000"
    assert money(-42.0) == "-$42"
    assert money(None) == "Not available"


def test_friendly_date_and_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dates and times read as plain English in the display time zone.

    # noqa
    """
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    assert friendly_date("2026-12-01") == "December 1, 2026"
    assert friendly_date(None) == ""
    assert friendly_time("2026-09-28T19:05:00+00:00") == "Sep 28, 2026, 3:05 PM"
    assert friendly_time("2026-09-28T00:30:00") == "Sep 27, 2026, 8:30 PM"
    assert friendly_time(None) == "not yet"


def test_friendly_time_falls_back_to_utc_for_bad_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid time zone setting falls back to UTC.

    # noqa
    """
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Not/AZone")
    assert friendly_time("2026-09-28T12:00:00+00:00") == "Sep 28, 2026, 12:00 PM"


@pytest.fixture(autouse=True)
def _required_env_for_templating(portal_env: dict[str, str]) -> None:
    """Templating reads settings, which need the required env vars.

    # noqa
    """
    del portal_env


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #


async def test_lifespan_initializes_schema_and_runs_scheduler(
    tmp_db_path: Path,
) -> None:
    """Startup creates the schema and starts, then stops, the scheduler.

    # noqa
    """
    import importlib  # noqa: PLC0415  # app.main exits at import without env

    from app import main  # noqa: PLC0415

    importlib.reload(main)
    fake = MagicMock()
    with patch.object(main, "build_scheduler", return_value=fake):
        async with main.lifespan(main.app):
            assert await anyio.Path(tmp_db_path).exists()
            fake.start.assert_called_once()
    fake.shutdown.assert_called_once_with(wait=False)


async def test_lifespan_skips_scheduler_when_disabled(
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SCHEDULER_ENABLED=false`` leaves the scheduler off.

    # noqa
    """
    import importlib  # noqa: PLC0415  # app.main exits at import without env

    from app import main  # noqa: PLC0415

    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    importlib.reload(main)
    with patch.object(main, "build_scheduler") as build:
        async with main.lifespan(main.app):
            assert await anyio.Path(tmp_db_path).exists()
        build.assert_not_called()


def test_money_rounds_half_even_from_decimal() -> None:
    """Decimal amounts round half to even, with no float drift.

    # noqa
    """
    assert money(Decimal("2.50")) == "$2"
    assert money(Decimal("3.50")) == "$4"
    assert money(Decimal("1234567.49")) == "$1,234,567"


def _insert_balance(
    path: Path, account_id: str, cents: int, as_of: str, currency: str = "USD"
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, category, "
            "source, value_cents, as_of, fetched_at, currency) "
            "VALUES (?, 'Acct', 'Cash', 'xero', ?, ?, ?, ?)",
            (
                account_id,
                cents,
                as_of,
                datetime.now(timezone.utc).isoformat(),
                currency,
            ),
        )
        conn.commit()


async def test_balances_summary_sums_usd_only(seeded_db: Path) -> None:
    """Only USD rows are totalled; other currencies are counted and left out.

    # noqa
    """
    today = datetime.now(timezone.utc).date()
    _insert_balance(seeded_db, "a1", 150_050, today.isoformat())
    _insert_balance(seeded_db, "a2", 25_000, (today - timedelta(days=3)).isoformat())
    _insert_balance(seeded_db, "a3", 999_999, today.isoformat(), currency="EUR")
    summary = await balances_summary()
    assert summary["total_value"] == Decimal("1750.50")
    assert summary["excluded_currency_count"] == 1
    assert summary["oldest_as_of"] == (today - timedelta(days=3)).isoformat()
    assert summary["as_of_stale"] is False


async def test_balances_summary_flags_old_as_of(seeded_db: Path) -> None:
    """A value older than the warning window is flagged even if fetched today.

    # noqa
    """
    old = datetime.now(timezone.utc).date() - timedelta(days=AS_OF_WARN_DAYS + 1)
    _insert_balance(seeded_db, "a1", 100, old.isoformat())
    summary = await balances_summary()
    assert summary["as_of_stale"] is True


async def test_balances_summary_empty(seeded_db: Path) -> None:
    """No USD balances gives no total rather than zero.

    # noqa
    """
    summary = await balances_summary()
    assert summary["total_value"] is None
    assert summary["oldest_as_of"] is None
    assert summary["excluded_currency_count"] == 0


def test_as_of_note_renders_warning() -> None:
    """The as-of macro warns in plain English and mentions left-out currencies.

    # noqa
    """
    template = templates.env.from_string(
        '{% from "partials/as_of.html" import as_of_note %}'
        "{{ as_of_note('2026-01-31', true, 2) }}"
    )
    html = template.render()
    assert "may need updating" in html
    assert "2 accounts in other currencies not included" in html
