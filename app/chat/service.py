# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Answer one chat question: search, balances, prompt, model, render.

Each question is handled on its own. Nothing is stored: no question, answer
or prompt is written to the database or the logs. Logs carry the outcome,
counts and timings only.

Failures become one plain sentence for the user; no stack trace or service
detail reaches the page. Logs name the failure by category, HTTP status and
exception class only, never a body, URL or key.

#ASSUME: timing: a warm search (about 2 s) plus the model call fits the
30-second target with two users at once. The model-call timeout
(``LLM_TIMEOUT_SECONDS``) does not cover the search or the image
preparation, which run first with their own timeouts (a cold search can take
about 50 s in the worst case). #VERIFY by reading ``elapsed_ms`` from
``chat_finished`` logs during the test session against the live services,
and check any reverse-proxy read timeout against search time plus the model
timeout; fakes cannot prove latency.

#CRITICAL: security: a Viewer must never see passage text from a document
the portal hides from Viewers. Two barriers apply: the ``is_confidential``
flag in the vector payload filters the search, and ``visible_results`` checks
each family-document hit against the portal's own document table, which is the
rule the preview route uses. If they disagree (a document reclassified after
the last index run, a failed re-index that kept old points, a deleted
document not yet swept), the hit is dropped. #VERIFY:
tests/integration/test_chat_route.py::test_viewer_never_sees_a_passage_the_cache_hides.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import anyio
import anyio.to_thread
import structlog
from pydantic import ValidationError

from app import cache
from app.chat.balances import read_balance_table
from app.chat.client import TIMEOUT, UNAVAILABLE, ChatError
from app.chat.images import ImageError, prepare_image
from app.chat.instructions import (
    InstructionsError,
    instructions_readable,
    load_instructions,
)
from app.chat.prompt import assemble_system_prompt
from app.chat.render import answer_paragraphs, citations
from app.chat.settings import ChatConfigError, ChatSettings, load_chat_settings
from app.retrieval.search import DocumentCitation, SearchError, SearchRequest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from app.chat.balances import AccountBalance, BalanceTable, BalanceTotal
    from app.chat.client import ChatClient, ModelAnswer
    from app.chat.images import PreparedImage
    from app.chat.render import Citation
    from app.chat.settings import ChatConnection
    from app.retrieval.search import SearchResponse, SearchResult, SearchService

logger = structlog.get_logger(__name__)

MAX_QUESTION_CHARS = 1000
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
MSG_BALANCES_DOWN = "The balances could not be read right now. Please try later."
MSG_UNEXPECTED = "Something went wrong and the question could not be answered."
MSG_CUT_SHORT = "This answer was cut short."


@dataclass(frozen=True)
class ChatOutcome:
    """What the page shows for one question.

    Attributes:
        question (str): The question as asked.
        paragraphs (tuple[str, ...]): Answer text, plain.
        citations (tuple[Citation, ...]): Sources searched for the answer.
        balances (tuple[AccountBalance, ...]): Account figures from the table.
        total (BalanceTotal | None): The overall total, when relevant.
        notice (str | None): A plain note shown with the answer, for example
            that it was cut short.
        error (str | None): A plain error sentence instead of an answer.
    """

    question: str
    paragraphs: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    balances: tuple[AccountBalance, ...] = ()
    total: BalanceTotal | None = None
    notice: str | None = None
    error: str | None = None


@dataclass
class _Timings:
    started: float = field(default_factory=time.perf_counter)
    search_ms: float | None = None
    model_ms: float | None = None

    def since(self, mark: float) -> float:
        return round((time.perf_counter() - mark) * 1000, 1)


def chat_connection(settings: ChatSettings | None = None) -> ChatConnection | None:
    """Return the model connection, or None when chat is not connected.

    Args:
        settings (ChatSettings | None): Settings already loaded for this
            request; None loads them from the environment.

    Returns:
        ChatConnection | None: The connection, or None when the URL is unset
        or the settings cannot form one.
    """
    try:
        return (settings or load_chat_settings()).connection()
    except (ChatConfigError, ValidationError):
        return None


def _load_settings_or_none() -> ChatSettings | None:
    try:
        return load_chat_settings()
    except ValidationError:
        # The settings were valid at startup, so this means the environment
        # changed since; treat chat as off rather than failing the page.
        return None


async def chat_panel_state(*, is_admin: bool) -> dict[str, bool]:
    """Say whether to show the chat panel and whether chat can answer.

    Settings are loaded once, and the instructions file is probed in a worker
    thread so a slow mount cannot stall the event loop.

    Args:
        is_admin (bool): True for the Admin role.

    Returns:
        dict[str, bool]: ``visible`` (the feature flag allows this role) and
        ``connected`` (the model is configured and the instructions file is
        readable).
    """
    settings = _load_settings_or_none()
    if settings is None or not settings.allows(is_admin=is_admin):
        return {"visible": False, "connected": False}
    connected = chat_connection(
        settings
    ) is not None and await anyio.to_thread.run_sync(
        instructions_readable, settings.instructions_file()
    )
    return {"visible": True, "connected": connected}


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


def _close_quietly(searcher: SearchService) -> None:
    """Close the search service, logging a failure instead of raising it.

    A close error must not replace a good answer or a search error, so it is
    logged by exception class only.

    Args:
        searcher (SearchService): The service to close.
    """
    try:
        searcher.close()
    except Exception as exc:  # noqa: BLE001  # a close failure must not mask the result
        logger.warning("chat_search_close_failed", error_type=type(exc).__name__)


