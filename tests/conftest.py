# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003, PLC0415  # app imports deferred to fixture bodies
"""Shared pytest fixtures for the family office portal test suite.

Identity fixtures emulate the Authentik proxy provider: tests mint RS256 JWTs
with ``jwt_factory`` and the JWKS fetch is patched to return the matching
public key, so no network call is ever made.
"""

from __future__ import annotations

import base64
import importlib
import time
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

TEST_ISSUER = "https://auth.test/application/o/family-office-portal/"
TEST_AUDIENCE = "test-client-id"
TEST_KID = "test-key-id"


# --------------------------------------------------------------------------- #
# Filesystem fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a temp path for a fresh SQLite database (not yet created)."""
    return tmp_path / "portal_cache.db"


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


@pytest.fixture
def portal_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db_path: Path,
) -> Iterator[dict[str, str]]:
    """Set every environment variable ``app.main`` requires at startup."""
    env = {
        "BACKEND_LLC_MANAGER_URL": "http://llc-manager.test",
        "BACKEND_LLC_MANAGER_API_KEY": "llc-key",  # pragma: allowlist secret
        "BACKEND_PP_SECURITY_URL": "http://pp-security.test",
        "BACKEND_PP_SECURITY_API_KEY": "pp-key",  # pragma: allowlist secret
        "BACKEND_XERO_CRYPTO_URL": "http://xero-crypto.test",
        "BACKEND_XERO_CRYPTO_API_KEY": "xero-key",  # pragma: allowlist secret
        "AUTHENTIK_JWKS_URL": "https://auth.test/application/o/family-office-portal/jwks/",
        "AUTHENTIK_ISSUER": TEST_ISSUER,
        "AUTHENTIK_AUDIENCE": TEST_AUDIENCE,
        "SQLITE_PATH": str(tmp_db_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    yield env


# --------------------------------------------------------------------------- #
# Identity fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey]:
    """Generate one RSA-2048 key pair for signing test JWTs."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _b64(value: int) -> str:
    byte_length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="session")
def jwks_document(rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey]) -> dict[str, Any]:
    """Return a JWKS document publishing the test public key."""
    _, public = rsa_key_pair
    numbers = public.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": TEST_KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }


@pytest.fixture
def jwt_factory(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> Callable[..., str]:
    """Return a factory that mints Authentik-style JWTs.

    Defaults produce a valid Viewer token. Override ``groups``, ``aud``,
    ``exp``, ``kid``, or ``private_key``; pass ``claims`` for anything else.
    """
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization

    default_private_key, _ = rsa_key_pair

    def _make(  # noqa: PLR0913  # one keyword per overridable claim
        *,
        groups: list[str] | None = None,
        username: str = "viewer",
        aud: str | list[str] = TEST_AUDIENCE,
        exp: int | None = None,
        kid: str = TEST_KID,
        private_key: RSAPrivateKey | None = None,
        claims: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": TEST_ISSUER,
            "aud": aud,
            "sub": f"sub-{username}",
            "preferred_username": username,
            "email": f"{username}@example.com",
            "name": username.title(),
            "groups": ["fo-viewer"] if groups is None else groups,
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
        return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})

    return _make


@pytest.fixture
def patched_jwks(
    monkeypatch: pytest.MonkeyPatch,
    jwks_document: dict[str, Any],
) -> dict[str, int]:
    """Stub the Authentik JWKS fetch and reset the key cache.

    Returns a dict whose ``calls`` counter records how often keys were fetched.
    """
    from app.middleware import authentik

    counter = {"calls": 0}

    def _stub(url: str, timeout: float) -> dict[str, Any]:
        del url, timeout
        counter["calls"] += 1
        return jwks_document

    monkeypatch.setattr(authentik, "fetch_authentik_jwks", _stub)
    authentik.jwks_cache.clear()
    return counter


@pytest.fixture
def viewer_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Request headers for a Viewer."""
    return {"X-authentik-jwt": jwt_factory(groups=["fo-viewer"], username="viewer")}


@pytest.fixture
def admin_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Request headers for an Admin."""
    return {"X-authentik-jwt": jwt_factory(groups=["fo-admin"], username="admin")}


# --------------------------------------------------------------------------- #
# App client
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(
    portal_env: dict[str, str],
    tmp_db_path: Path,
    patched_jwks: dict[str, int],
) -> AsyncIterator[AsyncClient]:
    """Yield an unopened HTTPX client bound to a freshly loaded app.

    Tests open it with ``async with client as ac``.

    ``httpx.ASGITransport`` does not run the lifespan, so the schema is
    initialized here and the scheduler never starts.
    """
    del portal_env, patched_jwks
    from app import db

    db.init_schema(str(tmp_db_path))
    import app.main as _main

    importlib.reload(_main)
    yield AsyncClient(transport=ASGITransport(app=_main.app), base_url="http://test")
