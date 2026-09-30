# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Unit tests for the Authentik forward-auth middleware (ADR-005).

Contract (``CLAUDE.md`` "Authentication rules", ADR-005, tech-spec section 6):

1. Identity comes only from the signed ``X-authentik-jwt`` header; the plain
   ``X-authentik-*`` identity headers are ignored.
2. The token must be HS256 only, signed with the provider's client secret
   (``AUTHENTIK_JWT_SECRET``), with a matching ``iss`` and ``aud``, an
   ``exp`` not in the past and no ``nbf``/``iat`` in the future (each within
   a small clock-skew leeway), and a non-empty identity claim. Any other
   algorithm, including a validly signed asymmetric token, is refused.
3. ``fo-admin`` grants Admin, ``fo-viewer`` grants Viewer, anyone else is 403.
4. Every failure is 403. ``/health`` and ``/static/`` are public; ``/admin``
   and ``/admin/...`` need Admin. Paths are judged after ``root_path`` is
   stripped.
5. The secret is required at startup and refused when it is the stack
   placeholder, shorter than 32 characters, or padded with whitespace; error
   messages name the variable and never print the value.

Most tests run the middleware in front of a tiny echo app so the resulting
principal can be asserted; a few run against the real ``app.main`` app.
Secrets are generated at runtime; no secret literal is committed.
"""

from __future__ import annotations

import base64
import importlib
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from structlog.testing import capture_logs

from app.middleware.authentik import (
    ALLOWED_ALGORITHMS,
    JWT_LEEWAY_SECONDS,
    MIN_JWT_SECRET_LENGTH,
    STACK_PLACEHOLDER_VALUE,
    AuthConfigError,
    AuthentikAuthMiddleware,
    AuthentikSettings,
    AuthError,
    Principal,
    Role,
    principal_from_claims,
    role_from_groups,
    validate_authentik_jwt,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.requests import Request
    from starlette.types import Message, Receive, Scope, Send

TEST_ISSUER = "https://auth.test/application/o/family-office-portal/"
TEST_AUDIENCE = "test-client-id"
JWT_HEADER = "X-authentik-jwt"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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
    root_path: str = "",
) -> AsyncClient:
    wrapped = AuthentikAuthMiddleware(_echo_app(), settings=settings)
    transport = ASGITransport(app=wrapped, root_path=root_path)
    return AsyncClient(transport=transport, base_url="http://test")


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
        "iat": now - 30,
        "exp": now + 3600,
        **overrides,
    }


def _unsigned_parts(header: dict[str, Any], payload: dict[str, Any]) -> str:
    return (
        _b64url(json.dumps(header).encode())
        + "."
        + _b64url(json.dumps(payload).encode())
    )


def _validate(token: str, secret: str) -> dict[str, Any]:
    return validate_authentik_jwt(
        token, key=secret, issuer=TEST_ISSUER, audience=TEST_AUDIENCE
    )


async def _get(
    settings: AuthentikSettings,
    path: str = "/whoami",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async with _client(settings) as ac:
        return await ac.get(path, headers=headers)


# --------------------------------------------------------------------------- #
# Valid tokens
# --------------------------------------------------------------------------- #


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


async def test_valid_admin_token_is_accepted_on_admin_path(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """An Admin token reaches ``/admin`` paths with the Admin role."""
    token = jwt_factory(username="admin", claims={"groups": ["fo-admin"]})
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 200
    assert response.json()["role"] == "Admin"


async def test_admin_wins_when_user_is_in_both_groups(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """Membership in both portal groups yields Admin."""
    token = jwt_factory(claims={"groups": ["fo-viewer", "fo-admin"]})
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.json()["role"] == "Admin"


async def test_audience_list_containing_client_id_is_accepted(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """An ``aud`` array that contains the configured audience is accepted."""
    token = jwt_factory(claims={"aud": ["other-app", TEST_AUDIENCE]})
    response = await _get(auth_settings, headers={JWT_HEADER: token})
    assert response.status_code == 200


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


async def test_configured_group_names_are_honoured(
    jwt_secret: str,
    jwt_factory: Callable[..., str],
) -> None:
    """``FO_ADMIN_GROUP`` / ``FO_VIEWER_GROUP`` replace the default names."""
    settings = AuthentikSettings(
        jwt_secret=jwt_secret,
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


async def test_missing_header_is_rejected(auth_settings: AuthentikSettings) -> None:
    """No ``X-authentik-jwt`` header means no access."""
    response = await _get(auth_settings)
    assert response.status_code == 403
    assert response.text == "Access denied"


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
    garbage: str,
) -> None:
    """Malformed tokens are rejected."""
    response = await _get(auth_settings, headers={JWT_HEADER: garbage})
    assert response.status_code == 403


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


_FROZEN_NOW = "2026-09-29 12:00:00"
_LEEWAY = JWT_LEEWAY_SECONDS


@pytest.mark.parametrize(
    ("offsets", "reason"),
    [
        pytest.param({"exp": 0}, None, id="exp-now-accepted"),
        pytest.param({"exp": -_LEEWAY + 1}, None, id="exp-inside-leeway-accepted"),
        pytest.param({"exp": -_LEEWAY}, "expired", id="exp-at-leeway-rejected"),
        pytest.param({"exp": -3600}, "expired", id="exp-long-past-rejected"),
        pytest.param({"nbf": _LEEWAY}, None, id="nbf-at-leeway-accepted"),
        pytest.param(
            {"nbf": _LEEWAY + 1}, "not_yet_valid", id="nbf-beyond-leeway-rejected"
        ),
        pytest.param({"iat": _LEEWAY}, None, id="iat-at-leeway-accepted"),
        pytest.param(
            {"iat": _LEEWAY + 1}, "not_yet_valid", id="iat-beyond-leeway-rejected"
        ),
    ],
)
def test_time_claims_allow_only_the_clock_skew_leeway(
    jwt_secret: str,
    jwt_factory: Callable[..., str],
    offsets: dict[str, int],
    reason: str | None,
) -> None:
    """Time-claim boundaries sit exactly ``JWT_LEEWAY_SECONDS`` from now.

    ``offsets`` are seconds relative to a frozen now; ``reason`` is the
    expected refusal, or ``None`` when the token must be accepted.
    """
    with freeze_time(_FROZEN_NOW):
        now = int(time.time())
        claims = {"iat": now - 30, "exp": now + 3600}
        claims.update({name: now + delta for name, delta in offsets.items()})
        token = jwt_factory(claims=claims)
        if reason is None:
            verified = _validate(token, jwt_secret)
            assert verified["preferred_username"] == "viewer"
            return
        with pytest.raises(AuthError) as exc_info:
            _validate(token, jwt_secret)
    assert exc_info.value.reason == reason


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
    jwt_secret: str,
    jwt_factory: Callable[..., str],
    overrides: dict[str, Any],
    omit: tuple[str, ...],
    reason: str,
) -> None:
    """Each pyjwt failure maps to a loggable reason category."""
    token = jwt_factory(claims=overrides, omit=omit)
    with pytest.raises(AuthError) as exc_info:
        _validate(token, jwt_secret)
    assert exc_info.value.reason == reason


async def test_token_signed_with_a_different_secret_is_rejected(
    auth_settings: AuthentikSettings,
    jwt_secret: str,
    jwt_factory: Callable[..., str],
) -> None:
    """An HS256 token signed with any other secret is 403 (``bad_signature``)."""
    other_secret = secrets.token_urlsafe(64)
    assert other_secret != jwt_secret
    token = jwt_factory(
        username="admin", secret=other_secret, claims={"groups": ["fo-admin"]}
    )
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 403
    with pytest.raises(AuthError) as exc_info:
        _validate(token, jwt_secret)
    assert exc_info.value.reason == "bad_signature"


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


def test_only_hs256_is_allowed() -> None:
    """The algorithm pin is a single symmetric algorithm, nothing else."""
    assert ALLOWED_ALGORITHMS == ("HS256",)


async def test_alg_none_is_rejected(
    auth_settings: AuthentikSettings,
    jwt_secret: str,
) -> None:
    """An unsigned ``alg=none`` token is 403 (``disallowed_algorithm``)."""
    token = _unsigned_parts({"alg": "none", "typ": "JWT"}, _claims()) + "."
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 403
    with pytest.raises(AuthError) as exc_info:
        _validate(token, jwt_secret)
    assert exc_info.value.reason == "disallowed_algorithm"


async def test_validly_signed_rs256_token_is_rejected(
    auth_settings: AuthentikSettings,
    jwt_secret: str,
) -> None:
    """A genuine RS256 signature with perfect claims is still refused.

    The token verifies under its own RSA public key, so the refusal comes
    from the HS256 pin alone. Accepting an asymmetric algorithm next to a
    symmetric one is what makes algorithm confusion possible.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(_claims(), private_key, algorithm="RS256")
    verified = jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["RS256"],
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
    )
    assert verified["groups"] == ["fo-admin"]

    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: token})
    assert response.status_code == 403
    with pytest.raises(AuthError) as exc_info:
        _validate(token, jwt_secret)
    assert exc_info.value.reason == "disallowed_algorithm"


