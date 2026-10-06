# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the internal search function.

Qdrant runs in-process (``QdrantClient(":memory:")``) and the embedding
service is the bag-of-words fake, so ranking follows shared words. All points
are synthetic. The confidentiality tests are the ones that matter most: a
non-admin search must never return a point that is confidential or that has
no ``is_confidential`` field, and the filter must sit inside the Qdrant query.
"""

from __future__ import annotations

import secrets
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.common.client_exceptions import ResourceExhaustedResponse
from qdrant_client.http.exceptions import UnexpectedResponse
from structlog.testing import capture_logs

from app.retrieval import search as search_module
from app.retrieval.embeddings import QUERY_PREFIX, EmbeddingClient
from app.retrieval.qdrant_store import (
    DENSE_VECTOR,
    FAMILY_DOCS_COLLECTION,
    FamilyDocsWriter,
    ensure_collection,
)
from app.retrieval.search import (
    DEFAULT_TOP_K,
    LATENCY_BUDGET_SECONDS,
    SEARCH_TIMEOUT_SECONDS,
    DocumentCitation,
    SearchError,
    SearchRequest,
    SearchService,
    TaxLawCitation,
    build_search_service,
    family_docs_filter,
)
from app.retrieval.settings import load_retrieval_settings
from app.retrieval.tax_law import TAX_LAW_COLLECTION
from tests.unit.fake_embeddings import MODEL, FakeEmbeddingService, fake_vector

if TYPE_CHECKING:
    from collections.abc import Iterator

    from app.retrieval.settings import EmbeddingConnection

ENTITY_A = "aaaaaaaa-0000-4000-8000-000000000001"
ENTITY_B = "bbbbbbbb-0000-4000-8000-000000000002"
QUERY = "manager holdings company"
MATCHING = "manager holdings company agreement"
_MISSING = object()


@pytest.fixture
def service() -> FakeEmbeddingService:
    """Return a fresh fake embedding service."""
    return FakeEmbeddingService()


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    """Yield an in-process Qdrant client with an empty family-docs collection."""
    client = QdrantClient(":memory:")
    FamilyDocsWriter(client).ensure_collection()
    yield client
    client.close()


@pytest.fixture
def embedder(service: FakeEmbeddingService) -> Iterator[EmbeddingClient]:
    """Yield an embeddings client wired to the fake service."""
    with EmbeddingClient(service.connection(), transport=service.transport()) as client:
        yield client


@pytest.fixture
def search(embedder: EmbeddingClient, qdrant: QdrantClient) -> SearchService:
    """Return a search service over the fakes."""
    return SearchService(embedder, qdrant)


def add_doc(qdrant: QdrantClient, name: str, text: str, **overrides: object) -> None:
    """Write one family-docs point.

    Keyword overrides replace payload fields. ``is_confidential`` defaults to
    False; passing ``_MISSING`` leaves the field out entirely.
    """
    payload: dict[str, Any] = {
        "document_id": f"doc-{name}",
        "chunk_id": f"chunk-{name}",
        "title": f"Title {name}",
        "entity_id": ENTITY_A,
        "document_date": "2024-01-31",
        "page_range": [2, 3],
        "section_hierarchy": ["Report", f"Section {name}"],
        "text": text,
        "is_confidential": False,
        **overrides,
    }
    if payload["is_confidential"] is _MISSING:
        del payload["is_confidential"]
    qdrant.upsert(
        FAMILY_DOCS_COLLECTION,
        points=[
            models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
                vector={DENSE_VECTOR: fake_vector(text)},
                payload=payload,
            )
        ],
        wait=True,
    )


def add_tax_law(qdrant: QdrantClient, subtopic_id: str, text: str) -> None:
    """Write one tax-law point shaped like the tax-law indexer's output."""
    ensure_collection(qdrant, TAX_LAW_COLLECTION)
    qdrant.upsert(
        TAX_LAW_COLLECTION,
        points=[
            models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"tax-{subtopic_id}")),
                vector={DENSE_VECTOR: fake_vector(text)},
                payload={
                    "id": subtopic_id,
                    "title": f"Rule {subtopic_id}",
                    "topic": "Widgets",
                    "text": text,
                },
            )
        ],
        wait=True,
    )


def doc_ids(response: search_module.SearchResponse) -> list[str | None]:
    """Return the document ids of family-docs results, in order."""
    return [
        result.citation.document_id
        for result in response.results
        if isinstance(result.citation, DocumentCitation)
    ]


