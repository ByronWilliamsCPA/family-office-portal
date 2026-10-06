# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Index the tax-law knowledge base into the ``tax-law`` Qdrant collection.

Run it as a command from the portal image, like the document indexer::

    python -m app.retrieval.tax_law

The knowledge base is one JSON file whose path is ``TAX_LAW_PATH``. Its shape
is a contract shared with the repository that maintains it::

    {"knowledgeBase": [
        {"id": ..., "topic": ..., "subtopics": [
            {"id": ..., "title": ..., "content": ...}]}]}

Each subtopic with non-blank content becomes one point. The payload carries
the subtopic ``id`` and ``title`` (what chat cites), the ``topic``, the
``text`` that search returns, and ``embedding_model`` and ``embedded_at``.
The embedded text is the title and content, with no query prefix.

Rules:

* The whole file is checked before anything is written. A subtopic id used
  twice anywhere in the file rejects the file.
* Blank subtopics are skipped. A file with nothing to index is refused rather
  than emptying the collection.
* Every subtopic is embedded before any write, so an embedding failure leaves
  the collection as it was. New points are written first, then points whose
  subtopic is no longer in the file are deleted.
* Logs and errors carry subtopic ids and counts, never content.

Exit status: 0 when the collection matches the file, 1 when the file cannot
be used or embedding failed (the collection is unchanged), 2 when the command
is not configured or Qdrant stops answering.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

import structlog
from qdrant_client import models
from qdrant_client.common.client_exceptions import QdrantException
from qdrant_client.http.exceptions import ApiException

from app.retrieval.embeddings import EmbeddingClient, EmbeddingError
from app.retrieval.indexer import EXIT_FAILURES, EXIT_NOT_READY, EXIT_OK
from app.retrieval.qdrant_store import (
    DENSE_VECTOR,
    UPSERT_BATCH_SIZE,
    VectorStoreError,
    ensure_collection,
    make_client,
)
from app.retrieval.settings import RetrievalConfigError, load_retrieval_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from qdrant_client import QdrantClient

    from app.retrieval.settings import EmbeddingConnection, QdrantConnection

logger = structlog.get_logger(__name__)

TAX_LAW_COLLECTION = "tax-law"
_SCROLL_PAGE = 256

# Fixed namespace: the same subtopic id always gives the same point ID.
_POINT_NAMESPACE = uuid.UUID("6f1d2a9c-4b7e-4c3a-8e51-0d9b7a2c5f18")


class KnowledgeBaseError(ValueError):
    """The knowledge-base file cannot be used. Messages never carry content."""


@dataclass(frozen=True)
class Subtopic:
    """One subtopic to index.

    Attributes:
        subtopic_id (str): The subtopic ``id`` that chat cites.
        title (str): Subtopic title.
        topic (str): Name of the topic it belongs to.
        topic_id (str): ID of the topic it belongs to.
        content (str): Subtopic text.
    """

    subtopic_id: str
    title: str
    topic: str
    topic_id: str
    content: str

    @property
    def embedding_text(self) -> str:
        """Return the text to embed: the title, a newline, then the content.

        Returns:
            str: Text sent to the embedding service.
        """
        return f"{self.title}\n{self.content}"


@dataclass(frozen=True)
class KnowledgeBase:
    """The subtopics worth indexing, in file order.

    Attributes:
        subtopics (tuple[Subtopic, ...]): Subtopics with non-blank content.
        skipped_blank (int): Subtopics left out for blank content.
    """

    subtopics: tuple[Subtopic, ...]
    skipped_blank: int


@dataclass(frozen=True)
class TaxLawReport:
    """What one run did.

    Attributes:
        indexed (int): Points written.
        removed (int): Points deleted because their subtopic is gone.
        skipped_blank (int): Subtopics skipped for blank content.
    """

    indexed: int
    removed: int
    skipped_blank: int


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{what} is not a JSON object"
        raise KnowledgeBaseError(msg)
    return cast("dict[str, object]", value)


def _list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{what} is not a list"
        raise KnowledgeBaseError(msg)
    return cast("list[object]", value)


def _string(source: dict[str, object], key: str, where: str) -> str:
    value = source.get(key)
    if not isinstance(value, str):
        msg = f"{where} {key} is not a string"
        raise KnowledgeBaseError(msg)
    return value


