# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""APScheduler jobs and the process-wide SQLite write path.

Each refresh job fetches one dataset with a synchronous ``httpx.Client``,
replaces the cached rows in a single transaction, and records the outcome in
``refresh_log``. On any failure the transaction is not started, so the
previous cached rows stay in place and the section shows stale data rather
than a blank screen (ADR-003).

Account balances are not pulled: the intake route delivers them and calls
``store_balance_delivery`` here, so its write shares ``_WRITE_LOCK`` with the
jobs. ``snapshot_balances_daily`` copies stored balances into the daily
history and calls no backend.

#ASSUME: external resources: backends may be down or return 5xx at any time
(pp-security-master is alpha). #VERIFY: every job catches transport, HTTP,
and payload errors and records ``status='error'`` instead of raising.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone, tzinfo
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog
from apscheduler.schedulers.background import (  # pyright: ignore[reportMissingTypeStubs]  # APScheduler 3 ships no stubs
    BackgroundScheduler,
)
from apscheduler.triggers.cron import (  # pyright: ignore[reportMissingTypeStubs]  # APScheduler 3 ships no stubs
    CronTrigger,
)
from apscheduler.triggers.interval import (  # pyright: ignore[reportMissingTypeStubs]  # APScheduler 3 ships no stubs
    IntervalTrigger,
)

from app import balances
from app.config import (
    BackendConfigError,
    BackendConnection,
    Settings,
    display_zone,
    load_settings,
)
from app.db import connect_sync

if TYPE_CHECKING:
    from collections.abc import Callable

    from apscheduler.triggers.base import (  # pyright: ignore[reportMissingTypeStubs]  # APScheduler 3 ships no stubs
        BaseTrigger,
    )

    from app.models import BalanceDelivery

logger = structlog.get_logger(__name__)

_MAX_MESSAGE_LENGTH = 500
_PAGE_SIZE = 100
_MAX_PAGES = 50

# Seconds a job may start late and still run. The daily snapshot gets a long
# grace so a run delayed by a busy or briefly paused process is coalesced into
# one late run instead of being dropped; every other job keeps the library
# default of one second.
DEFAULT_MISFIRE_GRACE_SECONDS = 1
JOB_MISFIRE_GRACE_SECONDS: dict[str, int] = {"snapshot_balances_daily": 3600}

# Refresh cadences in hours. Each is shorter than the staleness threshold in
# ``app.cache.STALENESS_HOURS`` so one missed run does not mark data stale.
JOB_INTERVAL_HOURS: dict[str, int] = {
    "refresh_entities": 4,
    "refresh_holdings": 2,
    "refresh_positions": 2,
    "refresh_documents": 12,
}

# ``snapshot_balances_daily`` is not a refresh: it copies the balances already
# stored into the durable daily history. It runs once at startup and then
# every day at this local wall-clock time in the display zone. A fixed local
# time, unlike a 24 hour interval, cannot drift across midnight when daylight
# saving time starts or ends, so no local date is skipped. Midday is used
# because no zone changes its clocks then; APScheduler 3 skips the day after
# the spring change for a time just after midnight. A balance delivery also
# snapshots, so a day is covered even when the job is late.
DAILY_SNAPSHOT_TIME: tuple[int, int] = (12, 0)

# The document categories the documents contract allows. A missing or
# unknown category is filed under "Other", never guessed into a specific
# folder, so a will or power of attorney is not filed as an LLC document.
DOCUMENT_CATEGORIES: frozenset[str] = frozenset(
    {
        "Estate Planning",
        "LLCs",
        "Trusts",
        "Tax Returns",
        "Insurance",
        "Personal records",
        "Other",
    }
)
_DEFAULT_DOCUMENT_CATEGORY = "Other"

# #CRITICAL: concurrency: APScheduler runs jobs on a thread pool and admins can
# trigger jobs by hand, so writes are serialized through one process-wide lock
# and each service can run only once at a time.
# #VERIFY: tests/unit/test_scheduler.py covers the skip-when-running path.
_WRITE_LOCK = threading.Lock()
_SERVICE_LOCKS: dict[str, threading.Lock] = {}
_SERVICE_LOCKS_GUARD = threading.Lock()


