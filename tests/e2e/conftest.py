# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Fixtures for the real-browser tests: a live app, a browser and tablet pages.

The app runs as a real ``uvicorn`` subprocess (one worker, scheduler off) on a
free localhost port, against a temporary SQLite file filled with made-up data.
The browser sends a freshly minted HS256 token in ``X-authentik-jwt``, the same
way the Authentik proxy does. Every secret is generated at run time and never
stored; no ``.env`` file is read (the subprocess gets a minimal environment).

The Tailwind stylesheet is not committed, so the ``built_css`` fixture builds it
with the standalone ``tailwindcss`` CLI into a temporary directory and the
browser is handed that file in place of ``/static/css/output.css``. When the
CLI is missing the whole module skips with a message, unless
``E2E_REQUIRE_BROWSER_STACK`` is set (CI sets it), in which case it fails.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import jwt as pyjwt
import pytest
from playwright.sync_api import sync_playwright

from app.db import init_schema

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Browser, BrowserContext, Page, Playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
VIEWPORT = {"width": 1024, "height": 768}
TEST_ISSUER = "https://auth.test/application/o/family-office-portal/"
TEST_AUDIENCE = "e2e-test-client"
SCREENSHOT_DIR = Path(
    os.environ.get("E2E_SCREENSHOT_DIR", str(REPO_ROOT / "e2e-screenshots"))
)
_STARTUP_TIMEOUT_SECONDS = 40.0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under ``tests/e2e`` with ``e2e``.

    Args:
        items (list[pytest.Item]): All collected tests.
    """
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.e2e)


def _unavailable(reason: str) -> None:
    """Skip the e2e tests, or fail when the CI stack must be complete.

    Args:
        reason (str): What is missing.
    """
    if os.environ.get("E2E_REQUIRE_BROWSER_STACK"):
        pytest.fail(f"E2E_REQUIRE_BROWSER_STACK is set but {reason}", pytrace=False)
    pytest.skip(f"e2e skipped: {reason} (see tests/e2e/README.md)")


# --------------------------------------------------------------------------- #
# Built stylesheet
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def built_css(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the Tailwind stylesheet into a temporary file.

    Args:
        tmp_path_factory (pytest.TempPathFactory): Session temp directories.

    Returns:
        Path: The compiled ``output.css``.
    """
    binary = shutil.which("tailwindcss")
    if binary is None:
        _unavailable("the `tailwindcss` CLI is not on PATH, so no CSS can be built")
    out = tmp_path_factory.mktemp("css") / "output.css"
    result = subprocess.run(
        [
            str(binary),
            "-i",
            str(REPO_ROOT / "static" / "css" / "input.css"),
            "-o",
            str(out),
            "--minify",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        pytest.fail(f"tailwindcss build failed: {result.stderr[-500:]}", pytrace=False)
    return out


# --------------------------------------------------------------------------- #
# Seed data (all made up)
# --------------------------------------------------------------------------- #

ENTITY_ID = "ent-alpha"
LONG_ENTITY_NAME = (
    "Example Family Heritage and Legacy Holdings Limited Liability Company"
)
LONG_DOCUMENT_NAME = (
    "Example Annual Statement With A Very Long File Name That Keeps Going "
    "And Going For Testing.pdf"
)


def _entity_rows(today: date, fetched: str) -> list[tuple[Any, ...]]:
    """Return made-up entity rows.

    Args:
        today (date): Today's date, so upcoming dates stay in the future.
        fetched (str): ``fetched_at`` timestamp.

    Returns:
        list[tuple[Any, ...]]: Rows for the ``entities`` table.
    """
    soon = (today + timedelta(days=30)).isoformat()
    later = (today + timedelta(days=60)).isoformat()
    latest = (today + timedelta(days=90)).isoformat()
    return [
        (ENTITY_ID, "Example Holdings LLC", "llc", "WY", "current", soon, fetched),
        ("ent-beta", "Example Family Trust", "trust", "NV", "current", later, fetched),
        ("ent-person", "Example Person", "individual", None, None, None, fetched),
        ("ent-long", LONG_ENTITY_NAME, "llc", "DE", "current", latest, fetched),
    ]


def _document_rows(fetched: str) -> list[tuple[Any, ...]]:
    """Return made-up document rows, one of them confidential.

    Args:
        fetched (str): ``fetched_at`` timestamp.

    Returns:
        list[tuple[Any, ...]]: Rows for the ``documents`` table.
    """
    return [
        ("doc-1", "Example Operating Agreement.pdf", "Legal", ENTITY_ID,
         "2025-03-01", 0, "2026-10-01T00:00:00", fetched),
        ("doc-2", "Example Insurance Policy.pdf", "Insurance", ENTITY_ID,
         "2025-06-15", 0, "2026-10-02T00:00:00", fetched),
        ("doc-3", "Example Trust Agreement.pdf", "Legal", "ent-beta",
         "2024-11-20", 0, "2026-10-03T00:00:00", fetched),
        ("doc-4", LONG_DOCUMENT_NAME, "Statements", "ent-long",
         "2026-01-05", 0, "2026-10-04T00:00:00", fetched),
        ("doc-5", "Example Confidential Memo.pdf", "Legal", ENTITY_ID,
         "2026-02-02", 1, "2026-10-05T00:00:00", fetched),
    ]  # fmt: skip


def seed_database(path: Path) -> None:
    """Create the schema and fill every section with made-up rows.

    Args:
        path (Path): SQLite file to create.
    """
    init_schema(str(path))
    now = datetime.now(timezone.utc)
    fetched = now.isoformat()
    today = now.date()
    holdings = [
        ("h1", "Example Total Market Fund", "Diversified", 250000.0, 50.0),
        ("h2", "Example Bond Fund", "Bonds", 150000.0, 30.0),
        (
            "h3",
            "Example International Equity Index Fund Class A",
            "Intl",
            100000.0,
            20.0,
        ),
    ]
    day = timedelta(days=1)
    accounts = [
        ("pp:acct-1", "Example Brokerage", ENTITY_ID, "Investments",
         "broker_report", 125_000_050, (today - day).isoformat()),
        ("pp:acct-2", "Example Retirement", "ent-person", "Retirement",
         "manual_mark", 40_000_000, (today - 20 * day).isoformat()),
        ("xero:acct-3", "Example Operating", ENTITY_ID, "Cash",
         "xero_bank", 2_500_000, (today - day).isoformat()),
        ("manual:acct-4", "Example Savings", "ent-beta", "Cash",
         "manual_mark", 1_000_000, (today - 45 * day).isoformat()),
    ]  # fmt: skip
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO entities (id, name, type, state, status, next_date, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            _entity_rows(today, fetched),
        )
        conn.executemany(
            "INSERT INTO documents (id, name, category, entity_id, document_type, "
            "document_date, is_confidential, added_at, proxy_url, fetched_at) "
            "VALUES (?, ?, ?, ?, 'pdf', ?, ?, ?, NULL, ?)",
            _document_rows(fetched),
        )
        conn.executemany(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(*h, fetched) for h in holdings],
        )
        conn.executemany(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("p1", "Example Coin", 1.5, 45000.0, fetched),
                ("p2", "Sample Token", 20.0, 900.0, fetched),
            ],
        )
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'USD')",
            [(*a, fetched) for a in accounts],
        )
        for days_ago, cents in ((3, 160_000_000), (2, 165_000_000), (1, 168_000_000)):
            when = (today - days_ago * day).isoformat()
            conn.execute(
                "INSERT INTO balances_daily (date, account_id, entity_id, category, "
                "value_cents, as_of, currency) VALUES (?, 'pp:acct-1', ?, "
                "'Investments', ?, ?, 'USD')",
                (when, ENTITY_ID, cents, when),
            )
        conn.commit()


