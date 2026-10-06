# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the tax-law knowledge-base indexer.

The knowledge base here is a small synthetic file written by the tests; it
has the real file's shape and nothing of its content. The embedding service
is a fake on ``httpx.MockTransport`` and Qdrant runs in-process.
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from structlog.testing import capture_logs

from app.retrieval import tax_law as tax_law_module
from app.retrieval.embeddings import QUERY_PREFIX, EmbeddingClient
from app.retrieval.qdrant_store import (
    DENSE_VECTOR,
    SPARSE_VECTOR,
    VectorStoreError,
    make_client,
)
from app.retrieval.tax_law import (
    REMOVAL_GUARD_MIN,
    TAX_LAW_COLLECTION,
    KnowledgeBaseError,
    TaxLawIndexer,
    main,
    parse_knowledge_base,
    read_knowledge_base,
    subtopic_point_id,
)
from tests.unit.fake_embeddings import MODEL, FakeEmbeddingService

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from app.retrieval.settings import EmbeddingConnection

STAMP = "2026-01-02T03:04:05+00:00"
MARKER_TEXT = "zebra quokka marmot"
# Generated per run so no credential-shaped literal sits in the source.
USERINFO_SECRET = secrets.token_urlsafe(9)


def knowledge_base(
    *subtopics: tuple[str, str, str], topic: str = "Widget Credits"
) -> dict[str, Any]:
    """Return a one-topic knowledge base from (id, title, content) triples."""
    return {
        "knowledgeBase": [
            {
                "id": "topic-1",
                "topic": topic,
                "subtopics": [
                    {"id": sid, "title": title, "content": content}
                    for sid, title, content in subtopics
                ],
            }
        ]
    }


BASE = knowledge_base(
    ("1.1", "Basic widget credit", "A widget credit applies per widget."),
    ("1.2", "Widget credit limits", "The widget credit has a yearly cap."),
    ("1.3", "Placeholder", "   "),
)


def numbered(count: int) -> dict[str, Any]:
    """Return a one-topic knowledge base with ``count`` subtopics, ids n1..."""
    return knowledge_base(
        *((f"n{i}", f"Title {i}", f"Text number {i}.") for i in range(1, count + 1))
    )


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
def embedder(service: FakeEmbeddingService) -> Iterator[EmbeddingClient]:
    """Yield an embeddings client wired to the fake service."""
    with EmbeddingClient(service.connection(), transport=service.transport()) as client:
        yield client


@pytest.fixture
def indexer(embedder: EmbeddingClient, qdrant: QdrantClient) -> TaxLawIndexer:
    """Return a tax-law indexer with a fixed clock."""
    return TaxLawIndexer(embedder, qdrant, clock=lambda: STAMP)


def write_kb(directory: Path, data: object) -> Path:
    """Write ``data`` as the knowledge-base file and return its path."""
    path = directory / "kb.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def stored(qdrant: QdrantClient) -> dict[str, dict[str, Any]]:
    """Return the stored payloads keyed by subtopic id."""
    if not qdrant.collection_exists(TAX_LAW_COLLECTION):
        return {}
    records, _ = qdrant.scroll(TAX_LAW_COLLECTION, limit=100, with_payload=True)
    return {
        str((record.payload or {})["id"]): dict(record.payload or {})
        for record in records
    }


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parse_keeps_subtopics_with_content_in_file_order() -> None:
    """Blank subtopics are left out; the rest keep their order and topic."""
    parsed = parse_knowledge_base(BASE)
    assert [sub.subtopic_id for sub in parsed.subtopics] == ["1.1", "1.2"]
    assert parsed.skipped_blank == 1
    assert parsed.subtopics[0].topic == "Widget Credits"
    assert parsed.subtopics[0].topic_id == "topic-1"


def test_subtopic_ids_are_trimmed() -> None:
    """Surrounding whitespace is not part of a subtopic id."""
    parsed = parse_knowledge_base(knowledge_base((" 1.1 ", "A", "text")))
    assert parsed.subtopics[0].subtopic_id == "1.1"


def test_ids_that_differ_only_by_whitespace_are_duplicates() -> None:
    """The duplicate check runs on trimmed ids."""
    data = knowledge_base((" 4.1", "A", "one"), ("4.1 ", "B", "two"))
    with pytest.raises(KnowledgeBaseError, match=r"duplicate subtopic ids: 4\.1"):
        parse_knowledge_base(data)


