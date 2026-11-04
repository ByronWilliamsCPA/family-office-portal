# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Every page survives a stale cache and a failing backend.

Project rule (``CLAUDE.md`` "Testing requirements", Resilience): when a
backend fails, the affected section shows the last cached values with a "last
updated" label, never a blank section or an error page. The refresh jobs are
covered in ``tests/unit/test_scheduler.py``; this module proves the pages.

Three situations, each against every page that reads the cache:

* Stale cache: every dataset is older than its staleness threshold. The page
  answers 200, shows the cached values, the last-updated label and the
  "may be out of date" notice, and no error text.
* Failing backend: a refresh job runs against a mocked backend that answers
  500 or times out. The cached rows are unchanged and the page still shows
  them under the stale label.
* Empty cache, backend connected: the page answers 200 with a plain-English
  empty state and the freshness label.

Time is frozen at ``NOW`` and fixture tokens are minted valid at that
instant, so no test depends on the wall clock. Nothing touches the network or
sleeps.

#ASSUME: external resources: pp-security-master (alpha) and the other
backends answer 5xx or time out from time to time, and a refresh job treats
both the same way: it leaves the cached rows alone.
#VERIFY: ``tests/unit/test_scheduler.py`` covers the jobs; the failing-backend
tests below run each job against a 500 and a timeout, then read the page.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pytest
from freezegun import freeze_time

from app import scheduler

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

# Fetch times older than each threshold in app.cache.STALENESS_HOURS
# (entities 8h, holdings and positions 4h, documents and balances 24h).
ENTITIES_FETCHED = NOW - timedelta(hours=10)
HOLDINGS_FETCHED = NOW - timedelta(hours=6)
POSITIONS_FETCHED = NOW - timedelta(hours=6)
DOCUMENTS_FETCHED = NOW - timedelta(hours=30)
BALANCES_FETCHED = NOW - timedelta(hours=30)
FRESH = NOW - timedelta(hours=1)

# The label each stale dataset produces (the display zone is UTC in tests).
ENTITIES_LABEL = "Last updated Sep 27, 2026, 2:00 AM. This may be out of date."
HOLDINGS_LABEL = "Last updated Sep 27, 2026, 6:00 AM. This may be out of date."
POSITIONS_LABEL = HOLDINGS_LABEL
DOCUMENTS_LABEL = "Last updated Sep 26, 2026, 6:00 AM. This may be out of date."
BALANCES_LABEL = DOCUMENTS_LABEL
FRESH_LABEL = "Last updated Sep 27, 2026, 11:00 AM"
NOT_UPDATED = "Not updated yet"
OUT_OF_DATE = "This may be out of date."

LLC_ID = "11111111-2222-4333-8444-555555555555"
TRUST_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"

# Text that only appears when a page leaks an exception or a raw value.
ERROR_MARKERS = (
    "Traceback",
    "Internal Server Error",
    "Exception",
    "jinja2",
    "OperationalError",
    "HTTPStatusError",
    "ReadTimeout",
    "ConnectError",
    "UndefinedError",
    "This is not available right now",
)
RAW_NONE = re.compile(r"\bNone\b")


