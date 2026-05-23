# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Cloudflare Zero Trust Access JWT validation middleware (Phase 0 stub).

Pass-through stub per ADR-002; full validation lands in Phase 1.

#CRITICAL: Phase 1 must validate the JWT ``aud`` claim against
``CF_ACCESS_APP_ID`` to prevent accepting tokens issued to other apps in the
same Cloudflare tenant. #VERIFY: assert a fixture token with a foreign ``aud``
returns 403 in `tests/unit/test_middleware.py` before the stub is removed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response


class CloudflareAccessMiddleware(BaseHTTPMiddleware):
    """Phase 0 stub for Cloudflare Access JWT validation.

    Accepts every request unchanged. Phase 1 replaces ``dispatch`` with the
    fail-closed JWT validation pipeline described in ADR-002.
    """

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
