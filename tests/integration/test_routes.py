# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003, PLC0415, PLR2004
"""Integration tests for portal route handlers.

Spec contract (tech-spec §4 endpoint table + ``CLAUDE.md`` auth rules):

* All routes require a valid CF Access JWT; unauthenticated requests get 403.
* ``/admin/*`` routes require role=Admin; Viewer requests get 403.
* Routes return ``TemplateResponse`` (HTML), not JSON, except HTMX partial routes
  which return HTML fragments.
* All five sections render even when their backing dataset is empty (graceful
  degradation -- no blank screens for primary users).

Tests use ``httpx.AsyncClient`` against the FastAPI app, with the JWKS fetcher
patched so ``jwt_factory``-minted tokens validate.
"""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

# Defer the actual import of ``app.main``/``app.db`` into the ``client``
# fixture: ``app.main`` is fail-fast on missing env vars, so importing it at
# module-collection time -- before ``cf_env`` populates the environment --
# would raise ``SystemExit`` and abort the suite.
if (
    importlib.util.find_spec("app.main") is None
    or importlib.util.find_spec("app.db") is None
):
    pytest.skip("app.main / app.db not implemented yet", allow_module_level=True)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPrivateKey,
        RSAPublicKey,
    )


# --------------------------------------------------------------------------- #
# Test client + auth fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(
    cf_env: dict[str, str],
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rsa_key_pair: tuple[RSAPrivateKey, RSAPublicKey],
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an ``httpx.AsyncClient`` bound to a freshly-loaded ``app.main.app``.

    The CF JWKS fetcher is patched so JWTs minted via ``jwt_factory`` validate
    against the test RSA public key.

    # noqa
    """
    import base64

    del cf_env
    monkeypatch.setenv("SQLITE_PATH", str(tmp_db_path))

    db = importlib.import_module("app.db")
    main = importlib.import_module("app.main")

    db.init_schema(str(tmp_db_path))

    _, public = rsa_key_pair
    numbers = public.public_numbers()

    def _b64(value: int) -> str:
        """Base64url-encode an unsigned big-endian integer.

        # noqa
        """
        byte_length = (value.bit_length() + 7) // 8
        raw = value.to_bytes(byte_length, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key-id",
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }

    importlib.reload(main)

    middleware_pkg = pytest.importorskip("app.middleware")
    target = middleware_pkg
    for sub in ("cf_jwt", "jwt", "auth"):
        try:
            mod = pytest.importorskip(f"app.middleware.{sub}")
        except pytest.skip.Exception:  # type: ignore[attr-defined]
            continue
        if hasattr(mod, "fetch_cf_public_keys"):
            target = mod
            break

    if hasattr(target, "fetch_cf_public_keys"):

        def _stub(*_a: object, **_kw: object) -> dict[str, list[dict[str, str]]]:
            """Return the static test JWKS document.

            # noqa
            """
            return jwks

        monkeypatch.setattr(target, "fetch_cf_public_keys", _stub)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as ac:
        yield ac


@pytest.fixture
def viewer_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Return request headers carrying a Viewer-role CF Access JWT.

    # noqa
    """
    return {
        "CF-Access-JWT-Assertion": jwt_factory(email="viewer@example.com"),
    }


@pytest.fixture
def admin_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Return request headers carrying an Admin-role CF Access JWT.

    # noqa
    """
    return {
        "CF-Access-JWT-Assertion": jwt_factory(email="admin@example.com"),
    }


# --------------------------------------------------------------------------- #
# Auth enforcement (every primary route requires a valid JWT)
# --------------------------------------------------------------------------- #

PRIMARY_ROUTES = ["/", "/documents", "/finances", "/portfolio", "/entities"]


@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_unauthenticated_request_is_rejected(
    client: httpx.AsyncClient, path: str
) -> None:
    """A request to any primary route without a JWT is rejected (401/403).

    # noqa
    """
    response = await client.get(path)
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_invalid_jwt_is_rejected(client: httpx.AsyncClient, path: str) -> None:
    """A request bearing a malformed JWT is rejected (401/403).

    # noqa
    """
    response = await client.get(
        path, headers={"CF-Access-JWT-Assertion": "garbage.token.here"}
    )
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_authenticated_viewer_can_load_primary_routes(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    path: str,
) -> None:
    """A Viewer-role JWT successfully loads each primary route.

    # noqa
    """
    response = await client.get(path, headers=viewer_headers)
    assert response.status_code == 200


@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_primary_routes_return_html(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    path: str,
) -> None:
    """Primary route responses carry an HTML content-type, not JSON.

    # noqa
    """
    response = await client.get(path, headers=viewer_headers)
    assert "text/html" in response.headers.get("content-type", "")


# --------------------------------------------------------------------------- #
# Admin-only authorization
# --------------------------------------------------------------------------- #


async def test_admin_route_rejects_viewer(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A Viewer-role JWT cannot access /admin/refresh-status.

    # noqa
    """
    response = await client.get("/admin/refresh-status", headers=viewer_headers)
    assert response.status_code in (401, 403)


