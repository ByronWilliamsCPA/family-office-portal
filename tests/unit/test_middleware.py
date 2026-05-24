# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLC0415, ANN401, TC003
"""Unit tests for the Cloudflare Access JWT middleware.

Spec contract (``CLAUDE.md`` "Authentication rules" + ADR-002 + tech-spec §5):

1. ``CF-Access-JWT-Assertion`` header is required on every non-static request.
2. JWT signature is verified against Cloudflare team-domain public keys.
3. ``aud`` claim is validated against ``CF_ACCESS_APP_ID``. Skipping this check
   is a documented critical security gap (#CRITICAL). The ``aud`` claim may be
   either a string or a JSON array; both forms must be accepted.
4. ``email`` claim is mapped to ``Viewer`` or ``Admin`` role via
   ``VIEWER_EMAILS`` / ``ADMIN_EMAILS`` env vars.
5. Missing or invalid JWT returns 403.

The middleware package exists in Phase 0 as a pass-through stub
(``app.middleware.cloudflare_access.CloudflareAccessMiddleware``). The Phase 1
JWT validation API (``validate_cf_jwt``, ``get_role_from_email``,
``fetch_cf_public_keys``) lands later. This module skips entirely until any
of those Phase 1 symbols is present; once one appears, missing siblings are a
contract violation and fail loudly.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

middleware = pytest.importorskip("app.middleware")

_SUBMODULE_CANDIDATES = ("cf_jwt", "jwt", "auth", "cloudflare_access")
_PHASE1_SENTINELS = ("validate_cf_jwt", "get_role_from_email")

# Phase 1 readiness gate: the Phase 0 middleware stub is importable but
# does not expose the JWT validation API. Skip the whole file until Phase 1
# implements validate_cf_jwt and get_role_from_email -- at that point,
# _resolve() failing means a broken implementation, not an absent one.
_has_phase1_api = any(hasattr(middleware, _s) for _s in _PHASE1_SENTINELS)
if not _has_phase1_api:
    for _sub in ("cf_jwt", "jwt", "auth"):
        try:
            _submod = importlib.import_module(f"app.middleware.{_sub}")
            if any(hasattr(_submod, _s) for _s in _PHASE1_SENTINELS):
                _has_phase1_api = True
                break
        except ModuleNotFoundError:
            continue

if not _has_phase1_api:
    pytest.skip(
        "app.middleware Phase 1 JWT API (validate_cf_jwt, get_role_from_email) "
        "not implemented yet",
        allow_module_level=True,
    )

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPrivateKey,
        RSAPublicKey,
    )


def _phase1_present() -> bool:
    """Return True iff at least one Phase 1 JWT API symbol is exposed.

    # noqa
    """
    for symbol in _PHASE1_SENTINELS:
        if hasattr(middleware, symbol):
            return True
        for sub in _SUBMODULE_CANDIDATES:
            try:
                mod = importlib.import_module(f"app.middleware.{sub}")
            except ModuleNotFoundError:
                continue
            if hasattr(mod, symbol):
                return True
    return False


if not _phase1_present():
    pytest.skip(
        "Phase 1 CF JWT API not yet present in app.middleware",
        allow_module_level=True,
    )


def _resolve(name: str) -> Any:
    """Return ``app.middleware.<name>`` if present, else search known submodules.

    Module-level guard guarantees at least one Phase 1 symbol exists, so a
    missing requested symbol is a real contract violation and fails loudly.

    # noqa
    """
    if hasattr(middleware, name):
        return getattr(middleware, name)
    for sub in _SUBMODULE_CANDIDATES:
        try:
            mod = importlib.import_module(f"app.middleware.{sub}")
        except ModuleNotFoundError:
            continue
        if hasattr(mod, name):
            return getattr(mod, name)
    pytest.fail(
        f"Spec contract violation: app.middleware exposes Phase 1 API but is "
        f"missing '{name}' at the package level or in any of "
        f"{_SUBMODULE_CANDIDATES}."
    )


# --------------------------------------------------------------------------- #
# get_role_from_email
# --------------------------------------------------------------------------- #


def test_get_role_returns_viewer_for_viewer_email(
    cf_env: dict[str, str],
) -> None:
    """A primary viewer email maps to the Viewer role.

    # noqa
    """
    del cf_env
    get_role = _resolve("get_role_from_email")
    assert get_role("viewer@example.com") == "Viewer"


def test_get_role_returns_viewer_for_second_viewer_email(
    cf_env: dict[str, str],
) -> None:
    """A second viewer email in VIEWER_EMAILS also maps to Viewer.

    # noqa
    """
    del cf_env
    get_role = _resolve("get_role_from_email")
    assert get_role("viewer2@example.com") == "Viewer"


def test_get_role_returns_admin_for_admin_email(
    cf_env: dict[str, str],
) -> None:
    """An email in ADMIN_EMAILS maps to the Admin role.

    # noqa
    """
    del cf_env
    get_role = _resolve("get_role_from_email")
    assert get_role("admin@example.com") == "Admin"


def test_get_role_returns_none_for_unknown_email(
    cf_env: dict[str, str],
) -> None:
    """Emails outside the configured lists are denied (no role).

    # noqa
    """
    del cf_env
    get_role = _resolve("get_role_from_email")
    assert get_role("stranger@example.com") is None


def test_get_role_email_match_is_case_insensitive(
    cf_env: dict[str, str],
) -> None:
    """Email comparison should be case-insensitive (RFC 5321 local-part is
    technically case-sensitive, but Cloudflare normalizes to lowercase). #ASSUME

    # noqa
    """
    del cf_env
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
    for sub in _SUBMODULE_CANDIDATES:
        try:
            mod = importlib.import_module(f"app.middleware.{sub}")
        except ModuleNotFoundError:
            continue
        if hasattr(mod, "fetch_cf_public_keys"):
            target = mod
            break

    if not hasattr(target, "fetch_cf_public_keys"):
        pytest.fail(
            "Spec contract violation: app.middleware exposes Phase 1 API but "
            "does not expose a patchable 'fetch_cf_public_keys' hook. JWT "
            "validation tests require this hook to inject a test JWKS without "
            "making a real network call to Cloudflare."
        )

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
    del cf_env, patch_jwks
    validate = _resolve("validate_cf_jwt")
    token = jwt_factory(email="viewer@example.com", aud="test-app-id")
    claims = validate(token)
    assert claims["email"] == "viewer@example.com"


def test_validate_jwt_accepts_aud_as_json_array(
    cf_env: dict[str, str],
    jwt_factory: Callable[..., str],
    patch_jwks: JWKSDocument,
) -> None:
    """Cloudflare Access may emit ``aud`` as a JSON array; both forms must be
    accepted as long as ``CF_ACCESS_APP_ID`` appears in the list. (#CRITICAL
    #ASSUME -- verify against a real CF token before Phase 1 ships.)

    # noqa
    """
    del cf_env, patch_jwks
    validate = _resolve("validate_cf_jwt")
    token = jwt_factory(
        email="viewer@example.com",
        aud=["test-app-id", "another-app-id"],  # type: ignore[arg-type]
    )
    claims = validate(token)
    assert claims["email"] == "viewer@example.com"


def test_validate_jwt_rejects_expired_token(
    cf_env: dict[str, str],
    jwt_factory: Callable[..., str],
    patch_jwks: JWKSDocument,
) -> None:
    """A token whose exp is in the past is rejected.

    # noqa
    """
    del cf_env, patch_jwks
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
    del cf_env, patch_jwks
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
    del cf_env, patch_jwks
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
    del cf_env, patch_jwks
    validate = _resolve("validate_cf_jwt")
    with pytest.raises(Exception):  # noqa: B017
        validate("not.a.jwt")
