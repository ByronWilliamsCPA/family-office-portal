# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for reading chunk-set files and their fail-closed rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from app.retrieval.chunk_sets import (
    ChunkSetError,
    list_chunk_set_files,
    parse_chunk_set,
    read_chunk_set,
)
from tests.unit.chunk_set_factory import DOC_A, DOC_B, chunk_set, write_chunk_set

if TYPE_CHECKING:
    from pathlib import Path

CONTRACT_FIELDS = {
    "chunk_id",
    "document_id",
    "trace_id",
    "trust_score",
    "ocr_engine_provenance",
    "page_range",
    "section_hierarchy",
    "hallucination_risk",
}
FAMILY_FIELDS = {
    "entity_id",
    "document_type",
    "category",
    "is_confidential",
    "title",
    "document_date",
    "sha256",
    "consent_on_file",
}


def test_payload_keeps_contract_and_family_fields() -> None:
    """Payload keeps contract and family fields."""
    parsed = parse_chunk_set(chunk_set(), DOC_A)
    assert parsed.document_id == DOC_A
    assert parsed.sha256 == "a" * 64
    assert [chunk.index for chunk in parsed.chunks] == [0, 1]
    for chunk in parsed.chunks:
        assert chunk.payload.keys() >= CONTRACT_FIELDS | FAMILY_FIELDS
        assert chunk.payload["document_id"] == DOC_A
        assert chunk.payload["chunk_index"] == chunk.index
        assert chunk.payload["page_range"] == [chunk.index + 1, chunk.index + 1]
        assert chunk.payload["title"] == "Synthetic Report"
        assert "text" not in chunk.payload  # the writer adds it


def test_null_trust_fields_stay_null() -> None:
    """Null trust fields stay null."""
    for chunk in parse_chunk_set(chunk_set(), DOC_A).chunks:
        assert chunk.payload["trust_score"] is None
        assert chunk.payload["ocr_engine_provenance"] is None
        assert chunk.payload["hallucination_risk"] is None


def test_missing_trust_fields_are_stored_as_null() -> None:
    """Missing trust fields are stored as null."""
    data = chunk_set(trust_score=..., hallucination_risk=...)
    for chunk in parse_chunk_set(data, DOC_A).chunks:
        assert chunk.payload["trust_score"] is None
        assert chunk.payload["hallucination_risk"] is None


def test_scored_trust_fields_are_kept_as_given() -> None:
    """Scored trust fields are kept as given."""
    data = chunk_set(trust_score=0.42, hallucination_risk="low")
    chunk = parse_chunk_set(data, DOC_A).chunks[0]
    assert chunk.payload["trust_score"] == 0.42
    assert chunk.payload["hallucination_risk"] == "low"


def test_set_level_fields_fill_in_for_chunks() -> None:
    """Set level fields fill in for chunks."""
    data = chunk_set(trace_id=...)
    for chunk in data["chunks"]:
        del chunk["category"]
    data["category"] = "Trusts"
    chunk = parse_chunk_set(data, DOC_A).chunks[0]
    assert chunk.payload["trace_id"] == f"trace-{DOC_A}"
    assert chunk.payload["category"] == "Trusts"


def test_blank_chunks_are_left_out_but_keep_positions() -> None:
    """Blank chunks are left out but keep positions."""
    parsed = parse_chunk_set(chunk_set(texts=("one", "  ", "three")), DOC_A)
    assert [chunk.index for chunk in parsed.chunks] == [0, 2]
    assert [chunk.text for chunk in parsed.chunks] == ["one", "three"]


@pytest.mark.parametrize(
    ("value", "stored"),
    [(False, False), (True, True), (None, True), ("false", True), (0, True)],
)
def test_is_confidential_fails_closed(value: object, *, stored: bool) -> None:
    """Is confidential fails closed."""
    chunk = parse_chunk_set(chunk_set(is_confidential=value), DOC_A).chunks[0]
    assert chunk.payload["is_confidential"] is stored


def test_missing_is_confidential_is_stored_as_confidential() -> None:
    """Missing is confidential is stored as confidential."""
    chunk = parse_chunk_set(chunk_set(is_confidential=...), DOC_A).chunks[0]
    assert chunk.payload["is_confidential"] is True


def test_chunk_false_does_not_override_a_set_level_true() -> None:
    """A chunk-level false never overrides a set-level true."""
    data = chunk_set(is_confidential=False)
    data["is_confidential"] = True
    parsed = parse_chunk_set(data, DOC_A)
    assert parsed.is_confidential is True
    assert all(c.payload["is_confidential"] is True for c in parsed.chunks)


def test_one_confidential_chunk_makes_the_document_confidential() -> None:
    """One chunk marked confidential makes every point confidential."""
    data = chunk_set(is_confidential=False)
    data["chunks"][0]["is_confidential"] = True
    parsed = parse_chunk_set(data, DOC_A)
    assert [c.payload["is_confidential"] for c in parsed.chunks] == [True, True]


def test_set_level_false_fills_in_for_chunks_without_a_value() -> None:
    """A set-level false covers chunks that carry no value of their own."""
    data = chunk_set(is_confidential=...)
    data["is_confidential"] = False
    assert parse_chunk_set(data, DOC_A).is_confidential is False


