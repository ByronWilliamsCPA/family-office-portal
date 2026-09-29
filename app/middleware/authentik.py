# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Authentik forward-auth identity validation (ADR-005, supersedes ADR-002).

Traefik sends every request for the portal to the Authentik embedded outpost
(forward auth). On success the outpost forwards identity headers to the
portal. This middleware trusts only the signed ``X-authentik-jwt`` header: it
verifies the RS256 signature against the proxy provider's JWKS, checks
``iss``, ``aud``, ``exp`` (plus ``nbf`` and ``iat`` when present) with zero
leeway, and maps the ``groups`` claim to a portal role. Every failure returns
403 (fail closed).

#CRITICAL: security: the plain ``X-authentik-username``,
``X-authentik-groups`` and ``X-authentik-email`` headers are never read,
because any container on the shared Traefik network could send them
straight to the portal.
#VERIFY: requests carrying only plain identity headers return 403, and plain
headers sent next to a valid JWT do not change the principal
(``tests/unit/test_middleware.py``).

#ASSUME: external resources: the Authentik proxy provider signs
``X-authentik-jwt`` with an RSA signing key (RS256, Authentik's default
certificate), includes a ``kid`` header, and publishes the key at
``AUTHENTIK_JWKS_URL``. A provider with no signing key falls back to HS256
with the client secret, which this module rejects.
#VERIFY: decode a real header from the deployed outpost and confirm
``alg == "RS256"``, a ``kid`` that appears in the JWKS, and the ``iss``,
``aud``, ``preferred_username`` and ``groups`` claims before first
production use.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx
import jwt
import structlog
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger(__name__)

JWT_HEADER = "X-authentik-jwt"
# #CRITICAL: security: RS256 only. Never add a symmetric algorithm next to an
# asymmetric one (pyjwt advisory GHSA-jq35-7prp-9v3f, algorithm confusion).
# #VERIFY: tests/unit/test_middleware.py rejects alg=none and an HS256 token
# signed with the public key bytes.
ALLOWED_ALGORITHMS = ("RS256",)
ADMIN_PATH = "/admin"
STATIC_PATH_PREFIX = "/static/"
HEALTH_PATH = "/health"

DEFAULT_ADMIN_GROUP = "fo-admin"
DEFAULT_VIEWER_GROUP = "fo-viewer"
DEFAULT_JWKS_CACHE_SECONDS = 600
# Per-operation httpx timeout, plus a total deadline for the whole fetch so a
# slow-drip response cannot hold the cache lock indefinitely.
JWKS_FETCH_TIMEOUT_SECONDS = 5.0
JWKS_FETCH_DEADLINE_SECONDS = 10.0
# An Authentik JWKS holds a few keys (a few KiB); anything larger is refused.
JWKS_MAX_BYTES = 64 * 1024
# A token whose kid is not cached can force at most one JWKS refetch per
# interval, so forged tokens cannot turn the portal into a JWKS load source.
MIN_REFETCH_INTERVAL_SECONDS = 30.0
_MIN_RSA_KEY_BITS = 2048


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