def _subtopics(raw: object) -> list[Subtopic]:
    """Check the file's shape and return every subtopic, blank ones included.

    Args:
        raw (object): The decoded JSON document.

    Returns:
        list[Subtopic]: All subtopics in file order.

    Raises:
        KnowledgeBaseError: If the shape is wrong or a subtopic id is blank.
    """
    root = _object(raw, "the knowledge base")
    found: list[Subtopic] = []
    topics = _list(root.get("knowledgeBase"), "knowledgeBase")
    for t_pos, raw_topic in enumerate(topics):
        where = f"topic {t_pos}"
        topic = _object(raw_topic, where)
        topic_id = _string(topic, "id", where)
        topic_name = _string(topic, "topic", where)
        raw_subs = _list(topic.get("subtopics"), f"{where} subtopics")
        for s_pos, raw_sub in enumerate(raw_subs):
            sub_where = f"{where} subtopic {s_pos}"
            sub = _object(raw_sub, sub_where)
            subtopic_id = _string(sub, "id", sub_where).strip()
            if not subtopic_id:
                msg = f"{sub_where} id is blank"
                raise KnowledgeBaseError(msg)
            found.append(
                Subtopic(
                    subtopic_id=subtopic_id,
                    title=_string(sub, "title", sub_where),
                    topic=topic_name,
                    topic_id=topic_id,
                    content=_string(sub, "content", sub_where),
                )
            )
    return found


def parse_knowledge_base(raw: object) -> KnowledgeBase:
    """Check a decoded knowledge base and keep the subtopics with content.

    Args:
        raw (object): The decoded JSON document.

    Returns:
        KnowledgeBase: Subtopics with content, and the blank count.

    Raises:
        KnowledgeBaseError: If the shape is wrong, a subtopic id is blank, or
            any subtopic id appears more than once (blank subtopics included).
    """
    every = _subtopics(raw)
    counts = Counter(sub.subtopic_id for sub in every)
    duplicates = sorted(sid for sid, count in counts.items() if count > 1)
    if duplicates:
        msg = f"duplicate subtopic ids: {', '.join(duplicates)}"
        raise KnowledgeBaseError(msg)
    kept = tuple(sub for sub in every if sub.content.strip())
    return KnowledgeBase(subtopics=kept, skipped_blank=len(every) - len(kept))


def read_knowledge_base(path: Path) -> KnowledgeBase:
    """Read and check the knowledge-base file.

    Args:
        path (Path): The JSON file.

    Returns:
        KnowledgeBase: The parsed knowledge base.

    Raises:
        KnowledgeBaseError: If the file cannot be read or decoded, or its
            content is not a usable knowledge base.
    """
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"cannot read the knowledge base: {type(exc).__name__}"
        raise KnowledgeBaseError(msg) from exc
    return parse_knowledge_base(raw)


