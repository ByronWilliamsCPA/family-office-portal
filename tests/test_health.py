# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLC0415
"""Health endpoint smoke test.

Verifies the Phase 0 liveness probe returns HTTP 200 with status and service
name. Acts as the first integration test that exercises the FastAPI app,
Authentik auth middleware (``/health`` is public), and ASGI plumbing end to
end.

``app.main`` is fail-fast on missing env vars, so it is imported inside the
test body after ``portal_env`` populates the environment -- not at module level.
"""

from __future__ import annotations

import importlib
from http import HTTPStatus

from httpx import ASGITransport, AsyncClient


async def test_health_returns_ok(portal_env: dict[str, str]) -> None:
    """GET /health must return 200 with status and service name.

    Args:
        portal_env: Fixture that populates required env vars before app import.
    """
    del portal_env
    import app.main as _main

    importlib.reload(_main)
    transport = ASGITransport(app=_main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "ok", "service": "family-office-portal"}