def _service_lock(service: str) -> threading.Lock:
    with _SERVICE_LOCKS_GUARD:
        return _SERVICE_LOCKS.setdefault(service, threading.Lock())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(
    service: str,
    status: str,
    *,
    message: str | None = None,
    rows: int | None = None,
) -> None:
    """Append one row to ``refresh_log``; never raise.

    Args:
        service (str): Backend service identifier.
        status (str): ``success`` or ``error``.
        message (str | None): Short error description.
        rows (int | None): Rows written on success.
    """
    try:
        conn = connect_sync(load_settings().sqlite_path)
        try:
            conn.execute(
                "INSERT INTO refresh_log (service, status, message, rows, ran_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    service,
                    status,
                    (message or "")[:_MAX_MESSAGE_LENGTH] or None,
                    rows,
                    _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.exception("refresh_log_write_failed", service=service)


def _get_json(
    client: httpx.Client, url: str, params: dict[str, Any] | None = None
) -> Any:  # noqa: ANN401  # JSON payload
    response = client.get(url, params=params)
    response.raise_for_status()
    return response.json()


def _get_items(client: httpx.Client, url: str) -> list[dict[str, Any]]:
    """Fetch a list endpoint that returns a bare list or ``{items, total}`` pages.

    Args:
        client (httpx.Client): Open client.
        url (str): Endpoint URL.

    Returns:
        list[dict[str, Any]]: All items across pages.

    Raises:
        TypeError: If the payload is neither a list nor a paged object.
        ValueError: If the pages do not add up to ``total``. The refresh then
            fails and the previous cached rows are kept, rather than
            replacing them with a partial set.
    """
    params: dict[str, Any] = {"page": 1, "size": _PAGE_SIZE}
    payload: object = _get_json(client, url, params)
    if isinstance(payload, list):
        return [dict(item) for item in cast("list[dict[str, Any]]", payload)]
    if not isinstance(payload, dict) or "items" not in payload:
        msg = "Unexpected payload shape"
        raise TypeError(msg)
    paged = cast("dict[str, Any]", payload)
    items: list[dict[str, Any]] = list(paged["items"])
    total = int(paged.get("total", len(items)))
    page = 1
    while len(items) < total:
        page += 1
        if page > _MAX_PAGES:
            msg = f"More than {_MAX_PAGES} pages; refusing a partial refresh"
            raise ValueError(msg)
        more: dict[str, Any] = dict(
            _get_json(client, url, {"page": page, "size": _PAGE_SIZE})
        )
        batch: list[dict[str, Any]] = list(more.get("items", []))
        if not batch:
            msg = f"Got {len(items)} of {total} items; refusing a partial refresh"
            raise ValueError(msg)
        items.extend(batch)
    return items


def _run_refresh(
    service: str,
    *,
    backend: str,
    fetch: Callable[[httpx.Client, str], Any],
    write: Callable[[sqlite3.Connection, Any, str], int],
) -> None:
    """Fetch, replace cached rows, and log the outcome for one dataset.

    A backend whose URL is unset is not connected: the job logs one line and
    makes no outbound call. A URL with a blank key is refused the same way
    and recorded as an error, so a request never goes out without its key.

    Args:
        service (str): Service identifier recorded in ``refresh_log``.
        backend (str): Backend settings key, for example ``llc_manager``.
        fetch (Callable[[httpx.Client, str], Any]): Pulls the payload.
        write (Callable[[sqlite3.Connection, Any, str], int]): Writes rows
            inside an open transaction and returns the row count.
    """
    try:
        connection = load_settings().backend_connection(backend)
    except BackendConfigError as exc:
        logger.warning(
            "refresh_skipped_backend_misconfigured", service=service, detail=str(exc)
        )
        _record(service, "error", message=str(exc))
        return
    if connection is None:
        logger.info("refresh_skipped_not_connected", service=service, backend=backend)
        return
    lock = _service_lock(service)
    if not lock.acquire(blocking=False):
        logger.info("refresh_skipped_already_running", service=service)
        return
    try:
        _refresh_once(service, connection=connection, fetch=fetch, write=write)
    finally:
        lock.release()


def _refresh_once(
    service: str,
    *,
    connection: BackendConnection,
    fetch: Callable[[httpx.Client, str], Any],
    write: Callable[[sqlite3.Connection, Any, str], int],
) -> None:
    settings = load_settings()
    headers = {"Accept": "application/json", "X-API-Key": connection.api_key}
    try:
        with httpx.Client(
            timeout=settings.backend_timeout_seconds,
            headers=headers,
        ) as client:
            payload = fetch(client, connection.url.rstrip("/"))
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        logger.warning("refresh_failed", service=service, error=type(exc).__name__)
        _record(service, "error", message=f"{type(exc).__name__}: {exc}")
        return
    fetched_at = _now()
    try:
        with _WRITE_LOCK:
            conn = connect_sync(settings.sqlite_path)
            try:
                with conn:
                    count = write(conn, payload, fetched_at)
            finally:
                conn.close()
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "refresh_write_failed", service=service, error=type(exc).__name__
        )
        _record(service, "error", message=f"{type(exc).__name__}: {exc}")
        return
    logger.info("refresh_succeeded", service=service, rows=count)
    _record(service, "success", rows=count)


