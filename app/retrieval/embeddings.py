# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Client for an OpenAI-compatible embeddings service (``POST /v1/embeddings``).

Documents are embedded as they are. Queries get a fixed instruction prefix,
which the embedding model expects on the query side only; the query text
follows ``Query:`` directly, with no space. All embedding is remote: no model
is loaded in the portal.

The request and response handling is split into ``build_payload`` and
``parse_response`` so an async client for search can reuse the same checks.

#CRITICAL: privacy: errors carry status codes and counts only, never input
text or the response body (which may echo input). #VERIFY:
tests/unit/test_embeddings.py checks error messages for leaked text.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    from app.retrieval.settings import EmbeddingConnection

EMBEDDINGS_PATH = "/v1/embeddings"
EMBEDDING_DIMENSIONS = 1024
DEFAULT_BATCH_SIZE = 32
_HTTP_OK = 200

# The query text is appended directly after "Query:". The newline is real.
QUERY_PREFIX = (
    "Instruct: Given a question about a family's financial, estate and tax "
    "documents, retrieve passages that answer it\nQuery:"
)


class EmbeddingError(RuntimeError):
    """The embedding service failed or returned data that cannot be used."""


def query_text(query: str) -> str:
    """Return a search query with the instruction prefix applied.

    Args:
        query (str): The user's question.

    Returns:
        str: The prefixed text to embed.
    """
    return f"{QUERY_PREFIX}{query}"


def build_payload(model: str, texts: Sequence[str]) -> dict[str, object]:
    """Build the JSON body for one embeddings request.

    A single text is sent as a string and several as a list; the service
    accepts both.

    Args:
        model (str): Model name from configuration.
        texts (Sequence[str]): Texts to embed, at least one.

    Returns:
        dict[str, object]: The request body.
    """
    batch = list(texts)
    return {"model": model, "input": batch[0] if len(batch) == 1 else batch}


def _vector_from_item(item: object) -> tuple[int, list[float]]:
    """Read the index and vector from one ``data`` entry.

    Args:
        item (object): One element of the response ``data`` list.

    Returns:
        tuple[int, list[float]]: Position and vector.

    Raises:
        EmbeddingError: If the entry is not a well-formed embedding.
    """
    if not isinstance(item, dict):
        msg = "Embedding service returned a malformed body"
        raise EmbeddingError(msg)
    entry = cast("dict[str, object]", item)
    index = entry.get("index", 0)
    raw = entry.get("embedding")
    if isinstance(index, bool) or not isinstance(index, int):
        msg = "Embedding service returned a malformed body"
        raise EmbeddingError(msg)
    if not isinstance(raw, list):
        msg = "Embedding service returned a malformed body"
        raise EmbeddingError(msg)
    values = cast("list[object]", raw)
    vector: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = "Embedding service returned a non-numeric vector"
            raise EmbeddingError(msg)
        number = float(value)
        if not math.isfinite(number):
            msg = "Embedding service returned a non-finite vector"
            raise EmbeddingError(msg)
        vector.append(number)
    return index, vector


def parse_response(response: httpx.Response, expected: int) -> list[list[float]]:
    """Check an embeddings response and return its vectors in input order.

    Args:
        response (httpx.Response): The service's answer.
        expected (int): How many texts were sent.

    Returns:
        list[list[float]]: One vector per input, in input order.

    Raises:
        EmbeddingError: On a non-200 status, a malformed body, a count that
            does not match the input, or a vector that is not
            ``EMBEDDING_DIMENSIONS`` long.
    """
    if response.status_code != _HTTP_OK:
        msg = f"Embedding service returned HTTP {response.status_code}"
        raise EmbeddingError(msg)
    try:
        body: object = response.json()
    except ValueError as exc:
        msg = "Embedding service returned a malformed body"
        raise EmbeddingError(msg) from exc
    data = (
        cast("dict[str, object]", body).get("data") if isinstance(body, dict) else None
    )
    if not isinstance(data, list):
        msg = "Embedding service returned a malformed body"
        raise EmbeddingError(msg)
    items = [_vector_from_item(item) for item in cast("list[object]", data)]
    if len(items) != expected:
        msg = f"Embedding service returned {len(items)} vectors for {expected} inputs"
        raise EmbeddingError(msg)
    if sorted(index for index, _ in items) != list(range(expected)):
        msg = "Embedding service returned vectors with unexpected indexes"
        raise EmbeddingError(msg)
    if any(len(vector) != EMBEDDING_DIMENSIONS for _, vector in items):
        msg = (
            "Embedding service returned vectors that are not "
            f"{EMBEDDING_DIMENSIONS} dimensions"
        )
        raise EmbeddingError(msg)
    return [vector for _, vector in sorted(items, key=lambda pair: pair[0])]


class EmbeddingClient:
    """Embeds documents and queries through the configured embedding service.

    Use it as a context manager, or call ``close`` when done.

    Args:
        connection (EmbeddingConnection): URL, key, model and timeout.
        batch_size (int): Most texts sent in one request.
        transport (httpx.BaseTransport | None): Transport override for
            tests; None uses the network.

    Raises:
        ValueError: If ``batch_size`` is less than 1.
    """

    def __init__(
        self,
        connection: EmbeddingConnection,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if batch_size < 1:
            msg = "batch_size must be at least 1"
            raise ValueError(msg)
        self.model = connection.model
        self._batch_size = batch_size
        self._client = httpx.Client(
            base_url=connection.base_url,
            headers={"Authorization": f"Bearer {connection.api_key}"},
            timeout=connection.timeout_seconds,
            transport=transport,
        )

    def __enter__(self) -> EmbeddingClient:
        """Return the client for use in a ``with`` block.

        Returns:
            EmbeddingClient: This client.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the connection pool on leaving the ``with`` block.

        Args:
            exc_type (type[BaseException] | None): Exception type, if any.
            exc (BaseException | None): Exception, if any.
            tb (TracebackType | None): Traceback, if any.
        """
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed document passages, with no prefix, in batches.

        Args:
            texts (Sequence[str]): Passages to embed.

        Returns:
            list[list[float]]: One vector per passage, in order.
        """
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query with the query prefix.

        Args:
            query (str): The user's question.

        Returns:
            list[float]: The query vector.
        """
        return self._embed_batch([query_text(query)])[0]

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Send one request and return its vectors in input order.

            Callers never pass an empty batch: ``embed_documents`` loops over
            non-empty slices and ``embed_query`` sends one text.

        Args:
            texts (Sequence[str]): One non-empty batch of texts.

        Returns:
            list[list[float]]: One vector per text.

        Raises:
            EmbeddingError: On a transport failure or a bad response. The
                message names the exception type only.
        """
        try:
            response = self._client.post(
                EMBEDDINGS_PATH, json=build_payload(self.model, texts)
            )
        except httpx.HTTPError as exc:
            msg = f"Embedding request failed: {type(exc).__name__}"
            raise EmbeddingError(msg) from exc
        return parse_response(response, len(texts))
