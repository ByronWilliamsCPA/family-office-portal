# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the document indexer command.

The embedding service is a fake on ``httpx.MockTransport`` and Qdrant runs
in-process (``QdrantClient(":memory:")``). These prove the rules: consent
fails closed, unchanged files are skipped only when hash, consent, model and
content digest all match, removed files lose their points, failures keep old
points (except an unreadable tax return), and one document's Qdrant failure
does not stop the run.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from structlog.testing import capture_logs

from app.retrieval import indexer as indexer_module
from app.retrieval import qdrant_store
from app.retrieval.embeddings import EmbeddingClient
from app.retrieval.indexer import DocumentIndexer, IndexerError, Outcome, main
from app.retrieval.qdrant_store import FAMILY_DOCS_COLLECTION, FamilyDocsWriter
from tests.unit.chunk_set_factory import (
    DOC_A,
    DOC_B,
    DOC_TAX,
    chunk_set,
    write_chunk_set,
)
from tests.unit.fake_embeddings import MODEL, FakeEmbeddingService

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from app.retrieval.settings import EmbeddingConnection

STAMP = "2026-01-02T03:04:05+00:00"
# Generated per run so no credential-shaped literal sits in the source.
USERINFO_SECRET = secrets.token_urlsafe(9)

# In-process Qdrant warns that payload indexes have no effect; that is expected.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Payload indexes have no effect:UserWarning"
)


def _no_wait(_seconds: float) -> None:
    """Skip the retry delay in tests."""


@pytest.fixture
def service() -> FakeEmbeddingService:
    """Return a fresh fake embedding service."""
    return FakeEmbeddingService()


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    """Yield an in-process Qdrant client."""
    client = QdrantClient(":memory:")
    yield client
    client.close()


@pytest.fixture
def chunks_dir(tmp_path: Path) -> Path:
    """Return an empty chunk-set directory."""
    directory = tmp_path / "chunks"
    directory.mkdir()
    return directory


@pytest.fixture
def embedder(service: FakeEmbeddingService) -> Iterator[EmbeddingClient]:
    """Yield an embeddings client wired to the fake service."""
    with EmbeddingClient(
        service.connection(), transport=service.transport(), sleep=_no_wait
    ) as client:
        yield client


@pytest.fixture
def indexer(embedder: EmbeddingClient, qdrant: QdrantClient) -> DocumentIndexer:
    """Return an indexer with a fixed embedded_at clock."""
    return DocumentIndexer(embedder, FamilyDocsWriter(qdrant), clock=lambda: STAMP)


def _payloads(qdrant: QdrantClient, document_id: str) -> list[dict[str, Any]]:
    """Return the stored payloads of one document, by chunk position."""
    if not qdrant.collection_exists(FAMILY_DOCS_COLLECTION):
        return []
    records, _ = qdrant.scroll(
        FAMILY_DOCS_COLLECTION,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )
            ]
        ),
        limit=100,
        with_payload=True,
    )
    return sorted(
        (dict(record.payload or {}) for record in records),
        key=lambda payload: payload["chunk_index"],
    )


# --------------------------------------------------------------------------- #
# Indexing and payload
# --------------------------------------------------------------------------- #


def test_indexes_a_new_document_with_the_full_payload(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Indexes a new document with the full payload."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert report.failed == 0
    payloads = _payloads(qdrant, DOC_A)
    assert [p["text"] for p in payloads] == ["alpha beta", "gamma delta"]
    first = payloads[0]
    assert first["embedding_model"] == MODEL
    assert first["embedded_at"] == STAMP
    assert first["trust_score"] is None
    assert first["ocr_engine_provenance"] is None
    assert first["hallucination_risk"] is None
    assert first["is_confidential"] is False
    assert first["consent_on_file"] is False
    assert first["sha256"] == "a" * 64
    assert first["chunk_count"] == 2
    assert first["chunk_id"] == f"chunk-{DOC_A}-0"


def test_documents_are_embedded_without_the_query_prefix(
    indexer: DocumentIndexer, service: FakeEmbeddingService, chunks_dir: Path
) -> None:
    """Documents are embedded without the query prefix."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    assert service.requests[0]["body"]["input"] == ["alpha beta", "gamma delta"]


def test_unchanged_file_is_skipped(
    indexer: DocumentIndexer, service: FakeEmbeddingService, chunks_dir: Path
) -> None:
    """Unchanged file is skipped."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    calls = len(service.requests)
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.UNCHANGED] == 1
    assert len(service.requests) == calls