# --------------------------------------------------------------------------- #
# Plain identity headers are never trusted (#CRITICAL)
# --------------------------------------------------------------------------- #

_PLAIN_ADMIN_HEADERS = {
    "X-authentik-username": "admin",
    "X-authentik-groups": "fo-admin|fo-viewer",
    "X-authentik-email": "admin@example.com",
    "X-authentik-name": "Admin",
}


@pytest.mark.parametrize("path", ["/whoami", "/admin/thing"])
async def test_plain_identity_headers_without_jwt_are_rejected(
    auth_settings: AuthentikSettings,
    path: str,
) -> None:
    """Spoofable plain headers alone never authenticate a request."""
    response = await _get(auth_settings, path, dict(_PLAIN_ADMIN_HEADERS))
    assert response.status_code == 403


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


@pytest.mark.parametrize("path", ["/health", "/static/css/output.css"])
async def test_public_paths_need_no_token(
    auth_settings: AuthentikSettings,
    path: str,
) -> None:
    """``/health`` and ``/static/`` bypass authentication."""
    response = await _get(auth_settings, path)
    assert response.status_code == 200


async def test_public_match_is_exact_for_health(
    auth_settings: AuthentikSettings,
) -> None:
    """``/healthz`` is not ``/health``: it still needs a token."""
    response = await _get(auth_settings, "/healthz")
    assert response.status_code == 403