def test_one_chunk_without_any_value_makes_the_document_confidential() -> None:
    """A chunk with no value and no set-level value fails closed."""
    data = chunk_set(is_confidential=False)
    del data["chunks"][1]["is_confidential"]
    parsed = parse_chunk_set(data, DOC_A)
    assert all(c.payload["is_confidential"] is True for c in parsed.chunks)


def test_payload_is_read_only() -> None:
    """The parsed payload cannot be changed in place."""
    chunk = parse_chunk_set(chunk_set(), DOC_A).chunks[0]
    with pytest.raises(TypeError):
        cast("dict[str, object]", chunk.payload)["is_confidential"] = False


# --------------------------------------------------------------------------- #
# Consent and tax returns
# --------------------------------------------------------------------------- #


def test_consent_true_on_every_chunk_is_granted() -> None:
    """Consent true on every chunk is granted."""
    parsed = parse_chunk_set(chunk_set(consent=True), DOC_A)
    assert parsed.consent_on_file is True
    assert all(c.payload["consent_on_file"] is True for c in parsed.chunks)


@pytest.mark.parametrize("value", [False, None, "true", 1, "yes"])
def test_consent_must_be_exactly_true(value: object) -> None:
    """Consent must be exactly true."""
    assert parse_chunk_set(chunk_set(consent=value), DOC_A).consent_on_file is False


def test_missing_consent_counts_as_false() -> None:
    """Missing consent counts as false."""
    parsed = parse_chunk_set(chunk_set(consent_on_file=...), DOC_A)
    assert parsed.consent_on_file is False


def test_one_chunk_without_consent_withholds_it() -> None:
    """One chunk without consent withholds it."""
    data = chunk_set(consent=True)
    data["chunks"][1]["consent_on_file"] = False
    assert parse_chunk_set(data, DOC_A).consent_on_file is False


def test_set_level_consent_must_agree_with_chunks() -> None:
    """Set level consent must agree with chunks."""
    data = chunk_set(consent=True, set_consent=False)
    assert parse_chunk_set(data, DOC_A).consent_on_file is False


def test_missing_set_level_consent_lets_the_chunks_decide() -> None:
    """With no set-level value, every chunk true grants consent."""
    data = chunk_set(consent=True)
    assert "consent_on_file" not in data
    assert parse_chunk_set(data, DOC_A).consent_on_file is True


def test_empty_set_without_set_level_consent_is_not_consented() -> None:
    """Empty set without set level consent is not consented."""
    assert parse_chunk_set(chunk_set(texts=()), DOC_A).consent_on_file is False


def test_empty_set_with_set_level_consent_is_consented() -> None:
    """Empty set with set level consent is consented."""
    data = chunk_set(texts=(), set_consent=True)
    assert parse_chunk_set(data, DOC_A).consent_on_file is True


@pytest.mark.parametrize(
    ("category", "document_type", "is_tax"),
    [
        ("Tax Returns", "other", True),
        ("  tax returns ", "other", True),
        ("Other", "tax_return", True),
        ("Other", "tax_election", True),
        ("Tax Return", "other", True),
        ("tax-returns", "other", True),
        ("Other", "Tax Return", True),
        ("Other", "TAX-ELECTION", True),
        ("Other", "tax_elections", True),
        ("LLCs", "operating_agreement", False),
        ("Taxes", "tax_summary", False),
    ],
)
def test_tax_returns_are_recognized(
    category: str, document_type: str, *, is_tax: bool
) -> None:
    """Tax returns are recognized."""
    data = chunk_set(category=category, document_type=document_type)
    parsed = parse_chunk_set(data, DOC_A)
    assert parsed.is_tax_return is is_tax
    assert parsed.may_be_indexed is not is_tax


def test_set_level_tax_category_counts_even_with_no_chunks() -> None:
    """Set level tax category counts even with no chunks."""
    data = chunk_set(texts=())
    data["category"] = "Tax Returns"
    parsed = parse_chunk_set(data, DOC_A)
    assert parsed.is_tax_return is True
    assert parsed.may_be_indexed is False


@pytest.mark.parametrize("value", [5, ["Tax Returns"], {"name": "x"}, True])
def test_non_string_classification_needs_consent(value: object) -> None:
    """A category or type that is not a string is treated as a tax return."""
    assert parse_chunk_set(chunk_set(category=value), DOC_A).is_tax_return is True
    data = chunk_set(document_type=value)
    assert parse_chunk_set(data, DOC_A).is_tax_return is True


def test_missing_classification_needs_consent() -> None:
    """A set with no category or type anywhere is treated as a tax return."""
    data = chunk_set(category=..., document_type=...)
    parsed = parse_chunk_set(data, DOC_A)
    assert parsed.is_tax_return is True
    assert parsed.may_be_indexed is False


def test_blank_classification_needs_consent() -> None:
    """Blank strings do not count as a classification."""
    data = chunk_set(category="  ", document_type="")
    assert parse_chunk_set(data, DOC_A).is_tax_return is True


