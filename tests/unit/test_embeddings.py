# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the embeddings client against a fake service.

Ported from the reference implementation's client tests and adapted to the
portal's connection settings and ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest

from app.retrieval.embeddings import (
    QUERY_PREFIX,
    EmbeddingClient,
    EmbeddingError,
    parse_response,
    query_text,
)
from tests.unit.fake_embeddings import MODEL, FakeEmbeddingService, fake_vector


@pytest.fixture
def service() -> FakeEmbeddingService:
    """Return a fresh fake embedding service."""
    return FakeEmbeddingService()


def _client(
    service: FakeEmbeddingService,
    *,
    key: str | None = None,
    batch_size: int = 32,
    sleeps: list[float] | None = None,
) -> EmbeddingClient:
    """Return a client wired to the fake service that records its waits."""
    waits = sleeps if sleeps is not None else []
    return EmbeddingClient(
        service.connection(key),
        batch_size=batch_size,
        transport=service.transport(),
        sleep=waits.append,
    )


def test_query_prefix_is_exact() -> None:
    """Query prefix is exact."""
    assert QUERY_PREFIX == (
        "Instruct: Given a question about a family's financial, estate and tax "
        "documents, retrieve passages that answer it\nQuery:"
    )
    assert "\\n" not in QUERY_PREFIX  # a real newline, not backslash and n


def test_query_gets_prefix_appended_without_space(
    service: FakeEmbeddingService,
) -> None:
    """Query gets prefix appended without space."""
    vector = _client(service).embed_query("what is the deadline")
    sent = service.requests[0]["body"]["input"]
    assert sent == f"{QUERY_PREFIX}what is the deadline"
    assert sent == query_text("what is the deadline")
    assert "Query:what" in sent
    assert len(vector) == 1024


def test_documents_get_no_prefix_and_keep_order(service: FakeEmbeddingService) -> None:
    """Documents get no prefix and keep order."""
    texts = ["alpha beta", "gamma delta", "epsilon"]
    vectors = _client(service).embed_documents(texts)
    assert service.requests[0]["body"]["input"] == texts
    assert vectors == [fake_vector(t) for t in texts]


def test_request_shape_and_bearer_header(service: FakeEmbeddingService) -> None:
    """Request shape and bearer header."""
    _client(service).embed_documents(["one"])
    request = service.requests[0]
    assert request["path"] == "/v1/embeddings"
    assert request["auth"] == f"Bearer {service.key}"
    assert request["body"]["model"] == MODEL
    assert request["body"]["input"] == "one"  # a single text is sent as a string


