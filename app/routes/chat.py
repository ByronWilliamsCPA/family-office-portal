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
#EDGE: abuse: there is no per-user rate limit; the only throttle is the
process-wide cap of two model calls, so one user can hold both slots. Per-user
limiting is deferred (ADR-009). #VERIFY the audience stays small (Admin only
by default) before widening ``CHAT_ENABLED_FOR``.
#EDGE: disk: a multipart upload is spooled to a temporary file as it
arrives. The route refuses a declared body over ``MAX_BODY_BYTES`` and stops
reading a streamed body once it passes that size. #VERIFY any reverse-proxy
body limit is not lower than this one, or users get a proxy error page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from starlette.datastructures import UploadFile
from starlette.responses import (
    Response,  # noqa: TC002  # FastAPI reads return annotations at runtime
)

from app.chat.client import ChatClient
from app.chat.images import MAX_UPLOAD_BYTES
from app.chat.service import (
    MSG_NOT_CONNECTED,
    ChatDeps,
    ChatOutcome,
    ChatQuestion,
    answer_question,
    chat_connection,
)
from app.chat.settings import load_chat_settings
from app.retrieval.search import build_search_service
from app.routes._context import home_context, include_confidential, is_admin
from app.templating import render, templates

if TYPE_CHECKING:
    from starlette.datastructures import FormData

    from app.chat.settings import ChatConnection

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


async def _outcome(request: Request, form: FormData) -> ChatOutcome:
    raw_question = form.get("question")
    question = raw_question.strip() if isinstance(raw_question, str) else ""
    connection = chat_connection()
    if connection is None:
        return ChatOutcome(question=question, error=MSG_NOT_CONNECTED)
    image = await _image_bytes(form)
    if isinstance(image, str):
        return ChatOutcome(question=question, error=image)
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


@router.post(
    "/ask",
    summary="Ask a question (HTMX partial, or the Home page without HTMX)",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    responses={
        403: {"description": "Cross-site post"},
        404: {"description": "Chat is not enabled for this role"},
        413: {"description": "Request body too large"},
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
            ``MAX_BODY_BYTES``.
    """
    if not load_chat_settings().allows(is_admin=is_admin(request)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if _cross_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if _declared_too_large(request):
        raise HTTPException(status_code=_TOO_LARGE)
    try:
        async with _capped(request).form(
            max_files=_MAX_FORM_FILES, max_fields=_MAX_FORM_FIELDS
        ) as form:
            outcome = await _outcome(request, form)
    except _BodyTooLargeError:
        raise HTTPException(status_code=_TOO_LARGE) from None
    if request.headers.get("hx-request") == "true":
        headers = {"HX-Trigger": ANSWERED_EVENT} if outcome.paragraphs else {}
        return templates.TemplateResponse(
            request, "partials/chat_turn.html", {"outcome": outcome}, headers=headers
        )
    context: dict[str, Any] = await home_context(request)
    return render(
        request, "pages/home.html", section="home", chat_outcome=outcome, **context
    )
