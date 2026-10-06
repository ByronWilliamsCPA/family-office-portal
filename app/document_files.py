# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Streaming proxy for document files held by llc-manager (ADR-007).

Document metadata is cached like every other dataset (ADR-003), but the file
bytes are not: caching every family document on the portal's disk would make
the portal a second document store. So preview and download are the one
place a request handler calls a backend directly. ADR-007 records that
exception and its limits.

The caller must decide whether the viewer may see the document *before*
calling ``open_upstream``; this module never sees the viewer and fetches
whatever it is asked for.

#CRITICAL: security: the confidential check in ``app.routes.documents`` runs
against the cache before ``open_upstream`` is called, so a Viewer's request
for a confidential document never produces an upstream request.
#VERIFY: ``tests/unit/test_document_files.py`` asserts that the fake upstream
recorded no request for a Viewer asking for a confidential file.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

import anyio
import httpx
import structlog
from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.types import Receive, Scope, Send

    from app.config import BackendConnection

logger = structlog.get_logger(__name__)

Disposition = Literal["inline", "attachment"]

# Largest file the proxy will pass through. The documents are scanned legal,
# tax and insurance papers; a larger file is refused rather than tying up the
# single worker.
# #ASSUME: external resources: no family document is over 50 MiB.
# #VERIFY: compare against the largest file the document service holds before
# connecting it; raise the cap here if needed.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

# Longest a single transfer may run. The backend timeout bounds each read,
# not the whole transfer, so an upstream that trickles bytes could otherwise
# hold the single worker's connection indefinitely.
# #ASSUME: timing: five minutes is enough for the largest file on the
# private network. #VERIFY: time a download of the largest stored file.
MAX_TRANSFER_SECONDS = 300.0

_CHUNK_BYTES = 64 * 1024

_HTTP_OK = 200
_HTTP_NOT_FOUND = 404
_HTTP_AUTH_REJECTED = frozenset({401, 403})

GENERIC_CONTENT_TYPE = "application/octet-stream"

# Types a browser may render inline. None of them can run script.
INLINE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)

# Every type the proxy will label as itself, mapped to its file extension.
# Anything else, including HTML and SVG, is served as a generic binary
# attachment so the browser cannot render it in the portal's origin.
_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/tiff": ".tif",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        ".docx"
    ),
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

_MAX_STEM_CHARS = 150
_DEFAULT_STEM = "document"
_PATH_SEPARATORS = re.compile(r"[/\\]")
_WHITESPACE = re.compile(r"\s+")
_UNSAFE_ASCII = re.compile(r"[^A-Za-z0-9 ._()\-]")
# Unicode categories removed from names: controls, format characters (such as
# bidirectional overrides), surrogates, private use, unassigned, and line or
# paragraph separators. Header injection needs one of these.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class FileProxyError(Exception):
    """The upstream file could not be served; carries the portal's status.

    Args:
        status_code (int): HTTP status for the portal's response.
        reason (str): Short category for logs; never upstream content.

    Attributes:
        status_code (int): HTTP status for the portal's response.
        reason (str): Short category for logs; never upstream content.
    """

    status_code: int
    reason: str

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


class DocumentStreamAbortedError(Exception):
    """The file stream stopped after the response began.

    Raised from inside the response body so the server drops the connection
    instead of ending the body cleanly; a browser then reports a failed
    download rather than keeping a truncated file.
    """


@dataclass
class UpstreamFile:
    """An open upstream response whose body has not been read yet.

    Attributes:
        client (httpx.AsyncClient): Client to close when the body is done.
        response (httpx.Response): Streaming upstream response.
        content_type (str): Allowlisted content type to serve.
        content_length (int | None): Declared size, when valid.
    """

    client: httpx.AsyncClient
    response: httpx.Response
    content_type: str
    content_length: int | None

    async def aclose(self) -> None:
        """Close the upstream response and its client, even when cancelled.

        Safe to call more than once.
        """
        with anyio.CancelScope(shield=True):
            try:
                await self.response.aclose()
            finally:
                await self.client.aclose()

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """Yield the body, enforcing the size cap, then close upstream.

        Yields:
            bytes: The next chunk of the file.

        Raises:
            DocumentStreamAbortedError: If the file grows past
                ``MAX_DOCUMENT_BYTES``, the transfer runs past
                ``MAX_TRANSFER_SECONDS``, or the upstream connection fails
                part way through.
        """
        sent = 0
        deadline = time.monotonic() + MAX_TRANSFER_SECONDS
        try:
            async for chunk in self.response.aiter_bytes(_CHUNK_BYTES):
                sent += len(chunk)
                if time.monotonic() > deadline:
                    logger.warning(
                        "document_stream_too_slow", limit_seconds=MAX_TRANSFER_SECONDS
                    )
                    msg = "file transfer took longer than the proxy allows"
                    raise DocumentStreamAbortedError(msg)
                if sent > MAX_DOCUMENT_BYTES:
                    logger.warning(
                        "document_stream_too_large", limit=MAX_DOCUMENT_BYTES
                    )
                    msg = "file is larger than the proxy allows"
                    raise DocumentStreamAbortedError(msg)
                yield chunk
        except httpx.HTTPError as exc:
            logger.warning("document_stream_interrupted", error=type(exc).__name__)
            msg = "upstream stream failed"
            raise DocumentStreamAbortedError(msg) from exc
        finally:
            await self.aclose()