# --------------------------------------------------------------------------- #
# Entities from llc-manager
# --------------------------------------------------------------------------- #


def _write_entities(
    conn: sqlite3.Connection, items: list[dict[str, Any]], fetched_at: str
) -> int:
    conn.execute("DELETE FROM entities")
    for item in items:
        active = item.get("is_active")
        status = item.get("status") or (
            None if active is None else ("active" if active else "inactive")
        )
        conn.execute(
            "INSERT INTO entities (id, name, type, state, agent, status, next_date, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(item["id"]),
                item.get("name") or item["legal_name"],
                item.get("type") or item.get("entity_type"),
                item.get("state") or item.get("formation_state"),
                item.get("agent"),
                status,
                item.get("next_date"),
                fetched_at,
            ),
        )
    return len(items)


def refresh_entities() -> None:
    """Refresh the entities cache from llc-manager."""
    _run_refresh(
        "llc-manager",
        backend="llc_manager",
        fetch=lambda client, base: _get_items(client, f"{base}/api/v1/entities"),
        write=_write_entities,
    )


# --------------------------------------------------------------------------- #
# Holdings and performance from pp-security-master
# --------------------------------------------------------------------------- #


def _write_holdings(
    conn: sqlite3.Connection, payload: dict[str, Any], fetched_at: str
) -> int:
    holdings: list[dict[str, Any]] = list(payload["holdings"])
    performance: list[dict[str, Any]] = list(payload.get("performance", []))
    conn.execute("DELETE FROM holdings")
    conn.execute("DELETE FROM performance")
    for row in holdings:
        conn.execute(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(row["id"]),
                row["security_name"],
                row.get("sector"),
                row.get("current_value"),
                row.get("allocation_pct"),
                fetched_at,
            ),
        )
    for row in performance:
        conn.execute(
            "INSERT INTO performance (date, total_value, benchmark, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (row["date"], row.get("total_value"), row.get("benchmark"), fetched_at),
        )
    return len(holdings)


def refresh_holdings() -> None:
    """Refresh holdings and performance from pp-security-master."""
    _run_refresh(
        "pp-security-master",
        backend="pp_security",
        fetch=lambda client, base: _get_json(
            client, f"{base}/api/v1/portfolio/summary"
        ),
        write=_write_holdings,
    )


# --------------------------------------------------------------------------- #
# Crypto positions from xero-crypto
# --------------------------------------------------------------------------- #


def _write_positions(
    conn: sqlite3.Connection, items: list[dict[str, Any]], fetched_at: str
) -> int:
    conn.execute("DELETE FROM positions")
    for row in items:
        conn.execute(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                str(row["id"]),
                row["asset"],
                row.get("quantity"),
                row.get("usd_value"),
                fetched_at,
            ),
        )
    return len(items)


