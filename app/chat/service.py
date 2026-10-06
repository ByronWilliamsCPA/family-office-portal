# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Answer one chat question: search, balances, prompt, model, render.

Each question is handled on its own. Nothing is stored: no question, answer
or prompt is written to the database or the logs. Logs carry the outcome,
counts and timings only.

Failures become one plain sentence for the user; no stack trace or service
detail reaches the page.

#ASSUME: timing: a warm search (about 2 s) plus the model call fits the
30-second target with two users at once. #VERIFY by reading ``elapsed_ms``
from ``chat_finished`` logs during the test session against the live
services; fakes cannot prove latency.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import structlog

from app.chat.balances import read_balance_table
from app.chat.client import TIMEOUT, UNAVAILABLE, ChatError
from app.chat.images import ImageError, prepare_image
from app.chat.prompt import assemble_system_prompt
from app.chat.render import answer_paragraphs, citations
from app.chat.settings import ChatConfigError, load_chat_settings
from app.retrieval.search import SearchError, SearchRequest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from app.chat.balances import AccountBalance, BalanceTotal
    from app.chat.client import ChatClient
    from app.chat.images import PreparedImage
    from app.chat.render import Citation
    from app.chat.settings import ChatConnection
    from app.retrieval.search import SearchResponse, SearchService

logger = structlog.get_logger(__name__)

MAX_QUESTION_CHARS = 1000
MAX_INSTRUCTIONS_BYTES = 64 * 1024
CHAT_COLLECTIONS = ("family-docs", "tax-law")
SEARCH_TOP_K = 8
_BALANCE_WORDS = re.compile(
    r"\b(balances?|totals?|worth|how much|value|cash)\b", re.IGNORECASE
)

MSG_EMPTY = "Please type a question."
MSG_TOO_LONG = f"Please keep your question under {MAX_QUESTION_CHARS} characters."
MSG_NOT_CONNECTED = "Questions are not connected yet. Please try again later."
MSG_SEARCH_OFF = "Document search is not connected yet, so I cannot answer now."
MSG_SEARCH_DOWN = "Document search is not responding right now. Please try later."
MSG_MODEL_TIMEOUT = "The answer took too long. Please try again in a minute."
MSG_MODEL_DOWN = "The question service is not available right now. Please try later."
MSG_MODEL_BAD = "The question service gave an answer the portal could not read."


class InstructionsError(RuntimeError):
    """The instructions file is unset, unreadable, empty or too large."""


@dataclass(frozen=True)
class ChatOutcome:
    """What the page shows for one question.

    Attributes:
        question (str): The question as asked.
        paragraphs (tuple[str, ...]): Answer text, plain.
        citations (tuple[Citation, ...]): Sources searched for the answer.
        balances (tuple[AccountBalance, ...]): Account figures from the table.
        total (BalanceTotal | None): The overall total, when relevant.
        error (str | None): A plain error sentence instead of an answer.
    """

    question: str
    paragraphs: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    balances: tuple[AccountBalance, ...] = ()
    total: BalanceTotal | None = None
    error: str | None = None


@dataclass
class _Timings:
    started: float = field(default_factory=time.perf_counter)
    search_ms: float | None = None
    model_ms: float | None = None

    def since(self, mark: float) -> float:
        return round((time.perf_counter() - mark) * 1000, 1)


def instructions_readable(path: Path | None) -> bool:
    """Say whether the instructions file exists and can be read.

    Args:
        path (Path | None): Configured path, or None when unset.

    Returns:
        bool: True when the file is a readable, non-empty regular file.
    """
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0 and _can_open(path)
    except OSError:
        return False


