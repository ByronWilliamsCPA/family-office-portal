# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Dense search over the ``family-docs`` and ``tax-law`` collections.

This is an internal function for the portal's chat code, not an HTTP route.
Its request and response follow the search interface the chat code is
written against, so search could move behind an API later without changing
the caller.

How a search runs:

1. The query is embedded with the query prefix (``EmbeddingClient.embed_query``).
2. Each requested collection gets one dense query on the ``dense`` vector,
   ``top_k`` at most 8, with no reranker.
3. For ``family-docs`` the payload filter is part of that Qdrant query:
   ``is_confidential`` must equal ``false`` unless the caller is an admin,
   and ``entity_id`` must be one of ``entity_ids`` when they are given. A
   point with no ``is_confidential`` field, or any value but ``false``, never
   matches, so unlabeled data fails closed. ``tax-law`` holds public law
   summaries with no entity or confidentiality, so it is not filtered.
4. Results from all collections are merged by score and cut to ``top_k``.
   Text is returned as plain data with a citation: document, chunk, title and
   pages for family documents; subtopic ``id`` and ``title`` for tax law.

A collection that has not been indexed yet is reported in
``SearchResponse.missing_collections``; when every requested collection is
missing, search raises ``SearchError`` so the caller can show "not connected"
instead of an empty answer.

Logs carry counts and timings, never the query or result text.

#CRITICAL: security: viewers must never receive confidential text. The
Qdrant filter is the barrier; ``_drop_confidential`` re-checks every
returned point as a second line. #VERIFY: tests/unit/test_search.py
::test_non_admin_never_sees_a_confidential_point,
::test_point_without_a_boolean_false_flag_is_excluded,
::test_filter_runs_inside_the_query_not_after_it and
::test_second_line_drops_every_flag_but_false.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import anyio.to_thread
import httpx
import structlog
from qdrant_client import models
from qdrant_client.common.client_exceptions import QdrantException
from qdrant_client.http.exceptions import ApiException

from app.retrieval.embeddings import EmbeddingClient, EmbeddingError
from app.retrieval.qdrant_store import (
    DENSE_VECTOR,
    FAMILY_DOCS_COLLECTION,
    make_client,
)
from app.retrieval.settings import RetrievalConfigError, load_retrieval_settings
from app.retrieval.tax_law import TAX_LAW_COLLECTION

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType

    from qdrant_client import QdrantClient

    from app.retrieval.settings import RetrievalSettings

logger = structlog.get_logger(__name__)

DEFAULT_TOP_K = 8
MAX_QUERY_CHARS = 4000
SEARCHABLE_COLLECTIONS = (FAMILY_DOCS_COLLECTION, TAX_LAW_COLLECTION)
# One search should finish well under this so chat stays inside its own
# target; a slower search still returns, with a warning.
LATENCY_BUDGET_SECONDS = 2.0
# Request timeout for the query embedding and each Qdrant call. Short, so a
# down service is reported quickly instead of holding the chat request. It is
# a per-call httpx timeout, not a deadline for the whole search: the worst
# case is one embedding call plus two Qdrant calls per collection, each
# bounded separately.
SEARCH_TIMEOUT_SECONDS = 10
# One embedding attempt: the indexer's retry and backoff (up to 30 seconds of
# Retry-After) would defeat the timeout above. A failed attempt is a
# SearchError and chat shows "not connected".
SEARCH_EMBED_ATTEMPTS = 1


class SearchError(RuntimeError):
    """Search could not run. Messages name the failure, never the query."""


def _check_entity_ids(entity_ids: tuple[str, ...] | None) -> None:
    if entity_ids is None:
        return
    if not entity_ids or any(not value.strip() for value in entity_ids):
        msg = "entity_ids must be None or a non-empty list of non-blank ids"
        raise ValueError(msg)


