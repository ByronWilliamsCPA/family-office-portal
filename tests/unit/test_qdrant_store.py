# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the Qdrant collection setup and the family-docs writer.

Ported from the reference implementation's writer tests. They run against
qdrant-client's in-process mode (``QdrantClient(":memory:")``), which keeps
no payload indexes, so index creation is checked with a recording client.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pytest
from qdrant_client import QdrantClient, models

from app.retrieval.qdrant_store import (
    FAMILY_DOCS_COLLECTION,
    DensePoint,
    FamilyDocsWriter,
    VectorStoreError,
    ensure_collection,
    make_client,
    point_id,
)
from app.retrieval.settings import QdrantConnection

if TYPE_CHECKING:
    from collections.abc import Iterator


class RecordingClient(QdrantClient):
    """In-memory client that records payload index requests."""

    def __init__(self) -> None:
        super().__init__(":memory:")
        self.indexes: dict[str, object] = {}

    def create_payload_index(
        self,
        collection_name: str,
        field_name: str,
        field_schema: object = None,
        **_: object,
    ) -> bool:
        """Record the index instead of creating it."""
        del collection_name
        self.indexes[field_name] = field_schema
        return True


@pytest.fixture
def client() -> Iterator[RecordingClient]:
    """Yield a recording in-memory Qdrant client."""
    qdrant = RecordingClient()
    yield qdrant
    qdrant.close()


@pytest.fixture
def writer(client: RecordingClient) -> FamilyDocsWriter:
    """Return a family-docs writer with its collection created."""
    store = FamilyDocsWriter(client)
    assert store.ensure_collection() is True
    return store


def _vector(seed: float = 1.0) -> list[float]:
    """Return a 1024-dimension test vector."""
    return [seed] + [0.0] * 1023


def _point(document_id: str, index: int, **extra: object) -> DensePoint:
    """Return a dense point for one chunk of a document."""
    payload: dict[str, object] = {
        "document_id": document_id,
        "chunk_index": index,
        "is_confidential": False,
        "entity_id": "entity-1",
        "text": f"text {document_id} {index}",
        **extra,
    }
    return DensePoint(chunk_index=index, vector=_vector(), payload=payload)


def _count(client: QdrantClient, document_id: str) -> int:
    """Count the points of one document."""
    return client.count(
        FAMILY_DOCS_COLLECTION,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id", match=models.MatchValue(value=document_id)
                )
            ]
        ),
    ).count


