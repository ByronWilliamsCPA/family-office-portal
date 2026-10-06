# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC002, TC003
"""Page tests for the balance totals on Home and Finances, and the admin view.

Everything is made up. The checks are about what a person sees: totals by
category and by person or household, a 90-day trend that also reads without
JavaScript, per-source dates, and no raw identifiers.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from freezegun import freeze_time
from httpx import AsyncClient

PERSON = "11111111-2222-4333-8444-555555555555"
HOUSEHOLD = "66666666-7777-4888-8999-aaaaaaaaaaaa"
LLC = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
NOT_CACHED = "99999999-9999-4999-8999-999999999999"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
CHART_TAG = '<script src="/static/chart.umd.min.js" defer></script>'
NOT_INCLUDED = "The home, vehicles, and loans are not included yet."
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A public file hash, not a secret: it pins the vendored Chart.js build.
CHART_SHA256 = (
    "48444a82d4edcb5bec0f1965faacdde1"  # pragma: allowlist secret
    "8d9c17db3063d042abada2f705c9f54a"  # pragma: allowlist secret
)


def _headers_at_frozen_now(
    jwt_factory: Callable[..., str], username: str, group: str
) -> dict[str, str]:
    """Mint a token valid at the frozen test time, not at the wall clock."""
    stamp = int(NOW.timestamp())
    token = jwt_factory(
        username=username,
        claims={"groups": [group], "iat": stamp - 30, "exp": stamp + 315_360_000},
    )
    return {"X-authentik-jwt": token}


@pytest.fixture
def viewer_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Return Viewer headers whose token is valid at the frozen time."""
    return _headers_at_frozen_now(jwt_factory, "viewer", "fo-viewer")


@pytest.fixture
def admin_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Return Admin headers whose token is valid at the frozen time."""
    return _headers_at_frozen_now(jwt_factory, "admin", "fo-admin")


def _seed(path: Path, *, fetched_at: str | None = None) -> None:
    fetched = fetched_at or NOW.isoformat()
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, ?, ?)",
            [
                (PERSON, "Example Person", "individual", fetched),
                (HOUSEHOLD, "Example Household", "household", fetched),
                (LLC, "Example Holdings LLC", "llc", fetched),
            ],
        )
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency, "
            "reconciled_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "pp:acct-111",
                    "Example Brokerage",
                    PERSON,
                    "Investments",
                    "broker_report",
                    125_000_050,
                    "2026-09-26",
                    fetched,
                    "USD",
                    None,
                ),
                (
                    "pp:acct-222",
                    "Example Retirement",
                    PERSON,
                    "Retirement",
                    "manual_mark",
                    40_000_000,
                    "2026-08-31",
                    fetched,
                    "USD",
                    None,
                ),
                (
                    "xero:acct-333",
                    "Example Operating",
                    LLC,
                    "Cash",
                    "xero_bank",
                    2_500_000,
                    "2026-09-25",
                    fetched,
                    "USD",
                    "2026-09-20",
                ),
                (
                    "xero:acct-444",
                    "Example Savings",
                    HOUSEHOLD,
                    "Cash",
                    "xero_bank",
                    100_000,
                    "2026-09-25",
                    fetched,
                    "USD",
                    None,
                ),
                (
                    "crypto:acct-555",
                    "Example Wallet",
                    NOT_CACHED,
                    "Digital currency",
                    "crypto_tracker",
                    700_000,
                    "2026-09-27",
                    fetched,
                    "USD",
                    None,
                ),
                (
                    "pp:acct-666",
                    "Example Euro Account",
                    PERSON,
                    "Cash",
                    "broker_report",
                    999_999_999,
                    "2026-09-26",
                    fetched,
                    "EUR",
                    None,
                ),
            ],
        )
        for days_ago, cents in ((2, 160_000_000), (1, 168_000_000), (0, 168_300_050)):
            day = (NOW - timedelta(days=days_ago)).date().isoformat()
            conn.execute(
                "INSERT INTO balances_daily (date, account_id, entity_id, category, "
                "value_cents, as_of, currency) VALUES (?, 'pp:acct-111', ?, "
                "'Investments', ?, ?, 'USD')",
                (day, PERSON, cents, day),
            )


def _trend_section(page: str) -> str:
    """Return the HTML of the trend section only."""
    start = page.index('id="trend-heading"')
    return page[start : page.index("</section>", start)]


async def _get(client: AsyncClient, path: str, headers: dict[str, str]) -> str:
    # The in-process transport holds no connection, so one client can be
    # used for several requests without entering its context.
    response = await client.get(path, headers=headers)
    assert response.status_code == 200
    return response.text


@pytest.mark.parametrize("path", ["/", "/finances"])
async def test_page_shows_total_categories_people_and_trend(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    path: str,
) -> None:
    """Both pages show the USD total, group totals and a trend table."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, path, viewer_headers)
    assert "$1,683,000" in html  # USD total only, no cents
    assert "Where the money is held" in html
    for label in ("Investments", "Retirement", "Cash", "Digital currency"):
        assert label in html
    assert "$1,250,000" in html
    assert "By person and household" in html
    assert "Example Person" in html
    assert "Example Household" in html
    assert "By company and trust" in html
    assert "Example Holdings LLC" in html
    assert "Unknown entity" in html
    assert "1 account in other currencies not included." in html
    assert "<table" in html
    assert "Total by day" in html
    assert "September 27, 2026" in html