def _headers_at_frozen_now(
    jwt_factory: Callable[..., str], username: str, group: str
) -> dict[str, str]:
    """Mint a token valid at the frozen test time, not at the wall clock."""
    stamp = int(NOW.timestamp())
    token = jwt_factory(
        username=username,
        claims={"groups": [group], "iat": stamp - 30, "exp": stamp + 3600},
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


# --------------------------------------------------------------------------- #
# Fixture data (made up; round numbers)
# --------------------------------------------------------------------------- #


DEFAULT_FETCHED: dict[str, datetime] = {
    "entities": ENTITIES_FETCHED,
    "holdings": HOLDINGS_FETCHED,
    "positions": POSITIONS_FETCHED,
    "documents": DOCUMENTS_FETCHED,
    "balances": BALANCES_FETCHED,
}


def _seed(path: Path, fetched: dict[str, datetime] | None = None) -> None:
    """Fill every cached dataset, each with its own fetch time.

    Args:
        path: Database file created by ``portal_env``.
        fetched: Fetch times that replace the stale defaults, by dataset.

    Balances total $1,750,000: Investments $1,000,000, Cash $250,000 and
    Alternatives $500,000 (a hand-valued account).
    """
    times = {**DEFAULT_FETCHED, **(fetched or {})}
    entities_at = times["entities"].isoformat()
    holdings_at = times["holdings"].isoformat()
    positions_at = times["positions"].isoformat()
    documents_at = times["documents"].isoformat()
    balances_at = times["balances"].isoformat()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO entities (id, name, type, state, status, next_date, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    LLC_ID,
                    "Example Holdings LLC",
                    "llc",
                    "WY",
                    "active",
                    "2026-12-01",
                    entities_at,
                ),
                (
                    TRUST_ID,
                    "Sample Family Trust",
                    "trust",
                    "DE",
                    "active",
                    "2027-01-15",
                    entities_at,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("h-1", "Example Index Fund", "Equities", 1_250_000, 70, holdings_at),
                ("h-2", "Sample Bond Fund", "Fixed income", 500_000, 30, holdings_at),
            ],
        )
        conn.executemany(
            "INSERT INTO performance (date, total_value, benchmark, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("2026-09-25", 1_700_000, 1_650_000, holdings_at),
                ("2026-09-26", 1_750_000, 1_700_000, holdings_at),
            ],
        )
        conn.executemany(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("p-1", "Example Coin", 2, 200_000, positions_at),
                ("p-2", "Sample Token", 100, 50_000, positions_at),
            ],
        )
        conn.executemany(
            "INSERT INTO documents (id, name, category, entity_id, document_type, "
            "document_date, is_confidential, added_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "d-1",
                    "Example Holdings LLC Operating Agreement",
                    "Legal",
                    LLC_ID,
                    "agreement",
                    "2025-03-01",
                    0,
                    "2026-09-01",
                    documents_at,
                ),
                (
                    "d-2",
                    "Sample Family Trust Summary",
                    "Tax",
                    TRUST_ID,
                    "summary",
                    "2025-04-15",
                    0,
                    "2026-09-10",
                    documents_at,
                ),
                (
                    "d-3",
                    "Private Sample Memo",
                    "Legal",
                    LLC_ID,
                    "memo",
                    "2025-05-01",
                    1,
                    "2026-09-20",
                    documents_at,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency, "
            "reconciled_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "pp:acct-1",
                    "Example Brokerage",
                    LLC_ID,
                    "Investments",
                    "broker_report",
                    100_000_000,
                    "2026-09-20",
                    balances_at,
                    "USD",
                    None,
                ),
                (
                    "xero:acct-2",
                    "Example Checking",
                    TRUST_ID,
                    "Cash",
                    "xero_bank",
                    25_000_000,
                    "2026-09-26",
                    balances_at,
                    "USD",
                    "2026-09-25",
                ),
                (
                    "pp:acct-3",
                    "Example Land Partnership",
                    TRUST_ID,
                    "Alternatives",
                    "manual_mark",
                    50_000_000,
                    "2026-08-30",
                    balances_at,
                    "USD",
                    None,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO balances_daily (date, account_id, entity_id, category, "
            "value_cents, as_of, currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "2026-09-25",
                    "pp:acct-1",
                    LLC_ID,
                    "Investments",
                    99_000_000,
                    "2026-09-20",
                    "USD",
                ),
                (
                    "2026-09-26",
                    "pp:acct-1",
                    LLC_ID,
                    "Investments",
                    100_000_000,
                    "2026-09-20",
                    "USD",
                ),
            ],
        )


def _rows(path: Path, table: str) -> list[tuple[object, ...]]:
    """Return every row of a table, in a stable order, for before/after checks."""
    # ``table`` is always a literal from this module, never input.
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(  # nosec B608
            f"SELECT * FROM {table} ORDER BY 1"
        ).fetchall()


def _assert_no_leaks(html: str) -> None:
    """Assert the page shows no exception text and no raw ``None``."""
    for marker in ERROR_MARKERS:
        assert marker not in html, marker
    assert not RAW_NONE.search(html), "raw None leaked into the page"


