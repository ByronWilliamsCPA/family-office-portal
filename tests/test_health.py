# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Health endpoint smoke test.

Verifies the Phase A liveness probe returns HTTP 200 with the canonical
``{"status": "ok"}`` payload. Acts as the first integration test that exercises
the FastAPI app, Cloudflare Access middleware stub, and ASGI plumbing end to end.
"""

from __future__ import annotations

from http import HTTPStatus

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_health_returns_ok() -> None:
    """GET /health must return 200 and ``{"status": "ok"}``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok"}
