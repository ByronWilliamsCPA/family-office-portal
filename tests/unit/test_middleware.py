# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Unit tests for the Authentik forward-auth middleware (ADR-005).

Contract (``CLAUDE.md`` "Authentication rules", ADR-005, tech-spec section 6):

1. Identity comes only from the signed ``X-authentik-jwt`` header; the plain
   ``X-authentik-*`` identity headers are ignored.
2. The token must be RS256, signed by a key in the provider JWKS, with a
   matching ``iss`` and ``aud``, a future ``exp``, no future ``nbf``/``iat``,
   and a non-empty identity claim.
3. ``fo-admin`` grants Admin, ``fo-viewer`` grants Viewer, anyone else is 403.
4. Every failure is 403. ``/health`` and ``/static/`` are public; ``/admin``
   paths need Admin.
5. The JWKS is cached with a TTL; an unknown ``kid`` forces at most one
   rate-limited refetch.

Most tests run the middleware in front of a tiny echo app so the resulting
principal can be asserted; a few run against the real ``app.main`` app.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib
import json
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from structlog.testing import capture_logs

from app.middleware import authentik
from app.middleware.authentik import (
    AuthConfigError,
    AuthentikAuthMiddleware,
    AuthentikSettings,
    AuthError,
    JwksCache,
    Principal,
    Role,
    fetch_authentik_jwks,
    parse_jwks,
    principal_from_claims,
    role_from_groups,
    validate_authentik_jwt,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPrivateKey,
        RSAPublicKey,
    )
    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

    MakeJwks = Callable[[Sequence[tuple[RSAPublicKey, str]]], dict[str, Any]]