def test_duplicate_ids_are_rejected_even_across_topics() -> None:
    """A subtopic id used twice anywhere rejects the whole file."""
    data = knowledge_base(("2.1", "First", "one"))
    data["knowledgeBase"].append(
        {
            "id": "topic-2",
            "topic": "Other",
            "subtopics": [{"id": "2.1", "title": "Second", "content": "two"}],
        }
    )
    with pytest.raises(KnowledgeBaseError, match=r"duplicate subtopic ids: 2\.1"):
        parse_knowledge_base(data)


def test_duplicate_of_a_blank_subtopic_is_still_rejected() -> None:
    """Duplicates are checked before blank subtopics are dropped."""
    data = knowledge_base(("3.1", "A", "text"), ("3.1", "B", "  "))
    with pytest.raises(KnowledgeBaseError, match="duplicate"):
        parse_knowledge_base(data)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "not a JSON object"),
        ({"knowledgeBase": {}}, "knowledgeBase is not a list"),
        ({"knowledgeBase": ["x"]}, "topic 0 is not a JSON object"),
        (
            {"knowledgeBase": [{"id": "t", "topic": "T", "subtopics": {}}]},
            "topic 0 subtopics is not a list",
        ),
        (
            {"knowledgeBase": [{"id": "t", "topic": 5, "subtopics": []}]},
            "topic 0 topic is not a string",
        ),
        (
            {"knowledgeBase": [{"topic": "T", "subtopics": []}]},
            "topic 0 id is not a string",
        ),
        (
            {"knowledgeBase": [{"id": "t", "topic": "T", "subtopics": [1]}]},
            r"topic 0 subtopic 0 is not a JSON object",
        ),
        (
            knowledge_base(("", "Title", "text")),
            "topic 0 subtopic 0 id is blank",
        ),
        (
            {
                "knowledgeBase": [
                    {
                        "id": "t",
                        "topic": "T",
                        "subtopics": [{"id": "1", "title": "x", "content": None}],
                    }
                ]
            },
            "topic 0 subtopic 0 content is not a string",
        ),
        (
            {
                "knowledgeBase": [
                    {
                        "id": "t",
                        "topic": "T",
                        "subtopics": [{"id": "1", "content": "c"}],
                    }
                ]
            },
            "topic 0 subtopic 0 title is not a string",
        ),
    ],
)
def test_malformed_files_are_rejected(data: object, message: str) -> None:
    """Every shape problem names its position, never content."""
    with pytest.raises(KnowledgeBaseError, match=message):
        parse_knowledge_base(data)


@pytest.mark.parametrize(
    "data",
    [
        [MARKER_TEXT],
        {"knowledgeBase": {MARKER_TEXT: 1}},
        {"knowledgeBase": [{"id": {MARKER_TEXT: 1}, "topic": "T", "subtopics": []}]},
        {"knowledgeBase": [{"id": "t", "topic": [MARKER_TEXT], "subtopics": []}]},
        {"knowledgeBase": [{"id": "t", "topic": "T", "subtopics": [MARKER_TEXT]}]},
        {
            "knowledgeBase": [
                {
                    "id": "t",
                    "topic": "T",
                    "subtopics": [{"id": "1", "title": [MARKER_TEXT], "content": "c"}],
                }
            ]
        },
        {
            "knowledgeBase": [
                {
                    "id": "t",
                    "topic": "T",
                    "subtopics": [{"id": "1", "title": "x", "content": [MARKER_TEXT]}],
                }
            ]
        },
    ],
)
def test_shape_errors_never_echo_the_offending_value(data: object) -> None:
    """A wrong-typed value is named by position only; its content is not shown."""
    with pytest.raises(KnowledgeBaseError) as excinfo:
        parse_knowledge_base(data)
    assert MARKER_TEXT not in str(excinfo.value)


def test_unreadable_file_is_rejected_without_its_content(tmp_path: Path) -> None:
    """A missing or broken file raises a typed error with no content."""
    with pytest.raises(KnowledgeBaseError, match="FileNotFoundError"):
        read_knowledge_base(tmp_path / "absent.json")
    broken = tmp_path / "broken.json"
    broken.write_text(f"{{{MARKER_TEXT}", encoding="utf-8")
    with pytest.raises(KnowledgeBaseError) as excinfo:
        read_knowledge_base(broken)
    assert MARKER_TEXT not in str(excinfo.value)


def test_point_ids_are_stable_per_subtopic() -> None:
    """The same subtopic id always maps to the same point id."""
    first = subtopic_point_id("1.1")
    again = subtopic_point_id("1.1")
    other = subtopic_point_id("1.2")
    assert first == again
    assert first != other


