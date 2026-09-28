# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Unit tests for ``app.middleware.authentik`` (ADR-004)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.middleware import authentik
from app.middleware.authentik import AuthError, Role, authenticate, role_from_groups

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.usefixtures("portal_env")


# --------------------------------------------------------------------------- #
# role_from_groups
# --------------------------------------------------------------------------- #


def test_role_viewer_group_maps_to_viewer() -> None:
    """The viewer group grants the Viewer role."""
    role = role_from_groups(
        ["homelab-family", "fo-viewer"],
        viewer_group="fo-viewer",
        admin_group="fo-admin",
    )
    assert role is Role.VIEWER


def test_role_admin_wins_over_viewer() -> None:
    """Membership in both groups yields Admin."""
    role = role_from_groups(
        ["fo-viewer", "fo-admin"], viewer_group="fo-viewer", admin_group="fo-admin"
    )
    assert role is Role.ADMIN


def test_role_match_is_case_insensitive() -> None:
    """Group names are compared case-insensitively."""
    role = role_from_groups(
        ["FO-Admin"], viewer_group="fo-viewer", admin_group="fo-admin"
    )
    assert role is Role.ADMIN


def test_role_none_for_unrelated_groups() -> None:
    """Broad homelab groups do not grant portal access."""
    role = role_from_groups(
        ["homelab-family", "homelab-users"],
        viewer_group="fo-viewer",
        admin_group="fo-admin",
    )
    assert role is None


# --------------------------------------------------------------------------- #
# authenticate
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_accepts_valid_viewer_token(
    jwt_factory: Callable[..., str],
) -> None:
    """A correctly signed token with the viewer group authenticates."""
    principal = authenticate(jwt_factory(username="mom"))
    assert principal.role is Role.VIEWER
    assert principal.username == "mom"
    assert principal.email == "mom@example.com"
    assert not principal.is_admin


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_accepts_audience_list(jwt_factory: Callable[..., str]) -> None:
    """An ``aud`` array containing the client ID is accepted."""
    token = jwt_factory(aud=["other", "test-client-id"])
    assert authenticate(token).role is Role.VIEWER


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_missing_token() -> None:
    """No header means no access."""
    with pytest.raises(AuthError):
        authenticate(None)


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_garbage_token() -> None:
    """A malformed token is rejected."""
    with pytest.raises(AuthError):
        authenticate("garbage.token.here")


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_expired_token(jwt_factory: Callable[..., str]) -> None:
    """An expired token is rejected."""
    with pytest.raises(AuthError):
        authenticate(jwt_factory(exp=int(time.time()) - 60))


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_wrong_audience(jwt_factory: Callable[..., str]) -> None:
    """A token issued to another Authentik application is rejected."""
    with pytest.raises(AuthError):
        authenticate(jwt_factory(aud="another-app"))


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_wrong_issuer(jwt_factory: Callable[..., str]) -> None:
    """A token from another issuer is rejected."""
    with pytest.raises(AuthError):
        authenticate(jwt_factory(claims={"iss": "https://evil.test/"}))


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_wrong_signing_key(
    jwt_factory: Callable[..., str],
) -> None:
    """A token signed by a different key is rejected."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError):
        authenticate(jwt_factory(private_key=other))


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_unknown_key_id(jwt_factory: Callable[..., str]) -> None:
    """A token whose ``kid`` is not in the JWKS is rejected."""
    with pytest.raises(AuthError):
        authenticate(jwt_factory(kid="unknown-kid"))


@pytest.mark.usefixtures("patched_jwks")
def test_authenticate_rejects_user_without_portal_group(
    jwt_factory: Callable[..., str],
) -> None:
    """A valid Authentik user outside the portal groups is rejected."""
    with pytest.raises(AuthError):
        authenticate(jwt_factory(groups=["homelab-family"]))


def test_jwks_is_cached_between_requests(
    patched_jwks: dict[str, int],
    jwt_factory: Callable[..., str],
) -> None:
    """Keys are fetched once and reused for later tokens."""
    authenticate(jwt_factory())
    authenticate(jwt_factory())
    assert patched_jwks["calls"] == 1


def test_jwks_fetch_failure_denies_access(
    monkeypatch: pytest.MonkeyPatch,
    jwt_factory: Callable[..., str],
) -> None:
    """If Authentik keys cannot be fetched, access is denied (fail closed)."""

    def _boom(url: str, timeout: float) -> dict[str, object]:
        del url, timeout
        msg = "unreachable"
        raise httpx.ConnectError(msg)

    monkeypatch.setattr(authentik, "fetch_authentik_jwks", _boom)
    authentik.jwks_cache.clear()
    with pytest.raises(AuthError):
        authenticate(jwt_factory())