def test_tax_flag_is_stored_on_every_point() -> None:
    """The resolved tax-return flag is stored on every point."""
    data = chunk_set(category="Tax Returns", consent=True)
    parsed = parse_chunk_set(data, DOC_A)
    assert all(c.payload["is_tax_return"] is True for c in parsed.chunks)
    other = parse_chunk_set(chunk_set(), DOC_A)
    assert all(c.payload["is_tax_return"] is False for c in other.chunks)


def test_consented_tax_return_may_be_indexed() -> None:
    """Consented tax return may be indexed."""
    data = chunk_set(category="Tax Returns", consent=True)
    assert parse_chunk_set(data, DOC_A).may_be_indexed is True


# --------------------------------------------------------------------------- #
# Shape errors
# --------------------------------------------------------------------------- #


def test_document_id_must_match_the_file_name() -> None:
    """Document id must match the file name."""
    with pytest.raises(ChunkSetError, match="file name"):
        parse_chunk_set(chunk_set(DOC_B), DOC_A)


def test_chunk_from_another_document_is_an_error() -> None:
    """Chunk from another document is an error."""
    data = chunk_set()
    data["chunks"][1]["document_id"] = DOC_B
    with pytest.raises(ChunkSetError, match="another document"):
        parse_chunk_set(data, DOC_A)


def test_chunks_that_disagree_on_sha256_are_an_error() -> None:
    """Chunks that disagree on sha256 are an error."""
    data = chunk_set()
    data["chunks"][1]["sha256"] = "b" * 64
    with pytest.raises(ChunkSetError, match="sha256"):
        parse_chunk_set(data, DOC_A)


def test_non_string_sha256_is_an_error() -> None:
    """Non string sha256 is an error."""
    with pytest.raises(ChunkSetError, match="sha256"):
        parse_chunk_set(chunk_set() | {"sha256": 12}, DOC_A)


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_sha256_counts_as_absent(blank: str) -> None:
    """A blank sha256 counts as absent rather than as a type error."""
    assert parse_chunk_set(chunk_set(sha256=blank), DOC_A).sha256 is None


def test_set_level_sha256_is_used_when_chunks_have_none() -> None:
    """A set-level sha256 is used when the chunks carry none."""
    data = chunk_set(sha256=...)
    data["sha256"] = "e" * 64
    assert parse_chunk_set(data, DOC_A).sha256 == "e" * 64


def test_set_level_sha256_must_agree_with_chunks() -> None:
    """A set-level sha256 that differs from the chunks' is an error."""
    data = chunk_set()
    data["sha256"] = "e" * 64
    with pytest.raises(ChunkSetError, match="disagree"):
        parse_chunk_set(data, DOC_A)


def test_missing_sha256_is_none() -> None:
    """Missing sha256 is none."""
    data = chunk_set()
    for chunk in data["chunks"]:
        del chunk["sha256"]
    assert parse_chunk_set(data, DOC_A).sha256 is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "not a JSON object"),
        ({"document_id": DOC_A}, "chunks is not a list"),
        ({"document_id": DOC_A, "chunks": ["x"]}, "chunk 0 is not a JSON object"),
        ({"document_id": DOC_A, "chunks": [{"text": 5}]}, "text is not a string"),
    ],
)
def test_bad_shapes_are_errors(raw: object, message: str) -> None:
    """Bad shapes are errors."""
    with pytest.raises(ChunkSetError, match=message):
        parse_chunk_set(raw, DOC_A)


def test_error_messages_never_include_chunk_text() -> None:
    """Error messages never include chunk text."""
    data = chunk_set(texts=("secret words here",))
    data["chunks"][0]["sha256"] = 7
    with pytest.raises(ChunkSetError) as excinfo:
        parse_chunk_set(data, DOC_A)
    assert "secret" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def test_read_chunk_set_uses_the_file_name(tmp_path: Path) -> None:
    """Read chunk set uses the file name."""
    path = write_chunk_set(tmp_path, chunk_set())
    assert read_chunk_set(path).document_id == DOC_A


def test_unreadable_json_is_an_error(tmp_path: Path) -> None:
    """Unreadable json is an error."""
    path = tmp_path / f"{DOC_A}.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ChunkSetError, match="JSONDecodeError"):
        read_chunk_set(path)


def test_deeply_nested_json_is_an_error(tmp_path: Path) -> None:
    """JSON nested too deeply to decode is an error, not a crash."""
    path = tmp_path / f"{DOC_A}.json"
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    with pytest.raises(ChunkSetError, match="RecursionError"):
        read_chunk_set(path)


def test_list_skips_hidden_temporary_and_other_files(tmp_path: Path) -> None:
    """List skips hidden temporary and other files."""
    write_chunk_set(tmp_path, chunk_set(DOC_B))
    write_chunk_set(tmp_path, chunk_set(DOC_A))
    (tmp_path / f".{DOC_A}.abc.tmp").write_text("{}", encoding="utf-8")
    (tmp_path / ".hidden.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "folder.json").mkdir()
    names = [path.name for path in list_chunk_set_files(tmp_path)]
    assert names == [f"{DOC_A}.json", f"{DOC_B}.json"]