def refresh_positions() -> None:
    """Refresh crypto positions from xero-crypto."""
    _run_refresh(
        "xero_crypto",
        backend="xero_crypto",
        fetch=lambda client, base: _get_items(client, f"{base}/api/v1/positions"),
        write=_write_positions,
    )


# --------------------------------------------------------------------------- #
# Documents from llc-manager
# --------------------------------------------------------------------------- #


def _document_category(item: dict[str, Any]) -> str:
    category = item.get("category")
    if isinstance(category, str) and category in DOCUMENT_CATEGORIES:
        return category
    return _DEFAULT_DOCUMENT_CATEGORY


def _document_title(item: dict[str, Any]) -> str:
    title = item.get("title")
    if not isinstance(title, str):
        msg = "document title is not text"
        raise TypeError(msg)
    if not title.strip():
        msg = "document has no title"
        raise ValueError(msg)
    return title.strip()


def _optional_text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    return None if value is None else str(value)


def _write_documents(
    conn: sqlite3.Connection, items: list[dict[str, Any]], fetched_at: str
) -> int:
    """Replace cached document metadata with the documents-contract fields.

    #CRITICAL: security: ``is_confidential`` fails closed. Only a JSON
    ``false`` makes a document visible to Viewers; a missing, null or
    non-boolean flag hides it. #VERIFY: tests/unit/test_scheduler.py covers
    each case.

    Args:
        conn (sqlite3.Connection): Open connection inside a transaction.
        items (list[dict[str, Any]]): Documents from the contract endpoint.
        fetched_at (str): ISO 8601 refresh time.

    Returns:
        int: Number of documents written.
    """
    conn.execute("DELETE FROM documents")
    for item in items:
        doc_id = str(item["id"])
        conn.execute(
            "INSERT INTO documents (id, name, category, entity_id, document_type, "
            "document_date, is_confidential, added_at, modified_at, proxy_url, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                _document_title(item),
                _document_category(item),
                _optional_text(item, "entity_id"),
                _optional_text(item, "document_type"),
                _optional_text(item, "document_date"),
                0 if item.get("is_confidential") is False else 1,
                _optional_text(item, "created_at"),
                _optional_text(item, "updated_at"),
                f"/documents/{doc_id}/preview",
                fetched_at,
            ),
        )
    return len(items)


def refresh_documents() -> None:
    """Refresh document metadata from llc-manager."""
    _run_refresh(
        "llc-manager-documents",
        backend="llc_manager",
        fetch=lambda client, base: _get_items(client, f"{base}/api/v1/documents"),
        write=_write_documents,
    )


# --------------------------------------------------------------------------- #
# Account balances: intake writes and the daily history snapshot
# --------------------------------------------------------------------------- #

_BALANCES_SERVICE = "balances"
_BALANCES_DAILY_SERVICE = "balances-daily"


def store_balance_delivery(delivery: BalanceDelivery) -> list[str]:
    """Store a validated balance delivery and today's snapshot in one transaction.

    This is the only writer the intake route calls. It runs under the same
    process-wide write lock as the scheduler jobs and is blocking, so the
    route runs it in a worker thread. On any database error the transaction
    is rolled back (the previous rows and history stay), the failure is
    recorded in ``refresh_log`` by error class only, and the error is raised.

    #CRITICAL: concurrency: the intake writes while scheduled jobs may also be
    writing. #VERIFY: the write holds ``_WRITE_LOCK``
    (``tests/unit/test_balance_jobs.py``) and the app runs one worker.

    Args:
        delivery (BalanceDelivery): Rows already checked by the intake model.

    Returns:
        list[str]: Providers whose rows were replaced.

    Raises:
        sqlite3.Error: If the database write fails; nothing was changed.
    """
    settings = load_settings()
    fetched_at = _now()
    today = balances.local_today().isoformat()
    try:
        with _WRITE_LOCK:
            conn = connect_sync(settings.sqlite_path)
            try:
                with conn:
                    count = balances.replace_balances(conn, delivery.items, fetched_at)
                    balances.drop_unreported_from_day(conn, today, delivery.providers())
                    balances.snapshot_daily(conn, today)
            finally:
                conn.close()
    except sqlite3.Error as exc:
        logger.warning("balance_delivery_failed", error=type(exc).__name__)
        _record(_BALANCES_SERVICE, "error", message=type(exc).__name__)
        raise
    providers = delivery.providers()
    logger.info("balance_delivery_stored", rows=count, providers=providers)
    _record(_BALANCES_SERVICE, "success", rows=count)
    return providers


