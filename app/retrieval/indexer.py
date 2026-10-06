# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Index chunk-set files into the ``family-docs`` Qdrant collection.

Run it as a scheduled command from the portal image, never inside the web
process::

    python -m app.retrieval.indexer

One run reads every ``<document_id>.json`` in ``CHUNKS_DIR`` and, per file:

1. A tax return without consent on file is not indexed, and any points it
   already has are deleted.
2. A file is skipped as unchanged only when it has a ``sha256``, its hash,
   consent and the embedding model match the stored points, every point is
   present, and the digest of its chunk text and payload matches the one
   stored on every point. A consent, confidentiality or chunking change does
   not change the hash, so the hash alone never decides. A file without a
   hash is never skipped; each one is logged.
3. Otherwise the chunks are embedded and the document's points replaced.
   A set with no chunk text leaves the document with no points.

Then documents that have points but no file any more are deleted, unless the
directory is empty or the sweep would delete most of the indexed documents at
once (both look like a mount that is not ready).

A file that cannot be read keeps its existing points, unless those points are
a tax return, which are deleted because the file may be a consent withdrawal
that failed to parse. A file that cannot be embedded keeps its points. A
Qdrant refusal for one document fails that document only; the run goes on.
Each such file makes the run exit with status 1. Logs name document IDs,
counts and exception type names only, never text.

Exit status: 0 when every file was handled, 1 when any file failed, 2 when
the indexer is not configured, cannot start, the embedding service refuses
the key, or Qdrant stops answering.

#ASSUME: concurrency: runs never overlap; the schedule starts one run at a
time and a run finishes well inside its interval. #VERIFY: the scheduler
config allows one concurrent run, and run time is checked against the
interval after the first full index.

#ASSUME: data integrity: the pipeline writes each file atomically
(temporary hidden file, then rename), so a run never reads half a file.
#VERIFY: the pipeline's writer stages under a hidden name in the same
directory and renames; ``list_chunk_set_files`` skips hidden names.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import structlog
from qdrant_client.common.client_exceptions import QdrantException
from qdrant_client.http.exceptions import ApiException, UnexpectedResponse

from app.retrieval.chunk_sets import ChunkSetError, list_chunk_set_files, read_chunk_set
from app.retrieval.embeddings import EmbeddingClient, EmbeddingError
from app.retrieval.qdrant_store import (
    INDEX_DIGEST_FIELD,
    DensePoint,
    FamilyDocsWriter,
    VectorStoreError,
    make_client,
)
from app.retrieval.settings import RetrievalConfigError, load_retrieval_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from qdrant_client import QdrantClient

    from app.retrieval.chunk_sets import ChunkSet
    from app.retrieval.qdrant_store import StoredDocument
    from app.retrieval.settings import EmbeddingConnection, QdrantConnection

logger = structlog.get_logger(__name__)

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_NOT_READY = 2

# The orphan sweep is skipped when it would delete at least this many
# documents and more than half of those indexed: a partial mount, not a
# real clean-up.
ORPHAN_GUARD_MIN = 5


class Outcome(Enum):
    """What happened to one document in a run."""

    INDEXED = "indexed"
    UNCHANGED = "unchanged"
    NO_CONSENT = "no_consent"
    EMPTY = "empty"
    REMOVED = "removed"
    FAILED = "failed"


class IndexerError(RuntimeError):
    """The run cannot start or must stop, for example a missing directory."""


@dataclass(frozen=True)
class Connections:
    """Everything one run needs from the settings.

    Attributes:
        embedding (EmbeddingConnection): Embedding service settings.
        qdrant (QdrantConnection): Qdrant settings.
        directory (Path): Chunk-set directory.
    """

    embedding: EmbeddingConnection
    qdrant: QdrantConnection
    directory: Path