async def test_viewer_is_denied_admin_paths(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """A Viewer token gets 403 on ``/admin`` paths."""
    response = await _get(auth_settings, "/admin/thing", {JWT_HEADER: jwt_factory()})
    assert response.status_code == 403


async def test_root_path_does_not_hide_admin_paths_from_the_role_check(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """Under ``root_path``, the admin gate sees the routed path (H1 regression).

    With ``root_path="/portal"`` the router serves ``/portal/admin/thing`` as
    ``/admin/thing``, so a Viewer must still get 403 there and an Admin must
    still get through.
    """
    viewer = jwt_factory()
    admin = jwt_factory(username="admin", claims={"groups": ["fo-admin"]})
    async with _client(auth_settings, root_path="/portal") as ac:
        viewer_response = await ac.get(
            "/portal/admin/thing", headers={JWT_HEADER: viewer}
        )
        admin_response = await ac.get(
            "/portal/admin/thing", headers={JWT_HEADER: admin}
        )
        viewer_section = await ac.get("/portal/whoami", headers={JWT_HEADER: viewer})
    assert viewer_response.status_code == 403
    assert admin_response.status_code == 200
    assert admin_response.json()["role"] == "Admin"
    assert viewer_section.status_code == 200


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("/portal/health", 200, id="health-under-root-path"),
        pytest.param("/portal/static/app.css", 200, id="static-under-root-path"),
        pytest.param("/health", 200, id="path-outside-root-path"),
        pytest.param("/portal", 403, id="bare-root-path"),
        pytest.param("/portalx/health", 403, id="root-path-not-a-segment"),
        pytest.param("/portal/admin/thing", 403, id="admin-under-root-path"),
    ],
)
async def test_root_path_public_paths_without_token(
    auth_settings: AuthentikSettings,
    path: str,
    expected: int,
) -> None:
    """Public paths are judged on the routed path; nothing else leaks."""
    async with _client(auth_settings, root_path="/portal") as ac:
        response = await ac.get(path)
    assert response.status_code == expected


