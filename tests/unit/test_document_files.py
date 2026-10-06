# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the document file proxy (``app.document_files``, ADR-007).

The upstream document service is faked with ``httpx.MockTransport``; no test
makes a network call. Every fake records the requests it receives, so a test
can prove that a refused request never reached the upstream service at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import gzip
import sqlite3
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
from structlog.testing import capture_logs

from app import document_files
from app.config import load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from starlette.types import Message, Scope

PDF_BYTES = b"%PDF-1.7\n" + b"0" * 2048 + b"\n%%EOF\n"
UPSTREAM_ERROR_BODY = "upstream internal detail 7f3a"


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


class _Upstream:
    """A fake document service that records every request it receives."""

    def __init__(
        self, respond: Callable[[httpx.Request], httpx.Response] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []
        self._respond = respond or (
            lambda _request: httpx.Response(
                200, content=PDF_BYTES, headers={"Content-Type": "application/pdf"}
            )
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


class _ChunkStream(httpx.AsyncByteStream):
    """An upstream body sent in chunks, optionally failing part way through."""

    def __init__(self, chunks: list[bytes], *, fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                msg = "connection reset"
                raise httpx.ReadError(msg)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _SlowStream(httpx.AsyncByteStream):
    """An upstream body that trickles small chunks with a pause before each."""

    def __init__(self, chunks: int, *, pause: float) -> None:
        self._chunks = chunks
        self._pause = pause
        self.sent = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            await asyncio.sleep(self._pause)
            self.sent += 1
            yield b"x"

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Upstream]:
    """Install a fake upstream behind the proxy's HTTP client.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Callable[..., _Upstream]: Installs a fake with an optional responder.
    """

    def _install(
        respond: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> _Upstream:
        fake = _Upstream(respond)
        transport = httpx.MockTransport(fake.handler)

        def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
            made = httpx.AsyncClient(
                transport=transport, timeout=timeout, follow_redirects=False
            )
            fake.clients.append(made)
            return made

        monkeypatch.setattr(document_files, "new_client", _client)
        return fake

    return _install


def _seed(
    path: Path,
    doc_id: str = "doc-1",
    *,
    name: str = "Operating Agreement",
    confidential: bool = False,
) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, is_confidential, proxy_url, "
            "fetched_at) VALUES (?, ?, 'LLCs', ?, ?, ?)",
            (
                doc_id,
                name,
                1 if confidential else 0,
                f"/documents/{doc_id}/preview",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


@pytest.fixture
def seeded_upstream(
    tmp_db_path: Path, upstream: Callable[..., _Upstream]
) -> Callable[..., _Upstream]:
    """Seed the open document ``doc-1`` and return the upstream installer.

    Args:
        tmp_db_path: Test database path.
        upstream: Fake upstream installer.

    Returns:
        Callable[..., _Upstream]: Installs a fake with an optional responder.
    """
    _seed(tmp_db_path)
    return upstream


# --------------------------------------------------------------------------- #
# Confidentiality: checked before any upstream request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["preview", "download"])
async def test_viewer_gets_404_for_confidential_document_with_no_upstream_call(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
    kind: str,
) -> None:
    """A Viewer asking for a confidential file gets 404 and nothing is fetched."""
    _seed(tmp_db_path, "doc-secret", confidential=True)
    fake = upstream()
    async with client as ac:
        response = await ac.get(f"/documents/doc-secret/{kind}", headers=viewer_headers)
    assert response.status_code == 404
    assert fake.requests == []
    assert PDF_BYTES not in response.content


@pytest.mark.parametrize("kind", ["preview", "download"])
async def test_unknown_document_is_404_with_no_upstream_call(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    upstream: Callable[..., _Upstream],
    kind: str,
) -> None:
    """A document that is not in the cache is never requested upstream."""
    fake = upstream()
    async with client as ac:
        response = await ac.get(f"/documents/nope/{kind}", headers=viewer_headers)
    assert response.status_code == 404
    assert fake.requests == []


async def test_admin_can_preview_confidential_document(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
) -> None:
    """Admins see confidential documents, so the file is streamed to them."""
    _seed(tmp_db_path, "doc-secret", confidential=True)
    fake = upstream()
    async with client as ac:
        response = await ac.get("/documents/doc-secret/preview", headers=admin_headers)
    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert len(fake.requests) == 1


# --------------------------------------------------------------------------- #
# Successful proxying
# --------------------------------------------------------------------------- #


async def test_preview_streams_pdf_inline_with_safe_headers(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    portal_env: dict[str, str],
    upstream: Callable[..., _Upstream],
) -> None:
    """Preview sends the key upstream and serves the PDF inline."""
    _seed(tmp_db_path)
    fake = upstream()
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'inline; filename="Operating Agreement.pdf"; '
        "filename*=UTF-8''Operating%20Agreement.pdf"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["content-length"] == str(len(PDF_BYTES))

    (sent,) = fake.requests
    assert sent.method == "GET"
    assert str(sent.url).startswith(portal_env["BACKEND_LLC_MANAGER_URL"])
    assert sent.url.path == "/api/v1/documents/doc-1/file"
    assert sent.headers["X-API-Key"] == portal_env["BACKEND_LLC_MANAGER_API_KEY"]
    # The viewer's identity token is never forwarded upstream.
    assert "x-authentik-jwt" not in sent.headers


async def test_download_uses_attachment(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
) -> None:
    """Download serves the same file as an attachment."""
    _seed(tmp_db_path)
    upstream()
    async with client as ac:
        response = await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert response.content == PDF_BYTES


@pytest.mark.parametrize(
    ("upstream_type", "served_type"),
    [
        ("image/png", "image/png"),
        ("IMAGE/JPEG", "image/jpeg"),
        ("application/pdf; charset=binary", "application/pdf"),
    ],
)
async def test_preview_serves_allowed_inline_types(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    upstream_type: str,
    served_type: str,
) -> None:
    """PDFs and images preview inline with a normalized content type."""
    seeded_upstream(
        lambda _r: httpx.Response(
            200, content=b"data", headers={"Content-Type": upstream_type}
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.headers["content-type"] == served_type
    assert response.headers["content-disposition"].startswith("inline; ")


@pytest.mark.parametrize(
    ("upstream_type", "served_type"),
    [
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("text/html", "application/octet-stream"),
        ("image/svg+xml", "application/octet-stream"),
        ("application/javascript", "application/octet-stream"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
    ],
)
async def test_preview_of_non_previewable_type_is_an_attachment(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    upstream_type: str | None,
    served_type: str,
) -> None:
    """Anything that is not a PDF or a safe image downloads instead of rendering.

    Types outside the allowlist, including ones a browser would run as script,
    are served as ``application/octet-stream``.
    """
    headers = {} if upstream_type is None else {"Content-Type": upstream_type}
    seeded_upstream(lambda _r: httpx.Response(200, content=b"<x>", headers=headers))
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == served_type
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_document_id_is_percent_encoded_in_upstream_path(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
) -> None:
    """An odd cached ID cannot change the upstream path or add a query."""
    _seed(tmp_db_path, "a b?x=1#y")
    fake = upstream()
    async with client as ac:
        response = await ac.get(
            "/documents/a%20b%3Fx%3D1%23y/preview", headers=viewer_headers
        )
    assert response.status_code == 200
    (sent,) = fake.requests
    assert sent.url.raw_path == b"/api/v1/documents/a%20b%3Fx%3D1%23y/file"
    assert sent.url.query == b""


# --------------------------------------------------------------------------- #
# Upstream failures map to plain errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("upstream_status", "portal_status"),
    [
        (404, 404),
        (401, 502),
        (403, 502),
        (302, 502),
        (500, 502),
        (503, 502),
    ],
)
async def test_upstream_errors_map_to_plain_errors_without_upstream_body(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    upstream_status: int,
    portal_status: int,
) -> None:
    """Upstream error bodies and headers never reach the browser."""
    seeded_upstream(
        lambda _r: httpx.Response(
            upstream_status,
            text=UPSTREAM_ERROR_BODY,
            headers={"Location": "http://elsewhere.test/", "X-Upstream": "1"},
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert response.status_code == portal_status
    assert UPSTREAM_ERROR_BODY not in response.text
    assert "location" not in response.headers
    assert "x-upstream" not in response.headers
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize(
    ("error", "portal_status"),
    [
        (httpx.ConnectTimeout("slow"), 504),
        (httpx.ReadTimeout("slow"), 504),
        (httpx.ConnectError("refused"), 502),
        (httpx.RemoteProtocolError("bad"), 502),
    ],
)
async def test_transport_failures_map_to_gateway_errors(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    error: httpx.HTTPError,
    portal_status: int,
) -> None:
    """Timeouts are 504 and other transport failures 502, as plain text."""

    def _raise(_request: httpx.Request) -> httpx.Response:
        raise error

    seeded_upstream(_raise)
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == portal_status
    assert response.text == "This is not available right now. Please try again later."


async def test_declared_length_over_cap_is_refused_before_streaming(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose declared size is over the cap is not streamed at all."""
    monkeypatch.setattr(document_files, "MAX_DOCUMENT_BYTES", 100)
    _seed(tmp_db_path)
    stream = _ChunkStream([b"x" * 200])
    upstream(
        lambda _r: httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "application/pdf", "Content-Length": "200"},
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 502
    assert b"x" * 200 not in response.content
    assert stream.closed


async def test_undeclared_length_over_cap_aborts_the_stream(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a length header, the cap is enforced while streaming.

    The response is aborted rather than ended cleanly, so a browser never
    keeps a silently truncated file.
    """
    monkeypatch.setattr(document_files, "MAX_DOCUMENT_BYTES", 100)
    _seed(tmp_db_path)
    stream = _ChunkStream([b"x" * 60, b"x" * 60, b"x" * 60])
    upstream(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    async with client as ac:
        with pytest.raises(document_files.DocumentStreamAbortedError):
            await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert stream.closed


async def test_upstream_failure_mid_stream_aborts_the_response(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
) -> None:
    """A dropped upstream connection aborts the response and closes upstream."""
    _seed(tmp_db_path)
    stream = _ChunkStream([b"a" * 10, b"b" * 10], fail_after=1)
    upstream(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    async with client as ac:
        with pytest.raises(document_files.DocumentStreamAbortedError):
            await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert stream.closed


async def test_malformed_content_length_is_not_forwarded(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
) -> None:
    """A length header that is not a whole number is ignored; the cap still holds."""
    _seed(tmp_db_path)
    stream = _ChunkStream([b"abc"])
    upstream(
        lambda _r: httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "application/pdf", "Content-Length": "-3"},
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 200
    assert response.content == b"abc"
    assert response.headers.get("content-length") != "-3"


async def test_compressed_upstream_body_is_decoded_without_stale_length(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
) -> None:
    """A compressed body is sent on decoded, without the compressed length."""
    compressed = gzip.compress(PDF_BYTES)
    fake = seeded_upstream(
        lambda _r: httpx.Response(
            200,
            content=compressed,
            headers={"Content-Type": "application/pdf", "Content-Encoding": "gzip"},
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers.get("content-length") != str(len(compressed))
    assert "content-encoding" not in response.headers
    assert fake.requests[0].headers["Accept-Encoding"] == "identity"


async def test_transfer_past_the_deadline_aborts_the_stream(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upstream that trickles one byte at a time is cut off at the limit.

    Each byte arrives well inside the read timeout, so only the whole-response
    limit can stop it. Without that limit this transfer would take 4 s.
    """
    monkeypatch.setattr(document_files, "MAX_TRANSFER_SECONDS", 0.3)
    stream = _SlowStream(80, pause=0.05)
    seeded_upstream(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    started = time.monotonic()
    with capture_logs() as logs:
        async with client as ac:
            with pytest.raises(document_files.DocumentStreamAbortedError):
                await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert time.monotonic() - started < 2.0
    assert 0 < stream.sent < 80
    assert stream.closed
    assert any(e["event"] == "document_stream_too_slow" for e in logs)


async def test_stalled_browser_is_cut_off_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser that stops reading cannot hold the upstream open forever.

    The server's ``send`` blocks on flow control; the limit still fires and
    the upstream file is closed.
    """
    monkeypatch.setattr(document_files, "MAX_TRANSFER_SECONDS", 0.2)
    stream = _ChunkStream([b"a" * 10, b"b" * 10])
    upstream_client, request = _upstream_file(stream)
    response = await upstream_client.send(request, stream=True)
    upstream = document_files.UpstreamFile(
        client=upstream_client,
        response=response,
        content_type="application/pdf",
        content_length=None,
    )
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/documents/doc-1/preview",
        "headers": [],
    }

    async def receive() -> Message:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            await asyncio.sleep(3600)

    file_response = document_files.UpstreamFileResponse(upstream, headers={})
    started = time.monotonic()
    with pytest.raises(document_files.DocumentStreamAbortedError):
        await file_response(scope, receive, send)
    assert time.monotonic() - started < 2.0
    assert stream.closed
    assert upstream_client.is_closed


@pytest.mark.parametrize("status_code", [200, 404, 500])
async def test_upstream_is_closed_after_success_and_failure(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    status_code: int,
) -> None:
    """The upstream response and its client close whether sent or refused.

    httpx closes an exhausted response stream by itself, so the client is
    what proves the proxy's own close ran on the success path.
    """
    stream = _ChunkStream([b"a" * 10])
    fake = seeded_upstream(
        lambda _r: httpx.Response(
            status_code, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    async with client as ac:
        await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert stream.closed
    (upstream_client,) = fake.clients
    assert upstream_client.is_closed


async def test_api_key_never_reaches_logs_or_responses(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    portal_env: dict[str, str],
) -> None:
    """The backend key is sent upstream only, never logged or echoed."""
    key = portal_env["BACKEND_LLC_MANAGER_API_KEY"]
    outcomes = iter(
        [
            httpx.Response(200, content=PDF_BYTES),
            httpx.Response(401, text="bad key"),
            httpx.Response(500, text="boom"),
        ]
    )
    seeded_upstream(lambda _r: next(outcomes))
    bodies: list[str] = []
    with capture_logs() as logs:
        async with client as ac:
            for _ in range(3):
                response = await ac.get(
                    "/documents/doc-1/preview", headers=viewer_headers
                )
                bodies.append(response.text)
                bodies.append(str(response.headers))
    assert logs
    assert key not in repr(logs)
    assert all(key not in body for body in bodies)


def _upstream_file(stream: _ChunkStream) -> tuple[httpx.AsyncClient, httpx.Request]:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    upstream_client = httpx.AsyncClient(transport=transport)
    return upstream_client, upstream_client.build_request("GET", "http://u.test/f")


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_client_disconnect_closes_upstream(spec_version: str) -> None:
    """A browser that goes away before or during the body still frees upstream.

    Under ASGI 2.0 the server reports the disconnect through ``receive``
    before any chunk is sent; under 2.4 ``send`` raises instead.
    """
    stream = _ChunkStream([b"a" * 10, b"b" * 10])
    upstream_client, request = _upstream_file(stream)
    response = await upstream_client.send(request, stream=True)
    upstream = document_files.UpstreamFile(
        client=upstream_client,
        response=response,
        content_type="application/pdf",
        content_length=None,
    )
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "method": "GET",
        "path": "/documents/doc-1/preview",
        "headers": [],
    }

    async def receive() -> Message:
        if spec_version == "2.0":
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if spec_version == "2.4" and message["type"] == "http.response.body":
            msg = "client went away"
            raise OSError(msg)
        if spec_version == "2.0":
            await asyncio.sleep(0)

    file_response = document_files.UpstreamFileResponse(upstream, headers={})
    # The disconnect may surface as an exception; closing is what matters.
    with contextlib.suppress(Exception):
        await file_response(scope, receive, send)
    assert stream.closed
    assert upstream_client.is_closed


async def test_cancelled_request_closes_the_client(
    monkeypatch: pytest.MonkeyPatch, portal_env: dict[str, str]
) -> None:
    """Cancellation while waiting for upstream headers still closes the client."""

    def _cancel(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    created: list[httpx.AsyncClient] = []

    def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
        made = httpx.AsyncClient(
            transport=httpx.MockTransport(_cancel), timeout=timeout
        )
        created.append(made)
        return made

    monkeypatch.setattr(document_files, "new_client", _client)
    connection = load_settings().backend_connection("llc_manager")
    assert connection is not None
    del portal_env
    with pytest.raises(asyncio.CancelledError):
        await document_files.open_upstream(connection, "doc-1", timeout_seconds=1.0)
    assert created[0].is_closed


@pytest.mark.parametrize(
    "case",
    [(100, 200), (101, 502)],
    ids=["at-cap", "over-cap"],
)
async def test_declared_length_cap_boundary(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[int, int],
) -> None:
    """A declared size equal to the cap is served; one byte more is refused."""
    declared, status_code = case
    monkeypatch.setattr(document_files, "MAX_DOCUMENT_BYTES", 100)
    seeded_upstream(
        lambda _r: httpx.Response(
            200, content=b"x" * declared, headers={"Content-Type": "application/pdf"}
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert response.status_code == status_code


async def test_streamed_size_at_the_cap_is_served(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a declared length, exactly the cap streams in full."""
    monkeypatch.setattr(document_files, "MAX_DOCUMENT_BYTES", 100)
    stream = _ChunkStream([b"x" * 50, b"x" * 50])
    seeded_upstream(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    async with client as ac:
        response = await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert response.status_code == 200
    assert response.content == b"x" * 100


async def test_streamed_size_one_over_the_cap_aborts(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a declared length, one byte over the cap aborts the stream."""
    monkeypatch.setattr(document_files, "MAX_DOCUMENT_BYTES", 100)
    stream = _ChunkStream([b"x" * 50, b"x" * 51])
    seeded_upstream(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )
    async with client as ac:
        with pytest.raises(document_files.DocumentStreamAbortedError):
            await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert stream.closed


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (401, "upstream_auth_rejected"),
        (403, "upstream_auth_rejected"),
        (404, "upstream_not_found"),
        (500, "upstream_error"),
    ],
)
async def test_upstream_refusals_log_their_reason(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    status_code: int,
    reason: str,
) -> None:
    """Each refusal logs its reason category, which is what operators search.

    ``upstream_auth_rejected`` is the signal to look for after a key rotation.
    """
    seeded_upstream(lambda _r: httpx.Response(status_code, text="detail"))
    with capture_logs() as logs:
        async with client as ac:
            await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    failures = [e for e in logs if e["event"] == "document_file_failed"]
    assert failures == [
        {
            "event": "document_file_failed",
            "reason": reason,
            "upstream_status": status_code,
            "log_level": "warning",
        }
    ]


async def test_failure_building_response_headers_closes_upstream(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the headers cannot be built, the open upstream file is still closed."""
    stream = _ChunkStream([b"a" * 10])
    fake = seeded_upstream(
        lambda _r: httpx.Response(
            200, stream=stream, headers={"Content-Type": "application/pdf"}
        )
    )

    def _broken(*_args: object, **_kwargs: object) -> dict[str, str]:
        msg = "header bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(document_files, "response_headers", _broken)
    async with client as ac:
        with pytest.raises(RuntimeError, match="header bug"):
            await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert stream.closed
    assert fake.clients[0].is_closed


@pytest.mark.parametrize("document_id", [".", ".."])
async def test_dot_segment_id_is_404_with_no_upstream_call(
    upstream: Callable[..., _Upstream],
    portal_env: dict[str, str],
    document_id: str,
) -> None:
    """An ID httpx would drop as a dot segment never reaches upstream."""
    del portal_env
    fake = upstream()
    connection = load_settings().backend_connection("llc_manager")
    assert connection is not None
    with pytest.raises(document_files.FileProxyError) as caught:
        await document_files.open_upstream(connection, document_id, timeout_seconds=1.0)
    assert caught.value.status_code == 404
    assert fake.requests == []


@pytest.mark.parametrize(
    "setting",
    [
        ("BACKEND_LLC_MANAGER_URL", "http://[::1"),
        ("BACKEND_LLC_MANAGER_API_KEY", "k\u00e9y-not-ascii"),
    ],
    ids=["bad-url", "non-ascii-key"],
)
async def test_unusable_backend_settings_are_503_and_close_the_client(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    seeded_upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
    setting: tuple[str, str],
) -> None:
    """A URL or key that cannot form a request is a 503, not an unmapped 500."""
    env, value = setting
    fake = seeded_upstream()
    async with client as ac:
        monkeypatch.setenv(env, value)
        with capture_logs() as logs:
            response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 503
    assert fake.requests == []
    assert all(c.is_closed for c in fake.clients)
    assert value not in repr(logs)
    assert any(e.get("reason") == "backend_misconfigured" for e in logs)


class _FailingCloseStream(_ChunkStream):
    """An upstream body whose close fails, as a broken connection can."""

    async def aclose(self) -> None:
        self.closed = True
        msg = "close failed"
        raise httpx.ReadError(msg)


async def test_a_failing_close_is_logged_not_raised() -> None:
    """A close error cannot replace the outcome the caller is reporting."""
    stream = _FailingCloseStream([b"a"])
    upstream_client, request = _upstream_file(stream)
    response = await upstream_client.send(request, stream=True)
    upstream = document_files.UpstreamFile(
        client=upstream_client,
        response=response,
        content_type="application/pdf",
        content_length=None,
    )
    with capture_logs() as logs:
        await upstream.aclose()
    assert stream.closed
    assert upstream_client.is_closed
    assert {
        "event": "document_upstream_close_failed",
        "error": "ReadError",
        "log_level": "warning",
    } in logs


class _SlowCloseStream(_ChunkStream):
    """An upstream body whose close takes a moment, so it can be cancelled."""

    async def aclose(self) -> None:
        await asyncio.sleep(0.05)
        self.closed = True


async def _open_slow_close() -> tuple[
    document_files.UpstreamFile, _SlowCloseStream, httpx.AsyncClient
]:
    stream = _SlowCloseStream([b"a"])
    upstream_client, request = _upstream_file(stream)
    response = await upstream_client.send(request, stream=True)
    upstream = document_files.UpstreamFile(
        client=upstream_client,
        response=response,
        content_type="application/pdf",
        content_length=None,
    )
    return upstream, stream, upstream_client


async def test_scope_cancelled_close_still_closes_everything() -> None:
    """The time limit or a disconnect cancelling mid-close cannot leak upstream.

    Both reach ``aclose`` as cancel-scope cancellation; the shielded scope
    lets the close finish. Without the shield the slow response close would
    be interrupted and the client never reached.
    """
    upstream, stream, upstream_client = await _open_slow_close()
    async with anyio.create_task_group() as group:
        group.start_soon(upstream.aclose)
        await asyncio.sleep(0.01)
        group.cancel_scope.cancel()
    assert stream.closed
    assert upstream_client.is_closed


async def test_task_cancelled_close_still_closes_the_client() -> None:
    """A direct task.cancel() mid-close still closes the client, then re-raises."""
    upstream, _stream, upstream_client = await _open_slow_close()
    task = asyncio.ensure_future(upstream.aclose())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert upstream_client.is_closed


def test_file_proxy_error_can_be_copied() -> None:
    """The error keeps its status and reason through copy, and prints the reason."""
    error = document_files.FileProxyError(502, "upstream_error")
    copied = copy.copy(error)
    assert copied.status_code == 502
    assert copied.reason == "upstream_error"
    assert str(copied) == "upstream_error"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Content-Length": "12"}, 12),
        ({"Content-Length": "12", "Content-Encoding": "identity"}, 12),
        ({"Content-Length": "12", "Content-Encoding": " IDENTITY "}, 12),
        ({"Content-Length": "12", "Content-Encoding": "br"}, None),
        ({"Content-Length": ""}, None),
        ({"Content-Length": "1\u0662"}, None),
        ({"Content-Length": "+12"}, None),
        ({"Content-Length": "9" * 30}, int("9" * 30)),
        ({}, None),
    ],
    ids=[
        "plain",
        "identity",
        "identity-padded",
        "brotli",
        "empty",
        "non-ascii-digit",
        "signed",
        "very-large",
        "missing",
    ],
)
def test_declared_length(headers: dict[str, str], expected: int | None) -> None:
    """Only a plain ASCII whole number for an uncompressed body is trusted."""
    raw = httpx.Headers([(k.encode(), v.encode("utf-8")) for k, v in headers.items()])
    assert document_files._declared_length(raw) == expected  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Backend not connected or misconfigured
# --------------------------------------------------------------------------- #


async def test_not_connected_backend_is_503_with_no_upstream_call(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no document service URL, a cached document cannot be fetched."""
    _seed(tmp_db_path)
    fake = upstream()
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        response = await ac.get("/documents/doc-1/preview", headers=viewer_headers)
    assert response.status_code == 503
    assert fake.requests == []


async def test_url_without_key_is_503_with_no_upstream_call(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL whose key went blank after startup never sends a keyless request."""
    _seed(tmp_db_path)
    fake = upstream()
    async with client as ac:
        monkeypatch.setenv("BACKEND_LLC_MANAGER_API_KEY", "   ")
        response = await ac.get("/documents/doc-1/download", headers=viewer_headers)
    assert response.status_code == 503
    assert fake.requests == []


async def test_default_client_does_not_follow_redirects() -> None:
    """The production client never follows a redirect off the backend."""
    client = document_files.new_client(httpx.Timeout(1.0))
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.timeout.read == 1.0
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# Content type, file name, and Content-Disposition helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("application/pdf", "application/pdf"),
        (" Application/PDF ; q=1", "application/pdf"),
        ("image/webp", "image/webp"),
        ("text/plain", "text/plain"),
        ("text/html; charset=utf-8", "application/octet-stream"),
        ("image/svg+xml", "application/octet-stream"),
        ("", "application/octet-stream"),
        (None, "application/octet-stream"),
    ],
)
def test_allowed_content_type(raw: str | None, expected: str) -> None:
    """Only allowlisted types pass; everything else is a generic binary."""
    assert document_files.allowed_content_type(raw) == expected


def test_inline_types_are_all_allowlisted() -> None:
    """Every inline type is also allowlisted, so no new type can go inline alone."""
    allowed = {
        document_files.allowed_content_type(t)
        for t in document_files.INLINE_CONTENT_TYPES
    }
    assert allowed == set(document_files.INLINE_CONTENT_TYPES)
    assert "image/svg+xml" not in document_files.INLINE_CONTENT_TYPES


@pytest.mark.parametrize(
    ("content_type", "requested", "expected"),
    [
        ("application/pdf", "inline", "inline"),
        ("image/png", "inline", "inline"),
        ("text/plain", "inline", "attachment"),
        ("application/octet-stream", "inline", "attachment"),
        ("application/pdf", "attachment", "attachment"),
    ],
)
def test_effective_disposition(
    content_type: str, requested: str, expected: str
) -> None:
    """Only PDFs and images are ever shown inline."""
    assert (
        document_files.effective_disposition(
            requested,
            content_type,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("title", "content_type", "expected"),
    [
        ("Operating Agreement", "application/pdf", "Operating Agreement.pdf"),
        ("Scan.PDF", "application/pdf", "Scan.PDF"),
        ("Photo", "image/jpeg", "Photo.jpg"),
        ("photo.jpeg", "image/jpeg", "photo.jpeg"),
        ("Scan.TIFF", "image/tiff", "Scan.TIFF"),
        ("Scan.tif", "image/tiff", "Scan.tif"),
        ("Notes", "application/octet-stream", "Notes"),
        ("", "application/pdf", "document.pdf"),
        ("   ...  ", "application/pdf", "document.pdf"),
        ("a/b\\c", "application/pdf", "a_b_c.pdf"),
        ("x" * 300, "application/pdf", "x" * 150 + ".pdf"),
    ],
)
def test_download_filename(title: str, content_type: str, expected: str) -> None:
    """File names come from the title, cleaned, with a matching extension."""
    assert document_files.download_filename(title, content_type) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Evil\r\nSet-Cookie: session=1",
        "Line\nbreak",
        'Quote" and backslash\\ and ;semicolon',
        "Tab\there\x00nul\x7fdel",
        "Bidi \u202eFDP.exe",
        "Line\u2028separator",
    ],
)
def test_content_disposition_cannot_inject_headers(title: str) -> None:
    """Control and formatting characters never reach the header value."""
    value = document_files.content_disposition("attachment", title, "application/pdf")
    assert all(ch.isprintable() for ch in value)
    assert "\r" not in value
    assert "\n" not in value
    ascii_part = value.split("; filename*=")[0]
    quoted = ascii_part.split('filename="', 1)[1]
    assert quoted.endswith('"')
    assert '"' not in quoted[:-1]
    assert "\\" not in quoted
    assert value.isascii()


def test_content_disposition_encodes_non_ascii_per_rfc_6266() -> None:
    """Non-ASCII names get an ASCII fallback plus an RFC 5987 ``filename*``."""
    value = document_files.content_disposition(
        "inline", "Résumé 2025 \u2013 final", "application/pdf"
    )
    assert value == (
        'inline; filename="Resume 2025 _ final.pdf"; '
        "filename*=UTF-8''R%C3%A9sum%C3%A9%202025%20%E2%80%93%20final.pdf"
    )


def test_content_disposition_for_entirely_non_latin_title() -> None:
    """A title with no ASCII letters still yields a usable fallback name."""
    value = document_files.content_disposition("attachment", "遗嘱", "application/pdf")
    assert value.startswith('attachment; filename="__.pdf"; ')
    assert value.endswith("filename*=UTF-8''%E9%81%97%E5%98%B1.pdf")


# --------------------------------------------------------------------------- #
# Links, including a page fragment for PDF citations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "/documents/doc-1/preview"),
        ({"page": 7}, "/documents/doc-1/preview#page=7"),
        ({"page": "12"}, "/documents/doc-1/preview#page=12"),
        ({"page": 0}, "/documents/doc-1/preview"),
        ({"page": -3}, "/documents/doc-1/preview"),
        ({"page": "7;alert(1)"}, "/documents/doc-1/preview"),
        ({"page": True}, "/documents/doc-1/preview"),
        ({"page": None}, "/documents/doc-1/preview"),
        ({"page": 999999}, "/documents/doc-1/preview#page=999999"),
        ({"page": "1" * 7}, "/documents/doc-1/preview"),
        ({"page": "1" * 5000}, "/documents/doc-1/preview"),
        ({"kind": "download"}, "/documents/doc-1/download"),
        ({"kind": "download", "page": 4}, "/documents/doc-1/download"),
    ],
)
def test_document_url(kwargs: dict[str, Any], expected: str) -> None:
    """Preview links can open at a page; download links never carry one."""
    assert document_files.document_url("doc-1", **kwargs) == expected


def test_document_url_encodes_the_id() -> None:
    """IDs are percent-encoded so they stay one path segment."""
    assert (
        document_files.document_url("a/b c", page=2)
        == "/documents/a%2Fb%20c/preview#page=2"
    )


async def test_preview_url_with_page_fragment_serves_the_pdf(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    upstream: Callable[..., _Upstream],
) -> None:
    """A citation link with ``#page=N`` reaches the same inline PDF.

    The fragment stays in the browser, which opens its PDF viewer at that
    page; the proxy must answer 200 inline with no redirect that could lose it.
    """
    _seed(tmp_db_path)
    upstream()
    url = document_files.document_url("doc-1", page=7)
    async with client as ac:
        response = await ac.get(url, headers=viewer_headers)
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline; ")


async def test_documents_page_links_preview_and_download(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Each listed document offers both a preview and a download link."""
    _seed(tmp_db_path)
    async with client as ac:
        page = await ac.get("/documents", headers=viewer_headers)
        search = await ac.get("/documents/search?q=Operating", headers=viewer_headers)
    for response in (page, search):
        assert 'href="/documents/doc-1/preview"' in response.text
        assert 'href="/documents/doc-1/download"' in response.text


async def test_search_without_document_service_lists_no_file_links(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnected, the search fragment says so instead of offering links."""
    _seed(tmp_db_path)
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        search = await ac.get("/documents/search?q=Operating", headers=viewer_headers)
    assert search.status_code == 200
    assert "Not connected yet" in search.text
    assert "/documents/doc-1/" not in search.text


async def test_home_and_entity_pages_use_document_links(
    client: httpx.AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
) -> None:
    """Home and entity pages build links with the same filter as Documents."""
    _seed(tmp_db_path, "doc 2")
    with contextlib.closing(sqlite3.connect(tmp_db_path)) as conn:
        conn.execute("UPDATE documents SET entity_id = 'e1', added_at = '2026-10-01'")
        conn.execute(
            "INSERT INTO entities (id, name, type, fetched_at) "
            "VALUES ('e1', 'Family LLC', 'LLC', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    async with client as ac:
        home = await ac.get("/", headers=viewer_headers)
        entity = await ac.get("/entities/e1", headers=viewer_headers)
    assert 'href="/documents/doc%202/preview"' in home.text
    assert 'href="/documents/doc%202/preview"' in entity.text
    assert 'href="/documents/doc%202/download"' in entity.text