def test_changed_hash_replaces_the_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Changed hash replaces the points."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("one", "two", "three")))
    indexer.run(chunks_dir)
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("new",), sha256="b" * 64))
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    payloads = _payloads(qdrant, DOC_A)
    assert [p["text"] for p in payloads] == ["new"]
    assert payloads[0]["sha256"] == "b" * 64


def test_consent_change_with_same_hash_is_not_skipped(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Consent change with same hash is not skipped."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, consent=False))
    indexer.run(chunks_dir)
    write_chunk_set(chunks_dir, chunk_set(DOC_A, consent=True))
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert all(p["consent_on_file"] is True for p in _payloads(qdrant, DOC_A))


def test_confidentiality_change_with_same_hash_is_applied(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """A confidentiality change with the same hash rewrites the points."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, is_confidential=False))
    indexer.run(chunks_dir)
    write_chunk_set(chunks_dir, chunk_set(DOC_A, is_confidential=True))
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert [p["is_confidential"] for p in _payloads(qdrant, DOC_A)] == [True, True]


def test_entity_change_with_same_hash_is_applied(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """An entity change with the same hash rewrites the points."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    write_chunk_set(chunks_dir, chunk_set(DOC_A, entity_id="entity-2"))
    assert indexer.run(chunks_dir).counts[Outcome.INDEXED] == 1
    assert {p["entity_id"] for p in _payloads(qdrant, DOC_A)} == {"entity-2"}


def test_rechunking_with_same_hash_is_applied(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """New chunks with the same hash replace the old text and count."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("one", "two")))
    indexer.run(chunks_dir)
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("uno", "dos", "tres")))
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert [p["text"] for p in _payloads(qdrant, DOC_A)] == ["uno", "dos", "tres"]
    assert indexer.run(chunks_dir).counts[Outcome.UNCHANGED] == 1


def test_shorter_rechunk_removes_the_extra_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """A rechunk into fewer chunks leaves no stale points behind."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("one", "two", "three")))
    indexer.run(chunks_dir)
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("only",)))
    indexer.run(chunks_dir)
    assert [p["text"] for p in _payloads(qdrant, DOC_A)] == ["only"]


def test_model_change_reembeds(
    service: FakeEmbeddingService, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Model change reembeds."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    writer = FamilyDocsWriter(qdrant)
    with EmbeddingClient(
        service.connection(), transport=service.transport(), sleep=_no_wait
    ) as one:
        DocumentIndexer(one, writer).run(chunks_dir)
    other = service.connection()
    other = type(other)(base_url=other.base_url, api_key=other.api_key, model="m2")
    with EmbeddingClient(other, transport=service.transport(), sleep=_no_wait) as two:
        report = DocumentIndexer(two, writer).run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert _payloads(qdrant, DOC_A)[0]["embedding_model"] == "m2"


def test_missing_hash_is_never_skipped(
    indexer: DocumentIndexer, chunks_dir: Path
) -> None:
    """Missing hash is never skipped."""
    data = chunk_set(DOC_A)
    for chunk in data["chunks"]:
        del chunk["sha256"]
    write_chunk_set(chunks_dir, data)
    indexer.run(chunks_dir)
    with capture_logs() as logs:
        assert indexer.run(chunks_dir).counts[Outcome.INDEXED] == 1
    hashless = [log for log in logs if log["event"] == "document_has_no_sha256"]
    assert hashless == [
        {
            "event": "document_has_no_sha256",
            "document_id": DOC_A,
            "log_level": "warning",
        }
    ]


def test_incomplete_points_are_rewritten(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Incomplete points are rewritten."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    records, _ = qdrant.scroll(FAMILY_DOCS_COLLECTION, limit=1)
    qdrant.delete(
        FAMILY_DOCS_COLLECTION,
        points_selector=models.PointIdsList(points=[records[0].id]),
    )
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert len(_payloads(qdrant, DOC_A)) == 2


def test_blank_chunk_text_is_not_embedded(
    indexer: DocumentIndexer, service: FakeEmbeddingService, chunks_dir: Path
) -> None:
    """Blank chunk text is not embedded."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("one", " ", "three")))
    indexer.run(chunks_dir)
    assert service.requests[0]["body"]["input"] == ["one", "three"]


def test_set_with_no_text_removes_existing_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Set with no text removes existing points."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    empty = chunk_set(DOC_A, texts=(), sha256="c" * 64)
    empty["category"] = "LLCs"  # classified, so no consent is needed
    write_chunk_set(chunks_dir, empty)
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.EMPTY] == 1
    assert _payloads(qdrant, DOC_A) == []


# --------------------------------------------------------------------------- #
# Consent for tax returns (fail closed)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("consent", [False, None, "true", ...])
def test_tax_return_without_consent_is_not_indexed(
    indexer: DocumentIndexer,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    chunks_dir: Path,
    consent: object,
) -> None:
    """Tax return without consent is not indexed."""
    data = chunk_set(DOC_TAX, category="Tax Returns", document_type="tax_return")
    for chunk in data["chunks"]:
        if consent is ...:
            del chunk["consent_on_file"]
        else:
            chunk["consent_on_file"] = consent
    write_chunk_set(chunks_dir, data)
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.counts[Outcome.NO_CONSENT] == 1
    assert service.requests == []
    assert _payloads(qdrant, DOC_TAX) == []
    skip = [log for log in logs if log["event"] == "document_not_indexed_no_consent"]
    assert skip == [
        {
            "event": "document_not_indexed_no_consent",
            "document_id": DOC_TAX,
            "log_level": "info",
        }
    ]


def test_tax_return_with_consent_is_indexed(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Tax return with consent is indexed."""
    data = chunk_set(DOC_TAX, category="Tax Returns", consent=True)
    write_chunk_set(chunks_dir, data)
    assert indexer.run(chunks_dir).counts[Outcome.INDEXED] == 1
    assert len(_payloads(qdrant, DOC_TAX)) == 2


def test_withdrawn_consent_deletes_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Withdrawn consent deletes points."""
    tax = chunk_set(DOC_TAX, category="Tax Returns", consent=True)
    write_chunk_set(chunks_dir, tax)
    indexer.run(chunks_dir)
    assert len(_payloads(qdrant, DOC_TAX)) == 2
    # The pipeline rewrites the set with consent false; the hash is unchanged.
    write_chunk_set(
        chunks_dir, chunk_set(DOC_TAX, category="Tax Returns", consent=False)
    )
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.NO_CONSENT] == 1
    assert _payloads(qdrant, DOC_TAX) == []


def test_withdrawal_written_as_an_empty_set_deletes_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Withdrawal written as an empty set deletes points."""
    tax = chunk_set(DOC_TAX, category="Tax Returns", consent=True)
    write_chunk_set(chunks_dir, tax)
    indexer.run(chunks_dir)
    empty = chunk_set(DOC_TAX, texts=())
    empty["category"] = "Tax Returns"
    empty["consent_on_file"] = False
    write_chunk_set(chunks_dir, empty)
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.NO_CONSENT] == 1
    assert report.counts[Outcome.EMPTY] == 0
    assert _payloads(qdrant, DOC_TAX) == []


# --------------------------------------------------------------------------- #
# Removed files, failures and the directory guard
# --------------------------------------------------------------------------- #


def test_removed_file_deletes_its_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Removed file deletes its points."""
    path_a = write_chunk_set(chunks_dir, chunk_set(DOC_A))
    write_chunk_set(chunks_dir, chunk_set(DOC_B))
    indexer.run(chunks_dir)
    path_a.unlink()
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.REMOVED] == 1
    assert _payloads(qdrant, DOC_A) == []
    assert len(_payloads(qdrant, DOC_B)) == 2


def test_empty_directory_deletes_nothing(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Empty directory deletes nothing."""
    path = write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    path.unlink()
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.orphan_check_skipped is True
    assert report.counts[Outcome.REMOVED] == 0
    assert len(_payloads(qdrant, DOC_A)) == 2
    assert any(log["event"] == "orphan_check_skipped_empty_directory" for log in logs)


def test_too_many_missing_files_skip_the_orphan_sweep(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """A sweep that would delete most documents at once is skipped."""
    ids = [f"doc-{number}" for number in range(6)]
    paths = [write_chunk_set(chunks_dir, chunk_set(doc)) for doc in ids]
    indexer.run(chunks_dir)
    for path in paths[1:]:
        path.unlink()
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.orphan_check_skipped is True
    assert report.counts[Outcome.REMOVED] == 0
    assert all(len(_payloads(qdrant, doc)) == 2 for doc in ids)
    skipped = [
        log for log in logs if log["event"] == "orphan_check_skipped_too_many_missing"
    ]
    assert skipped[0]["missing"] == 5
    assert skipped[0]["indexed"] == 6
    finished = [log for log in logs if log["event"] == "index_run_finished"]
    assert finished[0]["orphan_check_skipped"] is True


def test_missing_directory_stops_before_any_change(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path, tmp_path: Path
) -> None:
    """Missing directory stops before any change."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    with pytest.raises(IndexerError, match="does not exist"):
        indexer.run(tmp_path / "absent")
    assert len(_payloads(qdrant, DOC_A)) == 2


def test_unreadable_file_keeps_its_points_and_fails(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """Unreadable file keeps its points, fails, and logs no text."""
    data = chunk_set(DOC_A, texts=("secret words",))
    path = write_chunk_set(chunks_dir, data)
    indexer.run(chunks_dir)
    # A shape error right next to the text: the sha256 is not a string.
    data["chunks"][0]["sha256"] = ["secret words"]
    write_chunk_set(chunks_dir, data)
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.failed == 1
    assert report.counts[Outcome.REMOVED] == 0
    assert len(_payloads(qdrant, DOC_A)) == 1
    unreadable = [log for log in logs if log["event"] == "chunk_set_unreadable"]
    assert unreadable == [
        {
            "event": "chunk_set_unreadable",
            "document_id": DOC_A,
            "reason": "sha256 is not a string",
            "log_level": "error",
        }
    ]
    for log in logs:
        assert "exc_info" not in log
        assert all("secret" not in repr(value) for value in log.values())
    assert path.exists()


def test_unreadable_tax_return_loses_its_points(
    indexer: DocumentIndexer, qdrant: QdrantClient, chunks_dir: Path
) -> None:
    """An unreadable file whose points are a tax return loses them."""
    tax = chunk_set(DOC_TAX, category="Tax Returns", consent=True)
    path = write_chunk_set(chunks_dir, tax)
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    assert len(_payloads(qdrant, DOC_TAX)) == 2
    path.write_text('{"document_id": "', encoding="utf-8")
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.failed == 1
    assert _payloads(qdrant, DOC_TAX) == []
    assert len(_payloads(qdrant, DOC_A)) == 2
    assert any(log["event"] == "unreadable_tax_return_removed" for log in logs)


def test_embedding_failure_keeps_old_points_and_logs_no_text(
    indexer: DocumentIndexer,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    chunks_dir: Path,
) -> None:
    """Embedding failure keeps old points and logs no text."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    indexer.run(chunks_dir)
    write_chunk_set(
        chunks_dir, chunk_set(DOC_A, texts=("secret words",), sha256="d" * 64)
    )
    service.fail_with = 503
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.failed == 1
    assert [p["text"] for p in _payloads(qdrant, DOC_A)] == [
        "alpha beta",
        "gamma delta",
    ]
    assert "secret" not in repr(logs)
    failed = [log for log in logs if log["event"] == "document_embedding_failed"]
    assert failed[0]["document_id"] == DOC_A


def test_one_documents_qdrant_failure_does_not_stop_the_run(
    indexer: DocumentIndexer,
    qdrant: QdrantClient,
    chunks_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Qdrant refusal for one document fails it; later files still run."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    write_chunk_set(chunks_dir, chunk_set(DOC_B))
    stale = write_chunk_set(chunks_dir, chunk_set(DOC_TAX, sha256="f" * 64))
    indexer.run(chunks_dir)
    stale.unlink()
    write_chunk_set(chunks_dir, chunk_set(DOC_A, sha256="b" * 64))
    write_chunk_set(chunks_dir, chunk_set(DOC_B, sha256="c" * 64))
    real_replace = FamilyDocsWriter.replace_document

    def refuse_doc_a(
        self: FamilyDocsWriter, document_id: str, points: list[Any]
    ) -> int:
        """Refuse DOC_A like a server error; write the rest."""
        if document_id == DOC_A:
            raise UnexpectedResponse(400, "Bad Request", b"", httpx.Headers())
        return real_replace(self, document_id, points)

    monkeypatch.setattr(FamilyDocsWriter, "replace_document", refuse_doc_a)
    with capture_logs() as logs:
        report = indexer.run(chunks_dir)
    assert report.failed == 1
    assert report.counts[Outcome.INDEXED] == 1
    assert report.counts[Outcome.REMOVED] == 1  # the sweep still ran
    assert {p["sha256"] for p in _payloads(qdrant, DOC_A)} == {"a" * 64}
    assert {p["sha256"] for p in _payloads(qdrant, DOC_B)} == {"c" * 64}
    failed = [log for log in logs if log["event"] == "document_write_failed"]
    assert failed == [
        {
            "event": "document_write_failed",
            "document_id": DOC_A,
            "reason": "Qdrant request failed: UnexpectedResponse",
            "log_level": "error",
        }
    ]


def test_failed_batch_keeps_points_and_next_run_repairs(
    indexer: DocumentIndexer,
    qdrant: QdrantClient,
    chunks_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that fails after its first batch never leaves no points."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A, texts=("one", "two", "three")))
    indexer.run(chunks_dir)
    monkeypatch.setattr(qdrant_store, "UPSERT_BATCH_SIZE", 1)
    real_upsert = qdrant.upsert
    calls: list[int] = []

    def fail_second_batch(**kwargs: object) -> object:
        """Write the first batch, then fail like a timed-out request."""
        calls.append(1)
        if len(calls) == 2:
            raise UnexpectedResponse(500, "error", b"", httpx.Headers())
        return real_upsert(**kwargs)

    monkeypatch.setattr(qdrant, "upsert", fail_second_batch)
    write_chunk_set(
        chunks_dir, chunk_set(DOC_A, texts=("uno", "dos", "tres"), sha256="b" * 64)
    )
    assert indexer.run(chunks_dir).failed == 1
    texts = [p["text"] for p in _payloads(qdrant, DOC_A)]
    assert texts == ["uno", "two", "three"]  # mixed, never empty
    report = indexer.run(chunks_dir)
    assert report.counts[Outcome.INDEXED] == 1
    assert [p["text"] for p in _payloads(qdrant, DOC_A)] == ["uno", "dos", "tres"]


def test_refused_embedding_key_stops_the_run(
    indexer: DocumentIndexer,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    chunks_dir: Path,
) -> None:
    """A refused embedding key stops the run instead of failing every file."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    write_chunk_set(chunks_dir, chunk_set(DOC_B))
    service.key = "rotated"
    with pytest.raises(IndexerError, match="refused the key"):
        indexer.run(chunks_dir)
    assert len(service.requests) == 1
    assert _payloads(qdrant, DOC_A) == []


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #

RETRIEVAL_VARS = (
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBEDDING_MODEL",
    "EMBED_TIMEOUT_SECONDS",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "CHUNKS_DIR",
)


@pytest.fixture
def command_env(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    chunks_dir: Path,
) -> pytest.MonkeyPatch:
    """Configure every setting and route the command to the fakes."""
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBED_BASE_URL", "http://embed.test")
    monkeypatch.setenv("EMBED_API_KEY", service.key)
    monkeypatch.setenv("EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")
    monkeypatch.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))
    monkeypatch.setenv("CHUNKS_DIR", str(chunks_dir))
    monkeypatch.setattr(indexer_module, "make_client", lambda _connection: qdrant)
    real_client = indexer_module.EmbeddingClient

    def fake_embedding_client(connection: EmbeddingConnection) -> EmbeddingClient:
        """Build the real client on the fake transport."""
        return real_client(connection, transport=service.transport(), sleep=_no_wait)

    monkeypatch.setattr(indexer_module, "EmbeddingClient", fake_embedding_client)
    return monkeypatch


def test_main_runs_a_pass_and_exits_zero(
    command_env: pytest.MonkeyPatch, chunks_dir: Path, qdrant: QdrantClient
) -> None:
    """Main runs a pass and exits zero."""
    closed: list[bool] = []
    command_env.setattr(qdrant, "close", lambda: closed.append(True))
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    assert main() == 0
    assert closed == [True]
    assert len(_payloads(qdrant, DOC_A)) == 2


def test_main_exits_one_when_a_file_fails(
    command_env: pytest.MonkeyPatch, chunks_dir: Path
) -> None:
    """Main exits one when a file fails."""
    del command_env
    (chunks_dir / f"{DOC_A}.json").write_text("not json", encoding="utf-8")
    assert main() == 1


def test_main_is_off_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Main is off when not configured."""
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "indexer_not_connected"
    assert logs[0]["log_level"] == "warning"


@pytest.mark.parametrize("unset", ["EMBED_BASE_URL", "QDRANT_URL", "CHUNKS_DIR"])
def test_main_is_off_when_one_setting_is_missing(
    command_env: pytest.MonkeyPatch, unset: str
) -> None:
    """Main exits 2 when any one of the three locations is unset."""
    command_env.delenv(unset)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "indexer_not_connected"


@pytest.mark.parametrize("value", ["abc", "", "nan", "inf", "0", "-5"])
def test_main_refuses_a_bad_timeout_without_a_traceback(
    command_env: pytest.MonkeyPatch, value: str
) -> None:
    """A bad timeout exits 2 and names the variable, never the value."""
    command_env.setenv("EMBED_TIMEOUT_SECONDS", value)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "indexer_misconfigured"
    assert logs[0]["reason"] == "invalid value for EMBED_TIMEOUT_SECONDS"


def test_main_refuses_a_url_without_its_key(
    command_env: pytest.MonkeyPatch,
) -> None:
    """Main refuses a url without its key."""
    command_env.delenv("QDRANT_API_KEY")
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "indexer_misconfigured"
    assert "QDRANT_API_KEY" in logs[0]["reason"]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("QDRANT_URL", "ftp://qdrant.test"),
        ("QDRANT_URL", "qdrant.test:6333"),
        ("QDRANT_URL", f"http://user:{USERINFO_SECRET}@qdrant.test:99999"),
        ("EMBED_BASE_URL", "embed.test"),
    ],
)
def test_main_refuses_a_malformed_url_with_the_real_client_factory(
    command_env: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """A malformed URL exits 2 naming the variable, never raising or echoing it."""
    command_env.setattr(indexer_module, "make_client", qdrant_store.make_client)
    command_env.setenv(variable, value)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "indexer_misconfigured"
    assert variable in logs[0]["reason"]
    assert USERINFO_SECRET not in repr(logs)
    assert value not in repr(logs)


def test_main_stops_when_the_directory_is_missing(
    command_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Main stops when the directory is missing."""
    command_env.setenv("CHUNKS_DIR", str(tmp_path / "absent"))
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["event"] == "index_run_stopped"


def test_main_stops_when_qdrant_fails(
    command_env: pytest.MonkeyPatch, chunks_dir: Path
) -> None:
    """Main stops when qdrant fails."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))

    def broken(_self: FamilyDocsWriter) -> bool:
        """Fail like a Qdrant server error."""
        raise UnexpectedResponse(500, "error", b"", httpx.Headers())

    command_env.setattr(FamilyDocsWriter, "ensure_collection", broken)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["reason"] == "Qdrant request failed: UnexpectedResponse"


@pytest.mark.parametrize(
    "error",
    [
        ResponseHandlingException(httpx.ConnectError("refused")),
        ResourceExhaustedResponse("rate limited", 5),
    ],
    ids=["transport", "rate-limited"],
)
def test_main_stops_when_qdrant_stops_answering_mid_run(
    command_env: pytest.MonkeyPatch, chunks_dir: Path, error: Exception
) -> None:
    """A transport failure or a 429 mid-run stops the run with exit 2."""
    write_chunk_set(chunks_dir, chunk_set(DOC_A))

    def broken(_self: FamilyDocsWriter, _document_id: str) -> None:
        """Fail like Qdrant going away or rate-limiting."""
        raise error

    command_env.setattr(FamilyDocsWriter, "stored_document", broken)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["event"] == "index_run_stopped"
    assert logs[-1]["reason"] == f"Qdrant request failed: {type(error).__name__}"


def test_main_stops_on_a_collection_with_the_wrong_schema(
    command_env: pytest.MonkeyPatch, chunks_dir: Path, qdrant: QdrantClient
) -> None:
    """An existing collection with the wrong vector size stops the run."""
    del command_env
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    qdrant.create_collection(
        FAMILY_DOCS_COLLECTION,
        vectors_config={
            "dense": models.VectorParams(size=8, distance=models.Distance.COSINE)
        },
    )
    with capture_logs() as logs:
        assert main() == 2
    assert "unexpected vector schema" in logs[-1]["reason"]


def test_main_stops_when_the_embedding_key_is_refused(
    command_env: pytest.MonkeyPatch, chunks_dir: Path
) -> None:
    """A refused embedding key exits 2 with the status, not the key."""
    command_env.setenv("EMBED_API_KEY", "not-the-service-key")
    write_chunk_set(chunks_dir, chunk_set(DOC_A))
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["event"] == "index_run_stopped"
    assert "HTTP 401" in logs[-1]["reason"]
    assert "not-the-service-key" not in repr(logs)
