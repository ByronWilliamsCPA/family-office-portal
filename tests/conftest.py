# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Shared pytest fixtures for the family office portal test suite.

Identity fixtures emulate the Authentik proxy provider (ADR-005): tests mint
RS256 JWTs with ``jwt_factory`` and ``patched_jwks`` replaces the JWKS fetch
with the matching public key, so no test ever makes a network call.

``app.main`` exits at import when a required env var is missing, so it is
imported inside fixture bodies after ``portal_env`` has populated the
environment, never at module level.
"""

from __future__ import annotations

import base64
import importlib
import time
from typing import TYPE_CHECKING, Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from app.middleware import authentik
from app.middleware.authentik import AuthentikSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPrivateKey,
        RSAPublicKey,
    )
    from fastapi import FastAPI

TEST_ISSUER = "https://auth.test/application/o/family-office-portal/"
TEST_AUDIENCE = "test-client-id"
TEST_JWKS_URL = "https://auth.test/application/o/family-office-portal/jwks/"
TEST_KID = "test-key-id"

# Optional auth variables cleared by ``portal_env`` so a developer's shell
# cannot change test outcomes.
_OPTIONAL_AUTH_ENV_VARS = (
    "FO_ADMIN_GROUP",
    "FO_VIEWER_GROUP",
    "AUTHENTIK_JWKS_CACHE_SECONDS",
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
) -> Iterator[dict[str, str]]:
    """Set every environment variable ``app.main`` requires at startup.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_db_path: Temp SQLite path used for ``SQLITE_PATH``.

    Yields:
        dict[str, str]: The values set, as the single source of truth.
    """
    env = {
        "BACKEND_LLC_MANAGER_URL": "http://llc-manager.test",
        "BACKEND_PP_SECURITY_URL": "http://pp-security.test",
        "BACKEND_XERO_CRYPTO_URL": "http://xero-crypto.test",
        "BACKEND_FAMILY_OFFICE_URL": "http://family-office.test",
        "AUTHENTIK_JWKS_URL": TEST_JWKS_URL,
        "AUTHENTIK_ISSUER": TEST_ISSUER,
        "AUTHENTIK_AUDIENCE": TEST_AUDIENCE,
        "SQLITE_PATH": str(tmp_db_path),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in _OPTIONAL_AUTH_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    yield env


@pytest.fixture
def auth_settings() -> AuthentikSettings:
    """Return Authentik settings matching the tokens ``jwt_factory`` mints.

    Returns:
        AuthentikSettings: Test issuer, audience, JWKS URL and default groups.
    """
    return AuthentikSettings(
        jwks_url=TEST_JWKS_URL,
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
    )


# --------------------------------------------------------------------------- #
# Identity fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[RSAPrivateKey, RSAPublicKey]:
    """Generate one RSA-2048 key pair for signing test JWTs.

    Returns:
        tuple[RSAPrivateKey, RSAPublicKey]: The signing and verification keys.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _b64_uint(value: int) -> str:
    byte_length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_jwk(public_key: RSAPublicKey, kid: str = TEST_KID) -> dict[str, str]:
    """Return the RS256 signing JWK for ``public_key``.

    Args:
        public_key: RSA public key to publish.
        kid: Key ID to publish it under.

    Returns:
        dict[str, str]: One JWK entry.
    """
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64_uint(numbers.n),
        "e": _b64_uint(numbers.e),
    }


@pytest.fixture
def make_jwks() -> Callable[[Sequence[tuple[RSAPublicKey, str]]], dict[str, Any]]:
    """Return a builder for JWKS documents from ``(public_key, kid)`` pairs.

    Returns:
        Callable: Builder returning a ``{"keys": [...]}`` document.
    """

    def _build(entries: Sequence[tuple[RSAPublicKey, str]]) -> dict[str, Any]:
        return {"keys": [public_jwk(key, kid) for key, kid in entries]}

    return _build


@pytest.fixture(scope="session")
def jwks_document(rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey]) -> dict[str, Any]:
    """Return a JWKS document publishing the test public key.

    Args:
        rsa_key_pair: Session RSA key pair.

    Returns:
        dict[str, Any]: JWKS with one key under ``TEST_KID``.
    """
    return {"keys": [public_jwk(rsa_key_pair[1])]}


@pytest.fixture
def jwt_factory(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> Callable[..., str]:
    """Return a factory that mints Authentik-style ``X-authentik-jwt`` tokens.

    Defaults produce a valid Viewer token. Override ``username``, ``kid`` or
    ``private_key``; pass ``claims`` to add or replace any claim (for example
    ``groups``, ``aud`` or ``exp``) and ``omit`` to drop claims entirely.

    Args:
        rsa_key_pair: Session RSA key pair; its private key is the default
            signer.

    Returns:
        Callable[..., str]: Token factory.
    """
    default_private_key, _ = rsa_key_pair

    def _make(
        *,
        username: str = "viewer",
        kid: str = TEST_KID,
        private_key: RSAPrivateKey | None = None,
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
            "iat": now,
            "exp": now + 3600,
            **(claims or {}),
        }
        for claim in omit:
            payload.pop(claim, None)
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
) -> dict[str, Any]:
    """Replace the Authentik JWKS fetch with an in-memory stub.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        jwks_document: Default JWKS the stub serves.

    Returns:
        dict[str, Any]: Mutable stub state. ``calls`` counts fetches;
        ``document`` is the JWKS served and may be replaced by a test.
    """
    state: dict[str, Any] = {"calls": 0, "document": jwks_document}

    async def _stub(url: str, **_kwargs: object) -> dict[str, Any]:
        assert url == TEST_JWKS_URL
        state["calls"] += 1
        document: dict[str, Any] = state["document"]
        return document

    monkeypatch.setattr(authentik, "fetch_authentik_jwks", _stub)
    return state


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
    patched_jwks: dict[str, Any],
) -> AsyncClient:
    """Return an unopened client for the real app that sends no identity.

    Args:
        portal_env: Populates the required env vars before the app loads.
        patched_jwks: Keeps the JWKS fetch offline.

    Returns:
        AsyncClient: Client without an ``X-authentik-jwt`` header.
    """
    del portal_env, patched_jwks
    return AsyncClient(transport=ASGITransport(app=_load_app()), base_url="http://test")


@pytest.fixture
def client(
    portal_env: dict[str, str],
    patched_jwks: dict[str, Any],
    admin_headers: dict[str, str],
) -> AsyncClient:
    """Return an unopened client for the real app, authenticated as an Admin.

    Route tests use this client so they exercise handlers rather than auth;
    an Admin token reaches every route, including ``/admin/*``. Auth
    behaviour itself is covered in ``tests/unit/test_middleware.py``.

    Args:
        portal_env: Populates the required env vars before the app loads.
        patched_jwks: Keeps the JWKS fetch offline.
        admin_headers: Default identity header sent on every request.

    Returns:
        AsyncClient: Client sending an Admin ``X-authentik-jwt`` header.
    """
    del portal_env, patched_jwks
    return AsyncClient(
        transport=ASGITransport(app=_load_app()),
        base_url="http://test",
        headers=admin_headers,
    )