def subtopic_point_id(subtopic_id: str) -> str:
    """Return the deterministic point ID for a subtopic.

    Args:
        subtopic_id (str): The subtopic ``id``.

    Returns:
        str: A UUID string.
    """
    return str(uuid.uuid5(_POINT_NAMESPACE, subtopic_id))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaxLawIndexer:
    """Keeps the ``tax-law`` collection in step with the knowledge-base file.

    Args:
        embedder (EmbeddingClient): Embeds subtopic text.
        client (QdrantClient): The Qdrant client.
        collection (str): Collection name.
        clock (Callable[[], str]): Returns the ``embedded_at`` timestamp.
    """

    def __init__(
        self,
        embedder: EmbeddingClient,
        client: QdrantClient,
        *,
        collection: str = TAX_LAW_COLLECTION,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._embedder = embedder
        self._client = client
        self.collection = collection
        self._clock = clock

    def run(self, path: Path) -> TaxLawReport:
        """Index the file and drop points for subtopics it no longer has.

        Args:
            path (Path): The knowledge-base file.

        Returns:
            TaxLawReport: What the run did.

        Raises:
            KnowledgeBaseError: If the file cannot be used or has nothing to
                index. Nothing is written in that case.
        """
        knowledge_base = read_knowledge_base(path)
        if not knowledge_base.subtopics:
            # #EDGE: data integrity: an empty or all-blank file would delete
            # every point. #VERIFY: tests/unit/test_tax_law.py::
            # test_file_with_no_content_is_refused_and_keeps_points.
            msg = "no subtopic has content"
            raise KnowledgeBaseError(msg)
        subtopics = knowledge_base.subtopics
        vectors = self._embedder.embed_documents(
            [sub.embedding_text for sub in subtopics]
        )
        ensure_collection(self._client, self.collection)
        embedded_at = self._clock()
        points = [
            models.PointStruct(
                id=subtopic_point_id(sub.subtopic_id),
                vector={DENSE_VECTOR: vector},
                payload={
                    "id": sub.subtopic_id,
                    "title": sub.title,
                    "topic": sub.topic,
                    "topic_id": sub.topic_id,
                    "text": sub.content,
                    "embedding_model": self._embedder.model,
                    "embedded_at": embedded_at,
                },
            )
            for sub, vector in zip(subtopics, vectors, strict=True)
        ]
        for start in range(0, len(points), UPSERT_BATCH_SIZE):
            self._client.upsert(
                collection_name=self.collection,
                points=points[start : start + UPSERT_BATCH_SIZE],
                wait=True,
            )
        keep = {str(point.id) for point in points}
        stale = [pid for pid in self._stored_point_ids() if str(pid) not in keep]
        if stale:
            self._client.delete(
                collection_name=self.collection,
                points_selector=models.PointIdsList(points=stale),
                wait=True,
            )
        return TaxLawReport(
            indexed=len(points),
            removed=len(stale),
            skipped_blank=knowledge_base.skipped_blank,
        )

    def _stored_point_ids(self) -> list[models.ExtendedPointId]:
        """Return the ID of every point in the collection.

        Returns:
            list[models.ExtendedPointId]: Point IDs.
        """
        found: list[models.ExtendedPointId] = []
        offset: models.ExtendedPointId | None = None
        while True:
            records, next_offset = self._client.scroll(
                collection_name=self.collection,
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            found.extend(record.id for record in records)
            if next_offset is None:
                return found
            offset = cast("models.ExtendedPointId", next_offset)


def _connections() -> tuple[EmbeddingConnection, QdrantConnection, Path] | None:
    """Read the command's settings, logging why it cannot run.

    Returns:
        tuple[EmbeddingConnection, QdrantConnection, Path] | None: The
        embedding connection, Qdrant connection and knowledge-base file, or
        None when any is missing or misconfigured.
    """
    try:
        settings = load_retrieval_settings()
        embedding = settings.embedding_connection()
        qdrant = settings.qdrant_connection()
    except RetrievalConfigError as exc:
        problem = str(exc)
    else:
        path = settings.tax_law_file()
        if embedding is not None and qdrant is not None and path is not None:
            return embedding, qdrant, path
        logger.info(
            "tax_law_not_connected",
            detail="EMBED_BASE_URL, QDRANT_URL and TAX_LAW_PATH must all be set",
        )
        return None
    logger.error("tax_law_misconfigured", reason=problem)
    return None


def run_tax_law_indexer(
    embedding: EmbeddingConnection, client: QdrantClient, path: Path
) -> int:
    """Run one tax-law pass and turn its result into an exit status.

    Args:
        embedding (EmbeddingConnection): Embedding service settings.
        client (QdrantClient): Qdrant client; closed by the caller.
        path (Path): Knowledge-base file.

    Returns:
        int: Process exit status.
    """
    failure = ""
    stopped = ""
    try:
        with EmbeddingClient(embedding) as embedder:
            report = TaxLawIndexer(embedder, client).run(path)
    except (KnowledgeBaseError, EmbeddingError) as exc:
        failure = str(exc)
    except VectorStoreError as exc:
        stopped = str(exc)
    except (ApiException, QdrantException) as exc:
        stopped = f"Qdrant request failed: {type(exc).__name__}"
    else:
        logger.info(
            "tax_law_index_finished",
            indexed=report.indexed,
            removed=report.removed,
            skipped_blank=report.skipped_blank,
        )
        return EXIT_OK
    # Logged without a traceback, which could carry file content.
    if stopped:
        logger.error("tax_law_index_stopped", reason=stopped)
        return EXIT_NOT_READY
    logger.error("tax_law_index_failed", reason=failure)
    return EXIT_FAILURES


def main() -> int:
    """Index the configured knowledge base with settings from the environment.

    Returns:
        int: Process exit status: 0 on success, 1 when the file or embedding
        failed, 2 when the command is not configured or Qdrant failed.
    """
    connections = _connections()
    if connections is None:
        return EXIT_NOT_READY
    embedding, qdrant, path = connections
    client = make_client(qdrant)
    try:
        return run_tax_law_indexer(embedding, client, path)
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