class UpstreamFileResponse(StreamingResponse):
    """A streaming response that always closes its upstream file.

    Starlette runs background tasks only after a clean finish, and an async
    generator that is never resumed never runs its ``finally``. Closing in
    ``__call__`` covers a client that disconnects before or during the body.

    Args:
        upstream (UpstreamFile): The open upstream file.
        headers (dict[str, str]): Response headers to send.
    """

    def __init__(self, upstream: UpstreamFile, headers: dict[str, str]) -> None:
        super().__init__(upstream.iter_bytes(), headers=headers)
        self._close_upstream: Callable[[], Awaitable[None]] = upstream.aclose

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, then close upstream however sending ended.

        Args:
            scope (Scope): ASGI connection scope.
            receive (Receive): ASGI receive channel.
            send (Send): ASGI send channel.
        """
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._close_upstream()


def new_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Build the HTTP client used for one file request.

    Redirects are not followed and proxy environment variables are ignored,
    so the API key is only ever sent to the configured backend.

    Args:
        timeout (httpx.Timeout): Connect, read, write and pool timeouts.

    Returns:
        httpx.AsyncClient: A new client; the caller closes it.
    """
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)


def allowed_content_type(raw: str | None) -> str:
    """Reduce an upstream ``Content-Type`` to an allowlisted media type.

    Args:
        raw (str | None): Header value from upstream, possibly with parameters.

    Returns:
        str: The bare, lower-case media type when allowlisted, otherwise
        ``application/octet-stream``.
    """
    media_type = (raw or "").split(";", 1)[0].strip().lower()
    return media_type if media_type in _EXTENSIONS else GENERIC_CONTENT_TYPE


def effective_disposition(requested: Disposition, content_type: str) -> Disposition:
    """Decide whether a file is shown inline or downloaded.

    Args:
        requested (Disposition): What the route asked for.
        content_type (str): Allowlisted content type being served.

    Returns:
        Disposition: ``inline`` only for a preview of a PDF or image.
    """
    if requested == "inline" and content_type in INLINE_CONTENT_TYPES:
        return "inline"
    return "attachment"


def download_filename(title: str, content_type: str) -> str:
    """Build a safe file name from a document title.

    Control, format and separator characters are removed, path separators are
    replaced, length is capped, and the extension for the content type is
    added when the title does not already end with it.

    Args:
        title (str): Cached document title.
        content_type (str): Allowlisted content type being served.

    Returns:
        str: A non-empty Unicode file name.
    """
    kept = "".join(
        " " if unicodedata.category(ch) in _STRIPPED_CATEGORIES else ch for ch in title
    )
    stem = _WHITESPACE.sub(" ", _PATH_SEPARATORS.sub("_", kept)).strip(" .")
    stem = stem[:_MAX_STEM_CHARS].rstrip(" .") or _DEFAULT_STEM
    extension = _EXTENSIONS.get(content_type, "")
    if extension and not stem.lower().endswith(extension):
        return stem + extension
    return stem


def _ascii_fallback(filename: str) -> str:
    decomposed = unicodedata.normalize("NFKD", filename)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _UNSAFE_ASCII.sub("_", without_marks)


