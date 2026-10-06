# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Qdrant collections and the family-docs point writer.

Every collection is created with two named vectors from the start: ``dense``
(1024 dimensions, cosine) and ``sparse``. Only ``dense`` is filled; the
sparse slot is there so a later hybrid search can fill it without a rebuild.

#ASSUME: schema: Qdrant cannot add a new named sparse vector to an existing
collection (``update_collection`` changes the parameters of existing vectors
only). #VERIFY: check the Qdrant collection docs for the deployed server
version before relying on adding the slot later.

``FamilyDocsWriter`` keeps one set of points per document. Point IDs are
derived from the document ID and chunk position, so it writes the new points
first, in batches, and then deletes only the old points the new set does not
overwrite. A failure part way through never leaves a document with no
points; it leaves a mix that the next run sees as incomplete and rewrites.

#CRITICAL: schema: every collection must be created through
``ensure_collection`` so the sparse slot always exists. #VERIFY:
tests/unit/test_qdrant_store.py::test_collection_has_dense_and_sparse_slots.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.retrieval.embeddings import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.retrieval.settings import QdrantConnection

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
FAMILY_DOCS_COLLECTION = "family-docs"
QDRANT_TIMEOUT_SECONDS = 30
UPSERT_BATCH_SIZE = 64
_SCROLL_PAGE = 256
_HTTP_CONFLICT = 409

# Payload keys the writer and the indexer share.
DOCUMENT_ID_FIELD = "document_id"
CHUNK_COUNT_FIELD = "chunk_count"
INDEX_DIGEST_FIELD = "index_digest"
IS_TAX_RETURN_FIELD = "is_tax_return"
_STATE_FIELDS = [
    "sha256",
    "consent_on_file",
    "embedding_model",
    CHUNK_COUNT_FIELD,
    INDEX_DIGEST_FIELD,
    IS_TAX_RETURN_FIELD,
]

# Payload fields the search filter uses; indexed so filters stay fast.
FAMILY_DOCS_INDEXES: Mapping[str, models.PayloadSchemaType] = MappingProxyType(
    {
        "is_confidential": models.PayloadSchemaType.BOOL,
        "entity_id": models.PayloadSchemaType.KEYWORD,
        DOCUMENT_ID_FIELD: models.PayloadSchemaType.KEYWORD,
    }
)

# Fixed namespace: the same document and chunk position give the same point ID.
_POINT_NAMESPACE = uuid.UUID("3b8f2c1e-7d4a-4e6b-9f05-2a1c8d7e6b40")


class VectorStoreError(RuntimeError):
    """The collection has the wrong shape, or a write was refused locally.

    A refused write never reaches Qdrant.
    """


