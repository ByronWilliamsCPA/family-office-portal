# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003
"""Unit tests for ``app.balances``: cents conversion, replacement, snapshots.

Every account, entity and amount is made up.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from app import balances
from app.db import connect_sync, init_schema
from app.models import BalanceRow

ENTITY_A = "11111111-2222-4333-8444-555555555555"
ENTITY_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"


def _row(account_id: str = "pp:example-1", **overrides: object) -> BalanceRow:
    data: dict[str, object] = {
        "account_id": account_id,
        "account_name": "Example Brokerage",
        "entity_id": ENTITY_A,
        "category": "Investments",
        "source": "broker_report",
        "value": "1000.00",
        "currency": "USD",
        "as_of": "2026-09-26",
        **overrides,
    }
    return BalanceRow.model_validate(data)


@pytest.fixture
def conn(tmp_db_path: Path) -> sqlite3.Connection:
    """Open a connection to a fresh schema.

    Args:
        tmp_db_path: Per-test database path.

    Returns:
        sqlite3.Connection: Open connection with the schema applied.
    """
    init_schema(str(tmp_db_path))
    return connect_sync(str(tmp_db_path))


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("1234.56", 123456),
        ("100", 10000),
        ("0.00", 0),
        ("-12.34", -1234),
        ("0.005", 0),
        ("0.015", 2),
        ("0.025", 2),
        ("10.125", 1012),
        ("-12.345", -1234),
        ("-12.355", -1236),
        ("999999999999.9999", 100_000_000_000_000),
    ],
)
def test_to_cents_rounds_half_even(text: str, cents: int) -> None:
    """Values become integer cents with banker's rounding and no float math."""
    assert balances.to_cents(text) == cents


@pytest.mark.parametrize(
    ("account_id", "provider"),
    [("pp:broker:a1", "pp"), ("xero:abc", "xero"), ("crypto:w1", "crypto"), ("x", "")],
)
def test_provider_of_is_the_prefix(account_id: str, provider: str) -> None:
    """The provider is the text before the first colon, or empty."""
    assert balances.provider_of(account_id) == provider


