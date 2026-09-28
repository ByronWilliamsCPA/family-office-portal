# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003, ANN401, ERA001
"""Unit tests for ``app.scheduler`` refresh jobs.

Spec contract (``CLAUDE.md`` "Tech stack conventions" + tech-spec §4):

* Four refresh jobs: ``refresh_entities`` (llc-manager), ``refresh_holdings``
  (pp-security-master), ``refresh_positions`` (xero_crypto),
  ``refresh_documents`` (llc-manager documents, D-15 in the family office
  planning log; previously family_office).
* Use synchronous ``httpx.Client`` for outbound calls.
* Write fetched rows to SQLite with a ``fetched_at`` timestamp.
* Audit each run in the ``refresh_log`` table with status ``success`` or ``error``.
* On backend 5xx, log error and leave existing cached rows untouched (graceful
  degradation -- cached data is preferred to a blank section).

Tests mock ``httpx`` to avoid real network calls. Real SQLite is used per
``CLAUDE.md``'s preference for fixture-DB integration.
"""

from __future__ import annotations

import secrets
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
    portal_env: dict[str, str],
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
        rows = conn.execute("SELECT id, name, next_date FROM entities").fetchall()
    assert ("ent-1", "Holdings LLC", "2026-12-01") in rows


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
            "INSERT INTO entities (id, name, type, state, status, next_date, "
            "fetched_at) VALUES ('ent-old', 'Cached Trust', 'Trust', 'NV', "
            "'current', '2026-09-01', '2026-05-01T00:00:00')"
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
    """A successful pp-security-master fetch lands rows in holdings AND
    performance (per tech-spec the same endpoint returns both).

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
        "performance": [
            {
                "date": "2026-05-01",
                "total_value": 1000000.0,
                "benchmark": 950000.0,
            }
        ],
    }
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_holdings()

    with sqlite3.connect(initialized_db) as conn:
        holdings = conn.execute("SELECT id FROM holdings").fetchall()
        performance = conn.execute("SELECT date FROM performance").fetchall()
    assert ("hold-1",) in holdings
    assert ("2026-05-01",) in performance


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


def test_refresh_positions_preserves_cache_on_failure(
    initialized_db: Path,
) -> None:
    """A xero_crypto 5xx must not wipe existing cached positions.

    # noqa
    """
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES ('pos-old', 'ETH', 5.0, 15000.0, '2026-05-01T00:00:00')"
        )
        conn.commit()

    with patch(
        "httpx.Client",
        return_value=_mock_client(_mock_response({}, status_code=503)),
    ):
        scheduler.refresh_positions()

    with sqlite3.connect(initialized_db) as conn:
        assets = [
            row[0] for row in conn.execute("SELECT asset FROM positions").fetchall()
        ]
        log = conn.execute(
            "SELECT status FROM refresh_log "
            "WHERE service = 'xero_crypto' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert "ETH" in assets
    assert log and log[0][0] == "error"


# --------------------------------------------------------------------------- #
# refresh_documents (llc-manager documents)
# --------------------------------------------------------------------------- #


def test_refresh_documents_writes_rows_to_cache(initialized_db: Path) -> None:
    """A successful llc-manager documents fetch lands rows in documents.

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


