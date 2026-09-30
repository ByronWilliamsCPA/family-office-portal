# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Authentik forward-auth identity validation (ADR-005, supersedes ADR-002).

Traefik sends every request for the portal to the Authentik embedded outpost
(forward auth). On success the outpost forwards identity headers to the
portal. This middleware trusts only the signed ``X-authentik-jwt`` header: it
verifies the HS256 signature with the proxy provider's client secret, checks
``iss``, ``aud``, ``exp`` (plus ``nbf`` and ``iat`` when present) with a
``JWT_LEEWAY_SECONDS`` clock-skew allowance, and maps the ``groups`` claim
to a portal role. Every failure returns 403 (fail closed).

#CRITICAL: security: the plain ``X-authentik-username``,
``X-authentik-groups`` and ``X-authentik-email`` headers are never read,
because any container on the shared Traefik network could send them
straight to the portal.
#VERIFY: requests carrying only plain identity headers return 403, and plain
headers sent next to a valid JWT do not change the principal
(``tests/unit/test_middleware.py``).

#ASSUME: external resources: an Authentik proxy provider cannot keep a signing
key (Authentik resets it to none on every create and update), so it signs
``X-authentik-jwt`` with HS256 keyed by the provider's client secret, sets no
``kid`` header and publishes an empty key set. An asymmetric signature is
therefore impossible for this token and is refused (ADR-005 amendment
2026-09-29).
#VERIFY: decode a real header from the deployed outpost and confirm
``alg == "HS256"``, ``aud`` equal to the provider's client ID, ``iss`` equal
to ``AUTHENTIK_ISSUER`` and a ``groups`` claim listing the portal groups
before first production use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import jwt
import structlog
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger(__name__)

JWT_HEADER = "X-authentik-jwt"
# #CRITICAL: security: HS256 only; never add an asymmetric algorithm next to
# it. A verifier that accepts both an asymmetric and a symmetric algorithm is
# open to algorithm confusion (pyjwt advisory GHSA-jq35-7prp-9v3f); a single
# symmetric algorithm keyed with a random secret has no public key to confuse.
# #VERIFY: tests/unit/test_middleware.py refuses alg=none, a validly signed
# RS256 token with perfect claims, and a token signed with another secret.
ALLOWED_ALGORITHMS = ("HS256",)
# Clock-skew allowance for exp, nbf and iat. An Authentik proxy-provider token
# is not minted per request: its iat and exp come from the session's access
# token validity, so its iat can be seconds old or, after an NTP step on
# either host, appear slightly in the future.
# #ASSUME: timing: the Authentik host and the portal host are NTP-synced to
# within JWT_LEEWAY_SECONDS of each other.
# #VERIFY: compare `date -u +%s.%N` on both hosts, or check `chronyc tracking`
# (System time offset) on each, and confirm the offset is well under 10 s.
JWT_LEEWAY_SECONDS = 10
ADMIN_PATH = "/admin"
STATIC_PATH_PREFIX = "/static/"
HEALTH_PATH = "/health"

DEFAULT_ADMIN_GROUP = "fo-admin"
DEFAULT_VIEWER_GROUP = "fo-viewer"
# The HMAC key is the provider's client secret, so a short or placeholder value
# would let anyone on the shared network forge a token. 32 characters is the
# HS256 key size in bytes.
MIN_JWT_SECRET_LENGTH = 32
# Placeholder the deployment stack ships in AUTHENTIK_JWT_SECRET until a real
# secret is set. It is a known public string, never a credential, and is
# refused at startup.
STACK_PLACEHOLDER_VALUE = "CHANGE_ME_IN_PORTAINER"


class Role(Enum):
    """Portal access level derived from Authentik group membership."""

    VIEWER = "Viewer"
    ADMIN = "Admin"


