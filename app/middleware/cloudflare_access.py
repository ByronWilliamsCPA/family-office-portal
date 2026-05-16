# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Cloudflare Zero Trust Access JWT validation middleware (Phase A stub).

Per ADR-002 (docs/architecture/adr/adr-002-authentication-cloudflare-zero-trust.md),
authentication is enforced at the Cloudflare edge. This middleware is the
in-application defense-in-depth layer that will validate the
``CF-Access-JWT-Assertion`` header on every non-static request once Phase 1
lands. The current implementation is intentionally a pass-through so the
application can boot, expose ``/health``, and satisfy CI without depending on
a live Cloudflare tenant.

The full implementation will:

1. Require the ``CF-Access-JWT-Assertion`` header on non-static requests.
2. Validate the JWT signature against Cloudflare public keys fetched from
   ``https://<CF_TEAM_DOMAIN>/cdn-cgi/access/certs`` (cached with TTL).
3. Validate the ``aud`` claim against ``CF_ACCESS_APP_ID`` (#CRITICAL: skipping
   this allows tokens issued to other apps in the same Cloudflare tenant).
4. Map the ``email`` claim to ``Viewer`` or ``Admin`` via ``VIEWER_EMAILS``
   and ``ADMIN_EMAILS`` env vars and attach the role to ``request.state``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    """Phase A stub for Cloudflare Access JWT validation.

    Accepts every request unchanged. Phase 1 replaces ``dispatch`` with the
    full validation pipeline described in the module docstring.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Initialize the middleware.

        Args:
            app: The downstream ASGI application.
        """
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Pass the request through untouched.

        Args:
            request: The incoming Starlette request.
            call_next: The downstream handler.

        Returns:
            The downstream handler's response.
        """
        return await call_next(request)
