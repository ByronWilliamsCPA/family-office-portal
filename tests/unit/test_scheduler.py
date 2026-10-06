# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003, ANN401, ERA001
"""Unit tests for ``app.scheduler`` refresh jobs.

Spec contract (``CLAUDE.md`` "Tech stack conventions" + tech-spec §4):

* Four refresh jobs: ``refresh_entities`` (llc-manager), ``refresh_holdings``
  (pp-security-master), ``refresh_positions`` (xero_crypto),
  ``refresh_documents`` (llc-manager documents; previously family_office).
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
from contextlib import closing
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

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
    payload = {"items": [_contract_document(id="doc-1", category="Trusts")], "total": 1}
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
                "legal_name": "Sample Holdings LLC",
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
    assert row == ("Sample Holdings LLC", "llc", "WY", "active")


def test_refresh_sends_backend_api_key(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each job sends the per-service API key.

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


# --------------------------------------------------------------------------- #
# Optional backends: not connected, and never a request without the key
# --------------------------------------------------------------------------- #


class _Job(NamedTuple):
    """One refresh job and the backend variables it depends on."""

    name: str
    service: str
    url_var: str
    key_var: str
    path: str


REFRESH_JOBS = [
    _Job(
        "refresh_entities",
        "llc-manager",
        "BACKEND_LLC_MANAGER_URL",
        "BACKEND_LLC_MANAGER_API_KEY",
        "/api/v1/entities",
    ),
    _Job(
        "refresh_documents",
        "llc-manager-documents",
        "BACKEND_LLC_MANAGER_URL",
        "BACKEND_LLC_MANAGER_API_KEY",
        "/api/v1/documents",
    ),
    _Job(
        "refresh_holdings",
        "pp-security-master",
        "BACKEND_PP_SECURITY_URL",
        "BACKEND_PP_SECURITY_API_KEY",
        "/api/v1/portfolio/summary",
    ),
    _Job(
        "refresh_positions",
        "xero_crypto",
        "BACKEND_XERO_CRYPTO_URL",
        "BACKEND_XERO_CRYPTO_API_KEY",
        "/api/v1/positions",
    ),
]
JOB_IDS = [job.name for job in REFRESH_JOBS]


def _log_rows(path: Path) -> list[tuple[str, str, str | None]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT service, status, message FROM refresh_log"
        ).fetchall()


@pytest.mark.parametrize("job", REFRESH_JOBS, ids=JOB_IDS)
def test_every_request_carries_the_key_header(
    initialized_db: Path, portal_env: dict[str, str], job: _Job
) -> None:
    """Through a real client, every request to a backend has ``X-API-Key``.

    The header is built from the connection itself, so there is no branch that
    leaves it out.
    """
    del initialized_db
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client_with_transport(**kwargs: Any) -> httpx.Client:
        return real_client(transport=transport, **kwargs)

    with patch("httpx.Client", side_effect=client_with_transport):
        getattr(scheduler, job.name)()

    assert seen
    for request in seen:
        assert request.headers["X-API-Key"] == portal_env[job.key_var]
        assert str(request.url).startswith(portal_env[job.url_var])
        assert request.url.path == job.path


@pytest.mark.parametrize("job", REFRESH_JOBS, ids=JOB_IDS)
@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_url_without_usable_key_makes_no_request(
    initialized_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    job: _Job,
    blank: str | None,
) -> None:
    """A URL with a blank key sends nothing and records an error naming the key.

    This is the old "send with no key header" path, now unreachable.
    """
    if blank is None:
        monkeypatch.delenv(job.key_var)
    else:
        monkeypatch.setenv(job.key_var, blank)

    with patch("httpx.Client") as client_cls:
        getattr(scheduler, job.name)()

    client_cls.assert_not_called()
    rows = _log_rows(initialized_db)
    assert [(r[0], r[1]) for r in rows] == [(job.service, "error")]
    assert job.key_var in (rows[0][2] or "")


@pytest.mark.parametrize("job", REFRESH_JOBS, ids=JOB_IDS)
@pytest.mark.parametrize("key_value", [None, "a-key-without-a-url"])
def test_unconnected_backend_skips_with_one_log_line(
    initialized_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    job: _Job,
    key_value: str | None,
) -> None:
    """With no URL a job logs one skip line and makes no outbound call."""
    monkeypatch.delenv(job.url_var)
    if key_value is None:
        monkeypatch.delenv(job.key_var)
    else:
        monkeypatch.setenv(job.key_var, key_value)

    with patch("httpx.Client") as client_cls, capture_logs() as logs:
        getattr(scheduler, job.name)()

    client_cls.assert_not_called()
    assert len(logs) == 1
    assert logs[0]["event"] == "refresh_skipped_not_connected"
    assert logs[0]["log_level"] == "info"
    assert _log_rows(initialized_db) == []


def test_unconnected_backend_keeps_cached_rows(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping an unconnected backend leaves any cached rows untouched."""
    with closing(sqlite3.connect(initialized_db)) as conn, conn:
        conn.execute(
            "INSERT INTO entities (id, name, fetched_at) VALUES ('e1', 'Kept LLC', 'x')"
        )
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")

    scheduler.refresh_entities()

    with closing(sqlite3.connect(initialized_db)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM entities")]
    assert names == ["Kept LLC"]


def test_connecting_a_backend_later_resumes_refreshes(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job skips while the URL is unset and runs once it is set."""
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    scheduler.refresh_entities()
    assert _log_rows(initialized_db) == []

    monkeypatch.setenv("BACKEND_LLC_MANAGER_URL", "http://llc-manager.test")
    with patch("httpx.Client", return_value=_mock_client(_mock_response([]))):
        scheduler.refresh_entities()
    assert [(r[0], r[1]) for r in _log_rows(initialized_db)] == [
        ("llc-manager", "success")
    ]


def test_data_ingestor_has_no_refresh_job() -> None:
    """The data-ingestor pair is configuration only; nothing refreshes from it."""
    assert all("ingestor" not in name for name in scheduler.JOBS)
    assert all("ingestor" not in name for name in scheduler.TRIGGERS)


def _contract_document(**overrides: Any) -> dict[str, Any]:
    """Return one document in the documents contract shape, made-up values.

    # noqa
    """
    item: dict[str, Any] = {
        "id": "d1",
        "title": "Operating Agreement",
        "category": "LLCs",
        "document_type": "operating_agreement",
        "entity_id": "e1",
        "document_date": "2024-05-01",
        "effective_date": "2024-05-01",
        "is_confidential": False,
        "consent_on_file": False,
        "sha256": "0" * 64,
        "mime_type": "application/pdf",
        "created_at": "2026-09-28T14:02:11+00:00",
        "updated_at": "2026-09-29T09:00:00+00:00",
    }
    item.update(overrides)
    return item


def _refresh_documents_with(items: list[dict[str, Any]]) -> None:
    payload = {"items": items, "total": len(items)}
    with patch("httpx.Client", return_value=_mock_client(_mock_response(payload))):
        scheduler.refresh_documents()


def test_refresh_documents_maps_contract_fields(initialized_db: Path) -> None:
    """Every contract field the portal uses lands in its cache column.

    # noqa
    """
    _refresh_documents_with([_contract_document()])
    with closing(sqlite3.connect(initialized_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM documents WHERE id = 'd1'").fetchone()
    assert row["name"] == "Operating Agreement"
    assert row["category"] == "LLCs"
    assert row["entity_id"] == "e1"
    assert row["document_type"] == "operating_agreement"
    assert row["document_date"] == "2024-05-01"
    assert row["is_confidential"] == 0
    assert row["added_at"] == "2026-09-28T14:02:11+00:00"
    assert row["modified_at"] == "2026-09-29T09:00:00+00:00"
    assert row["proxy_url"] == "/documents/d1/preview"


@pytest.mark.parametrize(
    "category",
    [
        "Estate Planning",
        "LLCs",
        "Trusts",
        "Tax Returns",
        "Insurance",
        "Personal records",
        "Other",
    ],
)
def test_refresh_documents_keeps_every_contract_category(
    initialized_db: Path, category: str
) -> None:
    """Each of the seven contract categories is stored as sent.

    # noqa
    """
    _refresh_documents_with([_contract_document(category=category)])
    with closing(sqlite3.connect(initialized_db)) as conn:
        row = conn.execute("SELECT category FROM documents").fetchone()
    assert row[0] == category


@pytest.mark.parametrize(
    "category",
    [None, "", "llcs", "Wills", "LLC", 7],
    ids=["missing", "empty", "wrong-case", "unknown", "near-miss", "not-text"],
)
def test_refresh_documents_files_unknown_category_under_other(
    initialized_db: Path, category: object
) -> None:
    """A missing or unknown category is "Other", never a guessed folder.

    Even a type that looks like an LLC document is not guessed into "LLCs".

    # noqa
    """
    item = _contract_document(category=category, document_type="operating_agreement")
    if category is None:
        del item["category"]
    _refresh_documents_with([item])
    with closing(sqlite3.connect(initialized_db)) as conn:
        row = conn.execute("SELECT category FROM documents").fetchone()
    assert row[0] == "Other"


@pytest.mark.parametrize(
    ("flag", "stored"),
    [
        (False, 0),
        (True, 1),
        (None, 1),
        ("false", 1),
        (0, 1),
        ("missing", 1),
    ],
    ids=["false", "true", "null", "string-false", "zero", "missing"],
)
def test_refresh_documents_confidential_flag_fails_closed(
    initialized_db: Path, flag: object, stored: int
) -> None:
    """Only an explicit JSON ``false`` makes a document visible to Viewers.

    # noqa
    """
    item = _contract_document(is_confidential=flag)
    if flag == "missing":
        del item["is_confidential"]
    _refresh_documents_with([item])
    with closing(sqlite3.connect(initialized_db)) as conn:
        row = conn.execute("SELECT is_confidential FROM documents").fetchone()
    assert row[0] == stored


def test_refresh_documents_tolerates_missing_optional_fields(
    initialized_db: Path,
) -> None:
    """Dates, type and entity are optional in the cache; the row still lands.

    # noqa
    """
    item = {"id": "d2", "title": "  Insurance Policy  ", "is_confidential": False}
    _refresh_documents_with([item])
    with closing(sqlite3.connect(initialized_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM documents WHERE id = 'd2'").fetchone()
    assert row["name"] == "Insurance Policy"
    assert row["category"] == "Other"
    assert row["entity_id"] is None
    assert row["document_date"] is None
    assert row["added_at"] is None


@pytest.mark.parametrize(
    "title",
    [None, "", "   ", 42, "missing"],
    ids=["null", "empty", "blank", "not-text", "missing"],
)
def test_refresh_documents_skips_item_without_title(
    initialized_db: Path, title: object
) -> None:
    """A document with no usable title is skipped and logged by ID.

    The rest of the refresh still lands, so one bad item cannot freeze the
    cache (and with it every confidentiality change).

    # noqa
    """
    _refresh_documents_with([_contract_document(id="keep")])
    item = _contract_document(id="bad", title=title)
    if title == "missing":
        del item["title"]
    with capture_logs() as logs:
        _refresh_documents_with([_contract_document(id="new"), item])
    with closing(sqlite3.connect(initialized_db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM documents")]
        status = conn.execute(
            "SELECT status, rows FROM refresh_log "
            "WHERE service = 'llc-manager-documents' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert ids == ["new"]
    assert status == ("success", 1)
    skipped = [e for e in logs if e["event"] == "document_skipped"]
    assert skipped == [
        {
            "event": "document_skipped",
            "reason": "no_usable_title",
            "id": "bad",
            "log_level": "warning",
        }
    ]


def test_refresh_documents_applies_confidential_change_despite_bad_item(
    initialized_db: Path,
) -> None:
    """A newly confidential document is hidden even when another item is bad.

    # noqa
    """
    _refresh_documents_with([_contract_document(id="d1", is_confidential=False)])
    _refresh_documents_with(
        [
            _contract_document(id="d1", is_confidential=True),
            _contract_document(id="bad", title=""),
        ]
    )
    with closing(sqlite3.connect(initialized_db)) as conn:
        rows = conn.execute("SELECT id, is_confidential FROM documents").fetchall()
    assert rows == [("d1", 1)]


@pytest.mark.parametrize(
    "doc_id",
    [None, "", "  ", ".", "..", "a/b", True, 1.5, ["d"], "missing"],
    ids=[
        "null",
        "empty",
        "blank",
        "dot",
        "dot-dot",
        "slash",
        "bool",
        "float",
        "list",
        "missing",
    ],
)
def test_refresh_documents_skips_item_without_usable_id(
    initialized_db: Path, doc_id: object
) -> None:
    """An item whose ID cannot form one URL path segment is skipped.

    A null ID is never stored as the text "None".

    # noqa
    """
    item = _contract_document(id=doc_id)
    if doc_id == "missing":
        del item["id"]
    with capture_logs() as logs:
        _refresh_documents_with([item, _contract_document(id="ok")])
    with closing(sqlite3.connect(initialized_db)) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM documents")]
    assert ids == ["ok"]
    assert {"event": "document_skipped", "reason": "no_usable_id"}.items() <= next(
        e for e in logs if e["event"] == "document_skipped"
    ).items()


def test_refresh_documents_accepts_a_numeric_id(initialized_db: Path) -> None:
    """A whole-number ID is stored as text.

    # noqa
    """
    _refresh_documents_with([_contract_document(id=42)])
    with closing(sqlite3.connect(initialized_db)) as conn:
        row = conn.execute("SELECT id, proxy_url FROM documents").fetchone()
    assert row == ("42", "/documents/42/preview")


def test_refresh_documents_skips_a_repeated_id(initialized_db: Path) -> None:
    """The first item with an ID wins; a repeat is skipped and logged.

    # noqa
    """
    with capture_logs() as logs:
        _refresh_documents_with(
            [
                _contract_document(id="d1", title="First"),
                _contract_document(id="d1", title="Second"),
            ]
        )
    with closing(sqlite3.connect(initialized_db)) as conn:
        rows = conn.execute("SELECT id, name FROM documents").fetchall()
    assert rows == [("d1", "First")]
    assert any(
        e["event"] == "document_skipped" and e["reason"] == "duplicate_id" for e in logs
    )


def test_refresh_documents_uses_title_not_legacy_name(initialized_db: Path) -> None:
    """The contract ``title`` is stored; a legacy ``name`` field is ignored.

    # noqa
    """
    _refresh_documents_with([_contract_document(title="From Title", name="Old")])
    with closing(sqlite3.connect(initialized_db)) as conn:
        row = conn.execute("SELECT name FROM documents").fetchone()
    assert row == ("From Title",)


def test_refresh_documents_counts_unknown_categories_once(
    initialized_db: Path,
) -> None:
    """Unknown categories produce one warning per refresh, with a count.

    # noqa
    """
    del initialized_db
    with capture_logs() as logs:
        _refresh_documents_with(
            [
                _contract_document(id="a", category="Wills"),
                _contract_document(id="b", category=["LLCs"]),
                _contract_document(id="c", category="LLCs"),
            ]
        )
    unknown = [e for e in logs if e["event"] == "document_category_unknown"]
    assert unknown == [
        {"event": "document_category_unknown", "count": 2, "log_level": "warning"}
    ]


def test_refresh_documents_known_categories_log_nothing(
    initialized_db: Path,
) -> None:
    """A missing category is not counted as unknown.

    # noqa
    """
    del initialized_db
    item = _contract_document()
    del item["category"]
    with capture_logs() as logs:
        _refresh_documents_with([item])
    assert all(e["event"] != "document_category_unknown" for e in logs)


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("2024-05-01", "2024-05-01"),
        (20240501, "20240501"),
        (None, None),
        (True, None),
        ({"y": 2024}, None),
        (["2024"], None),
    ],
    ids=["text", "number", "null", "bool", "object", "list"],
)
def test_refresh_documents_optional_fields_store_scalars_only(
    initialized_db: Path, value: object, stored: str | None
) -> None:
    """An optional field is stored only when it is text or a number.

    # noqa
    """
    _refresh_documents_with([_contract_document(document_date=value)])
    with closing(sqlite3.connect(initialized_db)) as conn:
        row = conn.execute("SELECT document_date FROM documents").fetchone()
    assert row == (stored,)


def test_write_documents_skips_an_item_that_is_not_an_object(
    initialized_db: Path,
) -> None:
    """A non-object item is skipped rather than failing the refresh.

    # noqa
    """
    with closing(sqlite3.connect(initialized_db)) as conn, conn:
        written = scheduler._write_documents(  # noqa: SLF001
            conn, ["not-a-document", _contract_document(id="ok")], "2026-10-05"
        )
        ids = [r[0] for r in conn.execute("SELECT id FROM documents")]
    assert written == 1
    assert ids == ["ok"]


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


def test_refresh_follows_pages_until_total(initialized_db: Path) -> None:
    """A paged backend is read page by page until ``total`` rows arrive.

    # noqa
    """
    pages = [
        {"items": [{"id": "e1", "name": "Alpha LLC"}], "total": 2},
        {"items": [{"id": "e2", "name": "Beta LLC"}], "total": 2},
    ]
    client = _mock_client(_mock_response(None))
    client.get.side_effect = [_mock_response(p) for p in pages]
    with patch("httpx.Client", return_value=client):
        scheduler.refresh_entities()
    with sqlite3.connect(initialized_db) as conn:
        ids = {r[0] for r in conn.execute("SELECT id FROM entities")}
    assert ids == {"e1", "e2"}
    assert client.get.call_args_list[1].kwargs["params"] == {
        "page": 2,
        "size": scheduler._PAGE_SIZE,  # noqa: SLF001
    }


def test_refresh_keeps_cache_when_pages_fall_short(initialized_db: Path) -> None:
    """An empty page before ``total`` fails the refresh and keeps old rows.

    # noqa
    """
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, fetched_at) VALUES ('old', 'Old LLC', 'x')"
        )
        conn.commit()
    client = _mock_client(_mock_response(None))
    client.get.side_effect = [
        _mock_response({"items": [{"id": "e1", "name": "Alpha LLC"}], "total": 3}),
        _mock_response({"items": [], "total": 3}),
    ]
    with patch("httpx.Client", return_value=client):
        scheduler.refresh_entities()
    with sqlite3.connect(initialized_db) as conn:
        ids = {r[0] for r in conn.execute("SELECT id FROM entities")}
        status = conn.execute(
            "SELECT status, message FROM refresh_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert ids == {"old"}
    assert status[0] == "error"
    assert "partial" in status[1]


def test_refresh_refuses_too_many_pages(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend that never reaches ``total`` is stopped at the page cap.

    # noqa
    """
    monkeypatch.setattr(scheduler, "_MAX_PAGES", 2)
    client = _mock_client(
        _mock_response({"items": [{"id": "e1", "name": "Alpha LLC"}], "total": 99})
    )
    with patch("httpx.Client", return_value=client):
        scheduler.refresh_entities()
    with sqlite3.connect(initialized_db) as conn:
        status = conn.execute(
            "SELECT status, message FROM refresh_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert status[0] == "error"
    assert "pages" in status[1]


def test_refresh_skips_when_same_service_is_running(initialized_db: Path) -> None:
    """A second run of a service that is already running does nothing.

    # noqa
    """
    lock = scheduler._service_lock("llc-manager")  # noqa: SLF001
    assert lock.acquire(blocking=False)
    try:
        with patch("httpx.Client") as client_cls:
            scheduler.refresh_entities()
        client_cls.assert_not_called()
    finally:
        lock.release()
    with sqlite3.connect(initialized_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM refresh_log").fetchone()[0] == 0