@pytest.mark.parametrize("path", ["/", "/finances"])
async def test_page_shows_no_raw_identifiers(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    path: str,
) -> None:
    """No account id, provider prefix or entity id is visible to a person."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, path, viewer_headers)
    assert not UUID_RE.search(html)
    for hidden in ("acct-111", "pp:", "xero:", "crypto:", NOT_CACHED):
        assert hidden not in html


async def test_chart_is_loaded_locally_only_where_it_is_drawn(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Pages with a trend load the vendored script; other pages do not."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        home = await _get(client, "/", viewer_headers)
        finances = await _get(client, "/finances", viewer_headers)
        entities = await _get(client, "/entities", viewer_headers)
        documents = await _get(client, "/documents", viewer_headers)
    for html in (home, finances):
        assert CHART_TAG in html
        assert "<canvas" in html
        assert "data-points=" in html
        assert '<script src="/static/balance_trend.js" defer></script>' in html
    for html in (entities, documents):
        assert "chart.umd" not in html
    assert "cdn" not in home.lower()
    assert "http://" not in home
    assert "https://" not in home


async def test_chart_data_is_oldest_first_exact_decimal_strings(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The chart gets every day oldest first, totals as exact strings."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    match = re.search(r"data-points='([^']*)'", html)
    assert match is not None
    points = json.loads(html_lib.unescape(match.group(1)))
    assert points == [
        {"date": "2026-09-25", "total": "1600000.00"},
        {"date": "2026-09-26", "total": "1680000.00"},
        {"date": "2026-09-27", "total": "1683000.50"},
    ]


async def test_trend_table_lists_the_newest_day_first(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The no-JavaScript table reads newest first, unlike the chart."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, "/", viewer_headers)
    section = _trend_section(html)
    assert "The last 90 days" in html
    days = re.findall(r'<th scope="row"[^>]*>([^<]+)</th>', section)
    assert days == ["September 27, 2026", "September 26, 2026", "September 25, 2026"]


async def test_trend_with_too_few_points_has_no_chart(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """One day of history is a sentence and a table row, not a chart."""
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO balances_daily (date, account_id, entity_id, category, "
            "value_cents, as_of, currency) VALUES ('2026-09-27', 'pp:a', NULL, "
            "'Cash', 500000, '2026-09-27', 'USD')"
        )
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert "chart.umd" not in html
    assert "<canvas" not in html
    assert "$5,000" in html
    assert "Total by day" in html


async def test_trend_table_is_readable_without_javascript(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The trend is also a real table, and the canvas stays hidden until script runs."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert re.search(r"<canvas[^>]*aria-hidden=\"true\"", html)
    assert re.search(r"<div[^>]*data-trend-chart[^>]*\bhidden\b", html)
    assert "<caption" in html
    assert 'scope="col"' in html
    assert "$1,600,000" in html
    assert "$1,680,000" in html


async def test_finances_shows_not_included_sentence_and_sources(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The exact not-included sentence stays, and each source shows its dates."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert NOT_INCLUDED in html
    assert "Where these numbers come from" in html
    assert "Brokerage report" in html
    assert "Manual entry from a statement" in html
    assert "Bank accounts" in html
    assert "Digital currency tracker" in html
    assert "August 31, 2026" in html
    assert "Oldest value is from August 31, 2026." in html


async def test_finances_shows_reconciled_through_for_bank_accounts(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A bank account with a reconciled-through date shows it next to the name."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert "Example Operating" in html
    assert "Matched to the bank through September 20, 2026" in html
    assert html.count("Matched to the bank through") == 1
    assert "Example Savings" in html


async def test_home_page_keeps_total_when_no_history_or_entities(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """With no balances the page says so; with only balances it still renders."""
    with freeze_time(NOW):
        empty = await _get(client, "/", viewer_headers)
    assert "Daily totals will appear here once account balances are connected." in empty
    assert "Where the money is held" not in empty
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at) VALUES "
            "('pp:z', 'Example', NULL, 'Cash', 'm', 100, '2026-09-27', ?)",
            (NOW.isoformat(),),
        )
    with freeze_time(NOW):
        html = await _get(client, "/", viewer_headers)
    assert "Unknown entity" in html
    # With no daily history the trend section is left out entirely; the
    # total above already says what there is.
    assert 'id="trend-heading"' not in html
    assert "chart.umd" not in html


async def test_owners_not_on_file_get_their_own_heading(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Money with no cached owner is never labelled as company or trust money."""
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at) VALUES "
            "('pp:z', 'Example', NULL, 'Cash', 'm', 100, '2026-09-27', ?)",
            (NOW.isoformat(),),
        )
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert "Owner not on file" in html
    assert "Unknown entity" in html
    assert "By company and trust" not in html
    assert "By person and household" not in html