async def _get(
    client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    *,
    status: int = 200,
) -> str:
    """Request a page at the frozen time, check status and leaks, return the body."""
    with freeze_time(NOW):
        response = await client.get(path, headers=headers)
    assert response.status_code == status, path
    assert response.headers["content-type"].startswith("text/html"), path
    _assert_no_leaks(response.text)
    return response.text


# --------------------------------------------------------------------------- #
# 1. Stale cache: values, label and notice are all present
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Page:
    """One page and what it must show.

    Attributes:
        path (str): URL requested.
        values (tuple[str, ...]): Cached text that must be on the page.
        labels (tuple[str, ...]): Freshness lines that must be on the page.
    """

    path: str
    values: tuple[str, ...]
    labels: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Return a readable pytest id."""
        return self.path


# A partial has no freshness line, so it lists none.
ENTITY_VALUES = ("Example Holdings LLC", "Sample Family Trust", "December 1, 2026")
DOCUMENT_VALUES = (
    "Example Holdings LLC Operating Agreement",
    "Sample Family Trust Summary",
)
FINANCE_VALUES = (
    "$1,750,000",
    "Where the money is held",
    "Example Checking",
    "$250,000",
    "Example Coin",
    "$200,000",
    "Sample Token",
)
STALE_PAGES = [
    Page(
        "/",
        (
            "$1,750,000",
            "Where the money is held",
            "Example Holdings LLC",
            "December 1, 2026",
            "Example Holdings LLC Operating Agreement",
        ),
        (BALANCES_LABEL,),
    ),
    Page("/documents", (*DOCUMENT_VALUES, "Legal", "Tax"), (DOCUMENTS_LABEL,)),
    Page(
        "/documents?view=entity",
        (*DOCUMENT_VALUES, "Example Holdings LLC", "Sample Family Trust"),
        (DOCUMENTS_LABEL,),
    ),
    Page(
        "/documents?q=Operating",
        ("Example Holdings LLC Operating Agreement",),
        (DOCUMENTS_LABEL,),
    ),
    Page("/documents/folders", (*DOCUMENT_VALUES, "By category")),
    Page("/documents/folders?view=entity", (*DOCUMENT_VALUES, "By entity")),
    Page("/documents/search?q=Trust", ("Sample Family Trust Summary",)),
    Page("/finances", FINANCE_VALUES, (BALANCES_LABEL, POSITIONS_LABEL)),
    Page(
        "/portfolio",
        ("Example Index Fund", "Sample Bond Fund", "$1,250,000", "Equities"),
        (HOLDINGS_LABEL,),
    ),
    Page("/entities", ENTITY_VALUES, (ENTITIES_LABEL,)),
    Page(
        f"/entities/{LLC_ID}",
        ("Example Holdings LLC", "WY", "Operating Agreement"),
    ),
]


@pytest.mark.parametrize("page", STALE_PAGES, ids=lambda p: p.id)
async def test_stale_cache_page_shows_values_and_label(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    page: Page,
) -> None:
    """A page over a stale cache shows the cached rows and says they are old."""
    _seed(tmp_db_path)
    html = await _get(client, page.path, viewer_headers)
    for value in page.values:
        assert value in html, value
    for label in page.labels:
        assert label in html, label
    assert html.count(OUT_OF_DATE) == len(page.labels)


@pytest.mark.parametrize("page", STALE_PAGES, ids=lambda p: p.id)
async def test_stale_cache_page_hides_confidential_and_raw_ids(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    page: Page,
) -> None:
    """Staleness does not loosen the Viewer rules: no private memo, no raw ids."""
    _seed(tmp_db_path)
    html = await _get(client, page.path, viewer_headers)
    assert "Private Sample Memo" not in html
    if page.path.startswith("/entities/"):
        return
    # List and home views link by id but never print one as text.
    visible = re.sub(r"(href|data-[a-z-]+)=\"[^\"]*\"", "", html)
    assert TRUST_ID not in visible
    assert LLC_ID not in visible


async def test_stale_manual_marks_page_shows_values(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """The Admin hand-valued accounts page still lists its rows over a stale cache."""
    _seed(tmp_db_path)
    html = await _get(client, "/admin/manual-marks", admin_headers)
    assert "Example Land Partnership" in html
    assert "Sample Family Trust" in html
    assert "28 days old" in html


async def test_fresh_cache_shows_no_out_of_date_notice(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Control: recent data gets the plain label and never the notice.

    Without this the stale assertions above could pass for any page that
    always printed the notice.
    """
    _seed(tmp_db_path, dict.fromkeys(DEFAULT_FETCHED, FRESH))
    for path in ("/", "/documents", "/finances", "/portfolio", "/entities"):
        html = await _get(client, path, viewer_headers)
        assert FRESH_LABEL in html, path
        assert OUT_OF_DATE not in html, path


