# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Streaming proxy for document files held by llc-manager (ADR-007).

Document metadata is cached like every other dataset (ADR-003), but the file
bytes are not: caching every family document on the portal's disk would make
the portal a second document store. So preview and download call a backend
directly from a request handler, as a bounded exception to ADR-003. ADR-007
records that exception and its limits.

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
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

import anyio
import httpx
import structlog
from starlette.responses import StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

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

# Longest a whole file response may run, from the first header to the last
# byte. The backend timeout bounds each read, not the whole transfer, so an
# upstream that trickles bytes, or a browser that stops reading, could
# otherwise hold an upstream connection indefinitely. The limit covers the
# browser's own download time too: 50 MiB in 300 s needs about 1.4 Mbit/s.
# #ASSUME: timing: five minutes is enough for the largest file over the
# slowest real remote path to a viewer's device.
# #VERIFY: time a download of the largest stored file from a remote tablet.
MAX_TRANSFER_SECONDS = 300.0

_HTTP_OK = 200
_HTTP_NOT_FOUND = 404
_HTTP_AUTH_REJECTED = frozenset({401, 403})

# Path segments that httpx would remove as dot segments, changing which
# upstream path receives the key.
_DOT_SEGMENTS = frozenset({".", ".."})

ProxyStatus = Literal[404, 502, 503, 504]

GENERIC_CONTENT_TYPE = "application/octet-stream"

# Types a browser may render inline: PDF and raster images. None of them is
# HTML or SVG; a PDF's own script, if any, runs inside the browser's PDF
# viewer, not in the portal's origin.
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

# Other extensions a title may already end with for the same content type, so
# "photo.jpeg" is not renamed "photo.jpeg.jpg".
_EXTENSION_ALIASES: dict[str, tuple[str, ...]] = {
    "image/jpeg": (".jpeg",),
    "image/tiff": (".tiff",),
}

# Longest page number a link may carry; longer strings are ignored before
# ``int()`` sees them (Python refuses to parse over 4300 digits).
_MAX_PAGE_DIGITS = 6

_MAX_STEM_CHARS = 150
_DEFAULT_STEM = "document"
_PATH_SEPARATORS = re.compile(r"[/\\]")
_WHITESPACE = re.compile(r"\s+")
_UNSAFE_ASCII = re.compile(r"[^A-Za-z0-9 ._()\-]")
# Unicode categories replaced with a space in names: controls, format
# characters (such as bidirectional overrides), surrogates, private use,
# unassigned, and line or paragraph separators. Header injection needs one of
# these.
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class FileProxyError(Exception):
    """The upstream file could not be served; carries the portal's status.

    Args:
        status_code (ProxyStatus): HTTP status for the portal's response.
        reason (str): Short category for logs; never upstream content.

    Attributes:
        status_code (ProxyStatus): HTTP status for the portal's response.
        reason (str): Short category for logs; never upstream content.
    """

    status_code: ProxyStatus
    reason: str

    def __init__(self, status_code: ProxyStatus, reason: str) -> None:
        # Both values go to ``args`` so copy and pickle can rebuild the error.
        super().__init__(status_code, reason)
        self.status_code = status_code
        self.reason = reason

    def __str__(self) -> str:
        """Return the reason category.

        Returns:
            str: The short reason, never upstream content.
        """
        return self.reason


class DocumentStreamAbortedError(Exception):
    """The file stream stopped after the response began.

    Raised from inside the response body so the server drops the connection
    instead of ending the body cleanly; a browser then reports a failed
    download rather than keeping a truncated file.
    """