async def visible_results(
    results: tuple[SearchResult, ...], *, is_admin: bool
) -> tuple[SearchResult, ...]:
    """Keep only the results the caller may open in the portal.

    Tax-law passages are not tied to a document and always stay. A family
    document passage stays for an Admin, and for a Viewer only when the
    document is in the portal's cache and is not confidential, which is the
    rule the document preview route applies. A passage with no document id is
    dropped for a Viewer, because there is nothing to check it against.

    Args:
        results (tuple[SearchResult, ...]): Search results, best first.
        is_admin (bool): True for the Admin role.

    Returns:
        tuple[SearchResult, ...]: The results to use, in the same order.
    """
    if is_admin:
        return results
    allowed: dict[str, bool] = {}
    kept: list[SearchResult] = []
    for result in results:
        citation = result.citation
        if not isinstance(citation, DocumentCitation):
            kept.append(result)
            continue
        document_id = citation.document_id
        if not document_id:
            continue
        if document_id not in allowed:
            row = await cache.get_document(document_id, include_confidential=False)
            allowed[document_id] = row is not None
        if allowed[document_id]:
            kept.append(result)
    return tuple(kept)


async def _search(
    searcher_factory: Callable[[], SearchService | None],
    question: str,
    *,
    include_confidential: bool,
) -> SearchResponse | str:
    # Building the service creates a Qdrant client, which may contact the
    # server; keep that off the event loop.
    try:
        searcher = await anyio.to_thread.run_sync(searcher_factory)
    except Exception as exc:  # noqa: BLE001  # any build failure is "search is down"
        logger.warning("chat_search_unavailable", error_type=type(exc).__name__)
        return MSG_SEARCH_DOWN
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
        # Shielded so a cancelled request still closes its client.
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(_close_quietly, searcher)


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
    try:
        outcome = await _answer(question, deps, timings)
    except Exception as exc:  # noqa: BLE001  # last resort: one plain sentence
        logger.warning("chat_unexpected_failure", error_type=type(exc).__name__)
        outcome = ChatOutcome(question=question.text.strip(), error=MSG_UNEXPECTED)
    if outcome.error:
        label = "error"
    elif not outcome.paragraphs:
        label = "empty"
    else:
        label = "answered"
    logger.info(
        "chat_finished",
        outcome=label,
        has_image=question.image_bytes is not None,
        sources=len(outcome.citations),
        search_ms=timings.search_ms,
        model_ms=timings.model_ms,
        elapsed_ms=timings.since(timings.started),
    )
    return outcome


async def _prepare(
    question: ChatQuestion, deps: ChatDeps, text: str
) -> tuple[str, PreparedImage | None] | ChatOutcome:
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
    return instructions, image


@dataclass(frozen=True)
class _Inputs:
    """Everything the model step needs, once the checks have passed."""

    instructions: str
    image: PreparedImage | None
    results: tuple[SearchResult, ...]
    table: BalanceTable


async def _find_passages(
    question: ChatQuestion, deps: ChatDeps, text: str, timings: _Timings
) -> tuple[SearchResult, ...] | ChatOutcome:
    mark = time.perf_counter()
    found = await _search(
        deps.searcher_factory,
        text,
        include_confidential=question.include_confidential,
    )
    timings.search_ms = timings.since(mark)
    if isinstance(found, str):
        return ChatOutcome(question=text, error=found)
    if found.missing_collections:
        # Names only: a collection that is not indexed yet looks like "no
        # passages found" to the user, so the log is where it shows.
        logger.warning(
            "chat_collections_missing", collections=list(found.missing_collections)
        )
    return await visible_results(found.results, is_admin=question.include_confidential)


async def _gather(
    question: ChatQuestion, deps: ChatDeps, text: str, timings: _Timings
) -> _Inputs | ChatOutcome:
    prepared = await _prepare(question, deps, text)
    if isinstance(prepared, ChatOutcome):
        return prepared
    instructions, image = prepared
    results = await _find_passages(question, deps, text, timings)
    if isinstance(results, ChatOutcome):
        return results
    try:
        table = await read_balance_table()
    except sqlite3.Error as exc:
        # Never fall back to an empty table: the prompt would then say that
        # no balances exist.
        logger.warning("chat_balances_unavailable", error_type=type(exc).__name__)
        return ChatOutcome(question=text, error=MSG_BALANCES_DOWN)
    return _Inputs(instructions, image, results, table)


async def _answer(
    question: ChatQuestion, deps: ChatDeps, timings: _Timings
) -> ChatOutcome:
    text = question.text.strip()
    problem = check_question(text)
    if problem is not None:
        return ChatOutcome(question=text, error=problem)
    gathered = await _gather(question, deps, text, timings)
    if isinstance(gathered, ChatOutcome):
        return gathered
    prompt = assemble_system_prompt(
        gathered.instructions, gathered.table, gathered.results
    )
    mark = time.perf_counter()
    try:
        answer = await deps.client.ask(
            system_prompt=prompt, question=text, image=gathered.image
        )
    except ChatError as exc:
        logger.warning(
            "chat_model_failed",
            reason=exc.reason,
            status_code=exc.status_code,
            error_type=exc.error_type,
        )
        return ChatOutcome(question=text, error=_model_message(exc))
    finally:
        timings.model_ms = timings.since(mark)
    return _outcome(text, answer, gathered.table, gathered.results)


def _outcome(
    text: str,
    answer: ModelAnswer,
    table: BalanceTable,
    results: tuple[SearchResult, ...],
) -> ChatOutcome:
    wants_total = bool(_BALANCE_WORDS.search(text))
    return ChatOutcome(
        question=text,
        paragraphs=answer_paragraphs(answer.text),
        citations=citations(results),
        balances=table.accounts_named_in(text, answer.text),
        total=table.overall_total() if wants_total else None,
        notice=MSG_CUT_SHORT if answer.truncated else None,
    )