@dataclass(frozen=True)
class SearchRequest:
    """One search.

    Attributes:
        query (str): The user's question. Never logged.
        include_confidential (bool): True only for an admin caller; set it
            from the signed-in user's role, never from user input.
        entity_ids (tuple[str, ...] | None): Limit family documents to these
            entities; None searches every entity.
        collections (tuple[str, ...]): Collections to search.
        top_k (int): Results to return, 1 to 8.
    """

    query: str
    include_confidential: bool = False
    entity_ids: tuple[str, ...] | None = None
    collections: tuple[str, ...] = SEARCHABLE_COLLECTIONS
    top_k: int = DEFAULT_TOP_K

    def __post_init__(self) -> None:
        """Reject requests outside the search interface.

        Raises:
            ValueError: If ``include_confidential`` is not a bool, the query
                is blank or too long, ``top_k`` is out of range,
                ``entity_ids`` is empty or has a blank id, or a collection
                is unknown or none is given. Messages name the field, never
                its value.
        """
        # Exact type check: a truthy non-bool such as "false" must not grant
        # admin visibility, so reject it instead of coercing it.
        if type(self.include_confidential) is not bool:
            msg = "include_confidential must be a bool"
            raise ValueError(msg)
        if not self.query.strip() or len(self.query) > MAX_QUERY_CHARS:
            msg = f"query must be non-blank and at most {MAX_QUERY_CHARS} characters"
            raise ValueError(msg)
        if not 1 <= self.top_k <= DEFAULT_TOP_K:
            msg = f"top_k must be between 1 and {DEFAULT_TOP_K}"
            raise ValueError(msg)
        _check_entity_ids(self.entity_ids)
        if not self.collections or any(
            name not in SEARCHABLE_COLLECTIONS for name in self.collections
        ):
            msg = f"collections must be a non-empty subset of {SEARCHABLE_COLLECTIONS}"
            raise ValueError(msg)


@dataclass(frozen=True)
class DocumentCitation:
    """Where a family-docs result came from.

    Attributes:
        document_id (str | None): The source document.
        chunk_id (str | None): The chunk within it.
        title (str | None): Document title.
        entity_id (str | None): Entity the document belongs to.
        document_date (str | None): Document date, as stored.
        page_start (int | None): First page of the chunk.
        page_end (int | None): Last page of the chunk.
        section (str | None): Innermost section heading.
    """

    document_id: str | None
    chunk_id: str | None
    title: str | None
    entity_id: str | None
    document_date: str | None
    page_start: int | None
    page_end: int | None
    section: str | None

    def as_dict(self) -> dict[str, object]:
        """Return the citation as a plain dict.

        Returns:
            dict[str, object]: Field names to values.
        """
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class TaxLawCitation:
    """Which tax-law subtopic a result came from.

    Attributes:
        subtopic_id (str | None): The subtopic ``id`` chat cites.
        title (str | None): Subtopic title.
        topic (str | None): Topic the subtopic belongs to.
    """

    subtopic_id: str | None
    title: str | None
    topic: str | None

    def as_dict(self) -> dict[str, object]:
        """Return the citation with the knowledge base's own ``id`` key.

        Returns:
            dict[str, object]: ``id``, ``title`` and ``topic``.
        """
        return {"id": self.subtopic_id, "title": self.title, "topic": self.topic}


@dataclass(frozen=True)
class SearchResult:
    """One returned passage. ``text`` is data to quote, never instructions.

    Attributes:
        text (str): Passage text.
        score (float): Cosine similarity to the query.
        collection (str): Collection the passage came from.
        citation (DocumentCitation | TaxLawCitation): Where it came from.
    """

    text: str
    score: float
    collection: str
    citation: DocumentCitation | TaxLawCitation

    def as_dict(self) -> dict[str, object]:
        """Return the result as plain data.

        Returns:
            dict[str, object]: ``text``, ``score``, ``collection``, ``citation``.
        """
        return {
            "text": self.text,
            "score": self.score,
            "collection": self.collection,
            "citation": self.citation.as_dict(),
        }