def make_client(connection: QdrantConnection) -> QdrantClient:
    """Create a Qdrant client for a configured connection.

    Args:
        connection (QdrantConnection): URL and API key.

    Returns:
        QdrantClient: A client for the server.
    """
    return QdrantClient(
        url=connection.url,
        api_key=connection.api_key.get_secret_value(),
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


def document_filter(document_id: str, **values: str) -> models.Filter:
    """Return a filter that matches every point of one document.

    Args:
        document_id (str): The document.
        **values (str): Further payload fields the points must equal.

    Returns:
        models.Filter: The filter.
    """
    conditions: list[models.Condition] = [
        models.FieldCondition(key=name, match=models.MatchValue(value=value))
        for name, value in {DOCUMENT_ID_FIELD: document_id, **values}.items()
    ]
    return models.Filter(must=conditions)


def _check_schema(client: QdrantClient, collection: str) -> models.CollectionInfo:
    """Check that an existing collection has the expected vector slots.

    Args:
        client (QdrantClient): The Qdrant client.
        collection (str): Collection name.

    Returns:
        models.CollectionInfo: The collection's description.

    Raises:
        VectorStoreError: If ``dense`` is missing, is not
            ``EMBEDDING_DIMENSIONS`` long or not cosine, or ``sparse`` is
            missing.
    """
    info = client.get_collection(collection)
    params = info.config.params
    vectors = params.vectors if isinstance(params.vectors, dict) else {}
    dense = vectors.get(DENSE_VECTOR)
    if (
        dense is None
        or dense.size != EMBEDDING_DIMENSIONS
        or dense.distance != models.Distance.COSINE
        or SPARSE_VECTOR not in (params.sparse_vectors or {})
    ):
        msg = f"collection {collection} has an unexpected vector schema"
        raise VectorStoreError(msg)
    return info


def ensure_collection(
    client: QdrantClient,
    collection: str,
    indexed_fields: Mapping[str, models.PayloadSchemaType] | None = None,
) -> bool:
    """Create a collection with ``dense`` and ``sparse`` vectors if absent.

    An existing collection has its vector slots checked, and any missing
    payload index is created, so a run repairs an index lost or never made.
    A collection created by another process at the same moment (HTTP 409)
    counts as existing. An existing collection with the wrong vector slots
    makes ``_check_schema`` raise ``VectorStoreError``.

    Args:
        client (QdrantClient): The Qdrant client.
        collection (str): Collection name.
        indexed_fields (Mapping[str, models.PayloadSchemaType] | None):
            Payload fields to index.

    Returns:
        bool: True when the collection was created by this call.

    Raises:
        UnexpectedResponse: If Qdrant refuses a request for a reason other
            than the collection already existing.
    """
    created = False
    if not client.collection_exists(collection):
        try:
            client.create_collection(
                collection_name=collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=EMBEDDING_DIMENSIONS, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
            )
            created = True
        except UnexpectedResponse as exc:
            if exc.status_code != _HTTP_CONFLICT:
                raise
    info = _check_schema(client, collection)
    present = info.payload_schema or {}
    for name, schema in (indexed_fields or {}).items():
        if name not in present:
            client.create_payload_index(
                collection, field_name=name, field_schema=schema, wait=True
            )
    return created


@dataclass(frozen=True)
class StoredDocument:
    """What the points of one indexed document say about it.

    Attributes:
        sha256 (str | None): Document hash stored on the points.
        consent_on_file (bool | None): Consent stored on the points, or None
            when it is not a boolean.
        embedding_model (str | None): Model that made the vectors.
        chunk_count (int | None): Points written for the document.
        index_digest (str | None): Digest of the content and model the
            points were written from.
        is_tax_return (bool): True unless the points say plainly that the
            document is not a tax return.
        point_count (int): Points found for the document.
        digest_count (int): Points found that carry ``index_digest``.
    """

    sha256: str | None
    consent_on_file: bool | None
    embedding_model: str | None
    chunk_count: int | None
    index_digest: str | None
    is_tax_return: bool
    point_count: int
    digest_count: int

    @property
    def complete(self) -> bool:
        """Say whether every point of one complete write is present.

        Returns:
            bool: True when the recorded count matches the points found and
            every point carries the same digest.
        """
        return (
            self.chunk_count is not None
            and self.index_digest is not None
            and self.chunk_count == self.point_count == self.digest_count
        )


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


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _count_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
        """Create the collection if absent, check it, and add missing indexes.

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

    def _count(self, query: models.Filter) -> int:
        return self._client.count(
            collection_name=self.collection, count_filter=query, exact=True
        ).count

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
        consent = payload.get("consent_on_file")
        digest = _text_or_none(payload.get(INDEX_DIGEST_FIELD))
        return StoredDocument(
            sha256=_text_or_none(payload.get("sha256")),
            consent_on_file=consent if isinstance(consent, bool) else None,
            embedding_model=_text_or_none(payload.get("embedding_model")),
            chunk_count=_count_or_none(payload.get(CHUNK_COUNT_FIELD)),
            index_digest=digest,
            is_tax_return=payload.get(IS_TAX_RETURN_FIELD) is not False,
            point_count=self._count(document_filter(document_id)),
            digest_count=(
                0
                if digest is None
                else self._count(
                    document_filter(document_id, **{INDEX_DIGEST_FIELD: digest})
                )
            ),
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
                with_payload=[DOCUMENT_ID_FIELD],
                with_vectors=False,
            )
            for record in records:
                value = (record.payload or {}).get(DOCUMENT_ID_FIELD)
                if isinstance(value, str):
                    found.add(value)
            if next_offset is None:
                return found
            offset = cast("models.ExtendedPointId", next_offset)

    def replace_document(self, document_id: str, points: Sequence[DensePoint]) -> int:
        """Write a document's new points, then delete the ones left over.

        Only the dense vector is filled. Every point records ``chunk_count``
        so a later run can tell a complete write from an interrupted one.
        Points go up in batches of ``UPSERT_BATCH_SIZE``; the old points
        are deleted only after every batch is written, and only those whose
        IDs the new set does not reuse. An empty set deletes every point.

        #EDGE: the upserts and the delete are separate calls; a failure
        between them leaves new and old points mixed, never none. #VERIFY:
        the next run counts more points than ``chunk_count``, or points with
        another digest, and writes the document again.

        Args:
            document_id (str): The document.
            points (Sequence[DensePoint]): Points to write.

        Returns:
            int: Number of points written.

        Raises:
            VectorStoreError: If a vector has the wrong length, a payload
                names another document, or two points share a position.
                Nothing is written or deleted in that case.
        """
        for point in points:
            if len(point.vector) != EMBEDDING_DIMENSIONS:
                msg = f"every dense vector must have {EMBEDDING_DIMENSIONS} dimensions"
                raise VectorStoreError(msg)
            if point.payload.get(DOCUMENT_ID_FIELD, document_id) != document_id:
                msg = "a point payload names another document"
                raise VectorStoreError(msg)
        ids = [point_id(document_id, point.chunk_index) for point in points]
        if len(set(ids)) != len(ids):
            msg = "two points share a chunk position"
            raise VectorStoreError(msg)
        if not points:
            self.delete_document(document_id)
            return 0
        structs = [
            models.PointStruct(
                id=identifier,
                vector={DENSE_VECTOR: list(point.vector)},
                payload={
                    **point.payload,
                    DOCUMENT_ID_FIELD: document_id,
                    CHUNK_COUNT_FIELD: len(points),
                },
            )
            for identifier, point in zip(ids, points, strict=True)
        ]
        for start in range(0, len(structs), UPSERT_BATCH_SIZE):
            self._client.upsert(
                collection_name=self.collection,
                points=structs[start : start + UPSERT_BATCH_SIZE],
                wait=True,
            )
        stale = models.Filter(
            must=document_filter(document_id).must,
            must_not=[models.HasIdCondition(has_id=list(ids))],
        )
        self._client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(filter=stale),
            wait=True,
        )
        return len(structs)
