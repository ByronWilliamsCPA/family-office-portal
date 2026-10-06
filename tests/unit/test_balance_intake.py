# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC002, TC003
"""Tests for the balance intake endpoint (``POST /api/v1/balances``).

The endpoint is called by a machine collector with a shared key, not by a
signed-in person. It is disabled until its key setting is present, answers
401 for a wrong or missing key, replaces stored balances in one transaction,
and never echoes a submitted value in an error. Every account, entity and
amount below is made up, and keys are generated inside each test.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from starlette.concurrency import run_in_threadpool
from structlog.testing import capture_logs

from app.models import BalanceDelivery
from app.routes.balances import MAX_BODY_BYTES

URL = "/api/v1/balances"
ENTITY_A = "11111111-2222-4333-8444-555555555555"
ENTITY_B = "66666666-7777-4888-8999-aaaaaaaaaaaa"
SENTINEL = "ZZ-sentinel-42"


def _row(account_id: str = "pp:example-1", **overrides: object) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "account_name": "Example Brokerage",
        "entity_id": ENTITY_A,
        "category": "Investments",
        "source": "broker_report",
        "value": "1234.56",
        "currency": "USD",
        "as_of": "2026-09-26",
        **overrides,
    }


def _body(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"items": rows, "total": len(rows) if total is None else total}


@pytest.fixture
def intake_key(portal_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> str:
    """Enable the intake with a random key made for this test."""
    del portal_env
    key = secrets.token_urlsafe(32)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", key)
    return key


def _post_headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _stored(path: Path) -> dict[str, tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT account_id, value_cents, currency, entity_id, category, "
            "source, as_of, reconciled_through, account_name FROM account_balances"
        ).fetchall()
    return {r[0]: r for r in rows}


def _history(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT date, account_id, entity_id, category, value_cents, as_of, "
            "currency FROM balances_daily ORDER BY date, account_id"
        ).fetchall()


def _count(path: Path, table: str) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --------------------------------------------------------------------------- #
# Disabled, and authentication
# --------------------------------------------------------------------------- #


async def test_disabled_when_key_is_not_set(
    anon_client: AsyncClient, tmp_db_path: Path
) -> None:
    """Without the setting the endpoint answers 404 and stores nothing."""
    async with anon_client as ac:
        response = await ac.post(
            URL, json=_body([_row()]), headers={"X-API-Key": "anything"}
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
    assert _count(tmp_db_path, "account_balances") == 0


async def test_disabled_when_key_is_blank(
    anon_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_db_path: Path,
) -> None:
    """A blank or whitespace key counts as unset, so even a blank header fails."""
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", "   ")
    async with anon_client as ac:
        response = await ac.post(URL, json=_body([_row()]), headers={"X-API-Key": " "})
    assert response.status_code == 404
    assert _count(tmp_db_path, "account_balances") == 0


async def test_disabled_even_with_valid_jwt(
    anon_client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A signed-in admin cannot use the endpoint while it is disabled."""
    async with anon_client as ac:
        response = await ac.post(URL, json=_body([_row()]), headers=admin_headers)
    assert response.status_code == 404
    assert _count(tmp_db_path, "account_balances") == 0


async def test_missing_key_is_401(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """No key header: 401 with a JSON detail, nothing stored."""
    del intake_key
    async with anon_client as ac:
        response = await ac.post(URL, json=_body([_row()]))
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)
    assert _count(tmp_db_path, "account_balances") == 0