def snapshot_balances_daily() -> None:
    """Copy today's stored balances into the durable daily history.

    Runs once at startup and then daily at ``DAILY_SNAPSHOT_TIME`` local. A
    database error is recorded in ``refresh_log`` (error class only) and not
    raised, so the next run tries again; any other error is left to the
    scheduler, which logs it. A run is skipped when the previous one is still
    going.
    """
    lock = _service_lock(_BALANCES_DAILY_SERVICE)
    if not lock.acquire(blocking=False):
        logger.info("refresh_skipped_already_running", service=_BALANCES_DAILY_SERVICE)
        return
    try:
        today = balances.local_today().isoformat()
        try:
            with _WRITE_LOCK:
                conn = connect_sync(load_settings().sqlite_path)
                try:
                    with conn:
                        count = balances.snapshot_daily(conn, today)
                finally:
                    conn.close()
        except sqlite3.Error as exc:
            logger.warning(
                "balance_snapshot_failed",
                service=_BALANCES_DAILY_SERVICE,
                error=type(exc).__name__,
            )
            _record(_BALANCES_DAILY_SERVICE, "error", message=type(exc).__name__)
            return
        logger.info("balance_snapshot_written", rows=count)
        _record(_BALANCES_DAILY_SERVICE, "success", rows=count)
    finally:
        lock.release()


# Scheduled jobs. ``refresh_holdings`` and ``refresh_positions`` stay
# callable from the admin trigger but are not scheduled: no backend serves
# their endpoints yet, and account balances arrive through the intake endpoint
# instead of a pull.
JOBS: dict[str, Callable[[], None]] = {
    "refresh_entities": refresh_entities,
    "refresh_documents": refresh_documents,
    "snapshot_balances_daily": snapshot_balances_daily,
}

# Admin trigger names (``POST /admin/refresh/{service}``) mapped to jobs.
TRIGGERS: dict[str, Callable[[], None]] = {
    "entities": refresh_entities,
    "holdings": refresh_holdings,
    "positions": refresh_positions,
    "documents": refresh_documents,
}


def _trigger(name: str, zone: tzinfo) -> BaseTrigger:
    """Build the trigger for one job.

    Args:
        name (str): Job name from ``JOBS``.
        zone (tzinfo): Display zone for the daily snapshot's local time.

    Returns:
        BaseTrigger: A daily trigger at ``DAILY_SNAPSHOT_TIME`` in ``zone`` for
        the snapshot job, otherwise an interval trigger from
        ``JOB_INTERVAL_HOURS``.
    """
    if name == "snapshot_balances_daily":
        hour, minute = DAILY_SNAPSHOT_TIME
        return CronTrigger(hour=hour, minute=minute, timezone=zone)
    return IntervalTrigger(hours=JOB_INTERVAL_HOURS[name], timezone=timezone.utc)


def build_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    """Create a scheduler with every job, each run once at startup.

    Args:
        settings (Settings | None): Settings that name the display zone used
            for the daily snapshot's local time. When omitted, the snapshot
            runs at that time in UTC.

    Returns:
        BackgroundScheduler: Configured, not yet started.
    """
    zone: tzinfo = display_zone(settings) if settings is not None else timezone.utc
    scheduler = BackgroundScheduler(timezone="UTC")
    start = datetime.now(timezone.utc)
    for name, job in JOBS.items():
        scheduler.add_job(  # pyright: ignore[reportUnknownMemberType]
            job,
            _trigger(name, zone),
            id=name,
            next_run_time=start,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=JOB_MISFIRE_GRACE_SECONDS.get(
                name, DEFAULT_MISFIRE_GRACE_SECONDS
            ),
        )
    return scheduler
