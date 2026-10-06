# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Shared pytest fixtures for the family office portal test suite.

Identity fixtures emulate the Authentik proxy provider (ADR-005): tests mint
HS256 JWTs with ``jwt_factory``, signed with a random per-session secret from
``jwt_secret``, so no test ever makes a network call and no secret literal is
committed.

``app.main`` exits at import when a required env var is missing, so it is
imported inside fixture bodies after ``portal_env`` has populated the
environment, never at module level.

``httpx.ASGITransport`` does not run the app lifespan, so ``portal_env``
creates the SQLite schema at ``SQLITE_PATH`` itself (as the lifespan would in
production) and the refresh scheduler never starts.
"""

from __future__ import annotations

import importlib
import secrets
import time
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.db import init_schema
from app.middleware.authentik import AuthentikSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from fastapi import FastAPI

TEST_ISSUER = "https://auth.test/application/o/family-office-portal/"
TEST_AUDIENCE = "test-client-id"

# Optional auth variables cleared by ``portal_env`` so a developer's shell
# cannot change test outcomes.
_OPTIONAL_AUTH_ENV_VARS = (
    "FO_ADMIN_GROUP",
    "FO_VIEWER_GROUP",
)

# Optional backend and display settings (``app.config``) cleared by
# ``portal_env`` for the same reason; tests that need one set it themselves.
# The data-ingestor pair is cleared because no client uses it yet.
_OPTIONAL_PORTAL_ENV_VARS = (
    "BACKEND_DATA_INGESTOR_URL",
    "BACKEND_DATA_INGESTOR_API_KEY",
    "BACKEND_TIMEOUT_SECONDS",
    "BALANCE_INTAKE_API_KEY",
    "DISPLAY_TIMEZONE",
    "SCHEDULER_ENABLED",
)


# --------------------------------------------------------------------------- #
# Filesystem fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a temp path for a fresh SQLite database (not yet created).

    Args:
        tmp_path: Pytest per-test temporary directory.

    Returns:
        Path: Location for the test database file.
    """
    return tmp_path / "portal_cache.db"


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


