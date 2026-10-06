# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Qdrant collections and the family-docs point writer.

Every collection is created with two named vectors from the start: ``dense``
(1024 dimensions, cosine) and ``sparse``. Qdrant cannot add a sparse vector to
an existing collection, so the sparse slot exists now even though only
``dense`` is filled. A later hybrid search can fill it without a rebuild.

``FamilyDocsWriter`` keeps one set of points per document: it deletes a
document's points before writing new ones, so re-indexing never doubles them.

#CRITICAL: schema: every collection must be created through
``ensure_collection`` so the sparse slot always exists. #VERIFY:
tests/unit/test_qdrant_store.py::test_collection_has_dense_and_sparse_slots.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from qdrant_client import QdrantClient, models

from app.retrieval.embeddings import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.retrieval.settings import QdrantConnection

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
FAMILY_DOCS_COLLECTION = "family-docs"
QDRANT_TIMEOUT_SECONDS = 30
_SCROLL_PAGE = 256
_STATE_FIELDS = ["sha256", "consent_on_file", "embedding_model", "chunk_count"]

# Payload fields the search filter uses; indexed so filters stay fast.
FAMILY_DOCS_INDEXES: Mapping[str, models.PayloadSchemaType] = {
    "is_confidential": models.PayloadSchemaType.BOOL,
    "entity_id": models.PayloadSchemaType.KEYWORD,
    "document_id": models.PayloadSchemaType.KEYWORD,
}

# Fixed namespace: the same document and chunk position give the same point ID.
_POINT_NAMESPACE = uuid.UUID("3b8f2c1e-7d4a-4e6b-9f05-2a1c8d7e6b40")


class VectorStoreError(RuntimeError):
    """A write to the vector store was refused before it reached Qdrant."""


def make_client(connection: QdrantConnection) -> QdrantClient:
    """Create a Qdrant client for a configured connection.

    Args:
        connection (QdrantConnection): URL and API key.

    Returns:
        QdrantClient: A client for the server.
    """
    return QdrantClient(
        url=connection.url,
        api_key=connection.api_key,
        timeout=QDRANT_TIMEOUT_SECONDS,
    )


def point_id(document_id: str, chunk_index: int) -> str:
    """Return the deterministic point ID for one chunk of a document.

    Args:
        document_id (str): Document the chunk belongs to.
        chunk_index (int): Position of the chunk in its set.

    Returns:
        str: A UUID string.
    """
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{document_id}:{chunk_index}"))


def document_filter(document_id: str) -> models.Filter:
    """Return a filter that matches every point of one document.

    Args:
        document_id (str): The document.

    Returns:
        models.Filter: The filter.
    """
    return models.Filter(
        must=[
            models.FieldCondition(
                key="document_id", match=models.MatchValue(value=document_id)
            )
        ]
    )


def ensure_collection(
    client: QdrantClient,
    collection: str,
    indexed_fields: Mapping[str, models.PayloadSchemaType] | None = None,
) -> bool:
    """Create a collection with ``dense`` and ``sparse`` vectors if absent.

    Payload indexes are created with a new collection. An existing collection
    is left as it is.

    Args:
        client (QdrantClient): The Qdrant client.
        collection (str): Collection name.
        indexed_fields (Mapping[str, models.PayloadSchemaType] | None):
            Payload fields to index.

    Returns:
        bool: True when the collection was created by this call.
    """
    if client.collection_exists(collection):
        return False
    client.create_collection(
        collection_name=collection,
        vectors_config={
            DENSE_VECTOR: models.VectorParams(
                size=EMBEDDING_DIMENSIONS, distance=models.Distance.COSINE
            )
        },
        sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
    )
    for name, schema in (indexed_fields or {}).items():
        client.create_payload_index(collection, field_name=name, field_schema=schema)
    return True


@dataclass(frozen=True)
class StoredDocument:
    """What the points of one indexed document say about it.

    Attributes:
        sha256 (str | None): Document hash stored on the points.
        consent_on_file (object): Consent value stored on the points.
        embedding_model (str | None): Model that made the vectors.
        chunk_count (int | None): Points written for the document.
        point_count (int): Points found for the document.
    """

    sha256: str | None
    consent_on_file: object
    embedding_model: str | None
    chunk_count: int | None
    point_count: int

    @property
    def complete(self) -> bool:
        """Say whether every point written for the document is present.

        Returns:
            bool: True when the point count matches the recorded count.
        """
        return self.chunk_count is not None and self.chunk_count == self.point_count