def test_collection_has_dense_and_sparse_slots(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Collection has dense and sparse slots."""
    params = client.get_collection(writer.collection).config.params
    vectors = params.vectors
    assert isinstance(vectors, dict)
    assert vectors["dense"].size == 1024
    assert vectors["dense"].distance == models.Distance.COSINE
    assert params.sparse_vectors is not None
    assert "sparse" in params.sparse_vectors
    assert writer.collection == "family-docs"


def test_payload_indexes_cover_the_search_filters(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Payload indexes cover the search filters."""
    del writer
    assert client.indexes == {
        "is_confidential": models.PayloadSchemaType.BOOL,
        "entity_id": models.PayloadSchemaType.KEYWORD,
        "document_id": models.PayloadSchemaType.KEYWORD,
    }


def test_ensure_collection_is_idempotent(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Ensure collection is idempotent."""
    writer.replace_document("d1", [_point("d1", 0)])
    assert writer.ensure_collection() is False
    assert _count(client, "d1") == 1


def test_generic_collection_without_indexes(client: RecordingClient) -> None:
    """Generic collection without indexes."""
    assert ensure_collection(client, "tax-law") is True
    assert client.collection_exists("tax-law")
    assert client.indexes == {}


def test_only_dense_is_filled(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Only dense is filled."""
    writer.replace_document("d1", [_point("d1", 0)])
    points, _ = client.scroll(writer.collection, with_vectors=True, with_payload=True)
    (point,) = points
    assert isinstance(point.vector, dict)
    assert set(point.vector) == {"dense"}
    assert point.payload is not None
    assert point.payload["text"] == "text d1 0"
    assert point.payload["chunk_count"] == 1
    assert point.id == point_id("d1", 0)


def test_reindexing_leaves_one_set_of_points(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Reindexing leaves one set of points."""
    points = [_point("d1", i) for i in range(3)]
    writer.replace_document("d1", points)
    writer.replace_document("d1", points)
    assert _count(client, "d1") == 3


def test_reindexing_removes_stale_chunks(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Reindexing removes stale chunks."""
    writer.replace_document("d1", [_point("d1", i) for i in range(4)])
    writer.replace_document("d1", [_point("d1", 0)])
    assert _count(client, "d1") == 1


def test_other_documents_are_untouched(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Other documents are untouched."""
    writer.replace_document("d1", [_point("d1", 0)])
    writer.replace_document("d2", [_point("d2", 0)])
    writer.replace_document("d1", [_point("d1", 0)])
    writer.delete_document("d1")
    assert _count(client, "d1") == 0
    assert _count(client, "d2") == 1


def test_empty_replace_deletes_the_document(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Empty replace deletes the document."""
    writer.replace_document("d1", [_point("d1", 0)])
    assert writer.replace_document("d1", []) == 0
    assert _count(client, "d1") == 0


def test_rejects_wrong_dimensions_and_keeps_old_points(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Rejects wrong dimensions and keeps old points."""
    writer.replace_document("d1", [_point("d1", 0)])
    bad = DensePoint(chunk_index=0, vector=[0.1, 0.2], payload={})
    with pytest.raises(VectorStoreError, match="1024"):
        writer.replace_document("d1", [bad])
    assert _count(client, "d1") == 1


def test_rejects_a_payload_for_another_document(writer: FamilyDocsWriter) -> None:
    """Rejects a payload for another document."""
    with pytest.raises(VectorStoreError, match="another document"):
        writer.replace_document("d1", [_point("d2", 0)])


def test_stored_document_reads_the_state_fields(writer: FamilyDocsWriter) -> None:
    """Stored document reads the state fields."""
    assert writer.stored_document("d1") is None
    writer.replace_document(
        "d1",
        [
            _point("d1", i, sha256="abc", consent_on_file=True, embedding_model="m")
            for i in range(2)
        ],
    )
    stored = writer.stored_document("d1")
    assert stored is not None
    assert stored.sha256 == "abc"
    assert stored.consent_on_file is True
    assert stored.embedding_model == "m"
    assert stored.chunk_count == 2
    assert stored.point_count == 2
    assert stored.complete is True


def test_stored_document_with_odd_payload_is_incomplete(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Stored document with odd payload is incomplete."""
    client.upsert(
        FAMILY_DOCS_COLLECTION,
        points=[
            models.PointStruct(
                id=point_id("d1", 0),
                vector={"dense": _vector()},
                payload={"document_id": "d1", "sha256": 5, "chunk_count": True},
            )
        ],
    )
    stored = writer.stored_document("d1")
    assert stored is not None
    assert stored.sha256 is None
    assert stored.embedding_model is None
    assert stored.chunk_count is None
    assert stored.complete is False


def test_document_ids_pages_through_every_point(
    writer: FamilyDocsWriter, client: RecordingClient
) -> None:
    """Document ids pages through every point."""
    for number in range(30):
        document_id = f"doc-{number}"
        writer.replace_document(
            document_id, [_point(document_id, i) for i in range(10)]
        )
    client.upsert(
        FAMILY_DOCS_COLLECTION,
        points=[models.PointStruct(id=point_id("x", 0), vector={"dense": _vector()})],
    )
    assert writer.document_ids() == {f"doc-{n}" for n in range(30)}


def test_make_client_uses_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make client uses the connection."""
    seen: dict[str, object] = {}

    def fake_client(**kwargs: object) -> str:
        """Record the client arguments."""
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr("app.retrieval.qdrant_store.QdrantClient", fake_client)
    key = secrets.token_urlsafe(16)
    connection = QdrantConnection(url="http://qdrant.test:6333", api_key=key)
    assert make_client(connection) == "client"
    assert seen == {"url": "http://qdrant.test:6333", "api_key": key, "timeout": 30}