async def test_released_entity_types_are_listed_as_companies_and_trusts(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Types the entities backend sends today all land under company and trust."""
    released = ("llc", "trust", "s_corporation", "partnership", "other")
    with sqlite3.connect(tmp_db_path) as conn:
        for i, kind in enumerate(released):
            entity_id = f"0000000{i}-0000-4000-8000-000000000000"
            conn.execute(
                "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, ?, ?)",
                (entity_id, f"Example Owner {kind}", kind, NOW.isoformat()),
            )
            conn.execute(
                "INSERT INTO account_balances (account_id, account_name, "
                "entity_id, category, source, value_cents, as_of, fetched_at) "
                "VALUES (?, 'Example', ?, 'Cash', 'm', 100, '2026-09-27', ?)",
                (f"pp:{kind}", entity_id, NOW.isoformat()),
            )
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert "By company and trust" in html
    assert "By person and household" not in html
    for kind in released:
        assert f"Example Owner {kind}" in html


async def test_intake_supplied_names_are_escaped(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Account and owner names are text, never markup."""
    hostile = "<script>alert(\"x\")</script> & 'q'"
    owner = "14141414-2525-4636-8747-858585858585"
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, 'llc', ?)",
            (owner, hostile, NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at) VALUES "
            "('xero:z', ?, ?, 'Cash', 'xero_bank', 100, '2026-09-27', ?)",
            (hostile, owner, NOW.isoformat()),
        )
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt; &amp; &#39;q&#39;" in html


async def test_stale_provider_is_labelled_out_of_date(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """One provider that stopped reporting shows the existing stale label."""
    _seed(tmp_db_path)
    old = (NOW - timedelta(hours=72)).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "UPDATE account_balances SET fetched_at = ? WHERE account_id LIKE 'xero:%'",
            (old,),
        )
    with freeze_time(NOW):
        html = await _get(client, "/finances", viewer_headers)
    assert "This may be out of date." in html
    assert "$1,683,000" in html


# --------------------------------------------------------------------------- #
# Admin view
# --------------------------------------------------------------------------- #