# --------------------------------------------------------------------------- #
# Live app
# --------------------------------------------------------------------------- #


class PortalServer:
    """Handle on the running app: its base URL and the token signing secret."""

    def __init__(self, base_url: str, jwt_secret: str) -> None:
        """Store the address and the per-run signing secret.

        Args:
            base_url (str): ``http://127.0.0.1:<port>``.
            jwt_secret (str): Per-run HS256 secret.
        """
        self.base_url = base_url
        self._jwt_secret = jwt_secret

    def token(self, *, admin: bool) -> str:
        """Mint a fresh signed token for a Viewer or an Admin.

        Args:
            admin (bool): True for the ``fo-admin`` group.

        Returns:
            str: Signed JWT for the ``X-authentik-jwt`` header.
        """
        now = int(time.time())
        name = "admin" if admin else "viewer"
        claims = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": f"sub-{name}",
            "preferred_username": name,
            "email": f"{name}@example.com",
            "name": name.title(),
            "groups": ["fo-admin" if admin else "fo-viewer"],
            "iat": now - 30,
            "exp": now + 3600,
        }
        return pyjwt.encode(claims, self._jwt_secret, algorithm="HS256")


def _free_port() -> int:
    """Ask the OS for an unused localhost port.

    Returns:
        int: A port that was free a moment ago.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_healthy(base_url: str, proc: subprocess.Popen[str]) -> None:
    """Poll ``/health`` until the app answers or the deadline passes.

    Args:
        base_url (str): App base URL.
        proc (subprocess.Popen[str]): The server process, watched for early exit.
    """
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"the app exited early with status {proc.returncode}")
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    pytest.fail("the app did not become healthy in time")


@pytest.fixture(scope="session")
def portal(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PortalServer]:
    """Start the real app on a free port with seeded data, stop it afterwards.

    Args:
        tmp_path_factory (pytest.TempPathFactory): Session temp directories.

    Yields:
        PortalServer: Base URL and token minting for the running app.
    """
    work = tmp_path_factory.mktemp("portal")
    db_path = work / "portal_cache.db"
    seed_database(db_path)
    instructions = work / "chat-instructions.txt"
    instructions.write_text("Test instructions for the end-to-end run.\n")
    secret = secrets.token_hex(32)
    port = _free_port()
    # Minimal environment: nothing from the developer's shell or any .env file.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "AUTHENTIK_JWT_SECRET": secret,
        "AUTHENTIK_ISSUER": TEST_ISSUER,
        "AUTHENTIK_AUDIENCE": TEST_AUDIENCE,
        "SQLITE_PATH": str(db_path),
        "SCHEDULER_ENABLED": "false",
        # Backends are "connected" so pages show data; nothing is ever called.
        "BACKEND_LLC_MANAGER_URL": "http://llc-manager.invalid",
        "BACKEND_LLC_MANAGER_API_KEY": secrets.token_urlsafe(16),
        "BACKEND_PP_SECURITY_URL": "http://pp-security.invalid",
        "BACKEND_PP_SECURITY_API_KEY": secrets.token_urlsafe(16),
        "BACKEND_XERO_CRYPTO_URL": "http://xero-crypto.invalid",
        "BACKEND_XERO_CRYPTO_API_KEY": secrets.token_urlsafe(16),
        # Show the question panel to everyone so its form is checked too; the
        # model address is unreachable and no test submits the form.
        "LLM_BASE_URL": "http://llm.invalid",
        "LLM_API_KEY": secrets.token_urlsafe(16),
        "LLM_MODEL": "test-model",
        "CHAT_INSTRUCTIONS_PATH": str(instructions),
        "CHAT_ENABLED_FOR": "all",
    }
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_until_healthy(base_url, proc)
        yield PortalServer(base_url, secret)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """Launch one headless Chromium for the whole run and close it afterwards.

    Yields:
        Browser: The shared browser.
    """
    with sync_playwright() as pw:
        launched = _launch(pw)
        try:
            yield launched
        finally:
            launched.close()


def _launch(pw: Playwright) -> Browser:
    """Launch headless Chromium, or skip (fail in CI) when it is not installed.

    Args:
        pw (Playwright): The started Playwright driver.

    Returns:
        Browser: A headless Chromium.
    """
    try:
        return pw.chromium.launch(headless=True)
    except Exception as exc:  # Playwright raises its own Error
        _unavailable(
            f"Chromium could not start ({type(exc).__name__}); "
            "run `uv run playwright install chromium`"
        )
        raise  # pragma: no cover  # _unavailable always skips or fails


def make_context(
    browser: Browser,
    portal: PortalServer,
    built_css: Path,
    *,
    admin: bool = False,
    javascript: bool = True,
) -> BrowserContext:
    """Open a tablet-sized context that carries the identity header and the CSS.

    Args:
        browser (Browser): The shared browser.
        portal (PortalServer): The running app.
        built_css (Path): Compiled stylesheet served in place of ``output.css``.
        admin (bool): Send an Admin token instead of a Viewer token.
        javascript (bool): False turns JavaScript off for the context.

    Returns:
        BrowserContext: A 1024x768 context.
    """
    context = browser.new_context(
        viewport=VIEWPORT,
        java_script_enabled=javascript,
        extra_http_headers={"X-authentik-jwt": portal.token(admin=admin)},
    )
    context.route(
        "**/static/css/output.css",
        lambda route: route.fulfill(path=str(built_css), content_type="text/css"),
    )
    return context


class ConsolePage:
    """A page plus every console error and uncaught exception it produced."""

    def __init__(self, page: Page) -> None:
        """Attach listeners to the page.

        Args:
            page (Page): The page to watch.
        """
        self.page = page
        self.errors: list[str] = []
        page.on(
            "console",
            lambda msg: self.errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))


@pytest.fixture
def open_page(
    browser: Browser, portal: PortalServer, built_css: Path
) -> Iterator[object]:
    """Return a factory for pages in fresh tablet contexts, closed at teardown.

    Args:
        browser (Browser): The shared browser.
        portal (PortalServer): The running app.
        built_css (Path): Compiled stylesheet.

    Yields:
        Callable: ``open_page(admin=False, javascript=True) -> ConsolePage``.
    """
    contexts: list[BrowserContext] = []

    def _open(*, admin: bool = False, javascript: bool = True) -> ConsolePage:
        context = make_context(
            browser, portal, built_css, admin=admin, javascript=javascript
        )
        contexts.append(context)
        return ConsolePage(context.new_page())

    try:
        yield _open
    finally:
        for context in contexts:
            context.close()
