# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Read one chunk-set file (``RAGChunkSet.json``) into typed values.

The document pipeline writes one file per document, named
``<document_id>.json``, atomically (temporary file, then rename). Each chunk
carries the pipeline's chunk-contract fields plus the family-office document
fields. This module checks the shape, resolves the document-level values the
indexer decides on, and builds the payload kept on every Qdrant point.

Fail-closed rules applied here:

* Consent for a tax return is granted only when every ``consent_on_file``
  value is exactly ``true``. Every chunk counts, and a chunk without the
  field counts as false. A set-level value counts when present; when it is
  absent the chunks decide. So a set with no chunks needs a set-level
  ``true``, and a set with no chunks and no set-level value is not consented.
* A set is a tax return when any ``category`` or ``document_type``, on the
  set or on any chunk, names a tax return or tax election. Names are compared
  without case, and spaces and hyphens count as underscores, so ``Tax
  Returns``, ``tax-return`` and ``TAX_ELECTION`` all match. A value that is
  present but not a string, or a set where neither field is given anywhere,
  is also treated as a tax return, so it needs consent.
* ``is_confidential`` is resolved for the whole document: it is stored as
  ``false`` only when every value given (on the set and on every chunk) is
  exactly ``false`` and each chunk has a value, its own or the set's. Any
  other combination, including a missing value, stores ``true`` on every
  point.
* ``trust_score``, ``ocr_engine_provenance`` and ``hallucination_risk`` are
  copied as given. A null stays null ("not scored"); nothing is recomputed.
* A ``sha256`` that is missing, null or blank counts as absent.

#ASSUME: data integrity: the pipeline names tax documents with the category
``Tax Returns`` or the document types ``tax_return`` and ``tax_election``,
give or take case, spaces and hyphens. Both fields are checked against the
one name set, so a tax-election ``category`` also counts. #VERIFY: compare
``TAX_DOCUMENT_NAMES`` with the pipeline's classification vocabulary
whenever that vocabulary changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

# Normalized names that mark a tax return, matched in ``category`` and in
# ``document_type`` alike.
TAX_DOCUMENT_NAMES = frozenset(
    {"tax_return", "tax_returns", "tax_election", "tax_elections"}
)

# Fields from the pipeline's chunk contract, kept on every point as given.
CHUNK_CONTRACT_FIELDS = (
    "chunk_id",
    "trace_id",
    "trust_score",
    "ocr_engine_provenance",
    "page_range",
    "section_hierarchy",
    "hallucination_risk",
    "chunk_strategy",
    "token_count",
    "source_track",
)

# Document fields carried on every chunk; a chunk value wins over the set's.
DOCUMENT_FIELDS = (
    "entity_id",
    "document_type",
    "category",
    "title",
    "document_date",
)

_MISSING = object()

JsonObject = dict[str, object]


class ChunkSetError(ValueError):
    """A chunk-set file cannot be used. Messages never include chunk text."""


@dataclass(frozen=True)
class Chunk:
    """One chunk to embed, with the payload stored next to its vector.

    Attributes:
        index (int): Position of the chunk in the set.
        text (str): Chunk text to embed.
        payload (Mapping[str, object]): Read-only point payload without the
            ``text`` and embedding fields, which the indexer adds.
    """

    index: int
    text: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ChunkSet:
    """A parsed chunk set and the document-level values the indexer uses.

    Attributes:
        document_id (str): Document the set belongs to.
        sha256 (str | None): Document hash, or None when absent.
        consent_on_file (bool): True only when consent is exactly true
            everywhere it appears.
        is_tax_return (bool): True when the set is, or may be, a tax return.
        is_confidential (bool): The document's resolved confidentiality.
        chunks (tuple[Chunk, ...]): Chunks with non-blank text, in order.
    """

    document_id: str
    sha256: str | None
    consent_on_file: bool
    is_tax_return: bool
    is_confidential: bool
    chunks: tuple[Chunk, ...]

    @property
    def may_be_indexed(self) -> bool:
        """Say whether consent rules allow indexing this set.

        Returns:
            bool: False for a tax return without consent on file.
        """
        return self.consent_on_file or not self.is_tax_return


@dataclass(frozen=True)
class _DocumentValues:
    """Document-level values copied onto every chunk payload."""

    sha256: str | None
    consent_on_file: bool
    is_tax_return: bool
    is_confidential: bool


def _as_object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{what} is not a JSON object"
        raise ChunkSetError(msg)
    return cast("dict[str, object]", value)


def _resolve_sha256(document: JsonObject, chunks: list[JsonObject]) -> str | None:
    values: set[str] = set()
    for source in (document, *chunks):
        value = source.get("sha256")
        if value is None:
            continue
        if not isinstance(value, str):
            msg = "sha256 is not a string"
            raise ChunkSetError(msg)
        if value.strip():
            values.add(value.strip())
    if len(values) > 1:
        msg = "chunks disagree on sha256"
        raise ChunkSetError(msg)
    return values.pop() if values else None


def _resolve_consent(document: JsonObject, chunks: list[JsonObject]) -> bool:
    # #CRITICAL: security: consent fails closed; a chunk without the field is
    # false, and an empty set needs a set-level true. #VERIFY:
    # tests/unit/test_chunk_sets.py covers missing, null, string and mixed
    # values, and a missing set-level value.
    seen: list[object] = []
    set_value = document.get("consent_on_file", _MISSING)
    if set_value is not _MISSING:
        seen.append(set_value)
    seen.extend(chunk.get("consent_on_file", False) for chunk in chunks)
    return bool(seen) and all(value is True for value in seen)


