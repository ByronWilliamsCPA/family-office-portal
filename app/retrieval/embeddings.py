# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Client for an OpenAI-compatible embeddings service (``POST /v1/embeddings``).

Documents are embedded as they are. Queries get a fixed instruction prefix,
which the embedding model expects on the query side only; the query text
follows ``Query:`` directly, with no space. All embedding is remote: no model
is loaded in the portal.

The request and response handling is split into ``build_payload`` and
``parse_response`` so an async client for search can reuse the same checks.

A request that times out, loses its connection, or gets HTTP 429, 502, 503
or 504 is retried a bounded number of times with a growing delay, honoring a
``Retry-After`` header in seconds up to a cap. Any other failure is raised at
once; ``EmbeddingError.refused_key`` marks a 401 or 403, which no retry or
later document can fix.

#CRITICAL: privacy: error messages carry status codes, counts and exception
type names only, never input text or the response body (which may echo
input). Transport errors are chained with ``from exc``; httpx transport
errors describe the connection, not the request body, but callers must log
``str(exc)`` only and never the traceback or the chained cause. #VERIFY:
tests/unit/test_embeddings.py checks error messages for leaked text.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType

    from app.retrieval.settings import EmbeddingConnection

EMBEDDINGS_PATH = "/v1/embeddings"
EMBEDDING_DIMENSIONS = 1024
DEFAULT_BATCH_SIZE = 32
DEFAULT_MAX_ATTEMPTS = 3
_HTTP_OK = 200
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_KEY_REFUSED_STATUSES = frozenset({401, 403})
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)
_BACKOFF_SECONDS = 1.0
_MAX_RETRY_DELAY_SECONDS = 30.0

# The query text is appended directly after "Query:". The newline is real.
QUERY_PREFIX = (
    "Instruct: Given a question about a family's financial, estate and tax "
    "documents, retrieve passages that answer it\nQuery:"
)


class EmbeddingError(RuntimeError):
    """The embedding service failed or returned data that cannot be used.

    Args:
        message (str): What went wrong; never includes input text.
        status_code (int | None): HTTP status of the failed response, or None
            when there was no response. Kept as ``status_code``.
        retryable (bool): True when the same request may succeed later. Kept
            as ``retryable``.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable

    @property
    def refused_key(self) -> bool:
        """Say whether the service refused the key (HTTP 401 or 403).

        Returns:
            bool: True for a 401 or 403 response.
        """
        return self.status_code in _KEY_REFUSED_STATUSES


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
        EmbeddingError: If the entry is not a well-formed embedding, or a
            value is not a finite number.
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
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
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
        EmbeddingError: On a non-200 status (with ``status_code`` set), a
            malformed body, a non-numeric or non-finite value, a count that
            does not match the input, duplicate or missing indexes, or a
            vector that is not ``EMBEDDING_DIMENSIONS`` long.
    """
    if response.status_code != _HTTP_OK:
        msg = f"Embedding service returned HTTP {response.status_code}"
        raise EmbeddingError(
            msg,
            status_code=response.status_code,
            retryable=response.status_code in _RETRYABLE_STATUSES,
        )
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


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return how long to wait before the next attempt.

    Args:
        response (httpx.Response | None): The failed response, or None after
            a transport error.
        attempt (int): The attempt that just failed, counting from 1.

    Returns:
        float: Seconds to wait: ``Retry-After`` in seconds when the response
        gives one, otherwise a delay that doubles each attempt; never more
        than the cap.
    """
    delay = _BACKOFF_SECONDS * 2 ** (attempt - 1)
    header = response.headers.get("Retry-After") if response is not None else None
    if header is not None:
        try:
            given = float(header)
        except ValueError:
            given = math.nan  # an HTTP date; fall back to the backoff
        if math.isfinite(given) and given >= 0:
            delay = given
    return min(delay, _MAX_RETRY_DELAY_SECONDS)


class EmbeddingClient:
    """Embeds documents and queries through the configured embedding service.

    Use it as a context manager, or call ``close`` when done.

    Args:
        connection (EmbeddingConnection): URL, key, model and timeout.
        batch_size (int): Most texts sent in one request.
        max_attempts (int): Most attempts for one request, counting the
            first; retries apply only to timeouts, lost connections and
            HTTP 429, 502, 503 and 504.
        transport (httpx.BaseTransport | None): Transport override for
            tests; None uses the network.
        sleep (Callable[[float], None]): Waits between attempts; tests pass a
            no-op.

    Raises:
        ValueError: If ``batch_size`` or ``max_attempts`` is less than 1.
    """

    def __init__(
        self,
        connection: EmbeddingConnection,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if batch_size < 1:
            msg = "batch_size must be at least 1"
            raise ValueError(msg)
        if max_attempts < 1:
            msg = "max_attempts must be at least 1"
            raise ValueError(msg)
        self.model = connection.model
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._sleep = sleep
        key = connection.api_key.get_secret_value()
        self._client = httpx.Client(
            base_url=connection.base_url,
            headers={"Authorization": f"Bearer {key}"},
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

        If any batch fails, its ``EmbeddingError`` propagates and vectors
        from earlier batches are discarded.

        Returns:
            list[list[float]]: One vector per passage, in order.
        """
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query with the query prefix.

        A failed request or unusable response raises ``EmbeddingError``.

        Args:
            query (str): The user's question.

        Returns:
            list[float]: The query vector.
        """
        return self._embed_batch([query_text(query)])[0]

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Send one request, retrying transient failures, and return vectors.

        Callers never pass an empty batch: ``embed_documents`` loops over
        non-empty slices and ``embed_query`` sends one text.

        Args:
            texts (Sequence[str]): One non-empty batch of texts.

        Returns:
            list[list[float]]: One vector per text, in input order.

        Raises:
            EmbeddingError: On a transport failure or a bad response once the
                attempts are used up, or at once when the failure is not
                transient. The message names the status or the exception
                type only.
        """
        payload = build_payload(self.model, texts)
        attempt = 1
        while True:
            last_try = attempt >= self._max_attempts
            response: httpx.Response | None = None
            try:
                response = self._client.post(EMBEDDINGS_PATH, json=payload)
                return parse_response(response, len(texts))
            except httpx.HTTPError as exc:
                retryable = isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS)
                if last_try or not retryable:
                    msg = f"Embedding request failed: {type(exc).__name__}"
                    raise EmbeddingError(msg, retryable=retryable) from exc
            except EmbeddingError as exc:
                if last_try or not exc.retryable:
                    raise
            self._sleep(_retry_delay(response, attempt))
            attempt += 1