async def test_admin_route_accepts_admin(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """An Admin-role JWT successfully loads /admin/refresh-status.

    # noqa
    """
    response = await client.get("/admin/refresh-status", headers=admin_headers)
    assert response.status_code == 200


async def test_admin_refresh_trigger_rejects_viewer(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A Viewer-role JWT cannot trigger a manual refresh.

    # noqa
    """
    response = await client.post("/admin/refresh/llc-manager", headers=viewer_headers)
    assert response.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Graceful degradation: empty cache must not error
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", PRIMARY_ROUTES)
async def test_empty_cache_renders_without_error(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    path: str,
) -> None:
    """Per ADR-003: a stale or empty section must show the last cached value
    plus a 'last updated' label -- never an unhandled error or blank section.

    # noqa
    """
    response = await client.get(path, headers=viewer_headers)
    assert response.status_code == 200
    assert response.text  # non-empty body


# --------------------------------------------------------------------------- #
# Section content shows up when cache is populated
# --------------------------------------------------------------------------- #


async def test_entities_route_shows_seeded_entity_name(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A seeded entity name appears in the /entities response body.

    # noqa
    """
    fetched = datetime.now(UTC).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, state, status, fetched_at) "
            "VALUES ('ent-1', 'Cascade Holdings LLC', 'LLC', 'WY', 'current', ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/entities", headers=viewer_headers)
    assert response.status_code == 200
    assert "Cascade Holdings LLC" in response.text


async def test_documents_route_shows_seeded_document_name(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A seeded document name appears in the /documents response body.

    # noqa
    """
    fetched = datetime.now(UTC).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, added_at, proxy_url, "
            "fetched_at) VALUES ('doc-1', '2025 Tax Return.pdf', 'Tax Returns', "
            "'2026-04-15T00:00:00', '/documents/doc-1/download', ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/documents", headers=viewer_headers)
    assert response.status_code == 200
    assert "2025 Tax Return.pdf" in response.text


async def test_portfolio_route_shows_seeded_holding_name(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A seeded holding name appears in the /portfolio response body.

    # noqa
    """
    fetched = datetime.now(UTC).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES "
            "('hold-1', 'Vanguard Total Stock Market', 'Diversified', "
            "100000.0, 12.5, ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/portfolio", headers=viewer_headers)
    assert response.status_code == 200
    assert "Vanguard Total Stock Market" in response.text


# --------------------------------------------------------------------------- #
# Single-resource routes
# --------------------------------------------------------------------------- #


async def test_entity_detail_route_returns_200_for_existing_id(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """A GET on /entities/{id} for an existing entity returns 200.

    # noqa
    """
    fetched = datetime.now(UTC).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, state, status, fetched_at) "
            "VALUES ('ent-detail', 'Detail LLC', 'LLC', 'NV', 'current', ?)",
            (fetched,),
        )
        conn.commit()

    response = await client.get("/entities/ent-detail", headers=viewer_headers)
    assert response.status_code == 200


async def test_entity_detail_route_returns_404_for_unknown_id(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """A GET on /entities/{id} for an unknown entity returns 404.

    # noqa
    """
    response = await client.get("/entities/nonexistent", headers=viewer_headers)
    assert response.status_code == 404


async def test_documents_search_returns_html_partial(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
) -> None:
    """Per ``CLAUDE.md``: HTMX partial routes return HTML fragments, not full pages.

    # noqa
    """
    response = await client.get("/documents/search?q=tax", headers=viewer_headers)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