@dataclass(frozen=True)
class Principal:
    """The authenticated user attached to ``request.state.principal``.

    Attributes:
        username (str): ``preferred_username`` claim, or ``sub`` when the
            username is absent.
        email (str | None): ``email`` claim, if present.
        name (str | None): ``name`` claim, if present.
        role (Role): Portal role granted by group membership.
    """

    username: str
    email: str | None
    name: str | None
    role: Role

    @property
    def is_admin(self) -> bool:
        """Return True when the principal holds the Admin role."""
        return self.role is Role.ADMIN


class AuthConfigError(ValueError):
    """Raised when the Authentik settings in the environment are invalid."""


class AuthError(Exception):
    """Raised when a request's identity token is missing, invalid, or denied.

    Args:
        reason (str): Short failure category safe to log (never the token).

    Attributes:
        reason (str): Short failure category safe to log (never the token).
    """

    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _jwt_secret_from_env(env: Mapping[str, str]) -> str:
    """Read and validate ``AUTHENTIK_JWT_SECRET`` without altering it.

    The value is deliberately not stripped: stripping would change the HMAC
    key bytes and make every genuine token fail verification, so a value with
    leading or trailing whitespace is refused instead.

    Args:
        env (Mapping[str, str]): Variables to read.

    Returns:
        str: The secret, byte for byte as configured.

    Raises:
        AuthConfigError: If the secret is missing, has surrounding
            whitespace, is the stack placeholder, or is shorter than
            ``MIN_JWT_SECRET_LENGTH``. The message names the variable and
            never includes the value.
    """
    var = "AUTHENTIK_JWT_SECRET"
    secret = env.get(var, "")
    if not secret:
        msg = f"{var} is required"
        raise AuthConfigError(msg)
    if secret != secret.strip():
        msg = f"{var} must not have leading or trailing whitespace"
        raise AuthConfigError(msg)
    # #CRITICAL: security: the secret both signs and verifies, and any
    # container on the shared network can reach the portal directly, so a
    # placeholder or short value is exploitable.
    # #VERIFY: tests/unit/test_middleware.py refuses the placeholder, 31
    # characters and surrounding whitespace, and accepts exactly 32.
    if secret == STACK_PLACEHOLDER_VALUE:
        msg = f"{var} is still the placeholder value"
        raise AuthConfigError(msg)
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        msg = f"{var} must be at least {MIN_JWT_SECRET_LENGTH} characters"
        raise AuthConfigError(msg)
    return secret


@dataclass(frozen=True)
class AuthentikSettings:
    """Authentik forward-auth settings read from the environment.

    Attributes:
        jwt_secret (str): ``AUTHENTIK_JWT_SECRET``, the proxy provider's client
            secret, used as the HS256 verification key. Excluded from ``repr``
            so a settings dump never prints it.
        issuer (str): ``AUTHENTIK_ISSUER``, the exact expected ``iss`` claim.
        audience (str): ``AUTHENTIK_AUDIENCE``, the expected ``aud`` claim
            (the provider's client ID).
        admin_group (str): ``FO_ADMIN_GROUP``, group granting Admin.
        viewer_group (str): ``FO_VIEWER_GROUP``, group granting Viewer.
    """

    jwt_secret: str = field(repr=False)
    issuer: str
    audience: str
    admin_group: str = DEFAULT_ADMIN_GROUP
    viewer_group: str = DEFAULT_VIEWER_GROUP

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AuthentikSettings:
        """Build settings from environment variables and validate them.

        Args:
            environ (Mapping[str, str] | None): Variables to read; defaults to
                ``os.environ``.

        Returns:
            AuthentikSettings: Validated settings.

        Raises:
            AuthConfigError: If a required variable is missing or a value is
                invalid. The message names the offending variable and never
                includes the secret's value.
        """
        env: Mapping[str, str] = os.environ if environ is None else environ
        values: dict[str, str] = {}
        for var in ("AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE"):
            value = env.get(var, "").strip()
            if not value:
                msg = f"{var} is required"
                raise AuthConfigError(msg)
            values[var] = value
        jwt_secret = _jwt_secret_from_env(env)
        admin_group = env.get("FO_ADMIN_GROUP", "").strip() or DEFAULT_ADMIN_GROUP
        viewer_group = env.get("FO_VIEWER_GROUP", "").strip() or DEFAULT_VIEWER_GROUP
        if admin_group == viewer_group:
            msg = "FO_ADMIN_GROUP and FO_VIEWER_GROUP must name different groups"
            raise AuthConfigError(msg)
        return cls(
            jwt_secret=jwt_secret,
            issuer=values["AUTHENTIK_ISSUER"],
            audience=values["AUTHENTIK_AUDIENCE"],
            admin_group=admin_group,
            viewer_group=viewer_group,
        )