TEST_ISSUER = "https://auth.test/application/o/family-office-portal/"
TEST_AUDIENCE = "test-client-id"
TEST_KID = "test-key-id"
JWT_HEADER = "X-authentik-jwt"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class FakeClock:
    """Manually advanced monotonic clock for JWKS cache tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _whoami(request: Request) -> JSONResponse:
    principal: Principal = request.state.principal
    return JSONResponse(
        {
            "username": principal.username,
            "email": principal.email,
            "name": principal.name,
            "role": principal.role.value,
        }
    )


async def _ok(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def _echo_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/whoami", _whoami),
            Route("/admin/thing", _whoami),
            Route("/health", _ok),
            Route("/healthz", _ok),
            Route("/static/{path:path}", _ok),
        ]
    )


def _client(
    settings: AuthentikSettings,
    *,
    cache: JwksCache | None = None,
) -> AsyncClient:
    wrapped = AuthentikAuthMiddleware(_echo_app(), settings=settings, jwks_cache=cache)
    return AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _claims(**overrides: object) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "sub": "sub-admin",
        "preferred_username": "admin",
        "groups": ["fo-admin"],
        "iat": now,
        "exp": now + 3600,
        **overrides,
    }


def _unsigned_parts(header: dict[str, Any], payload: dict[str, Any]) -> str:
    return (
        _b64url(json.dumps(header).encode())
        + "."
        + _b64url(json.dumps(payload).encode())
    )


def _public_pem(public_key: RSAPublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


async def _get(
    settings: AuthentikSettings,
    path: str = "/whoami",
    headers: dict[str, str] | None = None,
    cache: JwksCache | None = None,
) -> httpx.Response:
    async with _client(settings, cache=cache) as ac:
        return await ac.get(path, headers=headers)


# --------------------------------------------------------------------------- #
# Valid tokens
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("patched_jwks")
async def test_valid_viewer_token_is_accepted(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """A correctly signed Viewer token reaches the app with its principal."""
    response = await _get(auth_settings, headers={JWT_HEADER: jwt_factory()})
    assert response.status_code == 200
    assert response.json() == {
        "username": "viewer",
        "email": "viewer@example.com",
        "name": "Viewer",
        "role": "Viewer",
    }


@pytest.mark.usefixtures("patched_jwks")
async def test_valid_admin_token_is_accepted_on_admin_path(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """An Admin token reaches ``/admin`` paths with the Admin role."""
    token = jwt_factory(username="admin", claims={"groups": ["fo-admin"]})
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 200
    assert response.json()["role"] == "Admin"


@pytest.mark.usefixtures("patched_jwks")
async def test_admin_wins_when_user_is_in_both_groups(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """Membership in both portal groups yields Admin."""
    token = jwt_factory(claims={"groups": ["fo-viewer", "fo-admin"]})
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.json()["role"] == "Admin"


@pytest.mark.usefixtures("patched_jwks")
async def test_audience_list_containing_client_id_is_accepted(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """An ``aud`` array that contains the configured audience is accepted."""
    token = jwt_factory(claims={"aud": ["other-app", TEST_AUDIENCE]})
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 200


@pytest.mark.usefixtures("patched_jwks")
async def test_sub_is_used_when_preferred_username_is_absent(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """``sub`` is the fallback identity claim; missing optional claims are None."""
    token = jwt_factory(omit=("preferred_username", "email", "name"))
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 200
    assert response.json() == {
        "username": "sub-viewer",
        "email": None,
        "name": None,
        "role": "Viewer",
    }


@pytest.mark.usefixtures("patched_jwks")
async def test_configured_group_names_are_honoured(
    jwt_factory: Callable[..., str],
) -> None:
    """``FO_ADMIN_GROUP`` / ``FO_VIEWER_GROUP`` replace the default names."""
    settings = AuthentikSettings(
        jwks_url="https://auth.test/application/o/family-office-portal/jwks/",
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
        admin_group="estate-admins",
        viewer_group="estate-family",
    )
    family = jwt_factory(claims={"groups": ["estate-family"]})
    default_name = jwt_factory(claims={"groups": ["fo-viewer"]})
    assert (await _get(settings, headers={JWT_HEADER: family})).status_code == 200
    assert (await _get(settings, headers={JWT_HEADER: default_name})).status_code == 403


# --------------------------------------------------------------------------- #
# Rejected tokens (every case is 403)
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("patched_jwks")
async def test_missing_header_is_rejected(auth_settings: AuthentikSettings) -> None:
    """No ``X-authentik-jwt`` header means no access."""
    response = await _get(auth_settings)
    assert response.status_code == 403
    assert response.text == "Access denied"


@pytest.mark.usefixtures("patched_jwks")
async def test_empty_header_is_rejected(auth_settings: AuthentikSettings) -> None:
    """An empty header value is treated as missing."""
    response = await _get(auth_settings, headers={JWT_HEADER: ""})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "garbage",
    ["garbage", "garbage.token.here", "a.b", "...", "eyJhbGciOiJSUzI1NiJ9.x.y"],
)
async def test_garbage_token_is_rejected(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
    garbage: str,
) -> None:
    """Malformed tokens are rejected without fetching the JWKS."""
    response = await _get(auth_settings, headers={JWT_HEADER: garbage})
    assert response.status_code == 403
    assert patched_jwks["calls"] == 0


@pytest.mark.parametrize(
    ("overrides", "omit"),
    [
        pytest.param({"exp": int(time.time()) - 60}, (), id="expired"),
        pytest.param({}, ("exp",), id="no-exp"),
        pytest.param({}, ("iss",), id="no-iss"),
        pytest.param({}, ("aud",), id="no-aud"),
        pytest.param({"aud": "another-app"}, (), id="wrong-aud"),
        pytest.param({"aud": ["a", "b"]}, (), id="wrong-aud-list"),
        pytest.param({"iss": "https://evil.test/"}, (), id="wrong-iss"),
        pytest.param({"iss": TEST_ISSUER.rstrip("/")}, (), id="iss-no-slash"),
        pytest.param({"nbf": int(time.time()) + 600}, (), id="future-nbf"),
        pytest.param({"iat": int(time.time()) + 600}, (), id="future-iat"),
        pytest.param({"groups": ["homelab-family"]}, (), id="no-portal-group"),
        pytest.param({"groups": []}, (), id="empty-groups"),
        pytest.param({"groups": "fo-admin"}, (), id="groups-not-a-list"),
        pytest.param({"groups": ["FO-Admin"]}, (), id="group-case-differs"),
        pytest.param({}, ("groups",), id="no-groups"),
        pytest.param({}, ("preferred_username", "sub"), id="no-identity"),
        pytest.param({"preferred_username": "", "sub": ""}, (), id="empty-identity"),
        pytest.param(
            {"preferred_username": 42, "sub": None}, (), id="non-string-identity"
        ),
    ],
)
@pytest.mark.usefixtures("patched_jwks")
async def test_invalid_claims_are_rejected(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
    overrides: dict[str, Any],
    omit: tuple[str, ...],
) -> None:
    """Each claim-level failure returns 403."""
    token = jwt_factory(claims=overrides, omit=omit)
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 403


def test_exp_exactly_now_is_rejected(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
    jwt_factory: Callable[..., str],
) -> None:
    """Zero leeway: a token whose ``exp`` equals the current second is expired."""
    with freeze_time("2026-09-29 12:00:00"):
        now = int(time.time())
        token = jwt_factory(claims={"exp": now, "iat": now - 10})
        with pytest.raises(AuthError) as exc_info:
            validate_authentik_jwt(
                token,
                key=rsa_key_pair[1],
                issuer=TEST_ISSUER,
                audience=TEST_AUDIENCE,
            )
    assert exc_info.value.reason == "expired"


def test_exp_one_second_ahead_is_accepted(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
    jwt_factory: Callable[..., str],
) -> None:
    """The boundary is exact: one second of remaining life is still valid."""
    with freeze_time("2026-09-29 12:00:00"):
        now = int(time.time())
        token = jwt_factory(claims={"exp": now + 1, "iat": now})
        claims = validate_authentik_jwt(
            token,
            key=rsa_key_pair[1],
            issuer=TEST_ISSUER,
            audience=TEST_AUDIENCE,
        )
    assert claims["preferred_username"] == "viewer"


@pytest.mark.parametrize(
    ("overrides", "omit", "reason"),
    [
        pytest.param({"exp": 1}, (), "expired", id="expired"),
        pytest.param({}, ("exp",), "missing_claim", id="no-exp"),
        pytest.param({"aud": "x"}, (), "wrong_audience", id="wrong-aud"),
        pytest.param({"iss": "https://x/"}, (), "wrong_issuer", id="wrong-iss"),
        pytest.param(
            {"nbf": int(time.time()) + 600}, (), "not_yet_valid", id="future-nbf"
        ),
        pytest.param({"exp": "soon"}, (), "malformed_token", id="exp-not-int"),
        pytest.param({"iat": "later"}, (), "invalid_token", id="iat-not-int"),
    ],
)
def test_validation_failures_carry_a_reason_category(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
    jwt_factory: Callable[..., str],
    overrides: dict[str, Any],
    omit: tuple[str, ...],
    reason: str,
) -> None:
    """Each pyjwt failure maps to a loggable reason category."""
    token = jwt_factory(claims=overrides, omit=omit)
    with pytest.raises(AuthError) as exc_info:
        validate_authentik_jwt(
            token, key=rsa_key_pair[1], issuer=TEST_ISSUER, audience=TEST_AUDIENCE
        )
    assert exc_info.value.reason == reason


@pytest.mark.usefixtures("patched_jwks")
async def test_token_signed_by_wrong_key_is_rejected(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """A token signed by a key outside the JWKS, under a known kid, is 403."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt_factory(private_key=other)
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 403