def test_point_ids_are_pinned() -> None:
    """A change to the namespace or derivation would orphan every stored point."""
    assert subtopic_point_id("1.1") == "1d56a936-43ba-5a46-8d4e-9960dc530361"


# --------------------------------------------------------------------------- #
# Indexing
# --------------------------------------------------------------------------- #


def test_indexes_one_point_per_subtopic_with_its_payload(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """Each subtopic with content becomes one point with id, title and topic."""
    report = indexer.run(write_kb(tmp_path, BASE))
    assert (report.indexed, report.removed, report.skipped_blank) == (2, 0, 1)
    payloads = stored(qdrant)
    assert set(payloads) == {"1.1", "1.2"}
    first = payloads["1.1"]
    assert first["title"] == "Basic widget credit"
    assert first["topic"] == "Widget Credits"
    assert first["topic_id"] == "topic-1"
    assert first["text"] == "A widget credit applies per widget."
    assert first["embedding_model"] == MODEL
    assert first["embedded_at"] == STAMP


def test_each_point_carries_its_own_topic(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """Points from different topics keep their own topic name and id."""
    data = knowledge_base(("1.1", "First", "one"), topic="Alpha")
    data["knowledgeBase"].append(
        {
            "id": "topic-2",
            "topic": "Beta",
            "subtopics": [{"id": "2.1", "title": "Second", "content": "two"}],
        }
    )
    indexer.run(write_kb(tmp_path, data))
    payloads = stored(qdrant)
    assert (payloads["1.1"]["topic"], payloads["1.1"]["topic_id"]) == (
        "Alpha",
        "topic-1",
    )
    assert (payloads["2.1"]["topic"], payloads["2.1"]["topic_id"]) == (
        "Beta",
        "topic-2",
    )


def test_collection_has_dense_and_sparse_slots(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """The tax-law collection is created with both named vectors."""
    indexer.run(write_kb(tmp_path, BASE))
    params = qdrant.get_collection(TAX_LAW_COLLECTION).config.params
    assert isinstance(params.vectors, dict)
    assert DENSE_VECTOR in params.vectors
    assert params.sparse_vectors is not None
    assert SPARSE_VECTOR in params.sparse_vectors


def test_subtopics_are_embedded_as_documents_without_the_query_prefix(
    indexer: TaxLawIndexer, service: FakeEmbeddingService, tmp_path: Path
) -> None:
    """The embedded text is title and content, with no query prefix."""
    indexer.run(write_kb(tmp_path, BASE))
    sent = service.requests[0]["body"]["input"]
    assert sent == [
        "Basic widget credit\nA widget credit applies per widget.",
        "Widget credit limits\nThe widget credit has a yearly cap.",
    ]
    assert not any(QUERY_PREFIX in text for text in sent)


def test_rerun_drops_removed_subtopics_and_updates_changed_ones(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """A removed subtopic loses its point; a changed one is replaced in place."""
    indexer.run(write_kb(tmp_path, BASE))
    changed = knowledge_base(
        ("1.1", "Basic widget credit", "Revised widget credit text."),
        ("1.4", "New widget rule", "A new widget rule."),
    )
    report = indexer.run(write_kb(tmp_path, changed))
    assert (report.indexed, report.removed) == (2, 1)
    payloads = stored(qdrant)
    assert set(payloads) == {"1.1", "1.4"}
    assert payloads["1.1"]["text"] == "Revised widget credit text."
    assert qdrant.count(TAX_LAW_COLLECTION).count == 2


def test_subtopic_blanked_on_rerun_loses_its_point(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """A subtopic whose content becomes blank is dropped from the collection."""
    indexer.run(write_kb(tmp_path, BASE))
    blanked = knowledge_base(
        ("1.1", "Basic widget credit", "A widget credit applies per widget."),
        ("1.2", "Widget credit limits", ""),
    )
    indexer.run(write_kb(tmp_path, blanked))
    assert set(stored(qdrant)) == {"1.1"}


def test_duplicate_ids_change_nothing(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """A file with duplicate ids is refused before any write."""
    indexer.run(write_kb(tmp_path, BASE))
    duplicate = knowledge_base(("1.1", "A", "one"), ("1.1", "B", "two"))
    path = write_kb(tmp_path, duplicate)
    with pytest.raises(KnowledgeBaseError):
        indexer.run(path)
    assert set(stored(qdrant)) == {"1.1", "1.2"}


def test_file_with_no_content_is_refused_and_keeps_points(
    indexer: TaxLawIndexer, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """A file with nothing to index never empties the collection."""
    indexer.run(write_kb(tmp_path, BASE))
    blank = write_kb(tmp_path, knowledge_base(("1.1", "A", " ")))
    with pytest.raises(KnowledgeBaseError, match="no subtopic has content"):
        indexer.run(blank)
    assert set(stored(qdrant)) == {"1.1", "1.2"}


def test_embedding_failure_keeps_points_and_hides_text(
    indexer: TaxLawIndexer,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    tmp_path: Path,
) -> None:
    """A failed embedding leaves the collection as it was."""
    indexer.run(write_kb(tmp_path, BASE))
    service.fail_with = 500
    marked = write_kb(tmp_path, knowledge_base(("9.9", "Hidden", MARKER_TEXT)))
    with pytest.raises(tax_law_module.EmbeddingError) as excinfo:
        indexer.run(marked)
    assert MARKER_TEXT not in str(excinfo.value)
    assert set(stored(qdrant)) == {"1.1", "1.2"}


def _qdrant_refusal(*_args: object, **_kwargs: object) -> None:
    """Fail like a Qdrant server error."""
    raise UnexpectedResponse(500, "error", b"", httpx.Headers())


def test_failed_upsert_leaves_the_old_points(
    indexer: TaxLawIndexer,
    qdrant: QdrantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New points go in before any delete, so a failed write loses nothing."""
    indexer.run(write_kb(tmp_path, BASE))
    changed = knowledge_base(
        ("1.1", "Basic widget credit", "Revised widget credit text."),
        ("1.4", "New widget rule", "A new widget rule."),
    )
    monkeypatch.setattr(qdrant, "upsert", _qdrant_refusal)
    with pytest.raises(UnexpectedResponse):
        indexer.run(write_kb(tmp_path, changed))
    payloads = stored(qdrant)
    assert set(payloads) == {"1.1", "1.2"}
    assert payloads["1.1"]["text"] == "A widget credit applies per widget."


def test_failed_delete_keeps_the_new_points(
    indexer: TaxLawIndexer,
    qdrant: QdrantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed delete leaves new and old points mixed; a rerun cleans up."""
    indexer.run(write_kb(tmp_path, BASE))
    changed = write_kb(
        tmp_path,
        knowledge_base(
            ("1.1", "Basic widget credit", "Revised widget credit text."),
            ("1.4", "New widget rule", "A new widget rule."),
        ),
    )
    monkeypatch.setattr(qdrant, "delete", _qdrant_refusal)
    with pytest.raises(UnexpectedResponse):
        indexer.run(changed)
    payloads = stored(qdrant)
    assert set(payloads) == {"1.1", "1.2", "1.4"}
    assert payloads["1.1"]["text"] == "Revised widget credit text."
    monkeypatch.undo()
    report = indexer.run(changed)
    assert report.removed == 1
    assert set(stored(qdrant)) == {"1.1", "1.4"}


@pytest.mark.parametrize(("before", "after"), [(10, 2), (REMOVAL_GUARD_MIN + 1, 1)])
def test_file_that_would_remove_most_points_is_refused(
    indexer: TaxLawIndexer,
    qdrant: QdrantClient,
    tmp_path: Path,
    before: int,
    after: int,
) -> None:
    """A valid file that would delete most stored points changes nothing."""
    indexer.run(write_kb(tmp_path, numbered(before)))
    shrunk = write_kb(tmp_path, numbered(after))
    removing = before - after
    with pytest.raises(
        KnowledgeBaseError, match=rf"would remove {removing} of {before}"
    ) as excinfo:
        indexer.run(shrunk)
    assert "delete the tax-law collection" in str(excinfo.value)
    assert len(stored(qdrant)) == before


@pytest.mark.parametrize(
    ("before", "after"),
    [(10, 5), (REMOVAL_GUARD_MIN, 1), (3, 1)],
)
def test_removals_at_or_under_the_guard_go_through(
    indexer: TaxLawIndexer,
    qdrant: QdrantClient,
    tmp_path: Path,
    before: int,
    after: int,
) -> None:
    """Half the points, or fewer than the minimum, can still be removed."""
    indexer.run(write_kb(tmp_path, numbered(before)))
    report = indexer.run(write_kb(tmp_path, numbered(after)))
    assert report.removed == before - after
    assert len(stored(qdrant)) == after


# --------------------------------------------------------------------------- #
# Command
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
def command_env(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    tmp_path: Path,
) -> pytest.MonkeyPatch:
    """Configure every setting and route the command to the fakes."""
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBED_BASE_URL", "http://embed.test")
    monkeypatch.setenv("EMBED_API_KEY", service.key)
    monkeypatch.setenv("EMBEDDING_MODEL", MODEL)
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")
    monkeypatch.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))
    monkeypatch.setenv("TAX_LAW_PATH", str(write_kb(tmp_path, BASE)))
    monkeypatch.setattr(tax_law_module, "make_client", lambda _connection: qdrant)
    # main() closes its client; keep the in-process one open for assertions.
    monkeypatch.setattr(qdrant, "close", lambda: None)
    real_client = tax_law_module.EmbeddingClient

    def fake_embedding_client(connection: EmbeddingConnection) -> EmbeddingClient:
        """Build the real client on the fake transport."""
        return real_client(connection, transport=service.transport())

    monkeypatch.setattr(tax_law_module, "EmbeddingClient", fake_embedding_client)
    return monkeypatch


def test_main_indexes_and_exits_zero(
    command_env: pytest.MonkeyPatch, qdrant: QdrantClient
) -> None:
    """Main indexes the configured file, exits zero and closes the client."""
    closed: list[bool] = []
    command_env.setattr(qdrant, "close", lambda: closed.append(True))
    with capture_logs() as logs:
        assert main() == 0
    assert closed == [True]
    finished = logs[-1]
    assert finished["event"] == "tax_law_index_finished"
    assert (finished["indexed"], finished["skipped_blank"]) == (2, 1)


def test_main_is_off_without_the_path(command_env: pytest.MonkeyPatch) -> None:
    """With TAX_LAW_PATH unset the command does nothing and says so."""
    command_env.delenv("TAX_LAW_PATH")
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "tax_law_not_connected"
    assert logs[0]["log_level"] == "warning"


def test_main_is_off_without_qdrant(command_env: pytest.MonkeyPatch) -> None:
    """With Qdrant unset the command does nothing."""
    command_env.delenv("QDRANT_URL")
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "tax_law_not_connected"


def test_main_refuses_a_url_without_its_key(command_env: pytest.MonkeyPatch) -> None:
    """A URL without its key is a configuration error naming the variable."""
    command_env.delenv("EMBED_API_KEY")
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "tax_law_misconfigured"
    assert "EMBED_API_KEY" in logs[0]["reason"]


def test_main_exits_one_for_a_bad_file(
    command_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unusable knowledge base exits one and logs the reason."""
    duplicate = knowledge_base(("1", "A", "x"), ("1", "B", "y"))
    command_env.setenv("TAX_LAW_PATH", str(write_kb(tmp_path, duplicate)))
    with capture_logs() as logs:
        assert main() == 1
    assert logs[-1]["event"] == "tax_law_index_failed"


def test_main_exits_one_when_embedding_fails(
    command_env: pytest.MonkeyPatch, service: FakeEmbeddingService
) -> None:
    """An embedding failure exits one."""
    del command_env
    service.fail_with = 503
    with capture_logs() as logs:
        assert main() == 1
    assert logs[-1]["reason"] == "Embedding service returned HTTP 503"


def test_main_exits_one_without_leaking_content_when_embedding_fails(
    command_env: pytest.MonkeyPatch, service: FakeEmbeddingService, tmp_path: Path
) -> None:
    """The fake echoes its input in the error body; neither logs nor result show it."""
    marked = write_kb(tmp_path, knowledge_base(("9.9", "Hidden", MARKER_TEXT)))
    command_env.setenv("TAX_LAW_PATH", str(marked))
    service.fail_with = 500
    with capture_logs() as logs:
        assert main() == 1
    assert MARKER_TEXT in json.dumps(service.requests)
    assert MARKER_TEXT not in repr(logs)


@pytest.mark.parametrize("status", [401, 403])
def test_main_stops_when_the_embedding_key_is_refused(
    command_env: pytest.MonkeyPatch,
    service: FakeEmbeddingService,
    qdrant: QdrantClient,
    status: int,
) -> None:
    """A refused key exits two like the document indexer, and writes nothing."""
    del command_env
    service.fail_with = status
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["event"] == "tax_law_index_stopped"
    assert logs[-1]["reason"] == f"Embedding service returned HTTP {status}"
    assert stored(qdrant) == {}


def test_main_exits_one_for_a_file_that_would_remove_most_points(
    command_env: pytest.MonkeyPatch, qdrant: QdrantClient, tmp_path: Path
) -> None:
    """A refused shrink exits one and leaves the collection alone."""
    command_env.setenv("TAX_LAW_PATH", str(write_kb(tmp_path, numbered(10))))
    assert main() == 0
    command_env.setenv("TAX_LAW_PATH", str(write_kb(tmp_path, numbered(2))))
    with capture_logs() as logs:
        assert main() == 1
    assert logs[-1]["event"] == "tax_law_index_failed"
    assert len(stored(qdrant)) == 10


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("QDRANT_URL", "ftp://qdrant.test"),
        ("QDRANT_URL", "qdrant.test:6333"),
        ("QDRANT_URL", f"http://user:{USERINFO_SECRET}@qdrant.test:99999"),
        ("QDRANT_URL", f"http://user:{USERINFO_SECRET}@qdrant.test:bad"),
        ("EMBED_BASE_URL", "embed.test"),
        ("EMBED_BASE_URL", f"http://user:{USERINFO_SECRET}@embed.test:99999"),
    ],
)
def test_main_refuses_a_malformed_url_with_the_real_client_factory(
    command_env: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """A malformed URL exits two naming the variable, never raising or echoing it."""
    command_env.setattr(tax_law_module, "make_client", make_client)
    command_env.setenv(variable, value)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "tax_law_misconfigured"
    assert variable in logs[0]["reason"]
    assert USERINFO_SECRET not in repr(logs)
    assert value not in repr(logs)


def test_main_stops_when_qdrant_fails(command_env: pytest.MonkeyPatch) -> None:
    """A Qdrant error exits two."""

    def broken(*_args: object, **_kwargs: object) -> bool:
        """Fail like a Qdrant server error."""
        raise UnexpectedResponse(500, "error", b"", httpx.Headers())

    command_env.setattr(tax_law_module, "ensure_collection", broken)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["reason"] == "Qdrant request failed: UnexpectedResponse"


def test_stale_points_are_found_across_scroll_pages(
    indexer: TaxLawIndexer,
    qdrant: QdrantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal reads every page of stored points, not just the first."""
    monkeypatch.setattr(tax_law_module, "_SCROLL_PAGE", 1)
    indexer.run(write_kb(tmp_path, numbered(6)))
    # Four of six points are stale; with one point per page, a reader that
    # stops after the first page can find at most one of them.
    report = indexer.run(write_kb(tmp_path, numbered(2)))
    assert report.removed == 4
    assert set(stored(qdrant)) == {"n1", "n2"}


def test_main_refuses_a_timeout_that_cannot_be_parsed(
    command_env: pytest.MonkeyPatch,
) -> None:
    """A bad setting value exits two naming the variable, never raising."""
    command_env.setenv("EMBED_TIMEOUT_SECONDS", "soon")
    with capture_logs() as logs:
        assert main() == 2
    assert logs[0]["event"] == "tax_law_misconfigured"
    assert "EMBED_TIMEOUT_SECONDS" in logs[0]["reason"]
    assert "soon" not in repr(logs)


def test_main_stops_when_the_collection_schema_is_wrong(
    command_env: pytest.MonkeyPatch,
) -> None:
    """A collection with the wrong vector slots exits two."""

    def wrong_schema(*_args: object, **_kwargs: object) -> bool:
        """Fail like the schema check."""
        msg = "collection has the wrong vector slots"
        raise VectorStoreError(msg)

    command_env.setattr(tax_law_module, "ensure_collection", wrong_schema)
    with capture_logs() as logs:
        assert main() == 2
    assert logs[-1]["event"] == "tax_law_index_stopped"
    assert logs[-1]["reason"] == "collection has the wrong vector slots"


def test_upserts_go_up_in_bounded_batches(
    indexer: TaxLawIndexer,
    qdrant: QdrantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Points are written in batches no larger than the upsert batch size."""
    monkeypatch.setattr(tax_law_module, "UPSERT_BATCH_SIZE", 1)
    with patch.object(qdrant, "upsert", wraps=qdrant.upsert) as upsert:
        indexer.run(write_kb(tmp_path, BASE))
    sizes = [len(call.kwargs["points"]) for call in upsert.call_args_list]
    assert sizes == [1, 1]
    assert set(stored(qdrant)) == {"1.1", "1.2"}