@dataclass(frozen=True)
class AuthentikSettings:
    """Authentik forward-auth settings read from the environment.

    Attributes:
        jwks_url (str): ``AUTHENTIK_JWKS_URL``, the provider's JWKS endpoint.
            Must use ``https://``.
        issuer (str): ``AUTHENTIK_ISSUER``, the exact expected ``iss`` claim.
        audience (str): ``AUTHENTIK_AUDIENCE``, the expected ``aud`` claim
            (the provider's client ID).
        admin_group (str): ``FO_ADMIN_GROUP``, group granting Admin.
        viewer_group (str): ``FO_VIEWER_GROUP``, group granting Viewer.
        jwks_cache_seconds (int): ``AUTHENTIK_JWKS_CACHE_SECONDS``, how long
            fetched keys are reused before a refetch.
    """

    jwks_url: str
    issuer: str
    audience: str
    admin_group: str = DEFAULT_ADMIN_GROUP
    viewer_group: str = DEFAULT_VIEWER_GROUP
    jwks_cache_seconds: int = DEFAULT_JWKS_CACHE_SECONDS

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
                invalid. The message names the offending variable.
        """
        env: Mapping[str, str] = os.environ if environ is None else environ
        values: dict[str, str] = {}
        for var in ("AUTHENTIK_JWKS_URL", "AUTHENTIK_ISSUER", "AUTHENTIK_AUDIENCE"):
            value = env.get(var, "").strip()
            if not value:
                msg = f"{var} is required"
                raise AuthConfigError(msg)
            values[var] = value
        jwks_url = values["AUTHENTIK_JWKS_URL"]
        # #CRITICAL: security: only https JWKS URLs are accepted, so a file://
        # or plain http URL can never feed signing keys to the portal (lesson
        # from pyjwt advisory GHSA-993g-76c3-p5m4, PyJWKClient file:// SSRF).
        # #ASSUME: homelab-infra serves the provider JWKS over https.
        # #VERIFY: curl the deployed AUTHENTIK_JWKS_URL and confirm a 200 JSON
        # key set over TLS before first production use.
        parts = urlsplit(jwks_url)
        if parts.scheme.lower() != "https" or not parts.netloc:
            msg = "AUTHENTIK_JWKS_URL must be an https:// URL"
            raise AuthConfigError(msg)
        admin_group = env.get("FO_ADMIN_GROUP", "").strip() or DEFAULT_ADMIN_GROUP
        viewer_group = env.get("FO_VIEWER_GROUP", "").strip() or DEFAULT_VIEWER_GROUP
        if admin_group == viewer_group:
            msg = "FO_ADMIN_GROUP and FO_VIEWER_GROUP must name different groups"
            raise AuthConfigError(msg)
        raw_ttl = env.get("AUTHENTIK_JWKS_CACHE_SECONDS", "").strip()
        ttl = DEFAULT_JWKS_CACHE_SECONDS
        if raw_ttl:
            try:
                ttl = int(raw_ttl)
            except ValueError:
                ttl = 0
            if ttl <= 0:
                msg = "AUTHENTIK_JWKS_CACHE_SECONDS must be a positive integer"
                raise AuthConfigError(msg)
        return cls(
            jwks_url=jwks_url,
            issuer=values["AUTHENTIK_ISSUER"],
            audience=values["AUTHENTIK_AUDIENCE"],
            admin_group=admin_group,
            viewer_group=viewer_group,
            jwks_cache_seconds=ttl,
        )


class _JwksTooLargeError(Exception):
    """Raised internally when a JWKS response exceeds ``JWKS_MAX_BYTES``."""


class _JwksDeadlineError(Exception):
    """Raised internally when a JWKS fetch outlives its total deadline."""


async def _read_jwks_body(
    url: str,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    async with (
        httpx.AsyncClient(
            timeout=JWKS_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            # Ignore HTTP(S)_PROXY, NO_PROXY and SSL_CERT_* from the
            # environment, so the key fetch cannot be rerouted through an
            # unexpected proxy or trust store.
            trust_env=False,
            transport=transport,
        ) as client,
        client.stream("GET", url, headers={"Accept": "application/json"}) as response,
    ):
        # raise_for_status also rejects 3xx, so an unfollowed redirect fails.
        response.raise_for_status()
        body = bytearray()
        # aiter_bytes yields decoded bytes, so the cap also bounds a
        # compressed body that inflates past the limit.
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > JWKS_MAX_BYTES:
                raise _JwksTooLargeError
        return bytes(body)


async def _within_deadline(
    url: str,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    # asyncio.wait plus an explicit cancel instead of asyncio.wait_for: on
    # Python 3.10 wait_for raises asyncio.TimeoutError, which is not the
    # builtin TimeoutError, and the py312 lint target rewrites one to the
    # other. This form needs no timeout exception class at all.
    task = asyncio.ensure_future(_read_jwks_body(url, transport))
    try:
        done, _pending = await asyncio.wait({task}, timeout=JWKS_FETCH_DEADLINE_SECONDS)
    finally:
        if not task.done():
            task.cancel()
    if task not in done:
        raise _JwksDeadlineError
    return task.result()


async def fetch_authentik_jwks(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Fetch the provider's JWKS document without blocking the event loop.

    Redirects are not followed, so a redirect cannot move the fetch off the
    configured https URL. Proxy and trust-store settings from the environment
    are ignored, the body is capped at ``JWKS_MAX_BYTES``, and the whole
    fetch must finish within ``JWKS_FETCH_DEADLINE_SECONDS``.

    Args:
        url (str): JWKS URL of the Authentik proxy provider.
        transport (httpx.AsyncBaseTransport | None): Optional transport
            override, used by tests.

    Returns:
        dict[str, Any]: The parsed JWKS document.

    Raises:
        AuthError: If the endpoint is unreachable, returns a non-2xx status,
            is too slow or too large, or returns something other than a JSON
            object.
    """
    msg = "jwks_unavailable"
    try:
        body = await _within_deadline(url, transport)
        document: object = json.loads(body)
    except (
        httpx.HTTPError,
        ValueError,
        _JwksTooLargeError,
        _JwksDeadlineError,
    ) as exc:
        logger.warning("jwks_fetch_failed", reason=type(exc).__name__)
        raise AuthError(msg) from exc
    if not isinstance(document, dict):
        logger.warning("jwks_fetch_failed", reason="not_an_object")
        raise AuthError(msg)
    return cast("dict[str, Any]", document)


