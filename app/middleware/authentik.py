# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Authentik forward-auth identity validation (ADR-004, supersedes ADR-002).

Traefik's ``authentik-chain@file`` middleware sends every request to the
Authentik embedded outpost and, on success, forwards identity headers to the
portal. This middleware trusts only the signed ``X-authentik-jwt`` header: it
verifies the signature against the proxy provider's JWKS, checks ``iss``,
``aud``, and ``exp``, and maps the ``groups`` claim to a portal role.

#CRITICAL: security: the plain ``X-authentik-username`` and
``X-authentik-groups`` headers are never trusted, because any container on the
shared ``traefik_proxy`` network could send them.
#VERIFY: a request carrying only plain identity headers returns 403
(``tests/unit/test_authentik_auth.py``).

#ASSUME: external resources: the Authentik proxy provider signs
``X-authentik-jwt`` with an RSA or EC key published at its JWKS URL and
includes a ``groups`` claim.
#VERIFY: decode a real header from the deployed outpost and confirm the claim
names and JWKS URL before first production use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import httpx
import jwt
import structlog
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import load_settings

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

logger = structlog.get_logger(__name__)

JWT_HEADER = "X-authentik-jwt"
ALLOWED_ALGORITHMS = ("RS256", "ES256")
PUBLIC_PATH_PREFIXES = ("/health", "/static/")
ADMIN_PATH_PREFIX = "/admin"
_MIN_REFRESH_INTERVAL_SECONDS = 30.0


class Role(str, Enum):  # noqa: UP042  # StrEnum needs 3.11; runtime floor is 3.10
    """Portal access level derived from Authentik group membership."""

    VIEWER = "viewer"
    ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    """The authenticated user attached to ``request.state.principal``.

    Attributes:
        username (str): Authentik username (``preferred_username`` or ``sub``).
        email (str | None): Email claim, if present.
        name (str | None): Display name claim, if present.
        role (Role): Portal role.
    """

    username: str
    email: str | None
    name: str | None
    role: Role

    @property
    def is_admin(self) -> bool:
        """Return True when the principal holds the Admin role."""
        return self.role is Role.ADMIN


class AuthError(Exception):
    """Raised when an identity token is missing, invalid, or unauthorized."""


def fetch_authentik_jwks(url: str, timeout: float) -> dict[str, Any]:
    """Fetch the JWKS document from Authentik.

    Args:
        url (str): JWKS URL of the proxy provider.
        timeout (float): Request timeout in seconds.

    Returns:
        dict[str, Any]: Parsed JWKS document.
    """
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        document: dict[str, Any] = response.json()
    return document


class JwksCache:
    """In-memory JWKS cache with a TTL and refresh on unknown key IDs."""

    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._fetched_at: float = 0.0

    def clear(self) -> None:
        """Drop cached keys so the next lookup refetches."""
        self._keys = {}
        self._fetched_at = 0.0

    def _refresh(self, url: str, timeout: float) -> None:
        document = fetch_authentik_jwks(url, timeout)
        keys: dict[str, Any] = {}
        for jwk in document.get("keys", []):
            kid = jwk.get("kid")
            if kid:
                keys[str(kid)] = jwt.PyJWK.from_dict(jwk).key
        self._keys = keys
        self._fetched_at = time.monotonic()

    def get_key(self, kid: str, *, url: str, timeout: float, ttl: int) -> Any:  # noqa: ANN401  # key type depends on algorithm
        """Return the verification key for ``kid``.

        Args:
            kid (str): Key ID from the token header.
            url (str): JWKS URL.
            timeout (float): Fetch timeout in seconds.
            ttl (int): Cache lifetime in seconds.

        Returns:
            Any: Public key usable by PyJWT.

        Raises:
            AuthError: If no key matches ``kid`` after a refresh.
        """
        age = time.monotonic() - self._fetched_at
        expired = not self._keys or age > ttl
        unknown = kid not in self._keys and age > _MIN_REFRESH_INTERVAL_SECONDS
        if expired or unknown:
            self._refresh(url, timeout)
        key = self._keys.get(kid)
        if key is None:
            msg = "Signing key not found"
            raise AuthError(msg)
        return key