async def test_finances_flags_only_the_stale_section(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """With fresh balances and stale positions, only digital currency is flagged."""
    _seed(tmp_db_path, {"balances": FRESH})
    html = await _get(client, "/finances", viewer_headers)
    assert html.count(OUT_OF_DATE) == 1
    assert POSITIONS_LABEL in html
    assert FRESH_LABEL in html
    assert "Example Coin" in html


async def test_stale_balances_flag_the_oldest_provider(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """One provider that stopped reporting makes the totals look old, not blank."""
    _seed(tmp_db_path, {"balances": FRESH})
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "UPDATE account_balances SET fetched_at = ? WHERE account_id = ?",
            (BALANCES_FETCHED.isoformat(), "xero:acct-2"),
        )
    for path in ("/", "/finances"):
        html = await _get(client, path, viewer_headers)
        assert BALANCES_LABEL in html, path
        assert "$1,750,000" in html, path


# --------------------------------------------------------------------------- #
# 2. Failing backend: the job fails, the cache and the page stay as they were
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JobCase:
    """One refresh job and the page it feeds.

    Attributes:
        name (str): Job function name in ``app.scheduler``.
        service (str): Service name the job records in ``refresh_log``.
        tables (tuple[str, ...]): Cache tables the job replaces.
        page (Page): The page that shows the data, with its label.
        also (tuple[Page, ...]): Other pages that read the same tables.
    """

    name: str
    service: str
    tables: tuple[str, ...]
    page: Page
    also: tuple[Page, ...] = ()

    @property
    def id(self) -> str:
        """Return a readable pytest id."""
        return self.name


JOB_CASES = [
    JobCase(
        "refresh_entities",
        "llc-manager",
        ("entities",),
        Page("/entities", ENTITY_VALUES, (ENTITIES_LABEL,)),
        (
            Page(
                "/",
                ("Example Holdings LLC", "December 1, 2026"),
                (BALANCES_LABEL,),
            ),
            Page(f"/entities/{LLC_ID}", ("Example Holdings LLC", "WY")),
        ),
    ),
    JobCase(
        "refresh_holdings",
        "pp-security-master",
        ("holdings", "performance"),
        Page(
            "/portfolio",
            ("Example Index Fund", "Sample Bond Fund", "$1,250,000"),
            (HOLDINGS_LABEL,),
        ),
    ),
    JobCase(
        "refresh_positions",
        "xero_crypto",
        ("positions",),
        Page("/finances", FINANCE_VALUES, (BALANCES_LABEL, POSITIONS_LABEL)),
    ),
    JobCase(
        "refresh_documents",
        "llc-manager-documents",
        ("documents",),
        Page("/documents", DOCUMENT_VALUES, (DOCUMENTS_LABEL,)),
        (
            Page("/documents/folders", DOCUMENT_VALUES),
            Page(f"/entities/{LLC_ID}", ("Operating Agreement",)),
            Page(
                "/",
                ("Example Holdings LLC Operating Agreement",),
                (BALANCES_LABEL,),
            ),
        ),
    ),
]


@pytest.fixture(params=["http_500", "timeout"])
def failing_backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> list[httpx.Request]:
    """Make every ``httpx.Client`` a scheduler job opens fail like a bad backend.

    Real ``httpx`` objects run (status handling, ``raise_for_status``, error
    classes); only the transport is replaced, so nothing leaves the process.

    Args:
        request: Pytest request; ``param`` is ``http_500`` (a 500 answer) or
            ``timeout`` (a read timeout).
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        list[httpx.Request]: Requests the backend received, to prove the mock
        was actually used.
    """
    mode = str(request.param)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if mode == "timeout":
            msg = "timed out"
            raise httpx.ReadTimeout(msg, request=request)
        return httpx.Response(500, json={"detail": "upstream failure"})

    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(httpx, "Client", factory)
    return calls


@pytest.mark.parametrize("case", JOB_CASES, ids=lambda c: c.id)
async def test_failed_refresh_keeps_cache_and_page_shows_stale_data(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    failing_backend: list[httpx.Request],
    case: JobCase,
) -> None:
    """A job that gets a 500 or a timeout leaves its rows and its page intact."""
    _seed(tmp_db_path)
    before = {table: _rows(tmp_db_path, table) for table in case.tables}
    assert all(before.values())

    getattr(scheduler, case.name)()

    assert failing_backend, "the mocked backend was never contacted"
    for table, rows in before.items():
        assert _rows(tmp_db_path, table) == rows, table
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        log = conn.execute(
            "SELECT status FROM refresh_log WHERE service = ?", (case.service,)
        ).fetchall()
    assert log == [("error",)]

    for page in (case.page, *case.also):
        html = await _get(client, page.path, viewer_headers)
        for value in page.values:
            assert value in html, (page.path, value)
        for label in page.labels:
            assert label in html, (page.path, label)
        assert html.count(OUT_OF_DATE) == len(page.labels), page.path


# --------------------------------------------------------------------------- #
# 3. Empty cache, backend connected: plain-English empty states
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EmptyPage:
    """One page over an empty cache.

    Attributes:
        path (str): URL requested.
        texts (tuple[str, ...]): Plain-English sentences that must appear.
        not_updated (int): How many "Not updated yet" labels the page shows.
    """

    path: str
    texts: tuple[str, ...]
    not_updated: int

    @property
    def id(self) -> str:
        """Return a readable pytest id."""
        return self.path


# The Portfolio sentence is deliberately not asserted: only status, no error
# text and the label.
EMPTY_PAGES = [
    EmptyPage(
        "/",
        (
            "Daily totals will appear here once account balances are connected.",
            "Nothing is due soon.",
            "No documents yet.",
        ),
        1,
    ),
    EmptyPage("/documents", ("No documents have been added yet.",), 1),
    EmptyPage("/documents?view=entity", ("No documents have been added yet.",), 1),
    EmptyPage("/documents?q=agreement", ('No documents match "agreement".',), 1),
    EmptyPage("/documents/folders", ("No documents have been added yet.",), 0),
    EmptyPage("/documents/search?q=agreement", ('No documents match "agreement".',), 0),
    EmptyPage(
        "/finances",
        (
            "Account totals will appear here once balances are connected.",
            "No digital currency to show.",
        ),
        2,
    ),
    EmptyPage("/portfolio", (), 1),
    EmptyPage("/entities", ("No entities to show yet.",), 1),
]


@pytest.mark.parametrize("page", EMPTY_PAGES, ids=lambda p: p.id)
async def test_empty_cache_renders_plain_english_empty_state(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    page: EmptyPage,
) -> None:
    """With nothing cached yet a page still renders, says so, and never errors."""
    html = await _get(client, page.path, viewer_headers)
    for text in page.texts:
        assert text in html, text
    assert html.count(NOT_UPDATED) == page.not_updated
    assert "Not connected yet" not in html
    assert OUT_OF_DATE not in html


async def test_empty_cache_admin_manual_marks_says_none(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    """The Admin hand-valued accounts page has a plain empty state."""
    html = await _get(client, "/admin/manual-marks", admin_headers)
    assert "No accounts are valued by hand right now." in html


async def test_unknown_entity_gets_a_plain_not_found_page(
    client: httpx.AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """An entity that is not cached (for example never fetched) is a calm 404."""
    html = await _get(client, f"/entities/{LLC_ID}", viewer_headers, status=404)
    assert "We could not find that page" in html
    assert "Go to the home page" in html
