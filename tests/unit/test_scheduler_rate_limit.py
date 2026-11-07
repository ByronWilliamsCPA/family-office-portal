# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Rate-limit (HTTP 429) handling in the scheduler refresh jobs.

The llc-manager backend is expected to allow about 60 requests per minute. A refresh
that meets a 429 reads ``Retry-After`` (whole seconds only), waits a bounded
time, and retries a bounded number of times. When the backend is still
limiting, the refresh fails the normal way: the cached rows stay and
``refresh_log`` records an error, so the section shows stale data.

The sleep is replaced in every refresh test, so none of them waits; only the
tests of the sleep itself make real waits, each under a second. Responses are real
``httpx.Response`` objects; only the client is faked.
"""

from __future__ import annotations

import importlib
import sqlite3
import time
from contextlib import closing
from typing import TYPE_CHECKING, Any, NamedTuple
from unittest.mock import MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

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
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[0] for row in conn.execute("SELECT id FROM entities")}


def _last_log(db_path: Path, service: str) -> tuple[str, str | None]:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT status, message FROM refresh_log WHERE service = ? "
            "ORDER BY id DESC LIMIT 1",
            (service,),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def _seed_cached_entity(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
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
        ("0005", 5.0),
        ("0" * 5000 + "7", 7.0),
        ("9" * 5000, float(scheduler.RATE_LIMIT_MAX_WAIT_SECONDS)),
        ("1000", float(scheduler.RATE_LIMIT_MAX_WAIT_SECONDS)),
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
    with closing(sqlite3.connect(initialized_db)) as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM documents")}
    assert ids == {"d1"}
    assert _last_log(initialized_db, "llc-manager-documents")[0] == "success"


def test_documents_retries_exhausted_keeps_cache(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """A document refresh that stays limited leaves cached documents alone."""
    with closing(sqlite3.connect(initialized_db)) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, is_confidential, "
            "proxy_url, fetched_at) VALUES "
            "('old', 'Old file', 'Other', 1, '/documents/old/preview', 'x')"
        )
        conn.commit()
    client = _client(*[_limited() for _ in range(scheduler.RATE_LIMIT_MAX_RETRIES + 1)])
    _run(scheduler.refresh_documents, client)
    with closing(sqlite3.connect(initialized_db)) as conn:
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


# --------------------------------------------------------------------------- #
# The same handling on the holdings and positions refreshes
# --------------------------------------------------------------------------- #


class _Case(NamedTuple):
    job: Callable[[], None]
    service: str
    payload: Any
    ids_sql: str
    seed: str


_HOLDINGS = _Case(
    scheduler.refresh_holdings,
    "pp-security-master",
    {"holdings": [{"id": "h1", "security_name": "Fund A"}], "performance": []},
    "SELECT id FROM holdings",
    "INSERT INTO holdings (id, security_name, fetched_at) "
    "VALUES ('cached', 'Cached Fund', 'x')",
)
_POSITIONS = _Case(
    scheduler.refresh_positions,
    "xero_crypto",
    [{"id": "p1", "asset": "BTC"}],
    "SELECT id FROM positions",
    "INSERT INTO positions (id, asset, fetched_at) VALUES ('cached', 'ETH', 'x')",
)
_CASES = [
    pytest.param(_HOLDINGS, id="holdings"),
    pytest.param(_POSITIONS, id="positions"),
]


def _ids(db_path: Path, ids_sql: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[0] for row in conn.execute(ids_sql)}


@pytest.mark.parametrize("case", _CASES)
def test_other_jobs_429_then_200_succeeds(
    initialized_db: Path, sleeps: list[float], case: _Case
) -> None:
    """Holdings and positions also wait out a 429 and then succeed."""
    client = _client(_limited("6"), _response(200, case.payload))
    _run(case.job, client)
    assert sleeps == [6.0]
    assert client.get.call_count == 2
    assert _ids(initialized_db, case.ids_sql) != set()
    assert _last_log(initialized_db, case.service)[0] == "success"


@pytest.mark.parametrize("case", _CASES)
def test_other_jobs_retries_exhausted_keep_cache(
    initialized_db: Path, sleeps: list[float], case: _Case
) -> None:
    """Still limited: the cached rows stay and an error is logged."""
    with closing(sqlite3.connect(initialized_db)) as conn:
        conn.execute(case.seed)
        conn.commit()
    attempts = scheduler.RATE_LIMIT_MAX_RETRIES + 1
    client = _client(*[_limited("1") for _ in range(attempts)])
    _run(case.job, client)
    assert client.get.call_count == attempts
    assert len(sleeps) == scheduler.RATE_LIMIT_MAX_RETRIES
    assert _ids(initialized_db, case.ids_sql) == {"cached"}
    status, message = _last_log(initialized_db, case.service)
    assert status == "error"
    assert message is not None
    assert "429" in message


def test_each_holdings_run_gets_a_full_budget(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """A single-request job starts every run with a fresh retry budget."""
    payload = _HOLDINGS.payload
    for _ in range(2):
        limited = [_limited("1") for _ in range(scheduler.RATE_LIMIT_MAX_RETRIES)]
        client = _client(*limited, _response(200, payload))
        _run(scheduler.refresh_holdings, client)
        assert _last_log(initialized_db, "pp-security-master")[0] == "success"
    assert len(sleeps) == 2 * scheduler.RATE_LIMIT_MAX_RETRIES


# --------------------------------------------------------------------------- #
# What is logged
# --------------------------------------------------------------------------- #


def test_rate_limit_event_names_the_request_and_nothing_sensitive(
    initialized_db: Path, sleeps: list[float]
) -> None:
    """The warning carries the request path, wait and retries left, no more."""
    del initialized_db, sleeps
    client = _client(_limited("3"), _response(200, []))
    with capture_logs() as logs:
        _run(scheduler.refresh_entities, client)
    events = [entry for entry in logs if entry["event"] == "backend_rate_limited"]
    assert len(events) == 1
    event = events[0]
    assert {k: v for k, v in event.items() if k != "log_level"} == {
        "event": "backend_rate_limited",
        "path": "/api/v1/entities",
        "wait_seconds": 3.0,
        "retries_left": scheduler.RATE_LIMIT_MAX_RETRIES - 1,
    }
    assert event["log_level"] == "warning"


def test_rate_limit_event_path_has_no_query() -> None:
    """Only the path is logged, even for a URL that carries a query."""
    client = _client(_limited("1"), _response(200, []))
    with capture_logs() as logs, patch("app.scheduler._sleep"):
        scheduler._get_json(  # noqa: SLF001  # exercised directly: the URL shape is the point
            client, "http://llc.test/api/v1/x?token=abc", {"page": 1}
        )
    event = next(e for e in logs if e["event"] == "backend_rate_limited")
    assert event["path"] == "/api/v1/x"
    assert "abc" not in str(event)


# --------------------------------------------------------------------------- #
# Shutdown: a sleeping retry must not hold the process open
# --------------------------------------------------------------------------- #


def test_sleep_waits_normally_when_no_shutdown() -> None:
    """Without a shutdown request the real sleep waits its time and returns."""
    started = time.monotonic()
    scheduler._sleep(0.05)  # noqa: SLF001  # the real default the tests replace
    assert time.monotonic() - started >= 0.04


def test_sleep_ends_at_once_on_shutdown() -> None:
    """A shutdown request ends the wait immediately and raises."""
    scheduler.request_shutdown()
    assert scheduler.shutdown_requested()
    started = time.monotonic()
    with pytest.raises(scheduler.RefreshInterruptedError):
        scheduler._sleep(60)  # noqa: SLF001  # the real default the tests replace
    assert time.monotonic() - started < 5


def test_shutdown_during_429_wait_fails_refresh_and_writes_nothing(
    initialized_db: Path,
) -> None:
    """Interrupted while waiting: error logged, no retry, cache untouched."""
    _seed_cached_entity(initialized_db)
    scheduler.request_shutdown()
    client = _client(_limited("30"), _response(200, [{"id": "e1", "name": "A"}]))
    _run(scheduler.refresh_entities, client)
    assert client.get.call_count == 1
    assert _entity_ids(initialized_db) == {"cached"}
    status, message = _last_log(initialized_db, "llc-manager")
    assert status == "error"
    assert message is not None
    assert "RefreshInterruptedError" in message


def test_clear_shutdown_restores_waiting() -> None:
    """A cleared flag lets later sleeps in the same process wait again."""
    scheduler.request_shutdown()
    scheduler.clear_shutdown()
    assert not scheduler.shutdown_requested()
    scheduler._sleep(0.01)  # noqa: SLF001  # the real default the tests replace


async def test_lifespan_clears_then_sets_the_flag(initialized_db: Path) -> None:
    """Startup clears a stale flag; leaving the app sets it before shutdown."""
    del initialized_db
    from app import main  # noqa: PLC0415  # app.main exits at import without env

    importlib.reload(main)
    scheduler.request_shutdown()
    fake = MagicMock()
    states: list[bool] = []
    fake.shutdown.side_effect = lambda **_: states.append(
        scheduler.shutdown_requested()
    )
    with patch.object(main, "build_scheduler", return_value=fake):
        async with main.lifespan(main.app):
            assert not scheduler.shutdown_requested()
    assert states == [True]


async def test_lifespan_sets_the_flag_when_the_scheduler_is_disabled(
    initialized_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the app sets the flag even with no scheduler to stop."""
    del initialized_db
    from app import main  # noqa: PLC0415  # app.main exits at import without env

    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    importlib.reload(main)
    with patch.object(main, "build_scheduler") as build:
        async with main.lifespan(main.app):
            assert not scheduler.shutdown_requested()
        build.assert_not_called()
    assert scheduler.shutdown_requested()
