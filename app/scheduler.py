# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""APScheduler refresh jobs: pull backend data into the SQLite cache.

Each job fetches one dataset with a synchronous ``httpx.Client``, replaces the
cached rows in a single transaction, and records the outcome in
``refresh_log``. On any failure the transaction is not started, so the
previous cached rows stay in place and the section shows stale data rather
than a blank screen (ADR-003).

#ASSUME: external resources: backends may be down or return 5xx at any time
(pp-security-master is alpha). #VERIFY: every job catches transport, HTTP,
and payload errors and records ``status='error'`` instead of raising.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast

import httpx
import structlog
from apscheduler.schedulers.background import (  # pyright: ignore[reportMissingTypeStubs]  # APScheduler 3 ships no stubs
    BackgroundScheduler,
)

from app.config import Settings, load_settings
from app.db import connect_sync

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

_MAX_MESSAGE_LENGTH = 500
_PAGE_SIZE = 100
_MAX_PAGES = 50

# Refresh cadences in hours. Each is shorter than the staleness threshold in
# ``app.cache.STALENESS_HOURS`` so one missed run does not mark data stale.
JOB_INTERVAL_HOURS: dict[str, float] = {
    "refresh_entities": 4,
    "refresh_holdings": 2,
    "refresh_positions": 2,
    "refresh_documents": 12,
}

# llc-manager document types mapped to the portal's document categories.
_DOCUMENT_CATEGORIES: dict[str, str] = {
    "tax_return": "Tax Returns",
    "tax_election": "Tax Returns",
    "insurance_policy": "Insurance",
    "operating_agreement": "LLCs",
    "articles_of_organization": "LLCs",
    "annual_report": "LLCs",
    "meeting_minutes": "LLCs",
}
# Unknown types land in "Other", never in a specific folder such as "LLCs",
# so a will or power of attorney is not filed as an LLC document.
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
    base_url: str,
    api_key: str,
    fetch: Callable[[httpx.Client, str], Any],
    write: Callable[[sqlite3.Connection, Any, str], int],
) -> None:
    """Fetch, replace cached rows, and log the outcome for one dataset.

    Args:
        service (str): Service identifier recorded in ``refresh_log``.
        base_url (str): Backend base URL.
        api_key (str): Backend API key sent as ``X-API-Key``; an empty key
            sends no key header.
        fetch (Callable[[httpx.Client, str], Any]): Pulls the payload.
        write (Callable[[sqlite3.Connection, Any, str], int]): Writes rows
            inside an open transaction and returns the row count.
    """
    lock = _service_lock(service)
    if not lock.acquire(blocking=False):
        logger.info("refresh_skipped_already_running", service=service)
        return
    try:
        _refresh_once(
            service, base_url=base_url, api_key=api_key, fetch=fetch, write=write
        )
    finally:
        lock.release()


def _refresh_once(
    service: str,
    *,
    base_url: str,
    api_key: str,
    fetch: Callable[[httpx.Client, str], Any],
    write: Callable[[sqlite3.Connection, Any, str], int],
) -> None:
    settings = load_settings()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        with httpx.Client(
            timeout=settings.backend_timeout_seconds,
            headers=headers,
        ) as client:
            payload = fetch(client, base_url.rstrip("/"))
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
    settings = load_settings()
    _run_refresh(
        "llc-manager",
        base_url=settings.backend_llc_manager_url,
        api_key=settings.backend_llc_manager_api_key.get_secret_value(),
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
    settings = load_settings()
    _run_refresh(
        "pp-security-master",
        base_url=settings.backend_pp_security_url,
        api_key=settings.backend_pp_security_api_key.get_secret_value(),
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
    settings = load_settings()
    _run_refresh(
        "xero_crypto",
        base_url=settings.backend_xero_crypto_url,
        api_key=settings.backend_xero_crypto_api_key.get_secret_value(),
        fetch=lambda client, base: _get_items(client, f"{base}/api/v1/positions"),
        write=_write_positions,
    )


# --------------------------------------------------------------------------- #
# Documents from llc-manager
# --------------------------------------------------------------------------- #


def _document_category(item: dict[str, Any]) -> str:
    category = item.get("category")
    if category:
        return str(category)
    return _DOCUMENT_CATEGORIES.get(
        str(item.get("document_type", "")), _DEFAULT_DOCUMENT_CATEGORY
    )


def _write_documents(
    conn: sqlite3.Connection, items: list[dict[str, Any]], fetched_at: str
) -> int:
    conn.execute("DELETE FROM documents")
    for item in items:
        doc_id = str(item["id"])
        conn.execute(
            "INSERT INTO documents (id, name, category, entity_id, document_type, "
            "document_date, is_confidential, added_at, modified_at, proxy_url, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                item.get("name") or item["title"],
                _document_category(item),
                None if item.get("entity_id") is None else str(item["entity_id"]),
                item.get("document_type"),
                item.get("document_date"),
                1 if item.get("is_confidential") else 0,
                item.get("added_at") or item.get("created_at"),
                item.get("modified_at") or item.get("updated_at"),
                f"/documents/{doc_id}/preview",
                fetched_at,
            ),
        )
    return len(items)


def refresh_documents() -> None:
    """Refresh document metadata from llc-manager."""
    settings = load_settings()
    _run_refresh(
        "llc-manager-documents",
        base_url=settings.backend_llc_manager_url,
        api_key=settings.backend_llc_manager_api_key.get_secret_value(),
        fetch=lambda client, base: _get_items(client, f"{base}/api/v1/documents"),
        write=_write_documents,
    )


# Scheduled jobs. ``refresh_holdings`` and ``refresh_positions`` stay
# callable from the admin trigger but are not scheduled: no backend serves
# their endpoints yet, and a later phase replaces positions with the account
# balance jobs.
JOBS: dict[str, Callable[[], None]] = {
    "refresh_entities": refresh_entities,
    "refresh_documents": refresh_documents,
}

# Admin trigger names (``POST /admin/refresh/{service}``) mapped to jobs.
TRIGGERS: dict[str, Callable[[], None]] = {
    "entities": refresh_entities,
    "holdings": refresh_holdings,
    "positions": refresh_positions,
    "documents": refresh_documents,
}


def build_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    """Create a scheduler with every refresh job, each run once at startup.

    Args:
        settings (Settings | None): Unused today; accepted for future cadence
            configuration.

    Returns:
        BackgroundScheduler: Configured, not yet started.
    """
    del settings
    scheduler = BackgroundScheduler(timezone="UTC")
    start = datetime.now(timezone.utc)
    for name, job in JOBS.items():
        scheduler.add_job(  # pyright: ignore[reportUnknownMemberType]
            job,
            "interval",
            hours=JOB_INTERVAL_HOURS[name],
            id=name,
            next_run_time=start,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