def test_refresh_documents_preserves_cache_on_failure(
    initialized_db: Path,
) -> None:
    """An llc-manager documents 5xx must not wipe existing cached documents.

    # noqa
    """
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, added_at, proxy_url, "
            "fetched_at) VALUES ('doc-old', 'Will.pdf', 'Estate Planning', "
            "'2026-01-01T00:00:00', '/documents/doc-old/download', "
            "'2026-05-01T00:00:00')"
        )
        conn.commit()

    with patch(
        "httpx.Client",
        return_value=_mock_client(_mock_response({}, status_code=503)),
    ):
        scheduler.refresh_documents()

    with sqlite3.connect(initialized_db) as conn:
        names = [
            row[0] for row in conn.execute("SELECT name FROM documents").fetchall()
        ]
        log = conn.execute(
            "SELECT status FROM refresh_log "
            "WHERE service = 'llc-manager-documents' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert "Will.pdf" in names
    assert log and log[0][0] == "error"


# --------------------------------------------------------------------------- #
# Backend contract details
# --------------------------------------------------------------------------- #


def test_refresh_entities_accepts_llc_manager_paged_shape(
    initialized_db: Path,
) -> None:
    """llc-manager returns ``{items, total}`` with ``legal_name`` fields.

    # noqa
    """
    payload = {
        "items": [
            {
                "id": "3f0c",
                "legal_name": "Williams Holdings LLC",
                "entity_type": "llc",
                "formation_state": "WY",
                "is_active": True,
            }
        ],
        "total": 1,
    }
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_entities()
    with sqlite3.connect(initialized_db) as conn:
        row = conn.execute("SELECT name, type, state, status FROM entities").fetchone()
    assert row == ("Williams Holdings LLC", "llc", "WY", "active")


def test_refresh_sends_backend_api_key(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each job sends the per-service API key (A5).

    # noqa
    """
    del initialized_db
    api_key = secrets.token_urlsafe(16)
    monkeypatch.setenv("BACKEND_LLC_MANAGER_API_KEY", api_key)
    with patch(
        "httpx.Client", return_value=_mock_client(_mock_response([]))
    ) as client_cls:
        scheduler.refresh_entities()
    headers = client_cls.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == api_key


def test_refresh_without_api_key_sends_no_key_header(initialized_db: Path) -> None:
    """An unset backend API key sends no ``X-API-Key`` header at all.

    # noqa
    """
    del initialized_db
    with patch(
        "httpx.Client", return_value=_mock_client(_mock_response([]))
    ) as client_cls:
        scheduler.refresh_entities()
    headers = client_cls.call_args.kwargs["headers"]
    assert "X-API-Key" not in headers
    assert headers["Accept"] == "application/json"


def test_refresh_documents_maps_type_to_category_and_flags_confidential(
    initialized_db: Path,
) -> None:
    """Documents without a category get one from ``document_type``.

    # noqa
    """
    payload = {
        "items": [
            {
                "id": "d1",
                "title": "2025 Form 1065",
                "document_type": "tax_return",
                "entity_id": "e1",
                "is_confidential": True,
            },
            {
                "id": "d2",
                "title": "Operating Agreement",
                "document_type": "operating_agreement",
            },
        ],
        "total": 2,
    }
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_documents()
    with sqlite3.connect(initialized_db) as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT id, category, is_confidential, proxy_url FROM documents"
            )
        }
    assert rows["d1"] == ("Tax Returns", 1, "/documents/d1/preview")
    assert rows["d2"][0] == "LLCs"


def test_refresh_logs_error_on_unexpected_payload(initialized_db: Path) -> None:
    """A payload of the wrong shape is logged as an error, not raised.

    # noqa
    """
    with patch("httpx.Client", return_value=_mock_client(_mock_response("nope"))):
        scheduler.refresh_positions()
    with sqlite3.connect(initialized_db) as conn:
        status = conn.execute(
            "SELECT status FROM refresh_log WHERE service = 'xero_crypto'"
        ).fetchone()
    assert status == ("error",)


def test_refresh_logs_error_on_connection_failure(initialized_db: Path) -> None:
    """A transport error is logged as an error.

    # noqa
    """
    client = _mock_client(_mock_response([]))
    client.get.side_effect = httpx.ConnectError("down")
    with patch("httpx.Client", return_value=client):
        scheduler.refresh_entities()
    with sqlite3.connect(initialized_db) as conn:
        status = conn.execute(
            "SELECT status, message FROM refresh_log WHERE service = 'llc-manager'"
        ).fetchone()
    assert status[0] == "error"
    assert "ConnectError" in status[1]


def test_build_scheduler_registers_every_job() -> None:
    """The scheduler has one job per dataset, none running concurrently.

    # noqa
    """
    sched = scheduler.build_scheduler()
    jobs = {job.id: job for job in sched.get_jobs()}
    assert set(jobs) == set(scheduler.JOBS)
    assert all(job.max_instances == 1 for job in jobs.values())
