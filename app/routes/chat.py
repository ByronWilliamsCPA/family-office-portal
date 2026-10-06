# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Chat: ask a question about the family's documents and balances.

Chat is a panel on the Home page, not a sixth section. A question is posted
to ``/chat/ask``. With HTMX the route returns one answer fragment that the
page appends below earlier answers; without JavaScript it returns the whole
Home page with the answer. Earlier answers live only in the page.

This route is the one place the portal calls a service while serving a
request (ADR-009); everything else reads the SQLite cache.

Who can use it is set by ``CHAT_ENABLED_FOR`` (Admin only by default). For
anyone else the route answers 404, as if it did not exist.

#CRITICAL: security: only ``question`` and one ``image`` are read from the
form; every other field (for example ``chat_template_kwargs``) is ignored,
and ``include_confidential`` comes from the signed-in role. #VERIFY:
tests/integration/test_chat_route.py.
#EDGE: security: the portal has no CSRF token. A browser marks a cross-site
form post with ``Sec-Fetch-Site: cross-site``; such posts get 403. #VERIFY
that the deployed browsers send Fetch Metadata headers (all current ones do).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from app.routes._context import include_confidential
from app.routes.home import home_context
from app.templating import render, templates

if TYPE_CHECKING:
    from starlette.datastructures import FormData

    from app.chat.settings import ChatConnection

router = APIRouter(prefix="/chat", tags=["chat"])

MSG_ONE_PICTURE = "Please attach one picture at most."
MSG_PICTURE_TOO_LARGE = "The picture is too large. Please use one under 10 MB."
_SAME_SITE = frozenset({"same-origin", "same-site", "none"})
_MAX_FORM_FILES = 2
_MAX_FORM_FIELDS = 8


def build_chat_client(connection: ChatConnection) -> ChatClient:
    """Build the model client for one request.

    Tests replace this to supply an ``httpx.MockTransport``.

    Args:
        connection (ChatConnection): The model connection.

    Returns:
        ChatClient: A client for this request.
    """
    return ChatClient(connection)


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
            403 for a cross-site post.
    """
    if not load_chat_settings().allows(is_admin=include_confidential(request)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in _SAME_SITE:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    async with request.form(
        max_files=_MAX_FORM_FILES, max_fields=_MAX_FORM_FIELDS
    ) as form:
        outcome = await _outcome(request, form)
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            request, "partials/chat_turn.html", {"outcome": outcome}
        )
    context: dict[str, Any] = await home_context(request)
    return render(
        request, "pages/home.html", section="home", chat_outcome=outcome, **context
    )