@pytest.mark.usefixtures("patched_jwks")
async def test_tampered_signature_is_rejected(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """Changing one character inside the signature invalidates the token."""
    header, payload, signature = jwt_factory().split(".")
    middle = len(signature) // 2
    flipped = "A" if signature[middle] != "A" else "B"
    tampered = signature[:middle] + flipped + signature[middle + 1 :]
    token = f"{header}.{payload}.{tampered}"
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 403


@pytest.mark.usefixtures("patched_jwks")
async def test_tampered_payload_is_rejected(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """Swapping in an Admin payload under a Viewer signature is 403."""
    header, _payload, signature = jwt_factory().split(".")
    forged_payload = _b64url(json.dumps(_claims()).encode())
    token = f"{header}.{forged_payload}.{signature}"
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 403


async def test_alg_none_is_rejected(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
) -> None:
    """An unsigned ``alg=none`` token is refused before any key lookup."""
    token = (
        _unsigned_parts({"alg": "none", "typ": "JWT", "kid": TEST_KID}, _claims()) + "."
    )
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 403
    assert patched_jwks["calls"] == 0


async def test_hs256_signed_with_public_key_is_rejected(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> None:
    """Algorithm confusion: HS256 keyed with the RSA public key PEM is refused."""
    public_pem = _public_pem(rsa_key_pair[1])
    signing_input = _unsigned_parts(
        {"alg": "HS256", "typ": "JWT", "kid": TEST_KID}, _claims()
    )
    mac = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    token = f"{signing_input}.{_b64url(mac)}"

    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 403
    assert patched_jwks["calls"] == 0

    # jwt.decode enforces RS256 again, independent of the header pre-check.
    with pytest.raises(AuthError) as exc_info:
        validate_authentik_jwt(
            token, key=rsa_key_pair[1], issuer=TEST_ISSUER, audience=TEST_AUDIENCE
        )
    assert exc_info.value.reason == "disallowed_algorithm"


@pytest.mark.parametrize(
    "header",
    [
        pytest.param({"alg": "RS256", "typ": "JWT"}, id="no-kid"),
        pytest.param({"alg": "RS256", "typ": "JWT", "kid": ""}, id="empty-kid"),
        pytest.param({"alg": "RS256", "typ": "JWT", "kid": 7}, id="int-kid"),
    ],
)
async def test_missing_key_id_is_rejected(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
    header: dict[str, Any],
) -> None:
    """A token without a usable ``kid`` header is refused without a fetch."""
    token = _unsigned_parts(header, _claims()) + ".c2ln"
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 403
    assert patched_jwks["calls"] == 0


# --------------------------------------------------------------------------- #
# Plain identity headers are never trusted (#CRITICAL)
# --------------------------------------------------------------------------- #

_PLAIN_ADMIN_HEADERS = {
    "X-authentik-username": "admin",
    "X-authentik-groups": "fo-admin|fo-viewer",
    "X-authentik-email": "admin@example.com",
    "X-authentik-name": "Admin",
}


@pytest.mark.usefixtures("patched_jwks")
@pytest.mark.parametrize("path", ["/whoami", "/admin/thing"])
async def test_plain_identity_headers_without_jwt_are_rejected(
    auth_settings: AuthentikSettings,
    path: str,
) -> None:
    """Spoofable plain headers alone never authenticate a request."""
    response = await _get(auth_settings, path, dict(_PLAIN_ADMIN_HEADERS))
    assert response.status_code == 403


@pytest.mark.usefixtures("patched_jwks")
async def test_plain_headers_do_not_override_jwt_identity(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """With a valid Viewer JWT, plain Admin headers change nothing."""
    headers = {**_PLAIN_ADMIN_HEADERS, JWT_HEADER: jwt_factory(username="mom")}
    response = await _get(auth_settings, headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "username": "mom",
        "email": "mom@example.com",
        "name": "Mom",
        "role": "Viewer",
    }
    admin_response = await _get(auth_settings, "/admin/thing", headers)
    assert admin_response.status_code == 403


# --------------------------------------------------------------------------- #
# Public and admin paths
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("patched_jwks")
@pytest.mark.parametrize("path", ["/health", "/static/css/output.css"])
async def test_public_paths_need_no_token(
    auth_settings: AuthentikSettings,
    path: str,
) -> None:
    """``/health`` and ``/static/`` bypass authentication."""
    response = await _get(auth_settings, path)
    assert response.status_code == 200


@pytest.mark.usefixtures("patched_jwks")
async def test_public_match_is_exact_for_health(
    auth_settings: AuthentikSettings,
) -> None:
    """``/healthz`` is not ``/health``: it still needs a token."""
    response = await _get(auth_settings, "/healthz")
    assert response.status_code == 403


@pytest.mark.usefixtures("patched_jwks")
async def test_viewer_is_denied_admin_paths(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """A Viewer token gets 403 on ``/admin`` paths."""
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: jwt_factory()})
    assert response.status_code == 403


async def test_real_app_admin_routes_enforce_role(
    anon_client: AsyncClient,
    viewer_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """On the real app: Viewer 403, Admin 200 on a real route and 404 on none."""
    async with anon_client as ac:
        viewer = await ac.get("/admin/refresh-status", headers=viewer_headers)
        viewer_post = await ac.post(
            "/admin/refresh/entities", json={}, headers=viewer_headers
        )
        admin = await ac.get("/admin/refresh-status", headers=admin_headers)
        admin_missing = await ac.get("/admin/does-not-exist", headers=admin_headers)
    assert viewer.status_code == 403
    assert viewer_post.status_code == 403
    assert admin.status_code == 200
    assert admin_missing.status_code == 404


@pytest.mark.parametrize("path", ["/", "/documents", "/admin/refresh-status"])
async def test_real_app_rejects_anonymous_requests(
    anon_client: AsyncClient,
    path: str,
) -> None:
    """On the real app, protected routes need a token."""
    async with anon_client as ac:
        response = await ac.get(path)
    assert response.status_code == 403


async def test_real_app_health_is_public(anon_client: AsyncClient) -> None:
    """On the real app, ``/health`` answers without a token."""
    async with anon_client as ac:
        response = await ac.get("/health")
    assert response.status_code == 200


async def test_real_app_viewer_reaches_sections(
    anon_client: AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """On the real app, a Viewer token loads a section page."""
    async with anon_client as ac:
        response = await ac.get("/", headers=viewer_headers)
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# JWKS fetching and caching
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("patched_jwks")
async def test_jwks_fetch_failure_denies_access(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable JWKS endpoint fails closed with 403."""
    real_fetch = fetch_authentik_jwks

    async def _unreachable(url: str) -> dict[str, Any]:
        def _refuse(request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg, request=request)

        return await real_fetch(url, transport=httpx.MockTransport(_refuse))

    monkeypatch.setattr(authentik, "fetch_authentik_jwks", _unreachable)
    response = await _get(auth_settings, headers={JWT_HEADER: jwt_factory()})
    assert response.status_code == 403


async def test_jwks_is_cached_between_requests(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
    jwt_factory: Callable[..., str],
) -> None:
    """Keys are fetched once and reused for later requests."""
    async with _client(auth_settings) as ac:
        first = await ac.get("/whoami", headers={JWT_HEADER: jwt_factory()})
        second = await ac.get("/whoami", headers={JWT_HEADER: jwt_factory()})
    assert (first.status_code, second.status_code) == (200, 200)
    assert patched_jwks["calls"] == 1


async def test_concurrent_cold_requests_share_one_fetch(
    auth_settings: AuthentikSettings,
    monkeypatch: pytest.MonkeyPatch,
    jwks_document: dict[str, Any],
    jwt_factory: Callable[..., str],
) -> None:
    """The cache lock collapses a burst of cold-cache requests into one fetch."""
    calls = 0

    async def _slow_fetch(url: str) -> dict[str, Any]:
        nonlocal calls
        del url
        calls += 1
        await asyncio.sleep(0.01)
        return jwks_document

    monkeypatch.setattr(authentik, "fetch_authentik_jwks", _slow_fetch)
    token = jwt_factory()
    async with _client(auth_settings) as ac:
        responses = await asyncio.gather(
            *(ac.get("/whoami", headers={JWT_HEADER: token}) for _ in range(5))
        )
    assert [r.status_code for r in responses] == [200] * 5
    assert calls == 1


async def test_jwks_is_refetched_after_ttl(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
) -> None:
    """Once the TTL passes, the next lookup refetches the key set."""
    clock = FakeClock()
    cache = JwksCache(auth_settings.jwks_url, ttl_seconds=600, clock=clock)
    await cache.get_key(TEST_KID)
    clock.now += 599
    await cache.get_key(TEST_KID)
    assert patched_jwks["calls"] == 1
    clock.now += 1
    await cache.get_key(TEST_KID)
    assert patched_jwks["calls"] == 2


async def test_unknown_kid_triggers_one_rate_limited_refetch(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
    make_jwks: MakeJwks,
    jwt_factory: Callable[..., str],
) -> None:
    """Key rotation: an unknown kid refetches once, then waits out the window."""
    clock = FakeClock()
    cache = JwksCache(auth_settings.jwks_url, ttl_seconds=600, clock=clock)
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_token = jwt_factory(kid="rotated-kid", private_key=rotated)

    async with _client(auth_settings, cache=cache) as ac:
        # Prime the cache with the original key set.
        assert (
            await ac.get("/whoami", headers={JWT_HEADER: jwt_factory()})
        ).status_code == 200
        # Authentik rotates its key; the portal has not seen it yet.
        patched_jwks["document"] = make_jwks(
            [(rsa_key_pair[1], TEST_KID), (rotated.public_key(), "rotated-kid")]
        )
        # Inside the rate-limit window the unknown kid is refused, no fetch.
        clock.now += 10
        early = await ac.get("/whoami", headers={JWT_HEADER: rotated_token})
        assert early.status_code == 403
        assert patched_jwks["calls"] == 1
        # After the window, one refetch picks up the rotated key.
        clock.now += 30
        late = await ac.get("/whoami", headers={JWT_HEADER: rotated_token})
        assert late.status_code == 200
        assert patched_jwks["calls"] == 2
        # A second unknown kid right away does not trigger another fetch.
        bogus = jwt_factory(kid="never-published", private_key=rotated)
        denied = await ac.get("/whoami", headers={JWT_HEADER: bogus})
        assert denied.status_code == 403
        assert patched_jwks["calls"] == 2


async def test_empty_jwks_is_cached_not_refetched_every_request(
    auth_settings: AuthentikSettings,
    patched_jwks: dict[str, Any],
) -> None:
    """A JWKS with no usable keys still counts as a fetch for rate limiting."""
    patched_jwks["document"] = {"keys": []}
    cache = JwksCache(auth_settings.jwks_url, ttl_seconds=600, clock=FakeClock())
    for _ in range(3):
        with pytest.raises(AuthError) as exc_info:
            await cache.get_key(TEST_KID)
        assert exc_info.value.reason == "unknown_key"
    assert patched_jwks["calls"] == 1


async def test_fetch_returns_json_document() -> None:
    """The real fetcher returns the parsed JWKS object."""

    def _serve(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        return httpx.Response(200, json={"keys": []})

    document = await fetch_authentik_jwks(
        "https://auth.test/jwks/", transport=httpx.MockTransport(_serve)
    )
    assert document == {"keys": []}


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500, json={"keys": []}), id="server-error"),
        pytest.param(
            httpx.Response(302, headers={"Location": "http://evil.test/"}),
            id="redirect-not-followed",
        ),
        pytest.param(httpx.Response(200, text="<html>"), id="not-json"),
        pytest.param(httpx.Response(200, json=["keys"]), id="not-an-object"),
    ],
)
async def test_fetch_failures_raise_jwks_unavailable(response: httpx.Response) -> None:
    """Bad JWKS responses raise ``AuthError('jwks_unavailable')``."""
    transport = httpx.MockTransport(lambda _request: response)
    with pytest.raises(AuthError) as exc_info:
        await fetch_authentik_jwks("https://auth.test/jwks/", transport=transport)
    assert exc_info.value.reason == "jwks_unavailable"


def test_parse_jwks_keeps_only_usable_rs256_keys(
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
    make_jwks: MakeJwks,
) -> None:
    """Non-RSA, encryption, other-alg, kid-less, weak, and malformed keys drop."""
    public = rsa_key_pair[1]
    good = make_jwks([(public, "good")])["keys"][0]
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    document = {
        "keys": [
            good,
            {**good, "kid": "enc", "use": "enc"},
            {**good, "kid": "ps256", "alg": "PS256"},
            {**good, "kid": ""},
            {k: v for k, v in good.items() if k != "kid"},
            {"kty": "EC", "kid": "ec", "crv": "P-256", "x": "AA", "y": "AA"},
            {**good, "kid": "broken", "n": "!!!"},
            make_jwks([(weak.public_key(), "weak")])["keys"][0],
            "not-a-dict",
        ]
    }
    keys = parse_jwks(document)
    assert list(keys) == ["good"]
    assert keys["good"].public_numbers() == public.public_numbers()


@pytest.mark.parametrize("document", [{}, {"keys": "nope"}, {"keys": None}])
def test_parse_jwks_tolerates_missing_key_list(document: dict[str, Any]) -> None:
    """A JWKS without a key list yields no keys rather than an exception."""
    assert parse_jwks(document) == {}


# --------------------------------------------------------------------------- #
# Role mapping and principal
# --------------------------------------------------------------------------- #


def test_role_from_groups() -> None:
    """Admin beats Viewer; unrelated groups grant nothing."""
    kwargs = {"viewer_group": "fo-viewer", "admin_group": "fo-admin"}
    assert role_from_groups(["homelab-family", "fo-viewer"], **kwargs) is Role.VIEWER
    assert role_from_groups(["fo-viewer", "fo-admin"], **kwargs) is Role.ADMIN
    assert role_from_groups(["homelab-family"], **kwargs) is None


def test_principal_from_claims_ignores_non_string_groups(
    auth_settings: AuthentikSettings,
) -> None:
    """Non-string group entries are skipped, not coerced."""
    principal = principal_from_claims(
        {"sub": "u1", "groups": [1, None, "fo-viewer"]}, auth_settings
    )
    assert principal == Principal(
        username="u1", email=None, name=None, role=Role.VIEWER
    )
    assert not principal.is_admin


# --------------------------------------------------------------------------- #
# Logging never includes the token
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("patched_jwks")
async def test_denial_logs_reason_but_never_the_token(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """Auth failures log a reason category; token and email never appear."""
    token = jwt_factory(claims={"aud": "another-app"})
    viewer = jwt_factory()
    with capture_logs() as logs:
        await _get(auth_settings, headers={JWT_HEADER: token})
        await _get(auth_settings, "/admin/thing", {JWT_HEADER: viewer})
    assert [(e["event"], e["reason"]) for e in logs] == [
        ("auth_denied", "wrong_audience"),
        ("auth_denied", "admin_required"),
    ]
    flattened = json.dumps(logs)
    assert token not in flattened
    assert viewer not in flattened
    assert "@example.com" not in flattened


# --------------------------------------------------------------------------- #
# Non-HTTP ASGI scopes
# --------------------------------------------------------------------------- #


async def test_websocket_connections_are_refused(
    auth_settings: AuthentikSettings,
) -> None:
    """The portal serves no WebSockets, so every connection attempt is closed."""
    sent: list[Message] = []
    reached: list[str] = []

    async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        reached.append(scope["type"])

    async def _receive() -> Message:
        return {"type": "websocket.connect"}

    async def _send(message: Message) -> None:
        sent.append(message)

    middleware = AuthentikAuthMiddleware(_inner, settings=auth_settings)
    await middleware({"type": "websocket", "path": "/ws"}, _receive, _send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert reached == []


async def test_lifespan_scope_passes_through(
    auth_settings: AuthentikSettings,
) -> None:
    """Lifespan events reach the app untouched."""
    reached: list[str] = []

    async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive, send
        reached.append(scope["type"])

    async def _noop_receive() -> Message:
        return {"type": "lifespan.startup"}

    async def _noop_send(message: Message) -> None:
        del message

    middleware = AuthentikAuthMiddleware(_inner, settings=auth_settings)
    await middleware({"type": "lifespan"}, _noop_receive, _noop_send)
    assert reached == ["lifespan"]


# --------------------------------------------------------------------------- #
# Settings and startup
# --------------------------------------------------------------------------- #

_BASE_ENV = {
    "AUTHENTIK_JWKS_URL": "https://auth.test/jwks/",
    "AUTHENTIK_ISSUER": TEST_ISSUER,
    "AUTHENTIK_AUDIENCE": TEST_AUDIENCE,
}


def test_settings_defaults() -> None:
    """Optional variables fall back to their documented defaults."""
    settings = AuthentikSettings.from_env(_BASE_ENV)
    assert settings == AuthentikSettings(
        jwks_url="https://auth.test/jwks/",
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
        admin_group="fo-admin",
        viewer_group="fo-viewer",
        jwks_cache_seconds=600,
    )


def test_settings_overrides() -> None:
    """Optional variables override the defaults; blank values are ignored."""
    env = {
        **_BASE_ENV,
        "AUTHENTIK_JWKS_URL": "HTTPS://auth.test/jwks/",
        "FO_ADMIN_GROUP": "estate-admins",
        "FO_VIEWER_GROUP": "  ",
        "AUTHENTIK_JWKS_CACHE_SECONDS": "120",
    }
    settings = AuthentikSettings.from_env(env)
    assert settings.admin_group == "estate-admins"
    assert settings.viewer_group == "fo-viewer"
    assert settings.jwks_cache_seconds == 120


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        pytest.param({"AUTHENTIK_ISSUER": " "}, "AUTHENTIK_ISSUER", id="blank-iss"),
        pytest.param({"AUTHENTIK_AUDIENCE": ""}, "AUTHENTIK_AUDIENCE", id="no-aud"),
        pytest.param(
            {"AUTHENTIK_JWKS_URL": "http://auth.test/jwks/"},
            "AUTHENTIK_JWKS_URL",
            id="http-jwks",
        ),
        pytest.param(
            {"AUTHENTIK_JWKS_URL": "file:///etc/jwks.json"},
            "AUTHENTIK_JWKS_URL",
            id="file-jwks",
        ),
        pytest.param(
            {"AUTHENTIK_JWKS_URL": "https:///jwks/"},
            "AUTHENTIK_JWKS_URL",
            id="no-host",
        ),
        pytest.param(
            {"FO_ADMIN_GROUP": "family", "FO_VIEWER_GROUP": "family"},
            "FO_ADMIN_GROUP",
            id="same-groups",
        ),
        pytest.param(
            {"AUTHENTIK_JWKS_CACHE_SECONDS": "ten"},
            "AUTHENTIK_JWKS_CACHE_SECONDS",
            id="ttl-not-int",
        ),
        pytest.param(
            {"AUTHENTIK_JWKS_CACHE_SECONDS": "0"},
            "AUTHENTIK_JWKS_CACHE_SECONDS",
            id="ttl-zero",
        ),
    ],
)
def test_settings_reject_invalid_values(
    overrides: dict[str, str],
    variable: str,
) -> None:
    """Invalid settings raise with the variable name in the message."""
    with pytest.raises(AuthConfigError, match=variable):
        AuthentikSettings.from_env({**_BASE_ENV, **overrides})


@pytest.mark.usefixtures("portal_env")
@pytest.mark.parametrize(
    "url",
    ["http://auth.test/jwks/", "file:///etc/jwks.json", "auth.test/jwks/"],
)
def test_non_https_jwks_url_exits_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    """``app.main`` exits 1 and names the variable when the JWKS URL is not https."""
    main = importlib.import_module("app.main")
    monkeypatch.setenv("AUTHENTIK_JWKS_URL", url)
    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1
    assert "AUTHENTIK_JWKS_URL" in capsys.readouterr().err


@pytest.mark.usefixtures("portal_env")
@pytest.mark.parametrize(
    "variable",
    ["AUTHENTIK_JWKS_URL", "AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE"],
)
def test_missing_auth_env_var_exits_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    """``app.main`` exits 1 and names any missing Authentik variable."""
    main = importlib.import_module("app.main")
    monkeypatch.delenv(variable)
    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1
    assert variable in capsys.readouterr().err


@pytest.mark.usefixtures("portal_env")
def test_optional_setting_error_exits_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid optional setting also stops startup with a named message."""
    main = importlib.import_module("app.main")
    monkeypatch.setenv("AUTHENTIK_JWKS_CACHE_SECONDS", "-5")
    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1
    assert "AUTHENTIK_JWKS_CACHE_SECONDS" in capsys.readouterr().err
