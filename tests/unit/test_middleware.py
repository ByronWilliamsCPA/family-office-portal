# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLC0415, ANN401, TC003
"""Unit tests for the Cloudflare Access JWT middleware.

Spec contract (``CLAUDE.md`` "Authentication rules" + ADR-002 + tech-spec §5):

1. ``CF-Access-JWT-Assertion`` header is required on every non-static request.
2. JWT signature is verified against Cloudflare team-domain public keys.
3. ``aud`` claim is validated against ``CF_ACCESS_APP_ID``. Skipping this check
   is a documented critical security gap (#CRITICAL).
4. ``email`` claim is mapped to ``Viewer`` or ``Admin`` role via
   ``VIEWER_EMAILS`` / ``ADMIN_EMAILS`` env vars.
5. Missing or invalid JWT returns 403.

The middleware is expected at ``app.middleware`` (package). Tests reach for
``validate_cf_jwt`` and ``get_role_from_email`` first; if not exposed at the
package level, individual tests fall back to expected submodule paths.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

middleware = pytest.importorskip("app.middleware")

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPrivateKey,
        RSAPublicKey,
    )


def _resolve(name: str) -> Any:
    """Return ``app.middleware.<name>`` if present, else attempt common submodules.

    # noqa
    """
    if hasattr(middleware, name):
        return getattr(middleware, name)
    for sub in ("cf_jwt", "jwt", "auth"):
        try:
            mod = pytest.importorskip(f"app.middleware.{sub}")
        except pytest.skip.Exception:  # type: ignore[attr-defined]
            continue
        if hasattr(mod, name):
            return getattr(mod, name)
    pytest.skip(f"app.middleware.{name} not implemented yet")


# --------------------------------------------------------------------------- #
# get_role_from_email
# --------------------------------------------------------------------------- #


def test_get_role_returns_viewer_for_viewer_email(
    cf_env: dict[str, str],
) -> None:
    """A primary viewer email maps to the Viewer role.

    # noqa
    """
    get_role = _resolve("get_role_from_email")
    assert get_role("viewer@example.com") == "Viewer"


def test_get_role_returns_viewer_for_second_viewer_email(
    cf_env: dict[str, str],
) -> None:
    """A second viewer email in VIEWER_EMAILS also maps to Viewer.

    # noqa
    """
    get_role = _resolve("get_role_from_email")
    assert get_role("viewer2@example.com") == "Viewer"


def test_get_role_returns_admin_for_admin_email(
    cf_env: dict[str, str],
) -> None:
    """An email in ADMIN_EMAILS maps to the Admin role.

    # noqa
    """
    get_role = _resolve("get_role_from_email")
    assert get_role("admin@example.com") == "Admin"


def test_get_role_returns_none_for_unknown_email(
    cf_env: dict[str, str],
) -> None:
    """Emails outside the configured lists are denied (no role).

    # noqa
    """
    get_role = _resolve("get_role_from_email")
    assert get_role("stranger@example.com") is None


def test_get_role_email_match_is_case_insensitive(
    cf_env: dict[str, str],
) -> None:
    """Email comparison should be case-insensitive (RFC 5321 local-part is
    technically case-sensitive, but Cloudflare normalizes to lowercase). #ASSUME

    # noqa
    """
    get_role = _resolve("get_role_from_email")
    assert get_role("VIEWER@example.com") == "Viewer"


# --------------------------------------------------------------------------- #
# validate_cf_jwt
# --------------------------------------------------------------------------- #


JWKSDocument = dict[str, list[dict[str, str]]]


def _public_keys_jwks(
    public_key: RSAPublicKey, kid: str = "test-key-id"
) -> JWKSDocument:
    """Return a JWKS document containing the test public key.

    # noqa
    """
    import base64

    numbers = public_key.public_numbers()

    def _b64(value: int) -> str:
        """Base64url-encode an unsigned big-endian integer.

        # noqa
        """
        byte_length = (value.bit_length() + 7) // 8
        raw = value.to_bytes(byte_length, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }


@pytest.fixture
def patch_jwks(
    monkeypatch: pytest.MonkeyPatch,
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> JWKSDocument:
    """Replace the middleware's JWKS fetcher with a static test key set.

    # noqa
    """
    _, public = rsa_key_pair
    jwks = _public_keys_jwks(public)

    target = middleware
    for sub in ("cf_jwt", "jwt", "auth"):
        try:
            mod = pytest.importorskip(f"app.middleware.{sub}")
        except pytest.skip.Exception:  # type: ignore[attr-defined]
            continue
        if hasattr(mod, "fetch_cf_public_keys"):
            target = mod
            break

    if not hasattr(target, "fetch_cf_public_keys"):
        pytest.skip("fetch_cf_public_keys not implemented yet")

    def _stub(*_args: object, **_kw: object) -> JWKSDocument:
        """Return the static test JWKS document.

        # noqa
        """
        return jwks

    monkeypatch.setattr(target, "fetch_cf_public_keys", _stub)
    return jwks


def test_validate_jwt_accepts_well_formed_token(
    cf_env: dict[str, str],
    jwt_factory: Callable[..., str],
    patch_jwks: JWKSDocument,
) -> None:
    """A token signed with the trusted key and matching aud is accepted.

    # noqa
    """
    validate = _resolve("validate_cf_jwt")
    token = jwt_factory(email="viewer@example.com", aud="test-app-id")
    claims = validate(token)
    assert claims["email"] == "viewer@example.com"
    assert claims["aud"] == "test-app-id"


def test_validate_jwt_rejects_expired_token(
    cf_env: dict[str, str],
    jwt_factory: Callable[..., str],
    patch_jwks: JWKSDocument,
) -> None:
    """A token whose exp is in the past is rejected.

    # noqa
    """
    validate = _resolve("validate_cf_jwt")
    expired = jwt_factory(exp=int(time.time()) - 60)
    with pytest.raises(Exception):  # noqa: B017 -- spec only says "reject"
        validate(expired)


def test_validate_jwt_rejects_wrong_audience(
    cf_env: dict[str, str],
    jwt_factory: Callable[..., str],
    patch_jwks: JWKSDocument,
) -> None:
    """#CRITICAL: tokens for a different Cloudflare app must be rejected.

    # noqa
    """
    validate = _resolve("validate_cf_jwt")
    foreign = jwt_factory(aud="some-other-app-id")
    with pytest.raises(Exception):  # noqa: B017
        validate(foreign)


def test_validate_jwt_rejects_token_signed_by_wrong_key(
    cf_env: dict[str, str],
    jwt_factory: Callable[..., str],
    patch_jwks: JWKSDocument,
) -> None:
    """A token signed with a key not in the JWKS is rejected.

    # noqa
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    validate = _resolve("validate_cf_jwt")
    bad_token = jwt_factory(private_key=other_private)
    with pytest.raises(Exception):  # noqa: B017
        validate(bad_token)


def test_validate_jwt_rejects_garbage_token(
    cf_env: dict[str, str],
    patch_jwks: JWKSDocument,
) -> None:
    """A non-JWT input string is rejected (no crash, no silent accept).

    # noqa
    """
    validate = _resolve("validate_cf_jwt")
    with pytest.raises(Exception):  # noqa: B017
        validate("not.a.jwt")