jwks_cache = JwksCache()


def role_from_groups(
    groups: Iterable[str],
    *,
    viewer_group: str,
    admin_group: str,
) -> Role | None:
    """Map Authentik group names to a portal role.

    Args:
        groups (Iterable[str]): Group names from the token.
        viewer_group (str): Group granting the Viewer role.
        admin_group (str): Group granting the Admin role.

    Returns:
        Role | None: Admin if in the admin group, Viewer if in the viewer
        group, otherwise None.
    """
    names = {group.strip().casefold() for group in groups}
    if admin_group.casefold() in names:
        return Role.ADMIN
    if viewer_group.casefold() in names:
        return Role.VIEWER
    return None


def validate_authentik_jwt(
    token: str,
    *,
    key: Any,  # noqa: ANN401  # key type depends on algorithm
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    """Verify an ``X-authentik-jwt`` token and return its claims.

    Args:
        token (str): Encoded JWT.
        key (Any): Verification key for the token's ``kid``.
        issuer (str): Expected ``iss``.
        audience (str): Expected ``aud``.

    Returns:
        dict[str, Any]: Verified claims.

    Raises:
        AuthError: If the signature, issuer, audience, or expiry is invalid.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        msg = "Invalid identity token"
        raise AuthError(msg) from exc
    return claims


def _key_id(token: str) -> str:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        msg = "Malformed identity token"
        raise AuthError(msg) from exc
    kid = header.get("kid")
    if not kid:
        msg = "Identity token has no key ID"
        raise AuthError(msg)
    return str(kid)


def authenticate(token: str | None) -> Principal:
    """Validate a token and build the principal for the request.

    Args:
        token (str | None): Value of the ``X-authentik-jwt`` header.

    Returns:
        Principal: The authenticated user.

    Raises:
        AuthError: If the token is missing, invalid, or grants no role.
    """
    if not token:
        msg = "Missing identity token"
        raise AuthError(msg)
    settings = load_settings()
    try:
        key = jwks_cache.get_key(
            _key_id(token),
            url=settings.authentik_jwks_url,
            timeout=settings.backend_timeout_seconds,
            ttl=settings.jwks_cache_seconds,
        )
    except httpx.HTTPError as exc:
        msg = "Could not fetch signing keys"
        raise AuthError(msg) from exc
    claims = validate_authentik_jwt(
        token,
        key=key,
        issuer=settings.authentik_issuer,
        audience=settings.authentik_audience,
    )
    raw_groups: object = claims.get("groups")
    groups = (
        [str(g) for g in cast("list[object]", raw_groups)]
        if isinstance(raw_groups, list)
        else []
    )
    role = role_from_groups(
        groups,
        viewer_group=settings.fo_viewer_group,
        admin_group=settings.fo_admin_group,
    )
    if role is None:
        msg = "User is not in a portal group"
        raise AuthError(msg)
    username = claims.get("preferred_username") or claims.get("sub") or ""
    return Principal(
        username=str(username),
        email=claims.get("email"),
        name=claims.get("name"),
        role=role,
    )


def _is_public(path: str) -> bool:
    return path == "/health" or any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES)


def _denied() -> PlainTextResponse:
    return PlainTextResponse("Access denied", status_code=403)


class AuthentikAuthMiddleware(BaseHTTPMiddleware):
    """Fail-closed identity check for every non-public request."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Authenticate the request, enforce the Admin role on admin paths.

        Args:
            request (Request): The incoming request.
            call_next (RequestResponseEndpoint): The downstream handler.

        Returns:
            Response: The downstream response, or 403 when access is denied.
        """
        path = request.url.path
        if _is_public(path):
            return await call_next(request)
        token = request.headers.get(JWT_HEADER)
        try:
            principal = await run_in_threadpool(authenticate, token)
        except AuthError as exc:
            logger.info("auth_denied", path=path, reason=str(exc))
            return _denied()
        if path.startswith(ADMIN_PATH_PREFIX) and not principal.is_admin:
            logger.info("auth_admin_denied", path=path, user=principal.username)
            return _denied()
        request.state.principal = principal
        return await call_next(request)
