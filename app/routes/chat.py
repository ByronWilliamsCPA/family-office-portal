# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Chat: ask a question about the family's documents and balances.

Chat is a panel on the Home page, not a sixth section. A question is posted
to ``/chat/ask``. With HTMX the route returns one answer fragment that the
page appends below earlier answers; without JavaScript it returns the whole
Home page with the answer. Earlier answers live only in the page.

This route is a bounded exception to the cached-read rule (ADR-009): it
searches and calls the chat model while serving a request, alongside the
document proxy (ADR-007) and document search (ADR-008).

Who can use it is set by ``CHAT_ENABLED_FOR`` (Admin only by default). For
anyone else the route answers 404, as if it did not exist.

Order of checks: role gate (404), cross-site check (403), declared body size
(413, nothing counted), per-user limiter (429), then the form and the
validation and model call. A question refused before any work (blank, too
long, a bad picture, a body over the cap, chat not connected) is refunded and
does not count against the quota. The in-flight slot is released in a
``finally`` block on every outcome.

#CRITICAL: security: only ``question`` and one ``image`` are read from the
form; every other field (for example ``chat_template_kwargs``) is ignored,
and ``include_confidential`` comes from the signed-in role. #VERIFY:
tests/integration/test_chat_route.py.
#EDGE: security: the portal has no CSRF token. A browser marks a cross-site
form post with ``Sec-Fetch-Site: cross-site`` or ``same-site``; such posts get
403, and only ``same-origin`` and ``none`` pass. When the header is absent
(an old browser or a non-browser client) the ``Origin`` header, if present,
must match the request's own host, or the post gets 403. #VERIFY that the
deployed browsers send Fetch Metadata headers (all current ones do).
#ASSUME: security: the sign-in cookie is ``SameSite=Lax`` or stricter, so a
cross-site form post does not carry it. #VERIFY the cookie attribute at the
auth proxy before relying on it.
#EDGE: abuse: each signed-in user gets a sliding-window quota and one question
at a time (``app.chat.limits``, ADR-009), so one user cannot hold both model
slots. The limiter runs after the role and cross-site checks and before the
form is read; a refused post gets 429 with ``Retry-After`` and makes no
search or model call. The counters are in memory and per process.
#ASSUME: one uvicorn worker. #VERIFY the deployment keeps a single worker.
#EDGE: a restart resets the counters. #VERIFY that is acceptable: the model
semaphore and timeout still bound the load.
#EDGE: disk: a multipart upload is spooled to a temporary file as it
arrives. The route refuses a declared body over ``MAX_BODY_BYTES`` and stops
reading a streamed body once it passes that size. #VERIFY any reverse-proxy
body limit is not lower than this one, or users get a proxy error page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app.chat.client import ChatClient
from app.chat.images import MAX_UPLOAD_BYTES
from app.chat.limits import LIMITER, Refusal, Ticket, refusal_message
from app.chat.service import (
    MSG_NOT_CONNECTED,
    ChatDeps,
    ChatOutcome,
    ChatQuestion,
    answer_question,
    chat_connection,
    check_question,
)
from app.chat.settings import load_chat_settings
from app.retrieval.search import build_search_service
from app.routes._context import home_context, include_confidential, is_admin
from app.templating import render, templates

if TYPE_CHECKING:
    from starlette.datastructures import FormData

    from app.chat.settings import ChatConnection

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

MSG_ONE_PICTURE = "Please attach one picture at most."
MSG_PICTURE_TOO_LARGE = "The picture is too large. Please use one under 10 MB."
# ``same-site`` is refused: a sibling subdomain is not this page.
_SAME_ORIGIN = frozenset({"same-origin", "none"})
_MAX_FORM_FILES = 2
_MAX_FORM_FIELDS = 8
# One picture, plus room for the question and the multipart framing.
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
_TOO_LARGE = 413
_TOO_MANY = 429
ANSWERED_EVENT = "chat-answered"


class _BodyTooLargeError(Exception):
    """The request body grew past ``MAX_BODY_BYTES`` while it was read."""


def build_chat_client(connection: ChatConnection) -> ChatClient:
    """Build the model client for one request.

    Tests replace this to supply an ``httpx.MockTransport``.

    Args:
        connection (ChatConnection): The model connection.

    Returns:
        ChatClient: A client for this request.
    """
    return ChatClient(connection)


def _capped(request: Request) -> Request:
    """Return a request whose body stops with an error past the size cap.

    Args:
        request (Request): Current request.

    Returns:
        Request: A request over the same scope that raises
        ``_BodyTooLargeError`` once more than ``MAX_BODY_BYTES`` has arrived.
    """
    received = 0

    async def receive() -> Any:  # noqa: ANN401  # ASGI message dict
        nonlocal received
        message = await request.receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > MAX_BODY_BYTES:
                raise _BodyTooLargeError
        return message

    return Request(request.scope, receive)


def _declared_too_large(request: Request) -> bool:
    declared = request.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > MAX_BODY_BYTES