# Ordered most specific first: InvalidSignatureError subclasses DecodeError,
# and the claim errors subclass InvalidTokenError.
_JWT_ERROR_REASONS: tuple[tuple[type[jwt.PyJWTError], str], ...] = (
    (jwt.ExpiredSignatureError, "expired"),
    (jwt.ImmatureSignatureError, "not_yet_valid"),
    (jwt.InvalidAudienceError, "wrong_audience"),
    (jwt.InvalidIssuerError, "wrong_issuer"),
    (jwt.MissingRequiredClaimError, "missing_claim"),
    (jwt.InvalidSignatureError, "bad_signature"),
    (jwt.InvalidAlgorithmError, "disallowed_algorithm"),
    (jwt.DecodeError, "malformed_token"),
)


def _reason_for(exc: jwt.PyJWTError) -> str:
    for error_type, reason in _JWT_ERROR_REASONS:
        if isinstance(exc, error_type):
            return reason
    return "invalid_token"


def validate_authentik_jwt(
    token: str,
    *,
    key: str,
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    """Verify an ``X-authentik-jwt`` token and return its claims.

    Time claims are checked with ``JWT_LEEWAY_SECONDS`` of clock-skew
    allowance. The outpost does not mint a fresh token per request: ``iat``
    and ``exp`` follow the Authentik session's access-token validity, and an
    NTP correction on either host can step the clock by a second or more, so
    an exact comparison would refuse valid users at the boundaries. The
    allowance is small next to the token lifetime, so expired tokens are
    still refused.

    Args:
        token (str): Encoded JWT.
        key (str): HS256 verification key, the provider's client secret.
        issuer (str): Expected ``iss``, compared exactly.
        audience (str): Expected ``aud``; a list containing it is accepted.

    Returns:
        dict[str, Any]: Verified claims.

    Raises:
        AuthError: If the signature, algorithm, issuer, audience, or any time
            claim is invalid, or ``exp``, ``iss`` or ``aud`` is missing. An
            ``alg=none`` token, or one signed with any algorithm other than
            HS256, fails as ``disallowed_algorithm``.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer,
            audience=audience,
            leeway=JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(_reason_for(exc)) from exc
    return claims


def role_from_groups(
    groups: Iterable[str],
    *,
    viewer_group: str,
    admin_group: str,
) -> Role | None:
    """Map Authentik group names to a portal role.

    Matching is exact and case-sensitive: group names are access-control
    identifiers, so ``FO-Admin`` does not grant what ``fo-admin`` grants.

    Args:
        groups (Iterable[str]): Group names from the token.
        viewer_group (str): Group granting the Viewer role.
        admin_group (str): Group granting the Admin role.

    Returns:
        Role | None: Admin if in the admin group, Viewer if in the viewer
        group, otherwise None.
    """
    names = set(groups)
    if admin_group in names:
        return Role.ADMIN
    if viewer_group in names:
        return Role.VIEWER
    return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def principal_from_claims(
    claims: Mapping[str, Any],
    settings: AuthentikSettings,
) -> Principal:
    """Build the request principal from verified claims.

    Args:
        claims (Mapping[str, Any]): Claims returned by
            ``validate_authentik_jwt``.
        settings (AuthentikSettings): Group names for role mapping.

    Returns:
        Principal: The authenticated user.

    Raises:
        AuthError: If no non-empty identity claim is present
            (``missing_identity``) or the user is in neither portal group
            (``no_portal_group``).
    """
    username = _optional_str(claims.get("preferred_username")) or _optional_str(
        claims.get("sub")
    )
    if username is None:
        msg = "missing_identity"
        raise AuthError(msg)
    raw_groups: object = claims.get("groups")
    groups = (
        [g for g in cast("list[object]", raw_groups) if isinstance(g, str)]
        if isinstance(raw_groups, list)
        else []
    )
    role = role_from_groups(
        groups,
        viewer_group=settings.viewer_group,
        admin_group=settings.admin_group,
    )
    if role is None:
        msg = "no_portal_group"
        raise AuthError(msg)
    return Principal(
        username=username,
        email=_optional_str(claims.get("email")),
        name=_optional_str(claims.get("name")),
        role=role,
    )


def authenticate(
    token: str | None,
    *,
    settings: AuthentikSettings,
) -> Principal:
    """Validate an ``X-authentik-jwt`` value and build the principal.

    No token header is read before the signature is verified, and nothing is
    fetched: the HS256 key is the configured client secret.

    Args:
        token (str | None): Value of the ``X-authentik-jwt`` header.
        settings (AuthentikSettings): Secret, issuer, audience, and group
            settings.

    Returns:
        Principal: The authenticated user.

    Raises:
        AuthError: If the token is missing, malformed, uses a disallowed
            algorithm, fails validation, or grants no portal role.
    """
    if not token:
        msg = "missing_token"
        raise AuthError(msg)
    claims = validate_authentik_jwt(
        token,
        key=settings.jwt_secret,
        issuer=settings.issuer,
        audience=settings.audience,
    )
    return principal_from_claims(claims, settings)


def _denied() -> PlainTextResponse:
    return PlainTextResponse("Access denied", status_code=403)


def _route_path(scope: Scope) -> str:
    # The path the router matches, relative to the app's mount point. This
    # mirrors Starlette's own rule (starlette._utils.get_route_path, which is
    # private): strip ``root_path`` when it is a whole-segment prefix.
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def _is_public(route_path: str) -> bool:
    return route_path == HEALTH_PATH or route_path.startswith(STATIC_PATH_PREFIX)


def _is_admin_path(route_path: str) -> bool:
    # Segment boundary: "/adminX" is an ordinary protected path, not admin.
    return route_path == ADMIN_PATH or route_path.startswith(ADMIN_PATH + "/")


class AuthentikAuthMiddleware:
    """Fail-closed ASGI middleware enforcing Authentik identity on every request.

    ``/health`` and ``/static/`` are public. Every other HTTP request needs a
    valid ``X-authentik-jwt`` granting a portal role, and ``/admin`` or
    ``/admin/...`` needs the Admin role. Paths are judged after stripping any
    ASGI ``root_path`` prefix, as the router matches them. WebSocket
    connections are refused because the portal serves none.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: AuthentikSettings,
    ) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate the request and pass it on, or answer 403.

        Args:
            scope (Scope): ASGI connection scope.
            receive (Receive): ASGI receive channel.
            send (Send): ASGI send channel.
        """
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        # #CRITICAL: security: the public and admin checks must see the same
        # path the router matches. Checking the raw ``scope["path"]`` let a
        # Viewer reach ``/portal/admin/...`` when the app runs under
        # ``root_path="/portal"``.
        # #VERIFY: tests/unit/test_middleware.py covers root_path requests for
        # Viewer, Admin and /health, and the "/adminX" boundary.
        path = _route_path(scope)
        if _is_public(path):
            await self.app(scope, receive, send)
            return
        token = Headers(scope=scope).get(JWT_HEADER)
        try:
            principal = authenticate(token, settings=self.settings)
        except AuthError as exc:
            logger.info("auth_denied", path=path, reason=exc.reason)
            await _denied()(scope, receive, send)
            return
        if _is_admin_path(path) and not principal.is_admin:
            logger.info(
                "auth_denied",
                path=path,
                reason="admin_required",
                user=principal.username,
            )
            await _denied()(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)