@pytest.fixture
def portal_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db_path: Path,
    jwt_secret: str,
) -> Iterator[dict[str, str]]:
    """Set every environment variable ``app.main`` requires at startup.

    The three backends that have clients are connected: each gets a URL and a
    random API key, because a URL without a key stops the app at startup.
    Backend values are generated per test, never literals. Also creates the
    cache schema at ``SQLITE_PATH``, because the ASGI test transport never
    runs the lifespan that does this in production.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_db_path: Temp SQLite path used for ``SQLITE_PATH``.
        jwt_secret: Random per-session HS256 secret for ``AUTHENTIK_JWT_SECRET``.

    Yields:
        dict[str, str]: The values set, as the single source of truth.
    """
    env = {
        "BACKEND_LLC_MANAGER_URL": "http://llc-manager.test",
        "BACKEND_LLC_MANAGER_API_KEY": secrets.token_urlsafe(16),
        "BACKEND_PP_SECURITY_URL": "http://pp-security.test",
        "BACKEND_PP_SECURITY_API_KEY": secrets.token_urlsafe(16),
        "BACKEND_XERO_CRYPTO_URL": "http://xero-crypto.test",
        "BACKEND_XERO_CRYPTO_API_KEY": secrets.token_urlsafe(16),
        "AUTHENTIK_JWT_SECRET": jwt_secret,
        "AUTHENTIK_ISSUER": TEST_ISSUER,
        "AUTHENTIK_AUDIENCE": TEST_AUDIENCE,
        "SQLITE_PATH": str(tmp_db_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in (*_OPTIONAL_AUTH_ENV_VARS, *_OPTIONAL_PORTAL_ENV_VARS):
        monkeypatch.delenv(key, raising=False)
    init_schema(str(tmp_db_path))
    yield env


@pytest.fixture
def auth_settings(jwt_secret: str) -> AuthentikSettings:
    """Return Authentik settings matching the tokens ``jwt_factory`` mints.

    Args:
        jwt_secret: Random per-session HS256 secret.

    Returns:
        AuthentikSettings: Test secret, issuer, audience and default groups.
    """
    return AuthentikSettings(
        jwt_secret=jwt_secret,
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
    )


# --------------------------------------------------------------------------- #
# Identity fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def jwt_secret() -> str:
    """Generate one random HS256 secret for signing test JWTs.

    Generated at runtime, never a literal, so no secret is committed.

    Returns:
        str: A URL-safe secret of at least 64 characters.
    """
    return secrets.token_urlsafe(64)


@pytest.fixture
def jwt_factory(jwt_secret: str) -> Callable[..., str]:
    """Return a factory that mints Authentik-style ``X-authentik-jwt`` tokens.

    Defaults produce a valid Viewer token signed HS256 with the session
    secret. Override ``username`` or ``secret``; pass ``claims`` to add or
    replace any claim (for example ``groups``, ``aud`` or ``exp``) and
    ``omit`` to drop claims entirely.

    Args:
        jwt_secret: Session secret; the default signing key.

    Returns:
        Callable[..., str]: Token factory.
    """

    def _make(
        *,
        username: str = "viewer",
        secret: str | None = None,
        claims: dict[str, Any] | None = None,
        omit: tuple[str, ...] = (),
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": TEST_ISSUER,
            "aud": TEST_AUDIENCE,
            "sub": f"sub-{username}",
            "preferred_username": username,
            "email": f"{username}@example.com",
            "name": username.title(),
            "groups": ["fo-viewer"],
            # Back-dated so a backward wall-clock step (seen on WSL2) cannot
            # make a fixture token look issued in the future.
            "iat": now - 30,
            "exp": now + 3600,
            **(claims or {}),
        }
        for claim in omit:
            payload.pop(claim, None)
        key = secret if secret is not None else jwt_secret
        return pyjwt.encode(payload, key, algorithm="HS256")

    return _make


@pytest.fixture
def viewer_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Return request headers carrying a Viewer token.

    Args:
        jwt_factory: Token factory.

    Returns:
        dict[str, str]: ``X-authentik-jwt`` header for the ``fo-viewer`` group.
    """
    return {
        "X-authentik-jwt": jwt_factory(
            username="viewer", claims={"groups": ["fo-viewer"]}
        )
    }


@pytest.fixture
def admin_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Return request headers carrying an Admin token.

    Args:
        jwt_factory: Token factory.

    Returns:
        dict[str, str]: ``X-authentik-jwt`` header for the ``fo-admin`` group.
    """
    return {
        "X-authentik-jwt": jwt_factory(
            username="admin", claims={"groups": ["fo-admin"]}
        )
    }


# --------------------------------------------------------------------------- #
# App clients
# --------------------------------------------------------------------------- #


def _load_app() -> FastAPI:
    main = importlib.import_module("app.main")
    return importlib.reload(main).app


@pytest.fixture
def anon_client(
    portal_env: dict[str, str],
) -> AsyncClient:
    """Return an unopened client for the real app that sends no identity.

    Args:
        portal_env: Populates the required env vars before the app loads.

    Returns:
        AsyncClient: Client without an ``X-authentik-jwt`` header.
    """
    del portal_env
    return AsyncClient(transport=ASGITransport(app=_load_app()), base_url="http://test")


@pytest.fixture
def client(
    portal_env: dict[str, str],
    admin_headers: dict[str, str],
) -> AsyncClient:
    """Return an unopened client for the real app, authenticated as an Admin.

    Route tests use this client so they exercise handlers rather than auth;
    an Admin token reaches every route, including ``/admin/*``. A test that
    needs another identity passes its own ``X-authentik-jwt`` header, which
    replaces this default for that request. Auth behaviour itself is covered
    in ``tests/unit/test_middleware.py``.

    Args:
        portal_env: Populates the required env vars before the app loads.
        admin_headers: Default identity header sent on every request.

    Returns:
        AsyncClient: Client sending an Admin ``X-authentik-jwt`` header.
    """
    del portal_env
    return AsyncClient(
        transport=ASGITransport(app=_load_app()),
        base_url="http://test",
        headers=admin_headers,
    )