def test_batches_by_batch_size(service: FakeEmbeddingService) -> None:
    """Batches by batch size."""
    vectors = _client(service, batch_size=2).embed_documents(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert len(service.requests) == 3


def test_batch_size_must_be_positive(service: FakeEmbeddingService) -> None:
    """Batch size must be positive."""
    with pytest.raises(ValueError, match="batch_size"):
        _client(service, batch_size=0)


def test_empty_input_makes_no_request(service: FakeEmbeddingService) -> None:
    """Empty input makes no request."""
    assert _client(service).embed_documents([]) == []
    assert service.requests == []


def test_wrong_key_is_an_error(service: FakeEmbeddingService) -> None:
    """Wrong key is an error, marked as a refused key and not retried."""
    with pytest.raises(EmbeddingError, match="401") as excinfo:
        _client(service, key="wrong-key").embed_query("q")
    assert excinfo.value.status_code == 401
    assert excinfo.value.refused_key is True
    assert excinfo.value.retryable is False
    assert len(service.requests) == 1


def test_server_error_does_not_leak_input_text(service: FakeEmbeddingService) -> None:
    """Server error does not leak input text and is not retried."""
    service.fail_with = 500
    with pytest.raises(EmbeddingError) as excinfo:
        _client(service).embed_documents(["confidential passage text"])
    assert "confidential" not in str(excinfo.value)
    assert "500" in str(excinfo.value)
    assert excinfo.value.refused_key is False
    assert len(service.requests) == 1


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_status_is_retried_then_succeeds(
    service: FakeEmbeddingService, status: int
) -> None:
    """A transient status is retried with a growing delay, then succeeds."""
    service.fail_with = status
    service.fail_limit = 2
    sleeps: list[float] = []
    vectors = _client(service, sleeps=sleeps).embed_documents(["one"])
    assert vectors == [fake_vector("one")]
    assert len(service.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_retries_stop_after_the_last_attempt(service: FakeEmbeddingService) -> None:
    """Retries stop after the last attempt and raise a retryable error."""
    service.fail_with = 503
    sleeps: list[float] = []
    with pytest.raises(EmbeddingError, match="503") as excinfo:
        _client(service, sleeps=sleeps).embed_query("q")
    assert excinfo.value.retryable is True
    assert len(service.requests) == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    ("header", "wait"),
    [("5", 5.0), ("0", 0.0), ("600", 30.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 1.0)],
)
def test_retry_after_is_honored_up_to_a_cap(
    service: FakeEmbeddingService, header: str, wait: float
) -> None:
    """Retry-After in seconds is honored up to a cap; a date uses the backoff."""
    service.fail_with = 429
    service.fail_limit = 1
    service.retry_after = header
    sleeps: list[float] = []
    _client(service, sleeps=sleeps).embed_query("q")
    assert sleeps == [wait]


def test_max_attempts_must_be_positive(service: FakeEmbeddingService) -> None:
    """Max attempts must be positive."""
    with pytest.raises(ValueError, match="max_attempts"):
        EmbeddingClient(service.connection(), max_attempts=0)


def test_a_later_batch_failure_fails_the_whole_call(
    service: FakeEmbeddingService,
) -> None:
    """A failure in a later batch fails the call; no partial result returns."""
    service.fail_with = 500
    service.fail_after = 1
    with pytest.raises(EmbeddingError, match="500"):
        _client(service, batch_size=2).embed_documents(["a", "b", "c"])
    assert len(service.requests) == 2


def test_wrong_dimensions_is_an_error(service: FakeEmbeddingService) -> None:
    """Wrong dimensions is an error."""
    service.dimensions = 768
    with pytest.raises(EmbeddingError, match="1024"):
        _client(service).embed_query("q")


def test_transport_failure_is_an_error_without_text(
    service: FakeEmbeddingService,
) -> None:
    """Transport failure is retried, then an error without text."""
    service.raise_error = httpx.ConnectError("refused")
    sleeps: list[float] = []
    with pytest.raises(EmbeddingError, match="ConnectError") as excinfo:
        _client(service, sleeps=sleeps).embed_documents(["private words"])
    assert "private" not in str(excinfo.value)
    assert excinfo.value.retryable is True
    assert sleeps == [1.0, 2.0]


def test_other_transport_errors_are_not_retried(
    service: FakeEmbeddingService,
) -> None:
    """An error that a retry cannot fix is raised at once."""
    service.raise_error = httpx.UnsupportedProtocol("bad scheme")
    sleeps: list[float] = []
    with pytest.raises(EmbeddingError, match="UnsupportedProtocol") as excinfo:
        _client(service, sleeps=sleeps).embed_query("q")
    assert excinfo.value.retryable is False
    assert sleeps == []


def test_client_is_a_context_manager(service: FakeEmbeddingService) -> None:
    """Client is a context manager."""
    with _client(service) as client:
        assert client.embed_query("q")
    assert client.model == MODEL


# --------------------------------------------------------------------------- #
# parse_response checks, shared with any future async client
# --------------------------------------------------------------------------- #


def _response(body: object, status: int = 200) -> httpx.Response:
    """Wrap a JSON body in an HTTP response."""
    return httpx.Response(status, json=body)


def _item(index: int, size: int = 1024, value: object = 0.5) -> dict[str, object]:
    """Return one embedding entry for a response body."""
    return {"index": index, "embedding": [value] * size}


def test_vectors_are_returned_in_index_order() -> None:
    """Vectors are returned in index order."""
    body = {"data": [{"index": 1, "embedding": [1.0] * 1024}, _item(0)]}
    vectors = parse_response(_response(body), 2)
    assert vectors[0][0] == 0.5
    assert vectors[1][0] == 1.0


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"data": "nope"},
        {"no_data": []},
        {"data": ["not an object"]},
        {"data": [{"index": "0", "embedding": [0.1] * 1024}]},
        {"data": [{"index": True, "embedding": [0.1] * 1024}]},
        {"data": [{"index": 0, "embedding": "nope"}]},
    ],
)
def test_malformed_bodies_are_errors(body: object) -> None:
    """Malformed bodies are errors."""
    with pytest.raises(EmbeddingError, match="malformed"):
        parse_response(_response(body), 1)


def test_non_json_body_is_an_error() -> None:
    """Non json body is an error."""
    response = httpx.Response(200, content=b"not json")
    with pytest.raises(EmbeddingError, match="malformed"):
        parse_response(response, 1)


@pytest.mark.parametrize("value", ["0.1", True, None])
def test_non_numeric_values_are_errors(value: object) -> None:
    """Non numeric values are errors."""
    with pytest.raises(EmbeddingError, match="non-numeric"):
        parse_response(_response({"data": [_item(0, value=value)]}), 1)


def test_non_finite_values_are_errors() -> None:
    """Non finite values are errors."""
    response = httpx.Response(
        200, content=b'{"data": [{"index": 0, "embedding": [NaN]}]}'
    )
    with pytest.raises(EmbeddingError, match="non-finite"):
        parse_response(response, 1)


def test_huge_integer_values_are_errors() -> None:
    """An integer too large for a float is a non-finite value, not a crash."""
    huge = "9" * 400
    response = httpx.Response(
        200, content=f'{{"data": [{{"index": 0, "embedding": [{huge}]}}]}}'.encode()
    )
    with pytest.raises(EmbeddingError, match="non-finite"):
        parse_response(response, 1)


def test_wrong_count_is_an_error() -> None:
    """Wrong count is an error."""
    with pytest.raises(EmbeddingError, match="1 vectors for 2 inputs"):
        parse_response(_response({"data": [_item(0)]}), 2)


def test_duplicate_indexes_are_an_error() -> None:
    """Duplicate indexes are an error."""
    with pytest.raises(EmbeddingError, match="unexpected indexes"):
        parse_response(_response({"data": [_item(0), _item(0)]}), 2)
