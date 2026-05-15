# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003, PLR2004, ANN401, ERA001
"""Unit tests for ``app.scheduler`` refresh jobs.

Spec contract (``CLAUDE.md`` "Tech stack conventions" + tech-spec §4):

* Four refresh jobs: ``refresh_entities`` (llc-manager), ``refresh_holdings``
  (pp-security-master), ``refresh_positions`` (xero_crypto),
  ``refresh_documents`` (family_office).
* Use synchronous ``httpx.Client`` for outbound calls.
* Write fetched rows to SQLite with a ``fetched_at`` timestamp.
* Audit each run in the ``refresh_log`` table with status ``success`` or ``error``.
* On backend 5xx, log error and leave existing cached rows untouched (graceful
  degradation -- cached data is preferred to a blank section).

Tests mock ``httpx`` to avoid real network calls. Real SQLite is used per
``CLAUDE.md``'s preference for fixture-DB integration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

scheduler = pytest.importorskip("app.scheduler")
db = pytest.importorskip("app.db")


@pytest.fixture
def initialized_db(
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cf_env: dict[str, str],
) -> Path:
    """Initialize a SQLite schema and point env at it for the scheduler.

    # noqa
    """
    monkeypatch.setenv("SQLITE_PATH", str(tmp_db_path))
    db.init_schema(str(tmp_db_path))
    return tmp_db_path


def _mock_response(payload: Any, status_code: int = 200) -> MagicMock:
    """Build an httpx.Response mock with the given JSON payload and status.

    # noqa
    """
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = payload
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "backend error", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


def _mock_client(response: MagicMock) -> MagicMock:
    """Build a context-manager mock that mimics httpx.Client().

    # noqa
    """
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = response
    return client


# --------------------------------------------------------------------------- #
# refresh_entities (llc-manager)
# --------------------------------------------------------------------------- #


def test_refresh_entities_writes_rows_to_cache(initialized_db: Path) -> None:
    """A successful llc-manager fetch lands rows in the entities table.

    # noqa
    """
    payload = [
        {
            "id": "ent-1",
            "name": "Holdings LLC",
            "type": "LLC",
            "state": "WY",
            "status": "current",
            "next_date": "2026-12-01",
        }
    ]
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_entities()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute("SELECT id, name FROM entities").fetchall()
    assert ("ent-1", "Holdings LLC") in rows


def test_refresh_entities_logs_success(initialized_db: Path) -> None:
    """A successful refresh writes a 'success' row into refresh_log.

    # noqa
    """
    with patch("httpx.Client", return_value=_mock_client(_mock_response([]))):
        scheduler.refresh_entities()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute(
            "SELECT service, status FROM refresh_log "
            "WHERE service = 'llc-manager' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert rows
    assert rows[0][1] == "success"


def test_refresh_entities_logs_error_on_backend_5xx(
    initialized_db: Path,
) -> None:
    """A 5xx backend response writes an 'error' row into refresh_log.

    # noqa
    """
    with patch(
        "httpx.Client",
        return_value=_mock_client(_mock_response({"error": "boom"}, status_code=500)),
    ):
        scheduler.refresh_entities()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute(
            "SELECT status FROM refresh_log "
            "WHERE service = 'llc-manager' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert rows
    assert rows[0][0] == "error"


def test_refresh_entities_preserves_cache_on_failure(
    initialized_db: Path,
) -> None:
    """Per ADR-003 graceful degradation: a failing refresh must NOT wipe
    existing cached rows. Stale data is preferred to a blank section.

    # noqa
    """
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, state, status, fetched_at) "
            "VALUES ('ent-old', 'Cached Trust', 'Trust', 'NV', 'current', "
            "'2026-05-01T00:00:00')"
        )
        conn.commit()

    with patch(
        "httpx.Client",
        return_value=_mock_client(_mock_response({}, status_code=503)),
    ):
        scheduler.refresh_entities()

    with sqlite3.connect(initialized_db) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM entities").fetchall()]
    assert "Cached Trust" in names


# --------------------------------------------------------------------------- #
# refresh_holdings (pp-security-master)
# --------------------------------------------------------------------------- #


def test_refresh_holdings_writes_rows_to_cache(initialized_db: Path) -> None:
    """A successful pp-security-master fetch lands rows in holdings.

    # noqa
    """
    payload = {
        "holdings": [
            {
                "id": "hold-1",
                "security_name": "Apple Inc.",
                "sector": "Technology",
                "current_value": 50000.0,
                "allocation_pct": 10.0,
            }
        ],
        "performance": [],
    }
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_holdings()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute("SELECT id FROM holdings").fetchall()
    assert ("hold-1",) in rows


def test_refresh_holdings_tolerates_alpha_500(initialized_db: Path) -> None:
    """pp-security-master is alpha; 500s are expected and must not raise.
    Per ``CLAUDE.md``: 'Treat its 500 responses as expected; surface as stale
    data, not as errors in user-visible templates.'

    # noqa
    """
    with patch(
        "httpx.Client",
        return_value=_mock_client(_mock_response({}, status_code=500)),
    ):
        scheduler.refresh_holdings()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute(
            "SELECT status FROM refresh_log "
            "WHERE service = 'pp-security-master' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert rows
    assert rows[0][0] == "error"


# --------------------------------------------------------------------------- #
# refresh_positions (xero_crypto)
# --------------------------------------------------------------------------- #


def test_refresh_positions_writes_rows_to_cache(initialized_db: Path) -> None:
    """A successful xero_crypto fetch lands rows in positions.

    # noqa
    """
    payload = [{"id": "pos-1", "asset": "BTC", "quantity": 0.5, "usd_value": 30000.0}]
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_positions()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute("SELECT asset FROM positions").fetchall()
    assert ("BTC",) in rows


# --------------------------------------------------------------------------- #
# refresh_documents (family_office)
# --------------------------------------------------------------------------- #


def test_refresh_documents_writes_rows_to_cache(initialized_db: Path) -> None:
    """A successful family_office fetch lands rows in documents.

    # noqa
    """
    payload = [
        {
            "id": "doc-1",
            "name": "Trust Agreement.pdf",
            "category": "Trusts",
            "added_at": "2026-04-15T00:00:00",
            "url": "/box/abc",
        }
    ]
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_documents()

    with sqlite3.connect(initialized_db) as conn:
        rows = conn.execute("SELECT id, category FROM documents").fetchall()
    assert ("doc-1", "Trusts") in rows