def content_disposition(disposition: Disposition, title: str, content_type: str) -> str:
    """Build a ``Content-Disposition`` value that cannot inject headers.

    Follows RFC 6266: a quoted ASCII ``filename`` for old clients plus an
    RFC 5987 ``filename*`` carrying the UTF-8 name percent-encoded.

    Args:
        disposition (Disposition): ``inline`` or ``attachment``.
        title (str): Cached document title.
        content_type (str): Allowlisted content type being served.

    Returns:
        str: Header value containing printable ASCII only.
    """
    filename = download_filename(title, content_type)
    fallback = _ascii_fallback(filename)
    encoded = quote(filename, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _declared_length(headers: httpx.Headers) -> int | None:
    """Return the upstream body length when it matches the bytes sent on.

    Args:
        headers (httpx.Headers): Upstream response headers.

    Returns:
        int | None: The declared length, or None when it is missing, not a
        whole number, or describes a compressed body that httpx decodes.
    """
    raw = headers.get("content-length")
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if raw is None or not (raw.isascii() and raw.isdigit()):
        return None
    if encoding not in {"", "identity"}:
        return None
    return int(raw)


async def open_upstream(
    connection: BackendConnection,
    document_id: str,
    *,
    timeout_seconds: float,
) -> UpstreamFile:
    """Request one document file from the backend and check the response.

    Only the status line and headers are read here; the body is streamed
    later by ``UpstreamFile.iter_bytes``. On any failure the upstream
    response is closed without reading its body, so no upstream content can
    reach the browser or the logs.

    Args:
        connection (BackendConnection): Document service URL and API key.
        document_id (str): Identifier of a document the viewer may see.
        timeout_seconds (float): Timeout for connect and each read.

    Returns:
        UpstreamFile: The open response, ready to stream.

    Raises:
        FileProxyError: 404 when the backend has no such file, 504 on a
            timeout, 502 for any other failure or a file over the size cap.
    """
    url = (
        f"{connection.url.rstrip('/')}/api/v1/documents/"
        f"{quote(document_id, safe='')}/file"
    )
    client = new_client(httpx.Timeout(timeout_seconds))
    request = client.build_request(
        "GET",
        url,
        headers={
            "Accept": "*/*",
            # The body is passed through decoded, so ask for no compression;
            # a declared length then matches the bytes sent on.
            "Accept-Encoding": "identity",
            "X-API-Key": connection.api_key,
        },
    )
    sent = False
    try:
        response = await client.send(request, stream=True)
        sent = True
    except httpx.TimeoutException as exc:
        logger.warning("document_file_failed", reason="upstream_timeout")
        raise FileProxyError(504, "upstream_timeout") from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "document_file_failed",
            reason="upstream_unreachable",
            error=type(exc).__name__,
        )
        raise FileProxyError(502, "upstream_unreachable") from exc
    finally:
        # Any failure, including cancellation when the browser gives up,
        # still closes the client.
        if not sent:
            with anyio.CancelScope(shield=True):
                await client.aclose()

    upstream = UpstreamFile(
        client=client,
        response=response,
        content_type=allowed_content_type(response.headers.get("content-type")),
        content_length=_declared_length(response.headers),
    )
    try:
        _check_response(response.status_code, upstream.content_length)
    except FileProxyError as failure:
        await upstream.aclose()
        logger.warning(
            "document_file_failed",
            reason=failure.reason,
            upstream_status=response.status_code,
        )
        raise
    return upstream


def _check_response(status_code: int, content_length: int | None) -> None:
    """Refuse an upstream response the proxy must not pass on.

    Args:
        status_code (int): Upstream HTTP status.
        content_length (int | None): Declared size, when valid.

    Raises:
        FileProxyError: 404 for an upstream 404; 502 for an auth refusal,
            any other non-200 status, or a declared size over the cap.
    """
    if status_code == _HTTP_NOT_FOUND:
        raise FileProxyError(404, "upstream_not_found")
    if status_code in _HTTP_AUTH_REJECTED:
        # #ASSUME: security: a 401 or 403 means the portal's key is wrong or
        # revoked, not that the viewer lacks access; the viewer check is ours.
        # #VERIFY: watch for this reason in logs after any key rotation.
        raise FileProxyError(502, "upstream_auth_rejected")
    if status_code != _HTTP_OK:
        raise FileProxyError(502, "upstream_error")
    if content_length is not None and content_length > MAX_DOCUMENT_BYTES:
        raise FileProxyError(502, "upstream_too_large")


def response_headers(
    upstream: UpstreamFile, *, title: str, requested: Disposition
) -> dict[str, str]:
    """Build the headers for the portal's file response.

    Args:
        upstream (UpstreamFile): The open upstream file.
        title (str): Cached document title, used for the file name.
        requested (Disposition): ``inline`` for preview, ``attachment`` for
            download.

    Returns:
        dict[str, str]: Content type, disposition, ``nosniff``, a private
        no-store cache policy, and the length when the backend declared it.
    """
    disposition = effective_disposition(requested, upstream.content_type)
    headers = {
        "Content-Type": upstream.content_type,
        "Content-Disposition": content_disposition(
            disposition, title, upstream.content_type
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if upstream.content_length is not None:
        headers["Content-Length"] = str(upstream.content_length)
    return headers


def document_url(
    document_id: str,
    kind: Literal["preview", "download"] = "preview",
    page: int | str | None = None,
) -> str:
    """Return the portal link for a document, optionally at a PDF page.

    ``#page=N`` is a URL fragment, so it never reaches the server: the
    browser's PDF viewer reads it and opens at that page. Download links
    never carry a page.

    Args:
        document_id (str): Document identifier.
        kind (Literal["preview", "download"]): Which route to link to.
        page (int | str | None): 1-based page for citations; ignored unless
            it is a positive whole number.

    Returns:
        str: Root-relative URL.
    """
    url = f"/documents/{quote(document_id, safe='')}/{kind}"
    if kind != "preview" or page is None or isinstance(page, bool):
        return url
    text = str(page)
    if text.isascii() and text.isdigit() and int(text) >= 1:
        return f"{url}#page={int(text)}"
    return url
