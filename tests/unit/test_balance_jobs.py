# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003
"""Tests for the balance jobs in ``app.scheduler``.

* ``store_balance_delivery`` is the single writer path for an intake request.
* ``snapshot_balances_daily`` runs once a day and snapshots today's balances;
  it takes the same process-wide write lock as every other writer and skips
  when a previous run is still going.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from apscheduler.triggers.interval import IntervalTrigger
from freezegun import freeze_time
from structlog.testing import capture_logs

from app import scheduler
from app.models import BalanceDelivery, BalanceRow

ENTITY = "11111111-2222-4333-8444-555555555555"


def _delivery(*account_ids: str, value: str = "10.00") -> BalanceDelivery:
    items = [
        {
            "account_id": account_id,
            "account_name": "Example Account",
            "entity_id": ENTITY,
            "category": "Cash",
            "source": "xero_bank",
            "value": value,
            "currency": "USD",
            "as_of": "2026-09-26",
        }
        for account_id in account_ids
    ]
    return BalanceDelivery.model_validate({"items": items, "total": len(items)})


def _query(path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def test_store_delivery_returns_providers_and_snapshots(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """A delivery is stored, snapshotted for the local date, and logged."""
    del portal_env
    with freeze_time("2026-09-27 15:00:00"):
        providers = scheduler.store_balance_delivery(_delivery("pp:a", "xero:b"))
    assert providers == ["pp", "xero"]
    assert _query(tmp_db_path, "SELECT date, account_id FROM balances_daily") == [
        ("2026-09-27", "pp:a"),
        ("2026-09-27", "xero:b"),
    ]
    assert _query(tmp_db_path, "SELECT service, status, rows FROM refresh_log") == [
        ("balances", "success", 2)
    ]


def test_store_delivery_uses_the_write_lock(
    portal_env: dict[str, str],
) -> None:
    """The write happens while the process-wide write lock is held."""
    del portal_env
    held: list[bool] = []
    real = scheduler.balances.replace_balances

    def spy(
        conn: sqlite3.Connection, rows: Sequence[BalanceRow], fetched_at: str
    ) -> int:
        held.append(scheduler._WRITE_LOCK.locked())  # noqa: SLF001
        return real(conn, rows, fetched_at)

    with patch("app.scheduler.balances.replace_balances", side_effect=spy):
        scheduler.store_balance_delivery(_delivery("pp:a"))
    assert held == [True]
    assert not scheduler._WRITE_LOCK.locked()  # noqa: SLF001


def test_store_delivery_failure_logs_class_only_and_reraises(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """A database error is recorded without its text, then raised to the route."""
    del portal_env
    with (
        capture_logs() as logs,
        patch(
            "app.scheduler.balances.replace_balances",
            side_effect=sqlite3.OperationalError("secret 123.45 detail"),
        ),
        pytest.raises(sqlite3.OperationalError),
    ):
        scheduler.store_balance_delivery(_delivery("pp:a"))
    assert "123.45" not in str(logs)
    rows = _query(tmp_db_path, "SELECT service, status, message FROM refresh_log")
    assert rows == [("balances", "error", "OperationalError")]


def test_daily_job_snapshots_current_balances(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """The job copies today's balances into history and logs a success."""
    del portal_env
    with freeze_time("2026-09-27 09:00:00"):
        scheduler.store_balance_delivery(_delivery("pp:a", value="1.00"))
    with freeze_time("2026-09-28 09:00:00"):
        scheduler.snapshot_balances_daily()
        scheduler.snapshot_balances_daily()
    assert _query(
        tmp_db_path, "SELECT date, value_cents FROM balances_daily ORDER BY date"
    ) == [("2026-09-27", 100), ("2026-09-28", 100)]
    services = _query(
        tmp_db_path, "SELECT service, status, rows FROM refresh_log ORDER BY id"
    )
    assert services[-1] == ("balances-daily", "success", 1)


def test_daily_job_with_no_balances_is_a_quiet_success(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """With nothing delivered yet the job writes nothing and does not fail."""
    del portal_env
    scheduler.snapshot_balances_daily()
    assert _query(tmp_db_path, "SELECT COUNT(*) FROM balances_daily") == [(0,)]
    assert _query(tmp_db_path, "SELECT status, rows FROM refresh_log") == [
        ("success", 0)
    ]


def test_daily_job_skips_when_already_running(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """A second run while one is in progress returns without writing."""
    del portal_env
    lock = scheduler._service_lock("balances-daily")  # noqa: SLF001
    assert lock.acquire(blocking=False)
    try:
        with capture_logs() as logs:
            scheduler.snapshot_balances_daily()
    finally:
        lock.release()
    assert any(e["event"] == "refresh_skipped_already_running" for e in logs)
    assert _query(tmp_db_path, "SELECT COUNT(*) FROM refresh_log") == [(0,)]


def test_daily_job_failure_is_recorded_not_raised(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """A database error is logged as an error row and never escapes the job."""
    del portal_env
    with patch(
        "app.scheduler.balances.snapshot_daily",
        side_effect=sqlite3.OperationalError("boom"),
    ):
        scheduler.snapshot_balances_daily()
    assert _query(tmp_db_path, "SELECT service, status, message FROM refresh_log") == [
        ("balances-daily", "error", "OperationalError")
    ]


def test_daily_job_is_scheduled_once_every_24_hours() -> None:
    """The job is registered with a 24 hour cadence and one instance at a time."""
    assert scheduler.JOB_INTERVAL_HOURS["snapshot_balances_daily"] == 24
    assert (
        scheduler.JOBS["snapshot_balances_daily"] is scheduler.snapshot_balances_daily
    )
    sched = scheduler.build_scheduler()
    job = next(j for j in sched.get_jobs() if j.id == "snapshot_balances_daily")
    assert job.max_instances == 1
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(hours=24)
    assert job.misfire_grace_time == 3600
    assert job.coalesce is True
    assert job.next_run_time is not None  # also runs once at startup


def test_a_same_day_account_swap_keeps_the_day_total_equal_to_the_headline(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """Two deliveries on one day for one provider leave one row per account."""
    del portal_env
    with freeze_time("2026-09-27 15:00:00"):
        scheduler.store_balance_delivery(_delivery("xero:old", value="100.00"))
        scheduler.store_balance_delivery(_delivery("xero:new", value="100.00"))
    assert _query(
        tmp_db_path, "SELECT SUM(value_cents) FROM account_balances"
    ) == _query(tmp_db_path, "SELECT SUM(value_cents) FROM balances_daily")
    assert _query(tmp_db_path, "SELECT account_id FROM balances_daily") == [
        ("xero:new",)
    ]