async def _close_quietly(close: Callable[[], Awaitable[None]]) -> None:
    """Run one close step, logging a transport failure instead of raising.

    Args:
        close (Callable[[], Awaitable[None]]): The close coroutine function.
    """
    try:
        await close()
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("document_upstream_close_failed", error=type(exc).__name__)


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

        Safe to call more than once. A failure while closing is logged and
        swallowed, so it cannot replace the outcome the caller is reporting.
        """
        with anyio.CancelScope(shield=True):
            try:
                await _close_quietly(self.response.aclose)
            finally:
                # The shield stops cancel-scope cancellation (the time limit,
                # a disconnect) but not a direct task.cancel(); the client
                # still closes before that cancellation goes on.
                await _close_quietly(self.client.aclose)

    async def iter_bytes(self) -> AsyncGenerator[bytes, None]:
        """Yield the body as it arrives, enforcing the size cap, then close.

        Each network read is passed on as soon as it arrives, so nothing is
        held back waiting for a full buffer. The time limit is enforced on
        the whole response by ``UpstreamFileResponse``.

        Yields:
            bytes: The next chunk of the file.

        Raises:
            DocumentStreamAbortedError: If the file grows past
                ``MAX_DOCUMENT_BYTES`` or the upstream connection fails part
                way through.
        """
        sent = 0
        try:
            async for chunk in self.response.aiter_bytes():
                sent += len(chunk)
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
    """A time-limited streaming response that always closes its upstream file.

    Starlette runs background tasks only after a clean finish, and an async
    generator that is never resumed never runs its ``finally``. Closing in
    ``__call__`` covers a client that disconnects before or during the body.

    Args:
        upstream (UpstreamFile): The open upstream file.
        headers (dict[str, str]): Response headers to send.
    """

    def __init__(self, upstream: UpstreamFile, headers: dict[str, str]) -> None:
        body = upstream.iter_bytes()
        super().__init__(body, headers=headers)
        self._body: AsyncGenerator[bytes, None] = body
        self._close_upstream: Callable[[], Awaitable[None]] = upstream.aclose

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response within the time limit, then close upstream.

        The limit covers the whole response, so an upstream that sends one
        byte just inside each read timeout, or a browser that stops reading,
        is cut off at ``MAX_TRANSFER_SECONDS`` all the same.

        Args:
            scope (Scope): ASGI connection scope.
            receive (Receive): ASGI receive channel.
            send (Send): ASGI send channel.

        Raises:
            DocumentStreamAbortedError: If the response runs past
                ``MAX_TRANSFER_SECONDS``; the server then drops the
                connection instead of ending the body cleanly.
        """
        try:
            with anyio.fail_after(MAX_TRANSFER_SECONDS):
                await super().__call__(scope, receive, send)
        except TimeoutError as exc:
            logger.warning(
                "document_stream_too_slow", limit_seconds=MAX_TRANSFER_SECONDS
            )
            msg = "file transfer took longer than the proxy allows"
            raise DocumentStreamAbortedError(msg) from exc
        finally:
            with anyio.CancelScope(shield=True):
                await self._body.aclose()
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

    Control, format and separator characters are replaced with a space,
    path separators with an underscore, length is capped, and the extension
    for the content type is added when the title does not already end with
    it or one of its aliases (``.jpeg`` for JPEG, ``.tiff`` for TIFF).

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
    accepted = (extension, *_EXTENSION_ALIASES.get(content_type, ()))
    if extension and not stem.lower().endswith(accepted):
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
        timeout_seconds (float): Timeout applied separately to connecting,
            each read, each write, and waiting for a pooled connection.

    Returns:
        UpstreamFile: The open response, ready to stream.

    Raises:
        FileProxyError: 404 when the ID is a dot segment or the backend has
            no such file, 503 when the backend URL or key cannot form a
            request, 504 on a timeout, 502 for any other failure or a file
            over the size cap.
    """
    if document_id in _DOT_SEGMENTS:
        logger.warning("document_file_failed", reason="invalid_document_id")
        raise FileProxyError(404, "invalid_document_id")
    url = (
        f"{connection.url.rstrip('/')}/api/v1/documents/"
        f"{quote(document_id, safe='')}/file"
    )
    client = new_client(httpx.Timeout(timeout_seconds))
    sent = False
    try:
        request = client.build_request(
            "GET",
            url,
            headers={
                "Accept": "*/*",
                # The body is passed through decoded, so ask for no
                # compression; a declared length then matches the bytes sent.
                "Accept-Encoding": "identity",
                "X-API-Key": connection.api_key,
            },
        )
        response = await client.send(request, stream=True)
        sent = True
    except (httpx.InvalidURL, UnicodeEncodeError) as exc:
        # A bad port in the URL or a non-ASCII key: configuration, not the
        # viewer. The key itself is never logged.
        logger.warning(
            "document_file_failed",
            reason="backend_misconfigured",
            error=type(exc).__name__,
        )
        raise FileProxyError(503, "backend_misconfigured") from exc
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
        no-store cache policy, same-origin framing and resource policies, and
        the length when the backend declared it.
    """
    disposition = effective_disposition(requested, upstream.content_type)
    # No ``Content-Security-Policy: sandbox``: it stops the built-in PDF
    # viewers. ``frame-ancestors 'self'`` and ``SAMEORIGIN`` (not DENY) keep a
    # later in-page preview possible while refusing framing by other sites.
    # #ASSUME: external resources: ``Cross-Origin-Resource-Policy:
    # same-origin`` only affects cross-origin subresource loads, so the
    # browser PDF viewers still open a top-level preview.
    # #VERIFY: open an inline PDF in Chrome, Firefox and Safari before the
    # document service is connected; drop CORP if any viewer breaks.
    headers = {
        "Content-Type": upstream.content_type,
        "Content-Disposition": content_disposition(
            disposition, title, upstream.content_type
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "frame-ancestors 'self'",
        "X-Frame-Options": "SAMEORIGIN",
        "Cross-Origin-Resource-Policy": "same-origin",
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
            it is a positive whole number of at most six digits.

    Returns:
        str: Root-relative URL.
    """
    url = f"/documents/{quote(document_id, safe='')}/{kind}"
    if kind != "preview" or page is None or isinstance(page, bool):
        return url
    text = str(page)
    if (
        len(text) <= _MAX_PAGE_DIGITS
        and text.isascii()
        and text.isdigit()
        and int(text) >= 1
    ):
        return f"{url}#page={int(text)}"
    return url
