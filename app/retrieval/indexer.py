# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Index chunk-set files into the ``family-docs`` Qdrant collection.

Run it as a scheduled command from the portal image, never inside the web
process::

    python -m app.retrieval.indexer

One run reads every ``<document_id>.json`` in ``CHUNKS_DIR`` and, per file:

1. A tax return without consent on file is not indexed, and any points it
   already has are deleted.
2. A file whose ``sha256``, consent and embedding model all match the stored
   points, with every point present, is skipped as unchanged. A consent
   change does not change the hash, so the hash alone never decides.
3. Otherwise the chunks are embedded and the document's points replaced.
   A set with no chunk text leaves the document with no points.

Then documents that have points but no file any more are deleted. A file that
cannot be read or embedded keeps its existing points and makes the run exit
with status 1. Logs name document IDs and counts only, never text.

Exit status: 0 when every file was handled, 1 when any file failed, 2 when
the indexer is not configured, cannot start, or Qdrant stops answering.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import structlog
from qdrant_client.http.exceptions import ApiException

from app.retrieval.chunk_sets import ChunkSetError, list_chunk_set_files, read_chunk_set
from app.retrieval.embeddings import EmbeddingClient, EmbeddingError
from app.retrieval.qdrant_store import DensePoint, FamilyDocsWriter, make_client
from app.retrieval.settings import RetrievalConfigError, load_retrieval_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from qdrant_client import QdrantClient

    from app.retrieval.chunk_sets import ChunkSet
    from app.retrieval.settings import EmbeddingConnection, QdrantConnection

logger = structlog.get_logger(__name__)

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_NOT_READY = 2


class Outcome(Enum):
    """What happened to one document in a run."""

    INDEXED = "indexed"
    UNCHANGED = "unchanged"
    NO_CONSENT = "no_consent"
    EMPTY = "empty"
    REMOVED = "removed"
    FAILED = "failed"


class IndexerError(RuntimeError):
    """The run cannot start, for example because the directory is missing."""