@dataclass
class RunReport:
    """Per-outcome document counts for one run.

    Attributes:
        counts (Counter[Outcome]): Documents per outcome.
        orphan_check_skipped (bool): True when the orphan sweep was skipped
            (an empty directory, or too many documents missing at once), so
            no points were deleted as orphans.
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


def index_digest(chunk_set: ChunkSet, model: str) -> str:
    """Return a digest of everything a document's points are built from.

    It covers the embedding model and, for every chunk, its position, text
    and payload (confidentiality, entity, consent, hash and the rest), so
    any change to them changes the digest.

    Args:
        chunk_set (ChunkSet): The parsed set.
        model (str): Embedding model name.

    Returns:
        str: A hex SHA-256 digest.
    """
    body = json.dumps(
        {
            "model": model,
            "chunks": [
                [chunk.index, chunk.text, dict(chunk.payload)]
                for chunk in chunk_set.chunks
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _is_unchanged(
    stored: StoredDocument | None, chunk_set: ChunkSet, model: str, digest: str
) -> bool:
    return (
        stored is not None
        and stored.complete
        and chunk_set.sha256 is not None
        and stored.sha256 == chunk_set.sha256
        and stored.consent_on_file == chunk_set.consent_on_file
        and stored.embedding_model == model
        and stored.index_digest == digest
    )


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
            IndexerError: If the directory does not exist, or the embedding
                service refuses the key.
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
        self._sweep_orphans({path.stem for path in files}, report)
        logger.info(
            "index_run_finished",
            orphan_check_skipped=report.orphan_check_skipped,
            **{outcome.value: report.counts[outcome] for outcome in Outcome},
        )
        return report

    def _sweep_orphans(self, on_disk: set[str], report: RunReport) -> None:
        """Delete the points of documents whose file is gone.

        Args:
            on_disk (set[str]): Document IDs that have a file.
            report (RunReport): Updated with removals or the skip.
        """
        if not on_disk:
            # #EDGE: an empty directory may be a mount that is not ready yet.
            # #VERIFY: deleting the last document leaves its points until a
            # file appears or an admin clears them.
            report.orphan_check_skipped = True
            logger.warning("orphan_check_skipped_empty_directory")
            return
        stored = self._writer.document_ids()
        orphans = sorted(stored - on_disk)
        if len(orphans) >= ORPHAN_GUARD_MIN and len(orphans) * 2 > len(stored):
            # #EDGE: a partly mounted directory would look like most
            # documents were removed. #VERIFY: after a real bulk removal an
            # admin deletes the points or runs again once the files settle.
            report.orphan_check_skipped = True
            logger.warning(
                "orphan_check_skipped_too_many_missing",
                missing=len(orphans),
                indexed=len(stored),
            )
            return
        for document_id in orphans:
            self._writer.delete_document(document_id)
            logger.info("document_points_removed", document_id=document_id)
            report.counts[Outcome.REMOVED] += 1

    def index_file(self, path: Path) -> Outcome:
        """Handle one chunk-set file; a Qdrant refusal fails this file only.

        ``IndexerError`` from a refused embedding key is not caught here, so
        it stops the run.

        Args:
            path (Path): The file, named ``<document_id>.json``.

        Returns:
            Outcome: What happened to the document.
        """
        try:
            return self._index_file(path)
        except (UnexpectedResponse, VectorStoreError) as exc:
            # A transport failure (ResponseHandlingException) or a 429 is not
            # caught here: Qdrant is not answering, so the run stops.
            reason = f"Qdrant request failed: {type(exc).__name__}"
        # Logged without a traceback, which could carry payload content.
        logger.error("document_write_failed", document_id=path.stem, reason=reason)
        return Outcome.FAILED

    def _index_file(self, path: Path) -> Outcome:
        try:
            chunk_set = read_chunk_set(path)
        except ChunkSetError as exc:
            reason = str(exc)
        else:
            return self.index_chunk_set(chunk_set)
        # Logged without a traceback, which could carry file content.
        logger.error("chunk_set_unreadable", document_id=path.stem, reason=reason)
        stored = self._writer.stored_document(path.stem)
        if stored is not None and stored.is_tax_return:
            # #CRITICAL: security: an unreadable file may be a consent
            # withdrawal that failed to parse, so a tax return loses its
            # points. #VERIFY: tests/unit/test_indexer.py::
            # test_unreadable_tax_return_loses_its_points.
            self._writer.delete_document(path.stem)
            logger.warning("unreadable_tax_return_removed", document_id=path.stem)
        return Outcome.FAILED

    def index_chunk_set(self, chunk_set: ChunkSet) -> Outcome:
        """Apply the consent, unchanged and replace rules to one chunk set.

        Args:
            chunk_set (ChunkSet): The parsed set.

        Returns:
            Outcome: What happened to the document.

        Raises:
            IndexerError: If the embedding service refuses the key.
        """
        document_id = chunk_set.document_id
        if not chunk_set.may_be_indexed:
            # #CRITICAL: security: tax returns are indexed only with consent;
            # withdrawn consent removes existing points. #VERIFY:
            # tests/unit/test_indexer.py::test_withdrawn_consent_deletes_points.
            self._writer.delete_document(document_id)
            logger.info("document_not_indexed_no_consent", document_id=document_id)
            return Outcome.NO_CONSENT
        model = self._embedder.model
        digest = index_digest(chunk_set, model)
        stored = self._writer.stored_document(document_id)
        if _is_unchanged(stored, chunk_set, model, digest):
            return Outcome.UNCHANGED
        if not chunk_set.chunks:
            self._writer.delete_document(document_id)
            logger.info("document_has_no_chunk_text", document_id=document_id)
            return Outcome.EMPTY
        if chunk_set.sha256 is None:
            logger.warning("document_has_no_sha256", document_id=document_id)
        try:
            vectors = self._embedder.embed_documents(
                [chunk.text for chunk in chunk_set.chunks]
            )
        except EmbeddingError as exc:
            if exc.refused_key:
                msg = f"the embedding service refused the key: {exc}"
                raise IndexerError(msg) from None
            failure = str(exc)
        else:
            return self._write(chunk_set, vectors, digest)
        logger.error(
            "document_embedding_failed", document_id=document_id, reason=failure
        )
        return Outcome.FAILED

    def _write(
        self, chunk_set: ChunkSet, vectors: list[list[float]], digest: str
    ) -> Outcome:
        """Replace a document's points with freshly embedded chunks.

        Args:
            chunk_set (ChunkSet): The parsed set.
            vectors (list[list[float]]): One vector per chunk, in order.
            digest (str): ``index_digest`` of the set and model.

        Returns:
            Outcome: ``Outcome.INDEXED``; a failed write raises instead.
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
                    INDEX_DIGEST_FIELD: digest,
                },
            )
            for chunk, vector in zip(chunk_set.chunks, vectors, strict=True)
        ]
        written = self._writer.replace_document(document_id, points)
        logger.info("document_indexed", document_id=document_id, points=written)
        return Outcome.INDEXED


def _connections() -> Connections | None:
    """Read the indexer's settings, logging why it cannot run.

    Returns:
        Connections | None: What the run needs, or None when any setting is
        missing or misconfigured.
    """
    try:
        settings = load_retrieval_settings()
        embedding = settings.embedding_connection()
        qdrant = settings.qdrant_connection()
    except RetrievalConfigError as exc:
        problem = str(exc)
    else:
        directory = settings.chunks_path()
        if embedding is not None and qdrant is not None and directory is not None:
            return Connections(embedding, qdrant, directory)
        logger.warning(
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
    except (IndexerError, VectorStoreError) as exc:
        problem = str(exc)
    except (ApiException, QdrantException) as exc:
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
    client = make_client(connections.qdrant)
    try:
        return run_indexer(connections.embedding, client, connections.directory)
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
