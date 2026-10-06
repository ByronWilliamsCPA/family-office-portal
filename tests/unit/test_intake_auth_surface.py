# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""The JWT-free intake exemption: ``POST /api/v1/balances``.

Besides the public ``/health`` and ``/static/`` paths, this is the only
request the middleware passes without a JWT. A machine collector cannot
present a signed-in identity, so the middleware lets exactly one method and
path through without it; the route itself then
demands the intake key. These tests drive the middleware directly with raw
ASGI scopes, so the path reaches it exactly as written (a test client would
tidy dot segments first), and record which requests reach the inner app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from app.middleware.authentik import AuthentikAuthMiddleware, AuthentikSettings

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import Receive, Scope, Send

INTAKE = "/api/v1/balances"


async def _call(
    settings: AuthentikSettings,
    *,
    method: str = "POST",
    path: str = INTAKE,
    headers: dict[str, str] | None = None,
    scope_extra: dict[str, Any] | None = None,
) -> tuple[int | None, list[dict[str, Any]]]:
    """Send one request through the middleware; return status and inner calls."""
    reached: list[dict[str, Any]] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        reached.append({"path": scope["path"], "state": dict(scope.get("state", {}))})
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:  # noqa: ANN401
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        **(scope_extra or {}),
    }
    middleware = AuthentikAuthMiddleware(inner, settings=settings)
    await middleware(cast("Scope", scope), receive, send)
    status = next(
        (m["status"] for m in sent if m["type"] == "http.response.start"), None
    )
    return status, reached


async def test_post_to_the_intake_path_reaches_the_app_without_a_jwt(
    auth_settings: AuthentikSettings,
) -> None:
    """The exact path with POST is passed on; the route enforces the key."""
    status, reached = await _call(auth_settings)
    assert status == 204
    assert len(reached) == 1
    assert "principal" not in reached[0]["state"]


@pytest.mark.parametrize(
    "method", ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "post"]
)
async def test_other_methods_on_the_intake_path_need_a_jwt(
    auth_settings: AuthentikSettings, method: str
) -> None:
    """Only an upper-case POST is exempt."""
    status, reached = await _call(auth_settings, method=method)
    assert status == 403
    assert reached == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1",
        "/api/v1/",
        "/api/v1/other",
        "/api/v1/entities",
        "/api/v1/balance",
        "/api/v1/balances/",
        "/api/v1/balances/extra",
        "/api/v1/balances/1",
        "/api/v1/balancesX",
        "/api/v1/balances.json",
        "/api/v1/balances;x=1",
        "/api/v1/balances%2F",
        "/api/v1/balances%00",
        "/api/v1/balances ",
        "/API/V1/BALANCES",
        "/Api/v1/balances",
        "//api/v1/balances",
        "/api//v1/balances",
        "/api/v1//balances",
        "/api/v1/./balances",
        "/api/v1/../v1/balances",
        "/api/v1/x/../balances",
        "/api/v2/balances",
        "/x/api/v1/balances",
        "/admin/api/v1/balances",
        "/api/v1/balances/../../admin/refresh-status",
        "/admin/refresh/entities",
        "/",
        "",
    ],
)
async def test_variants_of_the_path_need_a_jwt(
    auth_settings: AuthentikSettings, path: str
) -> None:
    """Prefixes, trailing segments, case changes and traversal are not exempt."""
    status, reached = await _call(auth_settings, path=path)
    assert status == 403
    assert reached == []


async def test_other_api_paths_still_require_the_jwt_and_accept_a_valid_one(
    auth_settings: AuthentikSettings, viewer_headers: dict[str, str]
) -> None:
    """A valid identity still gets through on a protected api path."""
    status, reached = await _call(
        auth_settings, path="/api/v1/other", headers=viewer_headers
    )
    assert status == 204
    assert reached[0]["state"]["principal"].username == "viewer"


async def test_a_bad_jwt_on_the_intake_path_is_ignored_not_trusted(
    auth_settings: AuthentikSettings,
) -> None:
    """The exemption does not read the identity header; no principal is set."""
    status, reached = await _call(
        auth_settings,
        headers={"X-authentik-jwt": "not-a-token", "X-authentik-groups": "fo-admin"},
    )
    assert status == 204
    assert "principal" not in reached[0]["state"]


async def test_intake_exemption_applies_after_root_path_is_stripped(
    auth_settings: AuthentikSettings,
) -> None:
    """Under a mount prefix the routed path is what is compared."""
    status, _ = await _call(
        auth_settings,
        path="/portal/api/v1/balances",
        scope_extra={"root_path": "/portal"},
    )
    assert status == 204
    status, _ = await _call(
        auth_settings,
        path="/portal/api/v1/balances/x",
        scope_extra={"root_path": "/portal"},
    )
    assert status == 403


async def test_websocket_to_the_intake_path_is_refused(
    auth_settings: AuthentikSettings,
) -> None:
    """Only HTTP requests can be exempt."""
    status, reached = await _call(auth_settings, scope_extra={"type": "websocket"})
    assert status is None
    assert reached == []


def test_exempt_helper_is_exactly_one_method_and_path() -> None:
    """The predicate is an exact match, not a prefix test."""
    from app.middleware import authentik  # noqa: PLC0415

    helper: Callable[[str, str], bool] = authentik.is_intake_request
    assert helper("POST", INTAKE) is True
    assert helper("POST", INTAKE + "/") is False
    assert helper("GET", INTAKE) is False
    assert helper("POST", "/api/v1") is False


async def test_real_app_blocks_other_api_paths_without_a_jwt(
    anon_client: Any,  # noqa: ANN401
) -> None:
    """Through the full app, only the intake path escapes the JWT check."""
    async with anon_client as ac:
        other = await ac.post("/api/v1/other", json={})
        trailing = await ac.post(INTAKE + "/", json={})
        admin = await ac.post("/admin/refresh/entities", json={})
        get = await ac.get(INTAKE)
        intake = await ac.post(INTAKE, json={})
    assert other.status_code == 403
    assert trailing.status_code == 403
    assert admin.status_code == 403
    assert get.status_code == 403
    assert intake.status_code == 404  # reached the route, which is disabled
