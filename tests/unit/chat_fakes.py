# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Fakes for chat tests: the model service, search, and an instructions file.

All data is made up. The instructions text is a short synthetic stand-in; the
real instructions file is never part of this repository.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from app.retrieval.search import (
    DocumentCitation,
    SearchError,
    SearchRequest,
    SearchResponse,
    SearchResult,
    TaxLawCitation,
)

if TYPE_CHECKING:
    from pathlib import Path

SYNTHETIC_INSTRUCTIONS = (
    "# Test instructions\n\nAnswer from the two sections below. Cite every passage.\n"
)


def write_instructions(directory: Path) -> Path:
    """Write a short synthetic instructions file.

    Args:
        directory (Path): Where to write it.

    Returns:
        Path: The file.
    """
    path = directory / "instructions.md"
    path.write_text(SYNTHETIC_INSTRUCTIONS, encoding="utf-8")
    return path


def chat_answer(
    content: object, *, finish_reason: str = "stop", **extra: object
) -> dict[str, object]:
    """Build an OpenAI-style chat completion body.

    Args:
        content (object): ``choices[0].message.content``.
        finish_reason (str): ``choices[0].finish_reason``.
        **extra (object): Extra fields for the message.

    Returns:
        dict[str, object]: Response body.
    """
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **extra},
                "finish_reason": finish_reason,
            }
        ],
    }


@dataclass
class FakeModel:
    """An ``httpx.MockTransport`` handler that records chat requests.

    Attributes:
        answer (str): Content to return.
        status (int): HTTP status to return.
        finish_reason (str): ``finish_reason`` to return.
        raise_error (Exception | None): Raised instead of answering.
        requests (list[httpx.Request]): Every request received.
    """

    answer: str = "The answer. This is educational, not legal or tax advice."
    status: int = 200
    finish_reason: str = "stop"
    raise_error: Exception | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one request.

        Args:
            request (httpx.Request): The request.

        Returns:
            httpx.Response: The canned answer.

        Raises:
            Exception: ``raise_error`` when set.
        """
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        return httpx.Response(
            self.status,
            json=chat_answer(self.answer, finish_reason=self.finish_reason),
        )

    def transport(self) -> httpx.MockTransport:
        """Return a transport that routes to this fake.

        Returns:
            httpx.MockTransport: The transport.
        """
        return httpx.MockTransport(self.handler)

    def bodies(self) -> list[dict[str, object]]:
        """Return each request body as JSON.

        Returns:
            list[dict[str, object]]: Decoded bodies.
        """
        return [json.loads(r.content) for r in self.requests]


def doc_result(
    text: str,
    *,
    title: str = "Operating Agreement",
    document_id: str | None = "doc-1",
    pages: tuple[int | None, int | None] = (3, 3),
) -> SearchResult:
    """Build a family-document search result.

    Args:
        text (str): Passage text.
        title (str): Document title.
        document_id (str | None): Document id, or None for a hit without one.
        pages (tuple[int | None, int | None]): First and last page.

    Returns:
        SearchResult: The result.
    """
    page_start, page_end = pages
    return SearchResult(
        text=text,
        score=0.9,
        collection="family-docs",
        citation=DocumentCitation(
            document_id=document_id,
            chunk_id=f"{document_id}-c1",
            title=title,
            entity_id="entity-secret-id",
            document_date="2026-01-02",
            page_start=page_start,
            page_end=page_end,
            section="Section 1",
        ),
    )


def tax_result(
    text: str, *, subtopic_id: str = "4.4", title: str = "Gifts"
) -> SearchResult:
    """Build a tax-law search result.

    Args:
        text (str): Passage text.
        subtopic_id (str): Subtopic id.
        title (str): Subtopic title.

    Returns:
        SearchResult: The result.
    """
    return SearchResult(
        text=text,
        score=0.5,
        collection="tax-law",
        citation=TaxLawCitation(subtopic_id=subtopic_id, title=title, topic="Gift tax"),
    )


@dataclass
class FakeSearcher:
    """Records search requests and returns canned results.

    Attributes:
        results (tuple[SearchResult, ...]): Results to return.
        fail (bool): Raise ``SearchError`` instead.
        missing (tuple[str, ...]): Collections reported as not indexed.
        close_error (Exception | None): Raised by ``close``.
        requests (list[SearchRequest]): Requests received.
        closed (int): Times ``close`` was called.
    """

    results: tuple[SearchResult, ...] = ()
    fail: bool = False
    missing: tuple[str, ...] = ()
    close_error: Exception | None = None
    requests: list[SearchRequest] = field(default_factory=list)
    closed: int = 0

    async def search_async(self, request: SearchRequest) -> SearchResponse:
        """Return the canned results.

        Args:
            request (SearchRequest): The request.

        Returns:
            SearchResponse: Canned results.

        Raises:
            SearchError: When ``fail`` is set.
        """
        self.requests.append(request)
        if self.fail:
            msg = "Qdrant request failed: ConnectError"
            raise SearchError(msg)
        return SearchResponse(
            results=self.results,
            embedding_model="test-embed",
            missing_collections=self.missing,
        )

    def close(self) -> None:
        """Count the close.

        Raises:
            Exception: ``close_error`` when set.
        """
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def seed_documents(db_path: Path, *, confidential: tuple[str, ...] = ()) -> None:
    """Seed made-up cached documents: ``doc-1``, ``doc-2`` and each id given.

    ``doc-1`` and ``doc-2`` are visible to a Viewer unless listed in
    ``confidential``; every id in ``confidential`` is added as confidential.

    Args:
        db_path (Path): SQLite file with the schema applied.
        confidential (tuple[str, ...]): Ids to flag confidential.
    """
    ids = dict.fromkeys(("doc-1", "doc-2", *confidential), 0)
    for doc_id in confidential:
        ids[doc_id] = 1
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executemany(
            "INSERT INTO documents (id, name, category, is_confidential, fetched_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (doc_id, f"Made-up document {doc_id}", "Legal", flag, "2026-10-01")
                for doc_id, flag in ids.items()
            ],
        )
        conn.commit()


def seed_balances(db_path: Path) -> None:
    """Seed two made-up accounts, an entity, and one day of daily rows.

    Args:
        db_path (Path): SQLite file with the schema applied.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, fetched_at) VALUES (?, ?, ?)",
            ("ent-1", "Maple Holdings LLC", "2026-10-01T00:00:00+00:00"),
        )
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "acct-1",
                    "Harbor Brokerage",
                    "ent-1",
                    "Investments",
                    "manual",
                    123456789,
                    "2026-09-30",
                    "2026-10-01T00:00:00+00:00",
                    "USD",
                ),
                (
                    "acct-2",
                    "Main Checking",
                    None,
                    "Cash",
                    "manual",
                    250000,
                    "2026-10-01",
                    "2026-10-01T00:00:00+00:00",
                    "USD",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO balances_daily (date, account_id, entity_id, category, "
            "value_cents, as_of, currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "2026-10-01",
                    "acct-1",
                    "ent-1",
                    "Investments",
                    123456789,
                    "2026-09-30",
                    "USD",
                ),
                ("2026-10-01", "acct-2", None, "Cash", 250000, "2026-10-01", "USD"),
            ],
        )
        conn.commit()


def random_key() -> str:
    """Return a random key value for tests (never a literal).

    Returns:
        str: Random URL-safe text.
    """
    return secrets.token_urlsafe(16)