@pytest.mark.parametrize("wrong", ["", "x", "not-the-key", "k" * 200, "café"])
async def test_wrong_key_is_401(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path, wrong: str
) -> None:
    """Any other key, of any length or alphabet, is 401."""
    assert wrong != intake_key
    async with anon_client as ac:
        response = await ac.post(
            URL,
            content=json.dumps(_body([_row()])).encode(),
            headers={
                "X-API-Key": wrong.encode("utf-8"),
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 401
    assert _count(tmp_db_path, "account_balances") == 0


async def _oversized_stream() -> AsyncIterator[bytes]:
    """Yield a body larger than ``MAX_BODY_BYTES`` with no declared length."""
    chunk = b" " * (1024 * 1024)
    for _ in range(MAX_BODY_BYTES // len(chunk) + 1):
        yield chunk


@pytest.mark.parametrize("key", [None, "wrong-key"], ids=["missing", "wrong"])
@pytest.mark.parametrize("body", ["broken", "declared-oversize", "streamed-oversize"])
async def test_key_is_checked_before_the_body_is_read(
    anon_client: AsyncClient, intake_key: str, key: str | None, body: str
) -> None:
    """A missing or wrong key is 401 whatever the body, never 413 or 422.

    A body over the size limit would be 413 if it were read first, and a
    broken one 422 if it were parsed first, so a 401 for both proves the key
    is checked before the body is touched.
    """
    del intake_key
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-API-Key"] = key
    content: bytes | AsyncIterator[bytes]
    if body == "broken":
        content = b"{not json"
    elif body == "declared-oversize":
        content = b" " * (MAX_BODY_BYTES + 1)
    else:
        content = _oversized_stream()
    async with anon_client as ac:
        response = await ac.post(URL, content=content, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "A valid API key is required."}


async def test_valid_jwt_alone_does_not_authorise_intake(
    anon_client: AsyncClient,
    intake_key: str,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Identity from the sign-in proxy is not a substitute for the key."""
    del intake_key
    async with anon_client as ac:
        response = await ac.post(URL, json=_body([_row()]), headers=admin_headers)
    assert response.status_code == 401
    assert _count(tmp_db_path, "account_balances") == 0


async def test_key_compare_is_constant_time(
    anon_client: AsyncClient, intake_key: str
) -> None:
    """The key is compared with ``hmac.compare_digest``."""
    with patch(
        "app.routes.balances.hmac.compare_digest", return_value=False
    ) as compare:
        async with anon_client as ac:
            response = await ac.post(
                URL, json=_body([_row()]), headers=_post_headers(intake_key)
            )
    assert response.status_code == 401
    compare.assert_called_once()


async def test_key_with_surrounding_spaces_in_setting_still_works(
    anon_client: AsyncClient,
    portal_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surrounding whitespace in the setting is ignored, as for backend keys."""
    del portal_env
    key = secrets.token_urlsafe(32)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", f"  {key}  ")
    async with anon_client as ac:
        response = await ac.post(URL, json=_body([_row()]), headers={"X-API-Key": key})
    assert response.status_code == 200


async def test_other_methods_are_not_allowed(
    anon_client: AsyncClient, intake_key: str, admin_headers: dict[str, str]
) -> None:
    """Only POST is served; the key does not turn on other methods."""
    del intake_key
    async with anon_client as ac:
        no_jwt = await ac.get(URL)
        with_jwt = await ac.get(URL, headers=admin_headers)
    assert no_jwt.status_code == 403
    assert with_jwt.status_code == 405
    assert with_jwt.json() == {"detail": "Method Not Allowed"}


async def test_unknown_api_path_errors_are_json(
    anon_client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """Errors under ``/api/`` are JSON for machine callers, not an HTML page."""
    async with anon_client as ac:
        response = await ac.get("/api/v1/unknown", headers=admin_headers)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------------- #
# Accepted deliveries
# --------------------------------------------------------------------------- #


async def test_valid_delivery_is_stored(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """Rows are stored as integer cents with currency, and today is snapshotted."""
    rows = [
        _row("pp:example-1", value="1234.56"),
        _row(
            "xero:example-bank",
            account_name="Example Operating",
            entity_id=ENTITY_B.upper(),
            category="Cash",
            source="xero_bank",
            value="-20.10",
            as_of="2026-09-25",
            reconciled_through="2026-09-20",
        ),
        _row(
            "crypto:example-wallet",
            category="Digital currency",
            source="crypto_tracker",
            value="10.125",
            currency="USD",
        ),
    ]
    async with anon_client as ac:
        response = await ac.post(
            URL, json=_body(rows), headers=_post_headers(intake_key)
        )
    assert response.status_code == 200
    assert response.json() == {"accepted": 3, "providers": ["crypto", "pp", "xero"]}
    stored = _stored(tmp_db_path)
    assert stored["pp:example-1"][1:3] == (123456, "USD")
    bank = stored["xero:example-bank"]
    assert bank[1] == -2010
    assert bank[3] == ENTITY_B
    assert bank[7] == "2026-09-20"
    assert stored["crypto:example-wallet"][1] == 1012
    assert _count(tmp_db_path, "balances_daily") == 3
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        log = conn.execute("SELECT service, status, rows FROM refresh_log").fetchall()
        fetched = conn.execute(
            "SELECT DISTINCT fetched_at FROM account_balances"
        ).fetchall()
    assert log == [("balances", "success", 3)]
    assert len(fetched) == 1
    assert datetime.fromisoformat(fetched[0][0]).tzinfo is not None


async def test_second_delivery_replaces_present_providers_only(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """A provider missing from a later delivery keeps its last rows."""
    first = [
        _row("pp:one"),
        _row("pp:two"),
        _row("xero:bank", source="xero_bank", category="Cash"),
    ]
    second = [_row("pp:one", value="5.00")]
    async with anon_client as ac:
        await ac.post(URL, json=_body(first), headers=_post_headers(intake_key))
        response = await ac.post(
            URL, json=_body(second), headers=_post_headers(intake_key)
        )
    assert response.json()["providers"] == ["pp"]
    stored = _stored(tmp_db_path)
    assert set(stored) == {"pp:one", "xero:bank"}
    assert stored["pp:one"][1] == 500


async def test_empty_delivery_is_accepted_and_clears_nothing(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """``{"items": [], "total": 0}`` is valid and removes no provider."""
    async with anon_client as ac:
        await ac.post(URL, json=_body([_row()]), headers=_post_headers(intake_key))
        response = await ac.post(URL, json=_body([]), headers=_post_headers(intake_key))
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "providers": []}
    assert set(_stored(tmp_db_path)) == {"pp:example-1"}


async def test_same_day_deliveries_overwrite_the_daily_row(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """Two deliveries on one day leave one daily row holding the later value."""
    async with anon_client as ac:
        await ac.post(
            URL,
            json=_body([_row(value="1.00")]),
            headers=_post_headers(intake_key),
        )
        await ac.post(
            URL,
            json=_body([_row(value="2.00")]),
            headers=_post_headers(intake_key),
        )
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        rows = conn.execute("SELECT value_cents FROM balances_daily").fetchall()
    assert rows == [(200,)]


async def test_write_runs_off_the_event_loop(
    anon_client: AsyncClient, intake_key: str
) -> None:
    """The blocking SQLite write goes through the thread pool."""
    with patch(
        "app.routes.balances.run_in_threadpool",
        new_callable=AsyncMock,
        side_effect=run_in_threadpool,
    ) as pool:
        async with anon_client as ac:
            response = await ac.post(
                URL, json=_body([_row()]), headers=_post_headers(intake_key)
            )
    assert response.status_code == 200
    pool.assert_called_once()


async def test_storage_failure_rolls_everything_back(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """If any step fails nothing changes: not the balances, not the history."""
    async with anon_client as ac:
        await ac.post(
            URL,
            json=_body([_row("pp:keep", value="9.00")]),
            headers=_post_headers(intake_key),
        )
        history_before = _history(tmp_db_path)
        with patch(
            "app.balances.snapshot_daily", side_effect=sqlite3.OperationalError("x")
        ):
            response = await ac.post(
                URL,
                json=_body([_row("pp:new", value="1.00")]),
                headers=_post_headers(intake_key),
            )
    assert response.status_code == 503
    assert response.json() == {"detail": "Balances could not be stored."}
    stored = _stored(tmp_db_path)
    assert set(stored) == {"pp:keep"}
    assert stored["pp:keep"][1] == 900
    # The failed delivery's same-day correction would have removed pp:keep
    # from today's history; the rollback must restore it.
    assert len(history_before) == 1
    assert _history(tmp_db_path) == history_before
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM refresh_log")]
    assert statuses == ["success", "error"]


# --------------------------------------------------------------------------- #
# Rejected bodies
# --------------------------------------------------------------------------- #

BAD_ROWS: list[tuple[str, dict[str, Any]]] = [
    ("float value", {"value": 1234.56}),
    ("int value", {"value": 1234}),
    ("null value", {"value": None}),
    ("exponent value", {"value": "1e3"}),
    ("text value", {"value": SENTINEL}),
    ("grouped value", {"value": "1,234.56"}),
    ("plus sign", {"value": "+5.00"}),
    ("too many places", {"value": "1.23456"}),
    ("nan", {"value": "NaN"}),
    ("spaces", {"value": " 5.00"}),
    ("huge value", {"value": "9" * 40 + ".00"}),
    ("thirteen integer digits", {"value": "1" + "0" * 12 + ".00"}),
    ("thirteen integer digits negative", {"value": "-" + "9" * 13}),
    ("lower case currency", {"currency": "usd"}),
    ("long currency", {"currency": "USDX"}),
    ("unknown category", {"category": SENTINEL}),
    ("lower case category", {"category": "cash"}),
    ("bad date", {"as_of": "2026-02-30"}),
    ("us date", {"as_of": "09/26/2026"}),
    ("timestamp date", {"as_of": "2026-09-26T00:00:00"}),
    ("non-ascii digit date", {"as_of": "\u0662\u0660\u0662\u0666-09-26"}),
    ("int date", {"as_of": 20260926}),
    ("null date", {"as_of": None}),
    ("null entity", {"entity_id": None}),
    ("bad entity", {"entity_id": SENTINEL}),
    ("short entity", {"entity_id": "1234"}),
    ("no prefix", {"account_id": SENTINEL}),
    ("unknown prefix", {"account_id": "other:" + SENTINEL}),
    ("empty id", {"account_id": "pp:"}),
    ("spaces in id", {"account_id": "pp:has space"}),
    ("empty name", {"account_name": ""}),
    ("long name", {"account_name": "n" * 500}),
    ("control character in name", {"account_name": "Example\x07 " + SENTINEL}),
    ("blank name", {"account_name": "   "}),
    ("c1 control in name", {"account_name": "Example\x85" + SENTINEL}),
    ("bidi override in name", {"account_name": "Example\u202e" + SENTINEL}),
    ("bidi isolate in name", {"account_name": "Example\u2066" + SENTINEL}),
    ("zero width space in name", {"account_name": "Example\u200b" + SENTINEL}),
    ("line separator in name", {"account_name": "Example\u2028" + SENTINEL}),
    ("misplaced uuid hyphens", {"entity_id": "1111111-12222-4333-8444-555555555555"}),
    ("trailing uuid hyphens", {"entity_id": "11111111222243338444555555555555----"}),
    ("bad source", {"source": "Bad Source " + SENTINEL}),
    ("extra field", {"balance_note": SENTINEL}),
    ("reconciled bad", {"reconciled_through": SENTINEL}),
    ("reconciled on non-xero row", {"reconciled_through": "2026-09-20"}),
    (
        "reconciled after as_of",
        {
            "account_id": "xero:b",
            "source": "xero_bank",
            "category": "Cash",
            "reconciled_through": "2026-12-31",
        },
    ),
]


@pytest.mark.parametrize(("label", "override"), BAD_ROWS, ids=[b[0] for b in BAD_ROWS])
async def test_bad_row_is_422_without_echo(
    anon_client: AsyncClient,
    intake_key: str,
    tmp_db_path: Path,
    label: str,
    override: dict[str, Any],
) -> None:
    """Each bad row gives 422 and nothing stored; error text echoes no input."""
    del label
    async with anon_client as ac:
        response = await ac.post(
            URL,
            json=_body([_row(**override)]),
            headers=_post_headers(intake_key),
        )
    assert response.status_code == 422
    text = response.text
    assert SENTINEL not in text
    assert "1234" not in text
    assert "9999" not in text
    assert isinstance(response.json()["detail"], str)
    assert _count(tmp_db_path, "account_balances") == 0
    assert _count(tmp_db_path, "balances_daily") == 0


async def test_error_names_the_field_not_the_value(
    anon_client: AsyncClient, intake_key: str
) -> None:
    """The collector can tell which row and field failed."""
    rows = [_row("pp:a"), _row("pp:b", value=12.5)]
    async with anon_client as ac:
        response = await ac.post(
            URL, json=_body(rows), headers=_post_headers(intake_key)
        )
    errors = response.json()["errors"]
    assert {"field": "items.1.value", "problem": "string_type"} in errors


async def test_float_in_raw_json_is_rejected(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """A float written the way a sloppy client would is still rejected."""
    raw = (
        '{"items": [{"account_id": "pp:a", "account_name": "Example", '
        f'"entity_id": "{ENTITY_A}", "category": "Cash", "source": "x", '
        '"value": 100.50, "currency": "USD", "as_of": "2026-09-26"}], "total": 1}'
    )
    async with anon_client as ac:
        response = await ac.post(
            URL,
            content=raw.encode(),
            headers={**_post_headers(intake_key), "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert "100.50" not in response.text
    assert _count(tmp_db_path, "account_balances") == 0


async def test_duplicate_account_id_in_one_delivery_is_422(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """The same account twice is ambiguous, so the whole delivery is refused."""
    rows = [_row("pp:dup"), _row("pp:dup", value="5.00")]
    async with anon_client as ac:
        response = await ac.post(
            URL, json=_body(rows), headers=_post_headers(intake_key)
        )
    assert response.status_code == 422
    assert "pp:dup" not in response.text
    assert _count(tmp_db_path, "account_balances") == 0


@pytest.mark.parametrize("total", [0, 2, -1, "1", 1.0, None])
async def test_total_must_match_the_row_count(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path, total: object
) -> None:
    """A mismatched, non-integer or missing total refuses the delivery."""
    body: dict[str, Any] = {"items": [_row()], "total": total}
    async with anon_client as ac:
        response = await ac.post(URL, json=body, headers=_post_headers(intake_key))
    assert response.status_code == 422
    assert _count(tmp_db_path, "account_balances") == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{not json",
        b"[]",
        b'"text"',
        b"null",
        b'{"items": "no", "total": 0}',
        b'{"total": 0}',
        b'{"items": [], "total": 0, "extra": 1}',
        b'{"items": [1], "total": 1}',
        b'{"items": [null], "total": 1}',
    ],
)
async def test_malformed_bodies_are_422(
    anon_client: AsyncClient, intake_key: str, raw: bytes
) -> None:
    """Broken JSON and wrong shapes are 422 with a plain detail."""
    async with anon_client as ac:
        response = await ac.post(
            URL,
            content=raw,
            headers={**_post_headers(intake_key), "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


async def test_too_many_rows_is_422(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """A delivery above the row limit is refused."""
    rows = [_row(f"pp:a{i}") for i in range(2001)]
    async with anon_client as ac:
        response = await ac.post(
            URL, json=_body(rows), headers=_post_headers(intake_key)
        )
    assert response.status_code == 422
    assert _count(tmp_db_path, "account_balances") == 0


async def test_oversized_body_is_413(anon_client: AsyncClient, intake_key: str) -> None:
    """A body above the size limit is refused before it is parsed."""
    big = b" " * (MAX_BODY_BYTES + 1)
    async with anon_client as ac:
        response = await ac.post(
            URL,
            content=big,
            headers={**_post_headers(intake_key), "Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large."}


async def test_oversized_body_without_content_length_is_413(
    anon_client: AsyncClient, intake_key: str
) -> None:
    """A streamed body that outgrows the limit is refused too."""
    async with anon_client as ac:
        response = await ac.post(
            URL,
            content=_oversized_stream(),
            headers={**_post_headers(intake_key), "Content-Type": "application/json"},
        )
    assert response.status_code == 413


def test_the_largest_valid_delivery_fits_the_body_limit() -> None:
    """Every delivery the model accepts is under ``MAX_BODY_BYTES``.

    Each of the most rows allowed uses every field at its longest, with a
    name of characters outside the Basic Multilingual Plane written as JSON
    escape pairs, and the body is pretty-printed.
    """
    from app.models import MAX_DELIVERY_ROWS, BalanceDelivery  # noqa: PLC0415

    rows = [
        _row(
            f"xero:{index:06d}" + "a" * 94,
            account_name="\U0001f600" * 200,
            category="Digital currency",
            source="a" * 40,
            value="-999999999999.9999",
            reconciled_through="2026-09-26",
        )
        for index in range(MAX_DELIVERY_ROWS)
    ]
    raw = json.dumps(_body(rows), ensure_ascii=True, indent=2).encode()
    BalanceDelivery.model_validate_json(raw)
    assert len(raw) <= MAX_BODY_BYTES


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


async def test_logs_never_carry_key_values_or_account_ids(
    anon_client: AsyncClient, intake_key: str
) -> None:
    """Accepted, refused and failed requests log counts and reasons only."""
    good = _body([_row("pp:secret-acct", value="777.77", account_name="Private")])
    bad = _body([_row("pp:secret-acct", value=12.5)])
    with capture_logs() as logs:
        async with anon_client as ac:
            await ac.post(URL, json=good, headers=_post_headers(intake_key))
            await ac.post(URL, json=bad, headers=_post_headers(intake_key))
            await ac.post(URL, json=good, headers={"X-API-Key": "wrong-key-value"})
            await ac.post(URL, json=good)
    assert logs
    dump = json.dumps(logs, default=str)
    for secret in (
        intake_key,
        "wrong-key-value",
        "secret-acct",
        "777.77",
        "77777",
        "Private",
        ENTITY_A,
    ):
        assert secret not in dump
    assert all(entry["log_level"] in {"info", "warning"} for entry in logs)


async def test_database_error_log_has_no_message_text(
    anon_client: AsyncClient, intake_key: str
) -> None:
    """A failed write logs the error class only, not the SQL error text."""
    with (
        capture_logs() as logs,
        patch(
            "app.balances.replace_balances",
            side_effect=sqlite3.OperationalError("detail with 777.77"),
        ),
    ):
        async with anon_client as ac:
            response = await ac.post(
                URL,
                json=_body([_row(value="777.77")]),
                headers=_post_headers(intake_key),
            )
    assert response.status_code == 503
    assert "777.77" not in json.dumps(logs, default=str)
    assert "777.77" not in response.text


MAX_VALUE = "9" * 12 + ".9999"


async def test_twelve_integer_digits_are_accepted(
    anon_client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """The largest accepted amount is stored as exact cents."""
    rows = [_row("pp:big", value=MAX_VALUE), _row("pp:small", value="-" + MAX_VALUE)]
    async with anon_client as ac:
        response = await ac.post(
            URL, json=_body(rows), headers=_post_headers(intake_key)
        )
    assert response.status_code == 200
    stored = _stored(tmp_db_path)
    assert stored["pp:big"][1] == 100_000_000_000_000
    assert stored["pp:small"][1] == -100_000_000_000_000


async def test_a_full_delivery_of_maximum_values_sums_and_renders(
    client: AsyncClient, intake_key: str, tmp_db_path: Path
) -> None:
    """2000 rows of the largest value stay inside SQLite's 64-bit sum."""
    rows = [_row(f"pp:m{n}", value=MAX_VALUE) for n in range(2000)]
    response = await client.post(
        URL, json=_body(rows), headers=_post_headers(intake_key)
    )
    assert response.status_code == 200
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        total = conn.execute("SELECT SUM(value_cents) FROM account_balances").fetchone()
        daily = conn.execute("SELECT SUM(value_cents) FROM balances_daily").fetchone()
    assert total[0] == daily[0] == 2000 * 100_000_000_000_000
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/finances")).status_code == 200


def _intake_postman_item() -> dict[str, Any]:
    """Return the committed Postman request for the intake route."""
    root = Path(__file__).resolve().parents[2]
    collection = json.loads(
        (root / "docs" / "api" / "postman-collection.json").read_text("utf-8")
    )
    pending: list[dict[str, Any]] = list(collection["item"])
    while pending:
        item = pending.pop()
        pending.extend(item.get("item", []))
        request = item.get("request", {})
        if request.get("method") == "POST" and "balances" in request["url"]["path"]:
            return item
    msg = "no POST balances request in the Postman collection"
    raise AssertionError(msg)


def test_schema_example_is_a_valid_delivery() -> None:
    """The OpenAPI request example passes the same validation as a delivery."""
    examples = BalanceDelivery.model_json_schema()["examples"]
    assert len(examples) == 1
    delivery = BalanceDelivery.model_validate(examples[0])
    assert delivery.total == len(delivery.items) == 1


def test_postman_intake_request_sends_a_valid_body_and_expects_success() -> None:
    """With a key the contract test sends a valid delivery and accepts only 200."""
    item = _intake_postman_item()
    body = json.loads(item["request"]["body"]["raw"])
    BalanceDelivery.model_validate(body)
    script = "\n".join(item["event"][0]["script"]["exec"])
    assert "keyed ? [200] : [401, 404]" in script