@dataclass
class RunReport:
    """Per-outcome document counts for one run.

    Attributes:
        counts (Counter[Outcome]): Documents per outcome.
        orphan_check_skipped (bool): True when the directory held no files,
            so no points were deleted as orphans.
    """

    counts: Counter[Outcome] = field(default_factory=Counter)
    orphan_check_skipped: bool = False

    @property
    def failed(self) -> int:
        """Return how many documents failed.

        Returns:
            int: Failed document count.
        """
        return self.counts[Outcome.FAILED]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentIndexer:
    """Keeps a Qdrant collection in step with a chunk-set directory.

    Args:
        embedder (EmbeddingClient): Embeds chunk text.
        writer (FamilyDocsWriter): Writes and removes points.
        clock (Callable[[], str]): Returns the ``embedded_at`` timestamp.
    """

    def __init__(
        self,
        embedder: EmbeddingClient,
        writer: FamilyDocsWriter,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._embedder = embedder
        self._writer = writer
        self._clock = clock

    def run(self, directory: Path) -> RunReport:
        """Index every chunk-set file in a directory and drop removed ones.

        Args:
            directory (Path): The chunk-set directory.

        Returns:
            RunReport: Counts per outcome.

        Raises:
            IndexerError: If the directory does not exist.
        """
        # #CRITICAL: data integrity: a missing mount must not look like "every
        # document was deleted". #VERIFY: the run stops before any delete.
        if not directory.is_dir():
            msg = "the chunk-set directory does not exist"
            raise IndexerError(msg)
        self._writer.ensure_collection()
        report = RunReport()
        files = list_chunk_set_files(directory)
        for path in files:
            report.counts[self.index_file(path)] += 1
        on_disk = {path.stem for path in files}
        if not files:
            # #EDGE: an empty directory may be a mount that is not ready yet.
            # #VERIFY: deleting the last document leaves its points until a
            # file appears or an admin clears them.
            report.orphan_check_skipped = True
            logger.warning("orphan_check_skipped_empty_directory")
        else:
            for document_id in sorted(self._writer.document_ids() - on_disk):
                self._writer.delete_document(document_id)
                logger.info("document_points_removed", document_id=document_id)
                report.counts[Outcome.REMOVED] += 1
        logger.info(
            "index_run_finished",
            **{outcome.value: report.counts[outcome] for outcome in Outcome},
        )
        return report

    def index_file(self, path: Path) -> Outcome:
        """Handle one chunk-set file.

        Args:
            path (Path): The file, named ``<document_id>.json``.

        Returns:
            Outcome: What happened to the document.
        """
        try:
            chunk_set = read_chunk_set(path)
        except ChunkSetError as exc:
            reason = str(exc)
        else:
            return self.index_chunk_set(chunk_set)
        # Logged without a traceback, which could carry file content.
        logger.error("chunk_set_unreadable", document_id=path.stem, reason=reason)
        return Outcome.FAILED

    def index_chunk_set(self, chunk_set: ChunkSet) -> Outcome:
        """Apply the consent, unchanged and replace rules to one chunk set.

        Args:
            chunk_set (ChunkSet): The parsed set.

        Returns:
            Outcome: What happened to the document.
        """
        document_id = chunk_set.document_id
        if not chunk_set.may_be_indexed:
            # #CRITICAL: security: tax returns are indexed only with consent;
            # withdrawn consent removes existing points. #VERIFY:
            # tests/unit/test_indexer.py::test_withdrawn_consent_deletes_points.
            self._writer.delete_document(document_id)
            logger.info("document_not_indexed_no_consent", document_id=document_id)
            return Outcome.NO_CONSENT
        stored = self._writer.stored_document(document_id)
        if (
            stored is not None
            and stored.complete
            and chunk_set.sha256 is not None
            and stored.sha256 == chunk_set.sha256
            and stored.consent_on_file is chunk_set.consent_on_file
            and stored.embedding_model == self._embedder.model
        ):
            return Outcome.UNCHANGED
        if not chunk_set.chunks:
            self._writer.delete_document(document_id)
            logger.info("document_has_no_chunk_text", document_id=document_id)
            return Outcome.EMPTY
        try:
            vectors = self._embedder.embed_documents(
                [chunk.text for chunk in chunk_set.chunks]
            )
        except EmbeddingError as exc:
            failure = str(exc)
        else:
            return self._write(chunk_set, vectors)
        logger.error(
            "document_embedding_failed", document_id=document_id, reason=failure
        )
        return Outcome.FAILED

    def _write(self, chunk_set: ChunkSet, vectors: list[list[float]]) -> Outcome:
        """Replace a document's points with freshly embedded chunks.

        Args:
            chunk_set (ChunkSet): The parsed set.
            vectors (list[list[float]]): One vector per chunk, in order.

        Returns:
            Outcome: Always ``Outcome.INDEXED``.
        """
        document_id = chunk_set.document_id
        embedded_at = self._clock()
        points = [
            DensePoint(
                chunk_index=chunk.index,
                vector=vector,
                payload={
                    **chunk.payload,
                    "text": chunk.text,
                    "embedding_model": self._embedder.model,
                    "embedded_at": embedded_at,
                },
            )
            for chunk, vector in zip(chunk_set.chunks, vectors, strict=True)
        ]
        written = self._writer.replace_document(document_id, points)
        logger.info("document_indexed", document_id=document_id, points=written)
        return Outcome.INDEXED


def _connections() -> tuple[EmbeddingConnection, QdrantConnection, Path] | None:
    """Read the indexer's settings, logging why it cannot run.

    Returns:
        tuple[EmbeddingConnection, QdrantConnection, Path] | None: The
        embedding connection, Qdrant connection and chunk-set directory, or
        None when any is missing or misconfigured.
    """
    settings = load_retrieval_settings()
    try:
        embedding = settings.embedding_connection()
        qdrant = settings.qdrant_connection()
    except RetrievalConfigError as exc:
        problem = str(exc)
    else:
        directory = settings.chunks_path()
        if embedding is not None and qdrant is not None and directory is not None:
            return embedding, qdrant, directory
        logger.info(
            "indexer_not_connected",
            detail="EMBED_BASE_URL, QDRANT_URL and CHUNKS_DIR must all be set",
        )
        return None
    logger.error("indexer_misconfigured", reason=problem)
    return None


def run_indexer(
    embedding: EmbeddingConnection,
    client: QdrantClient,
    directory: Path,
) -> int:
    """Run one indexing pass and turn its result into an exit status.

    Args:
        embedding (EmbeddingConnection): Embedding service settings.
        client (QdrantClient): Qdrant client; closed by the caller.
        directory (Path): Chunk-set directory.

    Returns:
        int: Process exit status.
    """
    problem = ""
    try:
        with EmbeddingClient(embedding) as embedder:
            report = DocumentIndexer(embedder, FamilyDocsWriter(client)).run(directory)
    except IndexerError as exc:
        problem = str(exc)
    except ApiException as exc:
        problem = f"Qdrant request failed: {type(exc).__name__}"
    else:
        return EXIT_FAILURES if report.failed else EXIT_OK
    logger.error("index_run_stopped", reason=problem)
    return EXIT_NOT_READY


def main() -> int:
    """Run one indexing pass with settings from the environment.

    Returns:
        int: Process exit status: 0 when every file was handled, 1 when any
        file failed, 2 when the run could not start or was stopped.
    """
    connections = _connections()
    if connections is None:
        return EXIT_NOT_READY
    embedding, qdrant, directory = connections
    client = make_client(qdrant)
    try:
        return run_indexer(embedding, client, directory)
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