@dataclass(frozen=True)
class DensePoint:
    """One chunk ready to write: its position, vector and payload.

    Attributes:
        chunk_index (int): Position of the chunk in its set.
        vector (Sequence[float]): Dense vector.
        payload (Mapping[str, object]): Payload to store.
    """

    chunk_index: int
    vector: Sequence[float]
    payload: Mapping[str, object]


class FamilyDocsWriter:
    """Writes and removes document points in one collection.

    Args:
        client (QdrantClient): The Qdrant client.
        collection (str): Collection name.
    """

    def __init__(
        self, client: QdrantClient, collection: str = FAMILY_DOCS_COLLECTION
    ) -> None:
        self._client = client
        self.collection = collection

    def ensure_collection(self) -> bool:
        """Create the collection and its payload indexes if absent.

        Returns:
            bool: True when the collection was created by this call.
        """
        return ensure_collection(self._client, self.collection, FAMILY_DOCS_INDEXES)

    def delete_document(self, document_id: str) -> None:
        """Delete every point of one document.

        Args:
            document_id (str): The document.
        """
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(filter=document_filter(document_id)),
            wait=True,
        )

    def stored_document(self, document_id: str) -> StoredDocument | None:
        """Read what the stored points say about a document.

        Args:
            document_id (str): The document.

        Returns:
            StoredDocument | None: The stored values, or None when the
            document has no points.
        """
        records, _ = self._client.scroll(
            collection_name=self.collection,
            scroll_filter=document_filter(document_id),
            limit=1,
            with_payload=_STATE_FIELDS,
            with_vectors=False,
        )
        if not records:
            return None
        payload = records[0].payload or {}
        count = self._client.count(
            collection_name=self.collection,
            count_filter=document_filter(document_id),
            exact=True,
        ).count
        sha256 = payload.get("sha256")
        model = payload.get("embedding_model")
        chunk_count = payload.get("chunk_count")
        return StoredDocument(
            sha256=sha256 if isinstance(sha256, str) else None,
            consent_on_file=payload.get("consent_on_file"),
            embedding_model=model if isinstance(model, str) else None,
            chunk_count=(
                chunk_count
                if isinstance(chunk_count, int) and not isinstance(chunk_count, bool)
                else None
            ),
            point_count=count,
        )

    def document_ids(self) -> set[str]:
        """Return the ID of every document that has points.

        Returns:
            set[str]: Document IDs.
        """
        found: set[str] = set()
        offset: models.ExtendedPointId | None = None
        while True:
            records, next_offset = self._client.scroll(
                collection_name=self.collection,
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=["document_id"],
                with_vectors=False,
            )
            for record in records:
                value = (record.payload or {}).get("document_id")
                if isinstance(value, str):
                    found.add(value)
            if next_offset is None:
                return found
            offset = cast("models.ExtendedPointId", next_offset)

    def replace_document(self, document_id: str, points: Sequence[DensePoint]) -> int:
        """Delete a document's points, then write the new ones.

            Only the dense vector is filled. Every point records ``chunk_count``
            so a later run can tell a complete write from an interrupted one.

            #EDGE: delete and upsert are two calls; a crash between them leaves
            the document with no points, never with two sets. #VERIFY: the next
            run finds no stored points and indexes the document again.

        Args:
            document_id (str): The document.
            points (Sequence[DensePoint]): Points to write.

        Returns:
            int: Number of points written.

        Raises:
            VectorStoreError: If a vector has the wrong length or a payload
                names another document. Nothing is deleted in that case.
        """
        for point in points:
            if len(point.vector) != EMBEDDING_DIMENSIONS:
                msg = f"every dense vector must have {EMBEDDING_DIMENSIONS} dimensions"
                raise VectorStoreError(msg)
            if point.payload.get("document_id", document_id) != document_id:
                msg = "a point payload names another document"
                raise VectorStoreError(msg)
        self.delete_document(document_id)
        if not points:
            return 0
        structs = [
            models.PointStruct(
                id=point_id(document_id, point.chunk_index),
                vector={DENSE_VECTOR: list(point.vector)},
                payload={
                    **point.payload,
                    "document_id": document_id,
                    "chunk_count": len(points),
                },
            )
            for point in points
        ]
        self._client.upsert(collection_name=self.collection, points=structs, wait=True)
        return len(structs)