def _stored(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM account_balances").fetchall()
    return {str(r["account_id"]): r for r in rows}


def test_replace_stores_cents_currency_and_reconciled_through(
    conn: sqlite3.Connection,
) -> None:
    """Rows are stored as integer cents with their currency and extras."""
    xero = _row(
        "xero:bank-1",
        category="Cash",
        source="xero_bank",
        value="10.125",
        reconciled_through="2026-09-20",
    )
    with conn:
        count = balances.replace_balances(
            conn, [_row(), xero], "2026-09-27T01:00:00+00:00"
        )
    stored = _stored(conn)
    assert count == 2
    assert stored["pp:example-1"]["value_cents"] == 100000
    assert stored["pp:example-1"]["currency"] == "USD"
    assert stored["pp:example-1"]["reconciled_through"] is None
    assert stored["pp:example-1"]["entity_id"] == ENTITY_A
    assert stored["xero:bank-1"]["value_cents"] == 1012
    assert stored["xero:bank-1"]["reconciled_through"] == "2026-09-20"
    assert stored["xero:bank-1"]["fetched_at"] == "2026-09-27T01:00:00+00:00"


def test_replace_swaps_present_providers_and_keeps_absent_ones(
    conn: sqlite3.Connection,
) -> None:
    """A provider in the delivery is replaced; a missing provider is kept."""
    first = [
        _row("pp:one"),
        _row("pp:two"),
        _row("xero:bank", source="xero_bank", category="Cash"),
        _row("crypto:wallet", source="crypto_tracker", category="Digital currency"),
    ]
    with conn:
        balances.replace_balances(conn, first, "2026-09-26T00:00:00+00:00")
    second = [_row("pp:one", value="2000.00"), _row("pp:three")]
    with conn:
        balances.replace_balances(conn, second, "2026-09-27T00:00:00+00:00")
    stored = _stored(conn)
    assert set(stored) == {"pp:one", "pp:three", "xero:bank", "crypto:wallet"}
    assert stored["pp:one"]["value_cents"] == 200000
    assert stored["pp:one"]["fetched_at"] == "2026-09-27T00:00:00+00:00"
    assert stored["xero:bank"]["fetched_at"] == "2026-09-26T00:00:00+00:00"
    assert stored["crypto:wallet"]["fetched_at"] == "2026-09-26T00:00:00+00:00"


def test_replace_with_no_rows_changes_nothing(conn: sqlite3.Connection) -> None:
    """An empty delivery clears no provider."""
    with conn:
        balances.replace_balances(conn, [_row()], "2026-09-26T00:00:00+00:00")
        count = balances.replace_balances(conn, [], "2026-09-27T00:00:00+00:00")
    assert count == 0
    assert set(_stored(conn)) == {"pp:example-1"}


def test_replace_does_not_treat_wildcards_in_prefix_as_patterns(
    conn: sqlite3.Connection,
) -> None:
    """Replacing ``pp`` never removes a row whose id merely resembles it."""
    conn.execute(
        "INSERT INTO account_balances (account_id, account_name, category, source, "
        "value_cents, as_of, fetched_at) VALUES ('ppx:odd', 'Odd', 'Cash', 'm', 1, "
        "'2026-01-01', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    with conn:
        balances.replace_balances(conn, [_row()], "2026-09-27T00:00:00+00:00")
    assert "ppx:odd" in _stored(conn)


def test_snapshot_upserts_one_row_per_account_per_day(
    conn: sqlite3.Connection,
) -> None:
    """A later run on the same day overwrites; a new day adds rows."""
    with conn:
        balances.replace_balances(conn, [_row()], "2026-09-26T00:00:00+00:00")
        assert balances.snapshot_daily(conn, "2026-09-27") == 1
        balances.replace_balances(
            conn, [_row(value="1500.00")], "2026-09-27T12:00:00+00:00"
        )
        balances.snapshot_daily(conn, "2026-09-27")
        balances.snapshot_daily(conn, "2026-09-28")
    rows = conn.execute(
        "SELECT date, account_id, value_cents, category, entity_id, currency, as_of "
        "FROM balances_daily ORDER BY date"
    ).fetchall()
    assert [(r["date"], r["value_cents"]) for r in rows] == [
        ("2026-09-27", 150000),
        ("2026-09-28", 150000),
    ]
    assert rows[0]["category"] == "Investments"
    assert rows[0]["entity_id"] == ENTITY_A
    assert rows[0]["currency"] == "USD"
    assert rows[0]["as_of"] == "2026-09-26"


def test_snapshot_never_deletes_history(conn: sqlite3.Connection) -> None:
    """An account that disappears keeps its earlier daily rows."""
    with conn:
        balances.replace_balances(
            conn, [_row("pp:gone"), _row("pp:stays")], "2026-09-26T00:00:00+00:00"
        )
        balances.snapshot_daily(conn, "2026-09-26")
        balances.replace_balances(conn, [_row("pp:stays")], "2026-09-27T00:00:00+00:00")
        balances.snapshot_daily(conn, "2026-09-26")
        balances.snapshot_daily(conn, "2026-09-27")
    ids = {
        (r["date"], r["account_id"])
        for r in conn.execute("SELECT date, account_id FROM balances_daily")
    }
    assert ids == {
        ("2026-09-26", "pp:gone"),
        ("2026-09-26", "pp:stays"),
        ("2026-09-27", "pp:stays"),
    }


def _deliver(
    conn: sqlite3.Connection, rows: list[BalanceRow], day: str, fetched: str
) -> None:
    """Apply one delivery the way the writer path does, in one transaction."""
    with conn:
        balances.replace_balances(conn, rows, fetched)
        balances.drop_unreported_from_day(conn, day, sorted({r.provider for r in rows}))
        balances.snapshot_daily(conn, day)


def _day_total(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(value_cents), 0) FROM balances_daily WHERE date = ?",
        (day,),
    ).fetchone()
    return int(row[0])


def test_same_day_account_swap_does_not_double_count(
    conn: sqlite3.Connection,
) -> None:
    """A later delivery that swaps an account corrects the day's total."""
    _deliver(conn, [_row("xero:old", value="100.00")], "2026-09-27", "t1")
    _deliver(conn, [_row("xero:new", value="100.00")], "2026-09-27", "t2")
    assert _day_total(conn, "2026-09-27") == 10_000
    ids = [r[0] for r in conn.execute("SELECT account_id FROM balances_daily")]
    assert ids == ["xero:new"]


def test_same_day_repeat_delivery_is_idempotent(conn: sqlite3.Connection) -> None:
    """Sending the same delivery twice leaves one row per account for the day."""
    rows = [_row("pp:a", value="5.00"), _row("pp:b", value="7.50")]
    _deliver(conn, rows, "2026-09-27", "t1")
    _deliver(conn, rows, "2026-09-27", "t2")
    assert _day_total(conn, "2026-09-27") == 1_250
    assert conn.execute("SELECT COUNT(*) FROM balances_daily").fetchone()[0] == 2


def test_absent_provider_is_untouched_by_the_correction(
    conn: sqlite3.Connection,
) -> None:
    """Today's rows of a provider that is not in the delivery are kept."""
    _deliver(
        conn,
        [_row("pp:a", value="1.00"), _row("xero:x", value="2.00")],
        "2026-09-27",
        "t1",
    )
    _deliver(conn, [_row("pp:a", value="1.00")], "2026-09-27", "t2")
    ids = sorted(r[0] for r in conn.execute("SELECT account_id FROM balances_daily"))
    assert ids == ["pp:a", "xero:x"]
    assert _day_total(conn, "2026-09-27") == 300


def test_previous_days_are_never_touched_by_the_correction(
    conn: sqlite3.Connection,
) -> None:
    """Only the current day is corrected; earlier history stays as it was."""
    _deliver(conn, [_row("xero:old", value="100.00")], "2026-09-26", "t1")
    _deliver(conn, [_row("xero:new", value="100.00")], "2026-09-27", "t2")
    rows = conn.execute(
        "SELECT date, account_id FROM balances_daily ORDER BY date"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-09-26", "xero:old"),
        ("2026-09-27", "xero:new"),
    ]


def test_snapshot_of_empty_table_writes_nothing(conn: sqlite3.Connection) -> None:
    """With no balances there is nothing to snapshot."""
    with conn:
        assert balances.snapshot_daily(conn, "2026-09-27") == 0


def test_local_today_uses_the_display_zone(
    monkeypatch: pytest.MonkeyPatch, portal_env: dict[str, str]
) -> None:
    """Today is the date in ``DISPLAY_TIMEZONE``, with UTC as the fallback."""
    del portal_env
    from freezegun import freeze_time  # noqa: PLC0415

    with freeze_time("2026-09-27 02:30:00"):
        monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
        assert balances.local_today() == date(2026, 9, 26)
        monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
        assert balances.local_today() == date(2026, 9, 27)
        monkeypatch.setenv("DISPLAY_TIMEZONE", "Not/AZone")
        assert balances.local_today() == date(2026, 9, 27)


def test_connection_helper_closes(tmp_db_path: Path) -> None:
    """Sanity check that the fixture schema has the new column."""
    init_schema(str(tmp_db_path))
    with closing(sqlite3.connect(tmp_db_path)) as raw:
        columns = {r[1] for r in raw.execute("PRAGMA table_info(account_balances)")}
    assert "reconciled_through" in columns
