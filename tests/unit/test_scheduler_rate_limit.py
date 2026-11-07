# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Rate-limit (HTTP 429) handling in the scheduler refresh jobs.

The llc-manager backend limits callers to 60 requests per minute. A refresh
that meets a 429 reads ``Retry-After`` (whole seconds only), waits a bounded
time, and retries a bounded number of times. When the backend is still
limiting, the refresh fails the normal way: the cached rows stay and
``refresh_log`` records an error, so the section shows stale data.

The sleep is replaced in every test, so none of them waits. Responses are real
``httpx.Response`` objects; only the client is faked.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import db, scheduler

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REQUEST = httpx.Request("GET", "http://llc-manager.test/api/v1/entities")


@pytest.fixture
def initialized_db(
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portal_env: dict[str, str],
) -> Path:
    """Create the schema and point the settings at it.

    Returns:
        Path: The initialized database file.
    """
    monkeypatch.setenv("SQLITE_PATH", str(tmp_db_path))
    db.init_schema(str(tmp_db_path))
    return tmp_db_path


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the scheduler sleep with a recorder so no test waits.

    Returns:
        list[float]: Every duration the code asked to sleep, in order.
    """
    recorded: list[float] = []
    monkeypatch.setattr("app.scheduler._sleep", recorded.append)
    return recorded


def _response(
    status: int,
    payload: Any = None,  # noqa: ANN401  # JSON payload
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {}, request=_REQUEST)


def _limited(retry_after: str | None = None) -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return _response(429, {"detail": "rate limited"}, headers)


def _client(*responses: httpx.Response) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.side_effect = list(responses)
    return client


def _entity_ids(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT id FROM entities")}


def _last_log(db_path: Path, service: str) -> tuple[str, str | None]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, message FROM refresh_log WHERE service = ? "
            "ORDER BY id DESC LIMIT 1",
            (service,),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _seed_cached_entity(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, fetched_at) "
            "VALUES ('cached', 'Cached LLC', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()


def _run(job: Callable[[], None], client: MagicMock) -> None:
    with patch("httpx.Client", return_value=client):
        job()


# --------------------------------------------------------------------------- #
# Retry-After parsing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        (" 12 ", 12.0),
        ("60", 60.0),
        ("600", float(scheduler.RATE_LIMIT_MAX_WAIT_SECONDS)),
        ("0", 1.0),
        (None, float(scheduler.RATE_LIMIT_DEFAULT_WAIT_SECONDS)),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("soon", None),
        ("", None),
        ("-5", None),
        ("1.5", None),
        ("٣", None),
        ("1e3", None),
    ],
)
def test_retry_after_wait_parsing(header: str | None, expected: float | None) -> None:
    """Whole seconds are honored and capped; anything else uses the default."""
    wait = scheduler.retry_after_seconds(header)
    if expected is None:
        expected = float(scheduler.RATE_LIMIT_DEFAULT_WAIT_SECONDS)
    assert wait == expected


def test_default_wait_is_within_the_cap() -> None:
    """The fallback can never exceed the cap."""
    assert (
        scheduler.RATE_LIMIT_DEFAULT_WAIT_SECONDS
        <= scheduler.RATE_LIMIT_MAX_WAIT_SECONDS
    )


# --------------------------------------------------------------------------- #
# Entities: single request
# --------------------------------------------------------------------------- #


def test_429_then_200_succeeds(initialized_db: Path, sleeps: list[float]) -> None:
    """A rate-limited first call is retried once and the refresh succeeds."""
    client = _client(_limited("3"), _response(200, [{"id": "e1", "name": "Alpha LLC"}]))
    _run(scheduler.refresh_entities, client)
    assert _entity_ids(initialized_db) == {"e1"}
    assert sleeps == [3.0]
    assert client.get.call_count == 2
    assert _last_log(initialized_db, "llc-manager")[0] == "success"


def test_retry_after_is_honored_per_attempt(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """Each wait follows the header of the 429 that caused it."""
    client = _client(
        _limited("2"),
        _limited("9"),
        _response(200, [{"id": "e1", "name": "Alpha LLC"}]),
    )
    _run(scheduler.refresh_entities, client)
    assert sleeps == [2.0, 9.0]
    assert _entity_ids(initialized_db) == {"e1"}


def test_retry_after_is_capped(initialized_db: Path, sleeps: list[float]) -> None:
    """A huge Retry-After is cut to the cap rather than stalling the job."""
    client = _client(
        _limited("86400"), _response(200, [{"id": "e1", "name": "Alpha LLC"}])
    )
    _run(scheduler.refresh_entities, client)
    assert sleeps == [float(scheduler.RATE_LIMIT_MAX_WAIT_SECONDS)]
    assert _entity_ids(initialized_db) == {"e1"}


@pytest.mark.parametrize("header", [None, "soon", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_missing_or_malformed_retry_after_uses_default(
    initialized_db: Path, sleeps: list[float], header: str | None
) -> None:
    """No usable seconds value means the default wait, then a retry."""
    client = _client(
        _limited(header), _response(200, [{"id": "e1", "name": "Alpha LLC"}])
    )
    _run(scheduler.refresh_entities, client)
    assert sleeps == [float(scheduler.RATE_LIMIT_DEFAULT_WAIT_SECONDS)]
    assert _entity_ids(initialized_db) == {"e1"}


def test_retries_exhausted_keeps_cache_and_logs_error(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """Still limited after the last retry: stale rows stay, an error is logged."""
    _seed_cached_entity(initialized_db)
    attempts = scheduler.RATE_LIMIT_MAX_RETRIES + 1
    client = _client(*[_limited("1") for _ in range(attempts)])
    _run(scheduler.refresh_entities, client)
    assert client.get.call_count == attempts
    assert len(sleeps) == scheduler.RATE_LIMIT_MAX_RETRIES
    assert _entity_ids(initialized_db) == {"cached"}
    status, message = _last_log(initialized_db, "llc-manager")
    assert status == "error"
    assert message is not None
    assert "429" in message


def test_other_errors_are_not_retried(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """A 500 is not a rate limit: no wait and no second call."""
    client = _client(_response(500, {"detail": "boom"}))
    _run(scheduler.refresh_entities, client)
    assert client.get.call_count == 1
    assert sleeps == []
    assert _last_log(initialized_db, "llc-manager")[0] == "error"


# --------------------------------------------------------------------------- #
# Documents: same behavior on the second llc-manager job
# --------------------------------------------------------------------------- #


def test_documents_429_then_200_succeeds(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """The documents refresh also waits and retries."""
    item = {
        "id": "d1",
        "title": "Operating agreement",
        "category": "LLCs",
        "is_confidential": False,
    }
    client = _client(_limited("4"), _response(200, [item]))
    _run(scheduler.refresh_documents, client)
    assert sleeps == [4.0]
    with sqlite3.connect(initialized_db) as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM documents")}
    assert ids == {"d1"}
    assert _last_log(initialized_db, "llc-manager-documents")[0] == "success"


def test_documents_retries_exhausted_keeps_cache(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """A document refresh that stays limited leaves cached documents alone."""
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, is_confidential, "
            "proxy_url, fetched_at) VALUES "
            "('old', 'Old file', 'Other', 1, '/documents/old/preview', 'x')"
        )
        conn.commit()
    client = _client(*[_limited() for _ in range(scheduler.RATE_LIMIT_MAX_RETRIES + 1)])
    _run(scheduler.refresh_documents, client)
    with sqlite3.connect(initialized_db) as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM documents")}
    assert ids == {"old"}
    assert len(sleeps) == scheduler.RATE_LIMIT_MAX_RETRIES
    assert _last_log(initialized_db, "llc-manager-documents")[0] == "error"


# --------------------------------------------------------------------------- #
# Paged fetches
# --------------------------------------------------------------------------- #


def _page(ids: list[str], total: int) -> httpx.Response:
    return _response(
        200, {"items": [{"id": i, "name": f"LLC {i}"} for i in ids], "total": total}
    )


def test_429_on_later_page_retries_only_that_page(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """Page 1 is kept and not fetched again; only the limited page is retried."""
    client = _client(_page(["e1"], 2), _limited("5"), _page(["e2"], 2))
    _run(scheduler.refresh_entities, client)
    assert sleeps == [5.0]
    pages = [call.kwargs["params"]["page"] for call in client.get.call_args_list]
    assert pages == [1, 2, 2]
    assert _entity_ids(initialized_db) == {"e1", "e2"}
    assert _last_log(initialized_db, "llc-manager")[0] == "success"


def test_429_that_persists_on_later_page_writes_nothing(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """A page set cut short by a rate limit is never written as a partial set.

    The earlier page is discarded with the failed refresh; the previous cache
    is kept whole and the failure is logged.
    """
    _seed_cached_entity(initialized_db)
    limited = [_limited("1") for _ in range(scheduler.RATE_LIMIT_MAX_RETRIES + 1)]
    client = _client(_page(["e1"], 2), *limited)
    _run(scheduler.refresh_entities, client)
    assert len(sleeps) == scheduler.RATE_LIMIT_MAX_RETRIES
    assert _entity_ids(initialized_db) == {"cached"}
    status, message = _last_log(initialized_db, "llc-manager")
    assert status == "error"
    assert message is not None
    assert "429" in message


def test_retry_budget_is_shared_across_pages(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """Retries are bounded per refresh, not per page.

    One 429 on every page of a long set cannot add up to an unbounded wait: the
    budget runs out and the refresh fails with the cache intact.
    """
    _seed_cached_entity(initialized_db)
    total = scheduler.RATE_LIMIT_MAX_RETRIES + 3
    responses: list[httpx.Response] = [_page(["p0"], total)]
    for index in range(1, total):
        responses.extend([_limited("1"), _page([f"p{index}"], total)])
    client = _client(*responses)
    _run(scheduler.refresh_entities, client)
    assert len(sleeps) == scheduler.RATE_LIMIT_MAX_RETRIES
    assert _entity_ids(initialized_db) == {"cached"}
    assert _last_log(initialized_db, "llc-manager")[0] == "error"


def test_default_sleep_delegates_to_time_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real sleep is a thin wrapper over ``time.sleep``."""
    waited: list[float] = []
    monkeypatch.setattr("time.sleep", waited.append)
    scheduler._sleep(2.5)  # noqa: SLF001  # the default the tests replace
    assert waited == [2.5]