def parse_jwks(document: Mapping[str, Any]) -> dict[str, RSAPublicKey]:
    """Extract usable RS256 signing keys from a JWKS document.

    Keys that are not RSA, not for signing, declared for another algorithm,
    shorter than 2048 bits, malformed, or missing a ``kid`` are skipped.

    Args:
        document (Mapping[str, Any]): Parsed JWKS document.

    Returns:
        dict[str, RSAPublicKey]: Verification keys indexed by ``kid``.
    """
    keys: dict[str, RSAPublicKey] = {}
    raw_keys: object = document.get("keys")
    if not isinstance(raw_keys, list):
        return keys
    for entry in cast("list[object]", raw_keys):
        if not isinstance(entry, dict):
            continue
        jwk_data = cast("dict[str, Any]", entry)
        kid: object = jwk_data.get("kid")
        if (
            not isinstance(kid, str)
            or not kid
            or jwk_data.get("kty") != "RSA"
            or jwk_data.get("use", "sig") != "sig"
            or jwk_data.get("alg", "RS256") != "RS256"
        ):
            continue
        try:
            key: object = jwt.PyJWK(jwk_data, algorithm="RS256").key
        except jwt.PyJWTError:
            logger.warning("jwks_key_skipped", kid=kid, reason="malformed_key")
            continue
        if not isinstance(key, RSAPublicKey) or key.key_size < _MIN_RSA_KEY_BITS:
            logger.warning("jwks_key_skipped", kid=kid, reason="unsupported_key")
            continue
        keys[kid] = key
    return keys