async def test_admin_match_is_on_a_segment_boundary(
    auth_settings: AuthentikSettings,
    jwt_factory: Callable[..., str],
) -> None:
    """``/adminX`` is an ordinary protected path: no token 403, Viewer passes."""
    anonymous = await _get(auth_settings, "/adminX")
    viewer = await _get(auth_settings, "/adminX", {JWT_HEADER: jwt_factory()})
    viewer_bare_admin = await _get(auth_settings, "/admin", {JWT_HEADER: jwt_factory()})
    assert anonymous.status_code == 403
    # Past the middleware; the echo app has no such route.
    assert viewer.status_code == 404
    assert viewer_bare_admin.status_code == 403


@pytest.mark.usefixtures("portal_env")
async def test_real_app_under_root_path_enforces_admin_role(
    viewer_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """The reviewer's probe on the real app: Viewer 403, Admin 200, health 200."""
    app = importlib.reload(importlib.import_module("app.main")).app
    transport = ASGITransport(app=app, root_path="/portal")
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        viewer = await ac.get("/portal/admin/refresh-status", headers=viewer_headers)
        admin = await ac.get("/portal/admin/refresh-status", headers=admin_headers)
        health = await ac.get("/portal/health")
    assert viewer.status_code == 403
    assert admin.status_code == 200
    assert health.status_code == 200


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

_JWT_ENV_NAME = "AUTHENTIK_JWT_SECRET"


@pytest.fixture
def base_env(jwt_secret: str) -> dict[str, str]:
    """Return the minimal valid Authentik environment.

    Args:
        jwt_secret: Random per-session HS256 secret.

    Returns:
        dict[str, str]: Secret, issuer and audience variables.
    """
    return {
        _JWT_ENV_NAME: jwt_secret,
        "AUTHENTIK_ISSUER": TEST_ISSUER,
        "AUTHENTIK_AUDIENCE": TEST_AUDIENCE,
    }


def test_settings_defaults(base_env: dict[str, str], jwt_secret: str) -> None:
    """Optional variables fall back to their documented defaults."""
    settings = AuthentikSettings.from_env(base_env)
    assert settings == AuthentikSettings(
        jwt_secret=jwt_secret,
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
        admin_group="fo-admin",
        viewer_group="fo-viewer",
    )


def test_settings_overrides(base_env: dict[str, str]) -> None:
    """Optional variables override the defaults; blank values are ignored."""
    env = {
        **base_env,
        "FO_ADMIN_GROUP": "estate-admins",
        "FO_VIEWER_GROUP": "  ",
    }
    settings = AuthentikSettings.from_env(env)
    assert settings.admin_group == "estate-admins"
    assert settings.viewer_group == "fo-viewer"


def test_settings_repr_never_shows_the_secret(base_env: dict[str, str]) -> None:
    """``repr`` (and so any settings dump or assertion diff) omits the secret."""
    settings = AuthentikSettings.from_env(base_env)
    assert base_env[_JWT_ENV_NAME] not in repr(settings)
    assert "jwt_secret" not in repr(settings)


def test_secret_of_exactly_the_minimum_length_is_accepted(
    base_env: dict[str, str],
) -> None:
    """A secret of exactly ``MIN_JWT_SECRET_LENGTH`` characters is kept as is."""
    secret = secrets.token_urlsafe(64)[:MIN_JWT_SECRET_LENGTH]
    assert len(secret) == 32
    settings = AuthentikSettings.from_env({**base_env, _JWT_ENV_NAME: secret})
    assert settings.jwt_secret == secret


@pytest.mark.parametrize(
    "make_secret",
    [
        pytest.param(lambda s: s[: MIN_JWT_SECRET_LENGTH - 1], id="31-chars"),
        pytest.param(lambda s: s[:8], id="short"),
        pytest.param(lambda _s: STACK_PLACEHOLDER_VALUE, id="placeholder"),
        pytest.param(lambda s: f" {s}", id="leading-space"),
        pytest.param(lambda s: f"{s}\n", id="trailing-newline"),
        pytest.param(lambda _s: "   ", id="whitespace-only"),
        pytest.param(lambda _s: "", id="empty"),
    ],
)
def test_invalid_secret_is_refused_without_echoing_it(
    base_env: dict[str, str],
    make_secret: Callable[[str], str],
) -> None:
    """Short, placeholder and padded secrets raise, naming only the variable."""
    secret = make_secret(secrets.token_urlsafe(64))
    with pytest.raises(AuthConfigError, match=_JWT_ENV_NAME) as exc_info:
        AuthentikSettings.from_env({**base_env, _JWT_ENV_NAME: secret})
    if secret.strip():
        assert secret.strip() not in str(exc_info.value)


def test_missing_secret_is_refused(base_env: dict[str, str]) -> None:
    """An absent ``AUTHENTIK_JWT_SECRET`` raises, naming the variable."""
    env = {k: v for k, v in base_env.items() if k != _JWT_ENV_NAME}
    with pytest.raises(AuthConfigError, match=_JWT_ENV_NAME):
        AuthentikSettings.from_env(env)


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [
        pytest.param({"AUTHENTIK_ISSUER": " "}, "AUTHENTIK_ISSUER", id="blank-iss"),
        pytest.param({"AUTHENTIK_AUDIENCE": ""}, "AUTHENTIK_AUDIENCE", id="no-aud"),
        pytest.param(
            {"FO_ADMIN_GROUP": "family", "FO_VIEWER_GROUP": "family"},
            "FO_ADMIN_GROUP",
            id="same-groups",
        ),
    ],
)
def test_settings_reject_invalid_values(
    base_env: dict[str, str],
    overrides: dict[str, str],
    variable: str,
) -> None:
    """Invalid settings raise with the variable name in the message."""
    with pytest.raises(AuthConfigError, match=variable):
        AuthentikSettings.from_env({**base_env, **overrides})


@pytest.mark.usefixtures("portal_env")
@pytest.mark.parametrize(
    "make_secret",
    [
        pytest.param(lambda s: s[: MIN_JWT_SECRET_LENGTH - 1], id="31-chars"),
        pytest.param(lambda _s: STACK_PLACEHOLDER_VALUE, id="placeholder"),
        pytest.param(lambda s: f"{s} ", id="trailing-space"),
    ],
)
def test_invalid_secret_exits_at_startup_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make_secret: Callable[[str], str],
) -> None:
    """``app.main`` exits 1, names the variable and never prints the value."""
    main = importlib.import_module("app.main")
    secret = make_secret(secrets.token_urlsafe(64))
    monkeypatch.setenv(_JWT_ENV_NAME, secret)
    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert _JWT_ENV_NAME in err
    assert secret.strip() not in err


@pytest.mark.usefixtures("portal_env")
@pytest.mark.parametrize(
    "variable",
    [_JWT_ENV_NAME, "AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE"],
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
    monkeypatch.setenv("FO_ADMIN_GROUP", "family")
    monkeypatch.setenv("FO_VIEWER_GROUP", "family")
    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1
    assert "FO_ADMIN_GROUP" in capsys.readouterr().err