def _normalized(value: str) -> str:
    return value.strip().casefold().replace("-", "_").replace(" ", "_")


def _is_tax_return(document: JsonObject, chunks: list[JsonObject]) -> bool:
    # #CRITICAL: security: an unrecognized shape needs consent rather than
    # bypassing it. #VERIFY: tests/unit/test_chunk_sets.py covers variants,
    # non-string values and a set with no classification at all.
    classified = False
    for source in (document, *chunks):
        for name in ("category", "document_type"):
            value = source.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or _normalized(value) in TAX_DOCUMENT_NAMES:
                return True
            classified = classified or bool(value.strip())
    return not classified


def _resolve_confidential(document: JsonObject, chunks: list[JsonObject]) -> bool:
    # #CRITICAL: security: anything but an all-false answer is confidential,
    # so viewers never see a document one value marks private. #VERIFY:
    # tests/unit/test_chunk_sets.py::test_is_confidential_fails_closed and
    # the mixed-set tests.
    set_value = document.get("is_confidential", _MISSING)
    values = [] if set_value is _MISSING else [set_value]
    values.extend(chunk.get("is_confidential", set_value) for chunk in chunks)
    return not values or not all(value is False for value in values)


def _chunk_payload(
    document_id: str,
    document: JsonObject,
    chunk: JsonObject,
    index: int,
    resolved: _DocumentValues,
) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "document_id": document_id,
        "chunk_index": index,
    }
    for name in CHUNK_CONTRACT_FIELDS:
        payload[name] = chunk.get(name, document.get(name))
    for name in DOCUMENT_FIELDS:
        payload[name] = chunk.get(name, document.get(name))
    payload["is_confidential"] = resolved.is_confidential
    payload["is_tax_return"] = resolved.is_tax_return
    payload["sha256"] = resolved.sha256
    payload["consent_on_file"] = resolved.consent_on_file
    return MappingProxyType(payload)


def parse_chunk_set(raw: object, document_id: str) -> ChunkSet:
    """Check a decoded chunk set and resolve its document-level values.

    Args:
        raw (object): The decoded JSON document.
        document_id (str): Document ID taken from the file name.

    Returns:
        ChunkSet: The parsed set; chunks with blank text are left out.

    Raises:
        ChunkSetError: If the shape is wrong, an ID does not match the file
            name, a ``sha256`` is not a string, or the chunks disagree on the
            document hash.
    """
    document = _as_object(raw, "the chunk set")
    set_id = document.get("document_id")
    if set_id != document_id:
        msg = "document_id does not match the file name"
        raise ChunkSetError(msg)
    raw_chunks = document.get("chunks")
    if not isinstance(raw_chunks, list):
        msg = "chunks is not a list"
        raise ChunkSetError(msg)
    chunks = [
        _as_object(item, f"chunk {position}")
        for position, item in enumerate(cast("list[object]", raw_chunks))
    ]
    for position, chunk in enumerate(chunks):
        if chunk.get("document_id", document_id) != document_id:
            msg = f"chunk {position} belongs to another document"
            raise ChunkSetError(msg)
        text = chunk.get("text", "")
        if not isinstance(text, str):
            msg = f"chunk {position} text is not a string"
            raise ChunkSetError(msg)

    resolved = _DocumentValues(
        sha256=_resolve_sha256(document, chunks),
        consent_on_file=_resolve_consent(document, chunks),
        is_tax_return=_is_tax_return(document, chunks),
        is_confidential=_resolve_confidential(document, chunks),
    )
    parsed = tuple(
        Chunk(
            index=position,
            text=cast("str", chunk.get("text", "")),
            payload=_chunk_payload(document_id, document, chunk, position, resolved),
        )
        for position, chunk in enumerate(chunks)
        if cast("str", chunk.get("text", "")).strip()
    )
    return ChunkSet(
        document_id=document_id,
        sha256=resolved.sha256,
        consent_on_file=resolved.consent_on_file,
        is_tax_return=resolved.is_tax_return,
        is_confidential=resolved.is_confidential,
        chunks=parsed,
    )


def read_chunk_set(path: Path) -> ChunkSet:
    """Read and parse one chunk-set file named ``<document_id>.json``.

    Args:
        path (Path): The file to read.

    Returns:
        ChunkSet: The parsed set.

    Raises:
        ChunkSetError: If the file cannot be read or decoded, or its content
            is not a usable chunk set.
    """
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        # ValueError covers UnicodeDecodeError and JSONDecodeError;
        # RecursionError is raised for very deeply nested JSON.
        msg = f"cannot read the file: {type(exc).__name__}"
        raise ChunkSetError(msg) from exc
    return parse_chunk_set(raw, path.stem)


def list_chunk_set_files(directory: Path) -> list[Path]:
    """List chunk-set files, skipping hidden and temporary files.

    The writer stages each file under a hidden temporary name before the
    rename, so hidden files are never read.

    Args:
        directory (Path): The chunk-set directory.

    Returns:
        list[Path]: ``*.json`` files, sorted by name.
    """
    return sorted(
        path
        for path in directory.glob("*.json")
        if path.is_file() and not path.name.startswith(".")
    )
