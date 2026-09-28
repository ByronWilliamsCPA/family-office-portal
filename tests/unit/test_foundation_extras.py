# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003
"""Extra coverage for cache filters, settings, templating, and lifespan."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import anyio
import pytest

from app import cache, db
from app.config import load_settings
from app.templating import friendly_date, friendly_time, money


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
# Settings
# --------------------------------------------------------------------------- #


def test_backend_api_keys_default_to_empty() -> None:
    """The per-backend API keys are optional; unset means an empty key.

    # noqa
    """
    settings = load_settings()
    assert settings.backend_llc_manager_api_key.get_secret_value() == ""
    assert settings.backend_pp_security_api_key.get_secret_value() == ""
    assert settings.backend_xero_crypto_api_key.get_secret_value() == ""


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
    # portal_env pre-creates the schema; remove it so startup must recreate it.
    await anyio.Path(tmp_db_path).unlink()
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
    await anyio.Path(tmp_db_path).unlink()
    with patch.object(main, "build_scheduler") as build:
        async with main.lifespan(main.app):
            assert await anyio.Path(tmp_db_path).exists()
        build.assert_not_called()