# --------------------------------------------------------------------------- #
# Confidentiality (fails closed, inside the query)
# --------------------------------------------------------------------------- #


def test_non_admin_never_sees_a_confidential_point(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """A viewer's search returns only points marked is_confidential false.

    More confidential points than ``top_k`` score above the open one, so a
    search that lost the in-query filter would fill every slot with them and
    the second-line check would leave nothing: this test fails without the
    filter instead of being rescued by the second line.
    """
    for n in range(DEFAULT_TOP_K + 4):
        add_doc(qdrant, f"secret-{n}", MATCHING, is_confidential=True)
    add_doc(qdrant, "open", "manager agreement", is_confidential=False)
    response = search.search(SearchRequest(query=QUERY))
    assert doc_ids(response) == ["doc-open"]


def test_admin_sees_confidential_points(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """An admin search applies no confidentiality filter."""
    add_doc(qdrant, "secret", MATCHING, is_confidential=True)
    add_doc(qdrant, "open", "manager agreement", is_confidential=False)
    response = search.search(SearchRequest(query=QUERY, include_confidential=True))
    assert set(doc_ids(response)) == {"doc-secret", "doc-open"}


@pytest.mark.parametrize("flag", [_MISSING, None, "false", 0])
def test_point_without_a_boolean_false_flag_is_excluded(
    search: SearchService, qdrant: QdrantClient, flag: object
) -> None:
    """A missing, null or non-boolean flag fails closed for a viewer.

    More unlabeled points than ``top_k`` outscore the open one, so only the
    in-query filter can let the open point through.
    """
    for n in range(DEFAULT_TOP_K + 4):
        add_doc(qdrant, f"unlabeled-{n}", MATCHING, is_confidential=flag)
    add_doc(qdrant, "open", "manager agreement", is_confidential=False)
    response = search.search(SearchRequest(query=QUERY))
    assert doc_ids(response) == ["doc-open"]


@pytest.mark.parametrize("flag", [_MISSING, None, "false", 0, True])
def test_viewer_filter_alone_excludes_points_without_a_false_flag(
    qdrant: QdrantClient, flag: object
) -> None:
    """Qdrant itself, given the viewer filter, returns only flag-false points."""
    add_doc(qdrant, "unlabeled", MATCHING, is_confidential=flag)
    add_doc(qdrant, "open", "manager agreement", is_confidential=False)
    points = qdrant.query_points(
        collection_name=FAMILY_DOCS_COLLECTION,
        query=fake_vector(QUERY),
        using=DENSE_VECTOR,
        query_filter=family_docs_filter(include_confidential=False, entity_ids=None),
        limit=DEFAULT_TOP_K,
        with_payload=True,
    ).points
    assert [(p.payload or {}).get("document_id") for p in points] == ["doc-open"]


def test_filter_runs_inside_the_query_not_after_it(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """Many better-scoring confidential points still leave k viewer results.

    If the filter ran on results after Qdrant returned its top k, the
    confidential points would fill those k slots and the viewer would get
    nothing.
    """
    for n in range(12):
        add_doc(qdrant, f"secret-{n}", MATCHING, is_confidential=True)
    for n in range(10):
        add_doc(qdrant, f"open-{n}", f"manager filler{n}", is_confidential=False)
    response = search.search(SearchRequest(query=QUERY))
    assert len(response.results) == DEFAULT_TOP_K
    assert all(cid and cid.startswith("doc-open-") for cid in doc_ids(response))


def test_viewer_filter_shape() -> None:
    """The viewer filter matches is_confidential false exactly."""
    query_filter = family_docs_filter(include_confidential=False, entity_ids=None)
    assert query_filter is not None
    assert query_filter.must == [
        models.FieldCondition(
            key="is_confidential", match=models.MatchValue(value=False)
        )
    ]
    assert family_docs_filter(include_confidential=True, entity_ids=None) is None


@pytest.mark.parametrize("flag", [True, _MISSING, None, "false", 0])
def test_second_line_drops_every_flag_but_false(
    search: SearchService,
    qdrant: QdrantClient,
    monkeypatch: pytest.MonkeyPatch,
    flag: object,
) -> None:
    """With the filter lost, only a point whose flag is exactly false is kept."""

    def no_filter(**_kwargs: object) -> None:
        """Build no filter, as if the filter step were lost."""

    monkeypatch.setattr(search_module, "family_docs_filter", no_filter)
    add_doc(qdrant, "secret", MATCHING, is_confidential=flag)
    add_doc(qdrant, "open", "manager agreement", is_confidential=False)
    request = SearchRequest(query=QUERY)
    with capture_logs() as logs:
        response = search.search(request)
    assert doc_ids(response) == ["doc-open"]
    dropped = [log for log in logs if log["event"] == "confidential_results_dropped"]
    assert dropped == [
        {"event": "confidential_results_dropped", "count": 1, "log_level": "error"}
    ]


@pytest.mark.parametrize("value", [1, "yes", "false", None, 0])
def test_include_confidential_must_be_a_bool(value: object) -> None:
    """A truthy non-bool never grants admin visibility; it is rejected."""
    not_a_bool = cast("bool", value)
    with pytest.raises(ValueError, match="include_confidential"):
        SearchRequest(query=QUERY, include_confidential=not_a_bool)


@pytest.mark.parametrize("value", [1, "yes", None])
def test_filter_treats_anything_but_true_as_a_viewer(value: object) -> None:
    """The filter builder fails closed if a non-bool reaches it."""
    query_filter = family_docs_filter(
        include_confidential=cast("bool", value), entity_ids=None
    )
    assert query_filter is not None
    assert query_filter.must == [
        models.FieldCondition(
            key="is_confidential", match=models.MatchValue(value=False)
        )
    ]


# --------------------------------------------------------------------------- #
# Entity filter
# --------------------------------------------------------------------------- #


def test_entity_filter_limits_family_results(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """Only points for the requested entities come back."""
    add_doc(qdrant, "a", MATCHING, entity_id=ENTITY_A)
    add_doc(qdrant, "b", MATCHING, entity_id=ENTITY_B)
    add_doc(qdrant, "b-secret", MATCHING, entity_id=ENTITY_B, is_confidential=True)
    response = search.search(SearchRequest(query=QUERY, entity_ids=(ENTITY_B,)))
    assert doc_ids(response) == ["doc-b"]


def test_entity_filter_combines_with_admin(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """For an admin the entity filter still applies on its own."""
    add_doc(qdrant, "a", MATCHING, entity_id=ENTITY_A)
    add_doc(qdrant, "b-secret", MATCHING, entity_id=ENTITY_B, is_confidential=True)
    response = search.search(
        SearchRequest(query=QUERY, include_confidential=True, entity_ids=(ENTITY_B,))
    )
    assert doc_ids(response) == ["doc-b-secret"]


def test_entity_filter_does_not_apply_to_tax_law(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """Tax-law subtopics belong to no entity and are still searched."""
    add_doc(qdrant, "a", MATCHING, entity_id=ENTITY_A)
    add_tax_law(qdrant, "1.1", MATCHING)
    response = search.search(SearchRequest(query=QUERY, entity_ids=(ENTITY_B,)))
    assert [r.collection for r in response.results] == [TAX_LAW_COLLECTION]


# --------------------------------------------------------------------------- #
# Results and citations
# --------------------------------------------------------------------------- #


def test_family_result_is_plain_data_with_a_citation(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """A family-docs result carries text, score and its citation."""
    add_doc(qdrant, "one", MATCHING)
    result = search.search(SearchRequest(query=QUERY)).results[0]
    assert result.text == MATCHING
    assert result.collection == FAMILY_DOCS_COLLECTION
    assert result.score > 0
    assert result.score <= 1
    assert result.citation == DocumentCitation(
        document_id="doc-one",
        chunk_id="chunk-one",
        title="Title one",
        entity_id=ENTITY_A,
        document_date="2024-01-31",
        page_start=2,
        page_end=3,
        section="Section one",
    )


@pytest.mark.parametrize(
    ("page_range", "section", "expected"),
    [
        ([4, 4], [], (4, 4, None)),
        ([5], ["Only"], (5, 5, "Only")),
        (None, None, (None, None, None)),
        (["1", 2], "Part", (None, None, None)),
        ([True, 2], ["", " "], (None, None, None)),
    ],
)
def test_odd_page_and_section_values_become_none(
    search: SearchService,
    qdrant: QdrantClient,
    page_range: object,
    section: object,
    expected: tuple[object, object, object],
) -> None:
    """Pages and section are read defensively from the payload."""
    add_doc(qdrant, "odd", MATCHING, page_range=page_range, section_hierarchy=section)
    citation = search.search(SearchRequest(query=QUERY)).results[0].citation
    assert isinstance(citation, DocumentCitation)
    actual = (citation.page_start, citation.page_end, citation.section)
    assert actual == expected


def test_tax_law_result_cites_subtopic_id_and_title(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """Tax-law results cite the subtopic, and are open to every caller."""
    add_tax_law(qdrant, "4.2", MATCHING)
    response = search.search(
        SearchRequest(query=QUERY, collections=(TAX_LAW_COLLECTION,))
    )
    result = response.results[0]
    assert result.collection == TAX_LAW_COLLECTION
    assert result.citation == TaxLawCitation(
        subtopic_id="4.2", title="Rule 4.2", topic="Widgets"
    )


def test_collections_are_merged_by_score_and_capped(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """Results from both collections are ranked together and cut at top_k."""
    add_tax_law(qdrant, "1.1", MATCHING)
    add_doc(qdrant, "weak", "manager")
    for n in range(3):
        add_doc(qdrant, f"strong-{n}", MATCHING)
    response = search.search(SearchRequest(query=QUERY, top_k=3))
    assert len(response.results) == 3
    scores = [r.score for r in response.results]
    assert scores == sorted(scores, reverse=True)
    assert "doc-weak" not in doc_ids(response)


def test_missing_collection_is_skipped_and_reported(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """A collection that was never indexed is skipped, warned and reported."""
    add_doc(qdrant, "one", MATCHING)
    request = SearchRequest(query=QUERY)
    with capture_logs() as logs:
        response = search.search(request)
    assert doc_ids(response) == ["doc-one"]
    assert response.missing_collections == (TAX_LAW_COLLECTION,)
    assert {
        "event": "search_collection_missing",
        "collection": TAX_LAW_COLLECTION,
        "log_level": "warning",
    } in logs


def test_search_fails_when_no_requested_collection_exists(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """All collections missing is an error, not an empty answer."""
    request = SearchRequest(query=QUERY, collections=(TAX_LAW_COLLECTION,))
    with pytest.raises(SearchError, match="indexed"):
        search.search(request)
    qdrant.delete_collection(FAMILY_DOCS_COLLECTION)
    request = SearchRequest(query=QUERY)
    with pytest.raises(SearchError, match="indexed"):
        search.search(request)


def test_response_as_dict_follows_the_search_contract(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """``as_dict`` gives the documented request and response shape."""
    add_doc(qdrant, "one", MATCHING)
    add_tax_law(qdrant, "1.1", "manager")
    body = search.search(SearchRequest(query=QUERY)).as_dict()
    assert body["embedding_model"] == MODEL
    assert body["missing_collections"] == []
    by_collection = {r["collection"]: r for r in body["results"]}
    assert set(by_collection[FAMILY_DOCS_COLLECTION]["citation"]) == {
        "document_id",
        "chunk_id",
        "title",
        "entity_id",
        "document_date",
        "page_start",
        "page_end",
        "section",
    }
    assert by_collection[TAX_LAW_COLLECTION]["citation"] == {
        "id": "1.1",
        "title": "Rule 1.1",
        "topic": "Widgets",
    }


def test_query_is_embedded_with_the_query_prefix(
    search: SearchService, service: FakeEmbeddingService
) -> None:
    """Only the query side gets the instruction prefix."""
    search.search(SearchRequest(query=QUERY))
    assert service.requests[-1]["body"]["input"] == f"{QUERY_PREFIX}{QUERY}"


# --------------------------------------------------------------------------- #
# Request checks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"query": "  "}, "query"),
        ({"query": "x" * 4001}, "query"),
        ({"query": "q", "top_k": 0}, "top_k"),
        ({"query": "q", "top_k": DEFAULT_TOP_K + 1}, "top_k"),
        ({"query": "q", "entity_ids": ()}, "entity_ids"),
        ({"query": "q", "entity_ids": (" ",)}, "entity_ids"),
        ({"query": "q", "collections": ()}, "collections"),
        ({"query": "q", "collections": ("other",)}, "collections"),
    ],
)
def test_bad_requests_are_rejected(kwargs: dict[str, Any], message: str) -> None:
    """Requests outside the contract raise ValueError naming the field."""
    with pytest.raises(ValueError, match=message):
        SearchRequest(**kwargs)


def test_duplicate_collections_are_searched_once(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """Naming a collection twice does not double its results."""
    add_doc(qdrant, "one", MATCHING)
    request = SearchRequest(
        query=QUERY, collections=(FAMILY_DOCS_COLLECTION, FAMILY_DOCS_COLLECTION)
    )
    assert doc_ids(search.search(request)) == ["doc-one"]


# --------------------------------------------------------------------------- #
# Failures, logging and latency
# --------------------------------------------------------------------------- #


def test_embedding_failure_raises_search_error_without_the_query(
    search: SearchService, service: FakeEmbeddingService
) -> None:
    """A failed query embedding is a SearchError that never names the query."""
    service.fail_with = 503
    request = SearchRequest(query=QUERY)
    with capture_logs() as logs, pytest.raises(SearchError) as excinfo:
        search.search(request)
    assert QUERY not in str(excinfo.value)
    assert all(QUERY not in str(log) for log in logs)


def test_qdrant_failure_raises_search_error(
    search: SearchService, qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Qdrant error is a SearchError naming only the error type."""

    def broken(*_args: object, **_kwargs: object) -> object:
        """Fail like a Qdrant server error."""
        raise UnexpectedResponse(500, "error", b"", httpx.Headers())

    monkeypatch.setattr(qdrant, "query_points", broken)
    request = SearchRequest(query=QUERY)
    with pytest.raises(SearchError, match="Qdrant request failed: UnexpectedResponse"):
        search.search(request)


def test_qdrant_rate_limit_raises_search_error(
    search: SearchService, qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Qdrant 429 (not an ApiException) is still a SearchError."""

    def limited(*_args: object, **_kwargs: object) -> object:
        """Fail like a rate-limited Qdrant server."""
        message = "slow down"
        raise ResourceExhaustedResponse(message, 1)

    monkeypatch.setattr(qdrant, "query_points", limited)
    request = SearchRequest(query=QUERY)
    with pytest.raises(SearchError, match="ResourceExhaustedResponse"):
        search.search(request)


def test_transport_failure_raises_search_error(
    search: SearchService, qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network error talking to Qdrant is a SearchError."""

    def broken(*_args: object, **_kwargs: object) -> object:
        """Fail like a dropped connection."""
        message = "refused"
        raise httpx.ConnectError(message)

    monkeypatch.setattr(qdrant, "collection_exists", broken)
    request = SearchRequest(query=QUERY)
    with pytest.raises(SearchError, match="ConnectError"):
        search.search(request)


def test_search_logs_counts_and_timing_never_text(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """The finish log has counts and elapsed time, not the query or results."""
    add_doc(qdrant, "one", MATCHING)
    with capture_logs() as logs:
        search.search(SearchRequest(query=QUERY))
    finished = logs[-1]
    assert finished["event"] == "search_finished"
    assert finished["results"] == 1
    assert finished["include_confidential"] is False
    assert isinstance(finished["elapsed_ms"], float)
    assert all(QUERY not in str(log) and MATCHING not in str(log) for log in logs)


def test_search_over_fakes_meets_the_latency_budget(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """With a warm in-process store and fake embedder, search is well under 2 s.

    This proves the portal adds no meaningful time of its own. The real
    embedding service and Qdrant over the network still need measuring.
    """
    for n in range(200):
        add_doc(qdrant, f"bulk-{n}", f"manager filler{n} words{n % 7}")
    add_tax_law(qdrant, "1.1", MATCHING)
    search.search(SearchRequest(query=QUERY))  # warm-up
    started = time.perf_counter()
    for _ in range(5):
        search.search(SearchRequest(query=QUERY))
    per_search = (time.perf_counter() - started) / 5
    assert per_search < LATENCY_BUDGET_SECONDS


def test_slow_search_logs_a_budget_warning(
    embedder: EmbeddingClient, qdrant: QdrantClient
) -> None:
    """A search slower than the budget still returns, with a warning."""
    ticks = iter([0.0, LATENCY_BUDGET_SECONDS + 0.5])
    slow = SearchService(embedder, qdrant, clock=lambda: next(ticks))
    with capture_logs() as logs:
        slow.search(SearchRequest(query=QUERY))
    warning = next(log for log in logs if log["event"] == "search_over_budget")
    assert warning["elapsed_ms"] == pytest.approx(2500.0)
    assert warning["budget_ms"] == pytest.approx(2000.0)


async def test_search_async_runs_off_the_event_loop(
    search: SearchService, qdrant: QdrantClient
) -> None:
    """The async entry point gives the same results."""
    add_doc(qdrant, "one", MATCHING)
    response = await search.search_async(SearchRequest(query=QUERY))
    assert doc_ids(response) == ["doc-one"]


# --------------------------------------------------------------------------- #
# Building the service from settings
# --------------------------------------------------------------------------- #

RETRIEVAL_VARS = (
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBEDDING_MODEL",
    "EMBED_TIMEOUT_SECONDS",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "CHUNKS_DIR",
    "TAX_LAW_PATH",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every retrieval variable."""
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_build_returns_none_when_not_connected(clean_env: pytest.MonkeyPatch) -> None:
    """With nothing set, search is off."""
    del clean_env
    assert build_search_service() is None


def test_build_returns_none_when_misconfigured(clean_env: pytest.MonkeyPatch) -> None:
    """A URL without its key counts as not connected, without raising."""
    clean_env.setenv("EMBED_BASE_URL", "http://embed.test")
    clean_env.setenv("EMBEDDING_MODEL", MODEL)
    clean_env.setenv("QDRANT_URL", "http://qdrant.test:6333")
    clean_env.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))
    with capture_logs() as logs:
        assert build_search_service() is None
    assert logs[0]["event"] == "search_not_connected"


def test_build_returns_none_for_an_unparseable_value(
    clean_env: pytest.MonkeyPatch, service: FakeEmbeddingService
) -> None:
    """A bad environment value means not connected, never an exception."""
    clean_env.setenv("EMBED_BASE_URL", "http://embed.test")
    clean_env.setenv("EMBED_API_KEY", service.key)
    clean_env.setenv("EMBEDDING_MODEL", MODEL)
    clean_env.setenv("EMBED_TIMEOUT_SECONDS", "abc")
    clean_env.setenv("QDRANT_URL", "http://qdrant.test:6333")
    clean_env.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))
    with capture_logs() as logs:
        assert build_search_service() is None
    assert logs[0]["event"] == "search_not_connected"


@pytest.mark.parametrize("variable", ["EMBED_API_KEY", "QDRANT_API_KEY"])
def test_build_returns_none_for_a_non_ascii_key(
    clean_env: pytest.MonkeyPatch, variable: str
) -> None:
    """A key that cannot be an HTTP header means not connected, with no client."""
    clean_env.setenv("EMBED_BASE_URL", "http://embed.test")
    clean_env.setenv("EMBED_API_KEY", secrets.token_urlsafe(16))
    clean_env.setenv("EMBEDDING_MODEL", MODEL)
    clean_env.setenv("QDRANT_URL", "http://qdrant.test:6333")
    clean_env.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))
    clean_env.setenv(variable, "cl\u00e9")
    with capture_logs() as logs:
        assert build_search_service() is None
    assert logs[0]["event"] == "search_not_connected"


def test_build_caps_timeouts_for_search(
    clean_env: pytest.MonkeyPatch, service: FakeEmbeddingService
) -> None:
    """Search uses short timeouts so chat can report an outage quickly."""
    clean_env.setenv("EMBED_BASE_URL", "http://embed.test")
    clean_env.setenv("EMBED_API_KEY", service.key)
    clean_env.setenv("EMBEDDING_MODEL", MODEL)
    clean_env.setenv("EMBED_TIMEOUT_SECONDS", "120")
    clean_env.setenv("QDRANT_URL", "http://qdrant.test:6333")
    clean_env.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))
    seen: dict[str, object] = {}
    real_embedding_client = search_module.EmbeddingClient

    def fake_make_client(connection: object, *, timeout: int) -> QdrantClient:
        """Record the timeout and return an in-process client."""
        seen["qdrant_timeout"] = timeout
        del connection
        client = QdrantClient(":memory:")
        FamilyDocsWriter(client).ensure_collection()
        return client

    def fake_embedding_client(
        connection: EmbeddingConnection, *, max_attempts: int
    ) -> EmbeddingClient:
        """Record the connection and attempts, and use the fake transport."""
        seen["embed_timeout"] = connection.timeout_seconds
        seen["embed_attempts"] = max_attempts
        return real_embedding_client(
            connection, max_attempts=max_attempts, transport=service.transport()
        )

    clean_env.setattr(search_module, "make_client", fake_make_client)
    clean_env.setattr(search_module, "EmbeddingClient", fake_embedding_client)
    built = build_search_service(load_retrieval_settings())
    assert built is not None
    with built:
        assert built.search(SearchRequest(query=QUERY)).results == ()
    assert seen == {
        "qdrant_timeout": SEARCH_TIMEOUT_SECONDS,
        "embed_timeout": SEARCH_TIMEOUT_SECONDS,
        "embed_attempts": 1,
    }