@dataclass(frozen=True)
class SearchResponse:
    """The results of one search, best first.

    Attributes:
        results (tuple[SearchResult, ...]): At most ``top_k`` results.
        embedding_model (str): Model that embedded the query.
        missing_collections (tuple[str, ...]): Requested collections that do
            not exist yet, so "no relevant passages" can be told apart from
            "not indexed".
    """

    results: tuple[SearchResult, ...]
    embedding_model: str
    missing_collections: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return the response as plain data.

        Returns:
            dict[str, object]: ``results``, ``embedding_model`` and
                ``missing_collections``.
        """
        return {
            "results": [result.as_dict() for result in self.results],
            "embedding_model": self.embedding_model,
            "missing_collections": list(self.missing_collections),
        }


def family_docs_filter(
    *, include_confidential: bool, entity_ids: Sequence[str] | None
) -> models.Filter | None:
    """Build the payload filter that runs inside the family-docs query.

    Args:
        include_confidential (bool): True for an admin caller. Any value
            other than ``True`` is treated as a viewer.
        entity_ids (Sequence[str] | None): Entities to limit results to.

    Returns:
        models.Filter | None: The filter, or None when nothing is limited.
    """
    must: list[models.Condition] = []
    # Only a real True lifts the filter; anything else is treated as a viewer.
    if include_confidential is not True:
        # A point without the field, or with any value but false, does not
        # match: unlabeled data fails closed.
        must.append(
            models.FieldCondition(
                key="is_confidential", match=models.MatchValue(value=False)
            )
        )
    if entity_ids:
        must.append(
            models.FieldCondition(
                key="entity_id", match=models.MatchAny(any=list(entity_ids))
            )
        )
    return models.Filter(must=must) if must else None


def _text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _page(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _pages(payload: Mapping[str, object]) -> tuple[int | None, int | None]:
    raw = payload.get("page_range")
    if not isinstance(raw, list) or not raw:
        return None, None
    values = cast("list[object]", raw)
    start, end = _page(values[0]), _page(values[-1])
    if start is None or end is None:
        return None, None
    return start, end


def _section(payload: Mapping[str, object]) -> str | None:
    raw = payload.get("section_hierarchy")
    if not isinstance(raw, list):
        return None
    names = [
        value.strip()
        for value in cast("list[object]", raw)
        if isinstance(value, str) and value.strip()
    ]
    return names[-1] if names else None


def _to_result(collection: str, point: models.ScoredPoint) -> SearchResult:
    payload: Mapping[str, object] = point.payload or {}
    citation: DocumentCitation | TaxLawCitation
    if collection == TAX_LAW_COLLECTION:
        citation = TaxLawCitation(
            subtopic_id=_text(payload, "id"),
            title=_text(payload, "title"),
            topic=_text(payload, "topic"),
        )
    else:
        page_start, page_end = _pages(payload)
        citation = DocumentCitation(
            document_id=_text(payload, "document_id"),
            chunk_id=_text(payload, "chunk_id"),
            title=_text(payload, "title"),
            entity_id=_text(payload, "entity_id"),
            document_date=_text(payload, "document_date"),
            page_start=page_start,
            page_end=page_end,
            section=_section(payload),
        )
    return SearchResult(
        text=_text(payload, "text") or "",
        score=point.score,
        collection=collection,
        citation=citation,
    )


def _drop_confidential(points: list[models.ScoredPoint]) -> list[models.ScoredPoint]:
    """Keep only points whose payload says ``is_confidential`` is false.

    The Qdrant filter already did this; a point that gets here otherwise
    means the filter did not run, so it is dropped and logged.

    Args:
        points (list[models.ScoredPoint]): Points from a viewer's query.

    Returns:
        list[models.ScoredPoint]: The points that are safe to return.
    """
    kept = [p for p in points if (p.payload or {}).get("is_confidential") is False]
    if len(kept) != len(points):
        logger.error("confidential_results_dropped", count=len(points) - len(kept))
    return kept


class SearchService:
    """Embeds a query and searches the requested collections.

    Build one with ``build_search_service`` and reuse it; it holds connection
    pools. ``close`` (or a ``with`` block) releases them.

    Args:
        embedder (EmbeddingClient): Embeds the query.
        client (QdrantClient): The Qdrant client.
        clock (Callable[[], float]): Monotonic seconds, for timing.
    """

    def __init__(
        self,
        embedder: EmbeddingClient,
        client: QdrantClient,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._embedder = embedder
        self._client = client
        self._clock = clock

    def __enter__(self) -> SearchService:
        """Return the service for use in a ``with`` block.

        Returns:
            SearchService: This service.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close both clients on leaving the ``with`` block.

        Args:
            exc_type (type[BaseException] | None): Exception type, if any.
            exc (BaseException | None): Exception, if any.
            tb (TracebackType | None): Traceback, if any.
        """
        self.close()

    def close(self) -> None:
        """Release the embedding and Qdrant connections."""
        self._embedder.close()
        self._client.close()

    def search(self, request: SearchRequest) -> SearchResponse:
        """Run one search. Blocking; async callers use ``search_async``.

        Args:
            request (SearchRequest): What to search for and who is asking.

        Returns:
            SearchResponse: Up to ``top_k`` results, best first.

        Raises:
            SearchError: If the embedding service or Qdrant fails, or none of
                the requested collections exists. The message names the
                failure type only.
        """
        started = self._clock()
        try:
            results, missing = self._run(request)
        except EmbeddingError as exc:
            reason = f"query embedding failed: {exc}"
        except (ApiException, QdrantException, httpx.HTTPError) as exc:
            reason = f"Qdrant request failed: {type(exc).__name__}"
        else:
            self._log_finished(request, len(results), self._clock() - started)
            return SearchResponse(
                results=tuple(results),
                embedding_model=self._embedder.model,
                missing_collections=missing,
            )
        logger.error("search_failed", reason=reason)
        raise SearchError(reason)

    async def search_async(self, request: SearchRequest) -> SearchResponse:
        """Run ``search`` in a worker thread so the event loop stays free.

        Args:
            request (SearchRequest): What to search for and who is asking.

        Returns:
            SearchResponse: Up to ``top_k`` results, best first.
        """
        return await anyio.to_thread.run_sync(self.search, request)

    def _run(
        self, request: SearchRequest
    ) -> tuple[list[SearchResult], tuple[str, ...]]:
        vector = self._embedder.embed_query(request.query)
        results: list[SearchResult] = []
        missing: list[str] = []
        wanted = tuple(dict.fromkeys(request.collections))
        for collection in wanted:
            if not self._client.collection_exists(collection):
                logger.warning("search_collection_missing", collection=collection)
                missing.append(collection)
                continue
            is_family = collection == FAMILY_DOCS_COLLECTION
            query_filter = (
                family_docs_filter(
                    include_confidential=request.include_confidential,
                    entity_ids=request.entity_ids,
                )
                if is_family
                else None
            )
            points = self._client.query_points(
                collection_name=collection,
                query=vector,
                using=DENSE_VECTOR,
                query_filter=query_filter,
                limit=request.top_k,
                with_payload=True,
            ).points
            if is_family and request.include_confidential is not True:
                points = _drop_confidential(points)
            results.extend(_to_result(collection, point) for point in points)
        if len(missing) == len(wanted):
            msg = "no requested collection has been indexed"
            raise SearchError(msg)
        results.sort(key=lambda result: result.score, reverse=True)
        return results[: request.top_k], tuple(missing)

    @staticmethod
    def _log_finished(request: SearchRequest, count: int, elapsed: float) -> None:
        elapsed_ms = round(elapsed * 1000, 1)
        logger.info(
            "search_finished",
            results=count,
            collections=list(dict.fromkeys(request.collections)),
            include_confidential=request.include_confidential,
            entity_filter=request.entity_ids is not None,
            elapsed_ms=elapsed_ms,
        )
        if elapsed > LATENCY_BUDGET_SECONDS:
            logger.warning(
                "search_over_budget",
                elapsed_ms=elapsed_ms,
                budget_ms=LATENCY_BUDGET_SECONDS * 1000,
            )


def build_search_service(
    settings: RetrievalSettings | None = None,
) -> SearchService | None:
    """Build the search service from settings, or None when search is off.

    Search is off when the embedding service or Qdrant is unset or
    misconfigured, including an unparseable environment value; the caller
    then shows "not connected".

    Args:
        settings (RetrievalSettings | None): Settings to use; None reads the
            environment.

    Returns:
        SearchService | None: A ready service, or None when not connected.
    """
    try:
        current = settings or load_retrieval_settings()
        embedding = current.embedding_connection()
        qdrant = current.qdrant_connection()
    except RetrievalConfigError:
        embedding = qdrant = None
    if embedding is None or qdrant is None:
        logger.info("search_not_connected")
        return None
    embedding = dataclasses.replace(
        embedding,
        timeout_seconds=min(embedding.timeout_seconds, SEARCH_TIMEOUT_SECONDS),
    )
    return SearchService(
        EmbeddingClient(embedding, max_attempts=SEARCH_EMBED_ATTEMPTS),
        make_client(qdrant, timeout=SEARCH_TIMEOUT_SECONDS),
    )