def _cross_origin(request: Request) -> bool:
    """Say whether the post looks like it came from another site.

    Args:
        request (Request): Current request.

    Returns:
        bool: True when Fetch Metadata says anything but same-origin or
        none; when that header is absent, True when an ``Origin`` header names
        a different host.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site not in _SAME_ORIGIN
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc != request.headers.get("host", "")


async def _image_bytes(form: FormData) -> bytes | str | None:
    """Return the uploaded image, None when there is none, or an error.

    Args:
        form (FormData): The parsed form.

    Returns:
        bytes | str | None: Image bytes, a plain error sentence, or None.
    """
    uploads = [
        item
        for item in form.getlist("image")
        if isinstance(item, UploadFile) and (item.filename or item.size)
    ]
    if len(uploads) > 1:
        return MSG_ONE_PICTURE
    if not uploads:
        return None
    data = await uploads[0].read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return MSG_PICTURE_TOO_LARGE
    return data


async def _outcome(request: Request, form: FormData, ticket: Ticket) -> ChatOutcome:
    raw_question = form.get("question")
    question = raw_question.strip() if isinstance(raw_question, str) else ""
    connection = chat_connection()
    if connection is None:
        ticket.refund()
        return ChatOutcome(question=question, error=MSG_NOT_CONNECTED)
    image = await _image_bytes(form)
    if isinstance(image, str):
        ticket.refund()
        return ChatOutcome(question=question, error=image)
    problem = check_question(question)
    if problem is not None:
        ticket.refund()
        return ChatOutcome(question=question, error=problem)
    return await answer_question(
        ChatQuestion(
            text=question,
            image_bytes=image,
            include_confidential=include_confidential(request),
        ),
        ChatDeps(
            instructions_path=load_chat_settings().instructions_file(),
            client=build_chat_client(connection),
            searcher_factory=build_search_service,
        ),
    )


def _user_key(request: Request) -> str:
    """Return the limiter key: the signed-in username, never logged.

    Args:
        request (Request): Current request.

    Returns:
        str: The username, or a shared fallback when there is none (the auth
        middleware always sets one, so the fallback only makes a missing
        identity share one strict budget).
    """
    principal = getattr(request.state, "principal", None)
    return str(getattr(principal, "username", "") or "unknown")


async def _respond(
    request: Request,
    outcome: ChatOutcome,
    *,
    status_code: int = status.HTTP_200_OK,
    headers: dict[str, str] | None = None,
) -> Response:
    """Render one outcome as an HTMX fragment or as the whole Home page.

    Args:
        request (Request): Current request.
        outcome (ChatOutcome): What to show.
        status_code (int): HTTP status code.
        headers (dict[str, str] | None): Extra response headers.

    Returns:
        Response: The answer fragment for HTMX, else the Home page.
    """
    extra = dict(headers or {})
    if request.headers.get("hx-request") == "true":
        if outcome.paragraphs:
            extra["HX-Trigger"] = ANSWERED_EVENT
        return templates.TemplateResponse(
            request,
            "partials/chat_turn.html",
            {"outcome": outcome},
            status_code=status_code,
            headers=extra,
        )
    context: dict[str, Any] = await home_context(request)
    response = render(
        request,
        "pages/home.html",
        section="home",
        status_code=status_code,
        chat_outcome=outcome,
        **context,
    )
    response.headers.update(extra)
    return response


async def _limited(request: Request, refusal: Refusal) -> Response:
    """Answer a post the limiter turned away: 429 and one plain sentence.

    Nothing from the request is read or echoed, and the log line carries the
    reason and the wait only.

    Args:
        request (Request): Current request.
        refusal (Refusal): What the limiter decided.

    Returns:
        Response: A 429 with ``Retry-After``, as a fragment or a page.
    """
    logger.info(
        "chat_rate_limited", reason=refusal.reason, retry_after=refusal.retry_after
    )
    return await _respond(
        request,
        ChatOutcome(question="", error=refusal_message(refusal)),
        status_code=_TOO_MANY,
        headers={"Retry-After": str(refusal.retry_after)},
    )


async def _read_and_answer(request: Request, ticket: Ticket) -> ChatOutcome:
    """Read the form and answer, refunding the quota when no work was done.

    Args:
        request (Request): Current request.
        ticket (Ticket): The caller's limiter ticket.

    Returns:
        ChatOutcome: The answer, or a plain error.

    Raises:
        HTTPException: 413 when the body outgrows the cap.
        StarletteHTTPException: The form parser's own refusal (too many files
            or fields), after the question is refunded.
    """
    try:
        async with _capped(request).form(
            max_files=_MAX_FORM_FILES, max_fields=_MAX_FORM_FIELDS
        ) as form:
            return await _outcome(request, form, ticket)
    except _BodyTooLargeError:
        ticket.refund()
        raise HTTPException(status_code=_TOO_LARGE) from None
    except StarletteHTTPException:
        # The form parser's own refusal (too many files or fields), raised as
        # Starlette's base class, not FastAPI's.
        ticket.refund()
        raise


@router.post(
    "/ask",
    summary="Ask a question (HTMX partial, or the Home page without HTMX)",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Cross-site post"},
        404: {"description": "Chat is not enabled for this role"},
        413: {"description": "Request body too large"},
        429: {"description": "Too many questions, or one still being answered"},
    },
)
async def ask(request: Request) -> Response:
    """Answer one question with citations and balances from the table.

    Authentication: Admin, or Viewer when ``CHAT_ENABLED_FOR`` is ``all``.

    Args:
        request (Request): Current request; the form carries ``question`` and
            an optional ``image``.

    Returns:
        Response: The answer fragment for HTMX, else the Home page.

    Raises:
        HTTPException: 404 when chat is not enabled for the caller's role,
            403 for a cross-site post, 413 when the body is over
            ``MAX_BODY_BYTES``. A question turned away by the per-user limit
            is returned as a 429 response, not raised.
    """
    if not load_chat_settings().allows(is_admin=is_admin(request)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if _cross_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if _declared_too_large(request):
        raise HTTPException(status_code=_TOO_LARGE)
    ticket = LIMITER.acquire(_user_key(request), load_chat_settings().rate_limits())
    if isinstance(ticket, Refusal):
        return await _limited(request, ticket)
    with ticket:
        outcome = await _read_and_answer(request, ticket)
        return await _respond(request, outcome)