def _can_open(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.read(1)
    return True


async def load_instructions(path: Path | None) -> str:
    """Read the instructions file.

    Args:
        path (Path | None): Configured path, or None when unset.

    Returns:
        str: The instructions text.

    Raises:
        InstructionsError: If the path is unset, the file cannot be read, is
            not UTF-8, is empty, or is over 64 KB.
    """
    if path is None:
        msg = "CHAT_INSTRUCTIONS_PATH is not set"
        raise InstructionsError(msg)
    try:
        raw = await anyio.Path(path).read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = "the chat instructions file cannot be read"
        raise InstructionsError(msg) from exc
    if not text.strip() or len(raw) > MAX_INSTRUCTIONS_BYTES:
        msg = "the chat instructions file is empty or too large"
        raise InstructionsError(msg)
    return text


def chat_connection() -> ChatConnection | None:
    """Return the model connection, or None when chat is not connected.

    Returns:
        ChatConnection | None: The connection, or None when the URL is unset
        or the settings cannot form one.
    """
    try:
        return load_chat_settings().connection()
    except ChatConfigError:
        return None


def chat_panel_state(*, is_admin: bool) -> dict[str, bool]:
    """Say whether to show the chat panel and whether chat can answer.

    Args:
        is_admin (bool): True for the Admin role.

    Returns:
        dict[str, bool]: ``visible`` (the feature flag allows this role) and
        ``connected`` (the model is configured and the instructions file is
        readable).
    """
    settings = load_chat_settings()
    visible = settings.allows(is_admin=is_admin)
    connected = (
        visible
        and chat_connection() is not None
        and instructions_readable(settings.instructions_file())
    )
    return {"visible": visible, "connected": connected}


def check_question(question: str) -> str | None:
    """Return a plain error for an unusable question, or None.

    Args:
        question (str): The question, already stripped.

    Returns:
        str | None: Error sentence, or None when the question is fine.
    """
    if not question:
        return MSG_EMPTY
    if len(question) > MAX_QUESTION_CHARS:
        return MSG_TOO_LONG
    return None


def _model_message(error: ChatError) -> str:
    if error.reason == TIMEOUT:
        return MSG_MODEL_TIMEOUT
    if error.reason == UNAVAILABLE:
        return MSG_MODEL_DOWN
    return MSG_MODEL_BAD


async def _search(
    searcher_factory: Callable[[], SearchService | None],
    question: str,
    *,
    include_confidential: bool,
) -> SearchResponse | str:
    # Building the service creates a Qdrant client, which may contact the
    # server; keep that off the event loop.
    searcher = await anyio.to_thread.run_sync(searcher_factory)
    if searcher is None:
        return MSG_SEARCH_OFF
    try:
        return await searcher.search_async(
            SearchRequest(
                question,
                include_confidential=include_confidential,
                entity_ids=None,
                collections=CHAT_COLLECTIONS,
                top_k=SEARCH_TOP_K,
            )
        )
    except SearchError:
        return MSG_SEARCH_DOWN
    finally:
        await anyio.to_thread.run_sync(searcher.close)


@dataclass(frozen=True)
class ChatQuestion:
    """One question from the page.

    Attributes:
        text (str): The question as typed.
        image_bytes (bytes | None): One uploaded image, if any.
        include_confidential (bool): True only for the Admin role; set from
            the signed-in role, never from the form.
    """

    text: str
    image_bytes: bytes | None = None
    include_confidential: bool = False


@dataclass(frozen=True)
class ChatDeps:
    """What answering needs besides the question.

    Attributes:
        instructions_path (Path | None): The instructions file.
        client (ChatClient): The chat model client.
        searcher_factory (Callable[[], SearchService | None]): Builds the search
            service, or returns None when search is not connected.
    """

    instructions_path: Path | None
    client: ChatClient
    searcher_factory: Callable[[], SearchService | None]


async def answer_question(question: ChatQuestion, deps: ChatDeps) -> ChatOutcome:
    """Answer one question from the documents and the balance table.

    Args:
        question (ChatQuestion): The question, image and caller's access.
        deps (ChatDeps): Instructions file, model client and search.

    Returns:
        ChatOutcome: The answer, or a plain error.
    """
    timings = _Timings()
    outcome = await _answer(question, deps, timings)
    logger.info(
        "chat_finished",
        outcome="error" if outcome.error else "answered",
        has_image=question.image_bytes is not None,
        sources=len(outcome.citations),
        search_ms=timings.search_ms,
        model_ms=timings.model_ms,
        elapsed_ms=timings.since(timings.started),
    )
    return outcome


async def _answer(
    question: ChatQuestion, deps: ChatDeps, timings: _Timings
) -> ChatOutcome:
    text = question.text.strip()
    problem = check_question(text)
    if problem is not None:
        return ChatOutcome(question=text, error=problem)
    try:
        instructions = await load_instructions(deps.instructions_path)
    except InstructionsError as exc:
        logger.warning("chat_instructions_unavailable", reason=str(exc))
        return ChatOutcome(question=text, error=MSG_NOT_CONNECTED)
    image: PreparedImage | None = None
    if question.image_bytes is not None:
        try:
            image = await anyio.to_thread.run_sync(prepare_image, question.image_bytes)
        except ImageError as exc:
            return ChatOutcome(question=text, error=str(exc))
    mark = time.perf_counter()
    found = await _search(
        deps.searcher_factory,
        text,
        include_confidential=question.include_confidential,
    )
    timings.search_ms = timings.since(mark)
    if isinstance(found, str):
        return ChatOutcome(question=text, error=found)
    table = await read_balance_table()
    prompt = assemble_system_prompt(instructions, table, found.results)
    mark = time.perf_counter()
    try:
        answer = await deps.client.ask(system_prompt=prompt, question=text, image=image)
    except ChatError as exc:
        logger.warning("chat_model_failed", reason=exc.reason)
        return ChatOutcome(question=text, error=_model_message(exc))
    finally:
        timings.model_ms = timings.since(mark)
    wants_total = bool(_BALANCE_WORDS.search(text))
    return ChatOutcome(
        question=text,
        paragraphs=answer_paragraphs(answer),
        citations=citations(found.results),
        balances=table.accounts_named_in(text, answer),
        total=table.overall_total() if wants_total else None,
    )
