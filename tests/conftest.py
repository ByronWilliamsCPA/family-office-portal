# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003, PLC0415  # app imports deferred to fixture body so collection works before Phase 1 modules exist
"""Shared pytest fixtures for the family office portal test suite.

Fixtures here intentionally avoid importing ``app`` submodules at import time so
the suite can be collected even when Phase 1 modules are not yet implemented.
Each test file that depends on a not-yet-written module uses
``pytest.importorskip`` to skip gracefully.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPrivateKey,
        RSAPublicKey,
    )


# --------------------------------------------------------------------------- #
# Filesystem fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a temp filesystem path for a fresh SQLite database.

    The file is *not* created; tests/factories must initialize the schema.

    # noqa
    """
    return tmp_path / "portal_cache.db"


# --------------------------------------------------------------------------- #
# Cryptographic fixtures for CF JWT middleware tests
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey]:
    """Generate a fresh RSA-2048 key pair for signing test JWTs.

    Cloudflare Access signs JWTs with RS256, so test fixtures must use RSA.
    Session scope avoids regenerating the key between tests.

    # noqa
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def jwt_factory(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> Callable[..., str]:
    """Return a factory that mints signed JWTs with caller-supplied claims.

    Defaults emulate a valid Cloudflare Access JWT for the configured app.
    Override any claim by passing it as a keyword argument; pass
    ``private_key=...`` to sign with a different key (useful for invalid-
    signature tests).

    # noqa
    """
    import time

    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization

    default_private_key, _ = rsa_key_pair

    def _make(
        *,
        email: str = "viewer@example.com",
        aud: str | list[str] = "test-app-id",
        exp: int | None = None,
        private_key: RSAPrivateKey | None = None,
        claims: dict[str, Any] | None = None,
    ) -> str:
        """Mint a signed JWT.

        Pass ``claims={"iss": "..."}`` to override the issuer or inject any
        extra claim needed for negative middleware tests.

        # noqa
        """
        now = int(time.time())
        base: dict[str, Any] = {
            "email": email,
            "aud": aud,
            "iss": "https://test-team.cloudflareaccess.com",
            "iat": now,
            "exp": exp if exp is not None else now + 3600,
            **(claims or {}),
        }
        key = private_key if private_key is not None else default_private_key
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return pyjwt.encode(
            base, pem, algorithm="RS256", headers={"kid": "test-key-id"}
        )

    return _make


@pytest.fixture
def cf_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db_path: Path,
) -> Iterator[dict[str, str]]:
    """Set the env vars required by ``app.main`` startup.

    Exposes the values as a dict so tests can assert against the same source
    of truth used by the application.

    # noqa
    """
    env = {
        "BACKEND_LLC_MANAGER_URL": "http://llc-manager.test",
        "BACKEND_PP_SECURITY_URL": "http://pp-security.test",
        "BACKEND_XERO_CRYPTO_URL": "http://xero-crypto.test",
        "BACKEND_FAMILY_OFFICE_URL": "http://family-office.test",
        "CF_TEAM_DOMAIN": "test-team.cloudflareaccess.com",
        "CF_ACCESS_APP_ID": "test-app-id",
        "VIEWER_EMAILS": "viewer@example.com,viewer2@example.com",
        "ADMIN_EMAILS": "admin@example.com",
        "SQLITE_PATH": str(tmp_db_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    yield env


@pytest.fixture
async def client(cf_env: dict[str, str]) -> AsyncIterator[AsyncClient]:
    """Yield an HTTPX async client wired to the FastAPI app via ASGI transport.

    Depends on ``cf_env`` so the required env vars are populated before
    ``app.main`` is imported (the module exits immediately if any are absent).
    Uses ``importlib.reload`` so tests that run after ``cf_env`` has already
    cached the module in ``sys.modules`` see the env-patched version.

    Args:
        cf_env: Fixture that sets the nine required env vars via monkeypatch.

    Yields:
        AsyncClient: HTTPX async client ready to make requests against the app.
    """
    del cf_env
    import app.main as _main

    importlib.reload(_main)
    yield AsyncClient(transport=ASGITransport(app=_main.app), base_url="http://test")