class JwksCache:
    """Concurrency-safe in-memory JWKS cache with a TTL.

    Keys are fetched lazily on the first request, reused for ``ttl_seconds``,
    and refetched early when a token names an unknown ``kid`` (key rotation).
    Fetch attempts, successful or not, are rate limited to one per
    ``min(ttl_seconds, MIN_REFETCH_INTERVAL_SECONDS)``, so neither forged
    ``kid`` values nor an unreachable JWKS endpoint turn requests into a
    fetch storm. An ``asyncio.Lock`` makes concurrent requests on a cold or
    expired cache share one fetch.

    #EDGE: availability over prompt revocation. When a refetch fails, the
    last good key set keeps validating tokens for one extra TTL (up to twice
    ``ttl_seconds`` after the last successful fetch) instead of locking every
    user out during a short Authentik outage. A key that Authentik removed
    after a compromise therefore stays trusted for up to that long if the
    JWKS endpoint is unreachable at the same time; after the grace period
    every request fails closed with ``jwks_unavailable``.
    #VERIFY: tests/unit/test_middleware.py shows a real token accepted during
    the grace period and refused after it; confirm with homelab-infra that
    ``2 * AUTHENTIK_JWKS_CACHE_SECONDS`` is an acceptable revocation delay.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._ttl = float(ttl_seconds)
        self._grace = float(ttl_seconds)
        self._refetch_interval = min(self._ttl, MIN_REFETCH_INTERVAL_SECONDS)
        self._clock = clock
        self._transport = transport
        self._keys: dict[str, RSAPublicKey] = {}
        # Last successful fetch, which dates the cached keys.
        self._fetched_at: float | None = None
        # Last fetch attempt, successful or not, which drives rate limiting.
        self._attempted_at: float | None = None
        self._last_fetch_failed = False
        self._lock = asyncio.Lock()

    def _key_within(self, kid: str, max_age: float) -> RSAPublicKey | None:
        if self._fetched_at is None:
            return None
        if self._clock() - self._fetched_at >= max_age:
            return None
        return self._keys.get(kid)

    def _fetch_due(self) -> bool:
        if self._attempted_at is None:
            return True
        return self._clock() - self._attempted_at >= self._refetch_interval

    async def _refresh(self) -> None:
        # Stamp the attempt before fetching, so a failure is rate limited too.
        self._attempted_at = self._clock()
        try:
            document = await fetch_authentik_jwks(self._url, transport=self._transport)
        except AuthError:
            self._last_fetch_failed = True
            return
        self._keys = parse_jwks(document)
        self._fetched_at = self._clock()
        self._last_fetch_failed = False

    async def get_key(self, kid: str) -> RSAPublicKey:
        """Return the verification key for ``kid``, fetching keys if needed.

        Args:
            kid (str): Key ID from the token header.

        Returns:
            RSAPublicKey: Public key that signed tokens with this ``kid``.

        Raises:
            AuthError: If no usable key matches ``kid``: ``jwks_unavailable``
                when the latest fetch attempt failed, else ``unknown_key``.
        """
        key = self._key_within(kid, self._ttl)
        if key is not None:
            return key
        async with self._lock:
            # Another request may have refreshed the cache while this one
            # waited for the lock.
            key = self._key_within(kid, self._ttl)
            if key is None and self._fetch_due():
                await self._refresh()
            # Fresh keys after a successful fetch, or last good keys within
            # the grace period after a failed or rate-limited one.
            key = self._key_within(kid, self._ttl + self._grace)
        if key is None:
            msg = "jwks_unavailable" if self._last_fetch_failed else "unknown_key"
            raise AuthError(msg)
        return key


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
    key: RSAPublicKey,
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    """Verify an ``X-authentik-jwt`` token and return its claims.

    Zero leeway: forward auth mints the token on the same request, so clock
    skew between Authentik and the portal host (both NTP-synced homelab
    machines) is the only gap, and it is well under a second.

    Args:
        token (str): Encoded JWT.
        key (RSAPublicKey): Verification key for the token's ``kid``.
        issuer (str): Expected ``iss``, compared exactly.
        audience (str): Expected ``aud``; a list containing it is accepted.

    Returns:
        dict[str, Any]: Verified claims.

    Raises:
        AuthError: If the signature, algorithm, issuer, audience, or any time
            claim is invalid, or ``exp``, ``iss`` or ``aud`` is missing.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=issuer,
            audience=audience,
            leeway=0,
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


async def authenticate(
    token: str | None,
    *,
    settings: AuthentikSettings,
    jwks_cache: JwksCache,
) -> Principal:
    """Validate an ``X-authentik-jwt`` value and build the principal.

    Args:
        token (str | None): Value of the ``X-authentik-jwt`` header.
        settings (AuthentikSettings): Issuer, audience, and group settings.
        jwks_cache (JwksCache): Source of verification keys.

    Returns:
        Principal: The authenticated user.

    Raises:
        AuthError: If the token is missing, malformed, uses a disallowed
            algorithm, fails validation, or grants no portal role.
    """
    if not token:
        msg = "missing_token"
        raise AuthError(msg)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        msg = "malformed_token"
        raise AuthError(msg) from exc
    # Reject before the key lookup so a forged header cannot trigger a JWKS
    # refetch; jwt.decode enforces the same list again.
    if header.get("alg") not in ALLOWED_ALGORITHMS:
        msg = "disallowed_algorithm"
        raise AuthError(msg)
    kid: object = header.get("kid")
    if not isinstance(kid, str) or not kid:
        msg = "missing_key_id"
        raise AuthError(msg)
    key = await jwks_cache.get_key(kid)
    claims = validate_authentik_jwt(
        token,
        key=key,
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
        jwks_cache: JwksCache | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.jwks_cache = jwks_cache or JwksCache(
            settings.jwks_url,
            ttl_seconds=settings.jwks_cache_seconds,
        )

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
            principal = await authenticate(
                token,
                settings=self.settings,
                jwks_cache=self.jwks_cache,
            )
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