async def test_manual_marks_page_is_admin_only(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """A Viewer gets the existing access-denied answer."""
    async with client as ac:
        response = await ac.get("/admin/manual-marks", headers=viewer_headers)
    assert response.status_code == 403


async def test_manual_marks_page_lists_oldest_first_with_ages(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The admin sees each manually marked account and how old its mark is."""
    _seed(tmp_db_path)
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at) VALUES "
            "('pp:acct-777', 'Example Zeta Alternatives', ?, 'Alternatives', "
            "'manual_mark', 100, '2026-06-30', ?)",
            (HOUSEHOLD, NOW.isoformat()),
        )
    with freeze_time(NOW):
        html = await _get(client, "/admin/manual-marks", admin_headers)
    # Oldest first; alphabetical order would put Retirement first.
    assert html.index("Example Zeta Alternatives") < html.index("Example Retirement")
    assert "89 days old" in html
    assert "27 days old" in html
    assert "June 30, 2026" in html
    assert "Example Household" in html
    assert "Example Brokerage" not in html
    assert not UUID_RE.search(html)
    assert "acct-" not in html


async def test_manual_marks_page_flags_a_future_date(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A mark dated after today asks to be checked instead of looking newest."""
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at) VALUES "
            "('pp:f', 'Example Future', NULL, 'Alternatives', 'manual_mark', 1, "
            "'2026-10-15', ?)",
            (NOW.isoformat(),),
        )
    with freeze_time(NOW):
        html = await _get(client, "/admin/manual-marks", admin_headers)
    assert "Dated after today; please check" in html
    assert "0 days old" not in html


async def test_manual_marks_page_empty_state(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    """With no manual marks the page says so in plain words."""
    html = await _get(client, "/admin/manual-marks", admin_headers)
    assert "No accounts are valued by hand right now." in html


async def test_admin_sees_a_link_to_manual_marks_on_finances(
    client: AsyncClient,
    admin_headers: dict[str, str],
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Only an admin is shown the link."""
    _seed(tmp_db_path)
    with freeze_time(NOW):
        admin_html = await _get(client, "/finances", admin_headers)
        viewer_html = await _get(client, "/finances", viewer_headers)
    assert 'href="/admin/manual-marks"' in admin_html
    assert "/admin/manual-marks" not in viewer_html


async def test_refresh_status_knows_the_balances_service(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The delivery shows in the refresh log with a staleness flag."""
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO refresh_log (service, status, message, rows, ran_at) "
            "VALUES ('balances', 'success', NULL, 3, ?)",
            (NOW.isoformat(),),
        )
    async with client as ac:
        response = await ac.get("/admin/refresh-status", headers=admin_headers)
    entry = next(e for e in response.json()["entries"] if e["service"] == "balances")
    assert entry["is_stale"] is True  # no balances cached yet


async def test_refresh_status_balances_follow_the_oldest_provider(
    client: AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Fresh balances are not stale; one provider behind makes them stale."""
    _seed(tmp_db_path)
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO refresh_log (service, status, message, rows, ran_at) "
            "VALUES ('balances', 'success', NULL, 3, ?)",
            (NOW.isoformat(),),
        )

    async def balances_stale() -> bool:
        with freeze_time(NOW):
            response = await client.get("/admin/refresh-status", headers=admin_headers)
        entries = response.json()["entries"]
        return next(e for e in entries if e["service"] == "balances")["is_stale"]

    assert await balances_stale() is False
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "UPDATE account_balances SET fetched_at = ? WHERE account_id LIKE 'xero:%'",
            ((NOW - timedelta(hours=72)).isoformat(),),
        )
    assert await balances_stale() is True


def test_source_label_known_and_unknown_feeds() -> None:
    """Known feeds get their label; others read as words, first letter upper."""
    from app.routes._context import source_label  # noqa: PLC0415

    assert source_label("manual_mark") == "Manual entry from a statement"
    assert source_label("custom_feed_x") == "Custom feed x"
    assert source_label("_") == ""


# --------------------------------------------------------------------------- #
# Vendored chart library
# --------------------------------------------------------------------------- #


async def test_chart_library_is_served_locally_and_pinned(
    anon_client: AsyncClient,
) -> None:
    """The vendored Chart.js is served from static and matches its pinned hash."""
    async with anon_client as ac:
        response = await ac.get("/static/chart.umd.min.js")
    assert response.status_code == 200
    assert response.text.startswith("/*!\n * Chart.js v4.5.1\n")
    assert hashlib.sha256(response.content).hexdigest() == CHART_SHA256


async def test_chart_enhancement_script_is_served(anon_client: AsyncClient) -> None:
    """The small script that draws the trend is a local static file."""
    async with anon_client as ac:
        response = await ac.get("/static/balance_trend.js")
    assert response.status_code == 200
    assert "Chart" in response.text
    assert "http" not in response.text
