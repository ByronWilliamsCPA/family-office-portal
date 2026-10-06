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
  value present (on the set and on every chunk) is exactly ``true``. A
  missing value counts as false, so a set with no chunks and no set-level
  value is not consented.
* A set is a tax return when any ``category`` is ``Tax Returns`` or any
  ``document_type`` is a tax-return type, on the set or on any chunk.
* ``is_confidential`` is stored as given when it is a boolean; any other
  value, including a missing one, is stored as ``true``.
* ``trust_score``, ``ocr_engine_provenance`` and ``hallucination_risk`` are
  copied as given. A null stays null ("not scored"); nothing is recomputed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

TAX_RETURN_CATEGORY = "Tax Returns"
TAX_RETURN_DOCUMENT_TYPES = frozenset({"tax_return", "tax_election"})

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
        payload (dict[str, object]): Point payload without the embedding
            fields, which the writer adds.
    """

    index: int
    text: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ChunkSet:
    """A parsed chunk set and the document-level values the indexer uses.

    Attributes:
        document_id (str): Document the set belongs to.
        sha256 (str | None): Document hash, or None when absent.
        consent_on_file (bool): True only when consent is exactly true
            everywhere it appears.
        is_tax_return (bool): True when any category or document type marks
            the set as a tax return.
        chunks (tuple[Chunk, ...]): Chunks with non-blank text, in order.
    """

    document_id: str
    sha256: str | None
    consent_on_file: bool
    is_tax_return: bool
    chunks: tuple[Chunk, ...]

    @property
    def may_be_indexed(self) -> bool:
        """Say whether consent rules allow indexing this set.

        Returns:
            bool: False for a tax return without consent on file.
        """
        return self.consent_on_file or not self.is_tax_return


def _as_object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{what} is not a JSON object"
        raise ChunkSetError(msg)
    return cast("dict[str, object]", value)


def _resolve_sha256(document: JsonObject, chunks: list[JsonObject]) -> str | None:
    values: set[str] = set()
    for source in (document, *chunks):
        value = source.get("sha256")
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
        elif value is not None:
            msg = "sha256 is not a string"
            raise ChunkSetError(msg)
    if len(values) > 1:
        msg = "chunks disagree on sha256"
        raise ChunkSetError(msg)
    return values.pop() if values else None


def _resolve_consent(document: JsonObject, chunks: list[JsonObject]) -> bool:
    # #CRITICAL: security: consent fails closed; a missing value is false.
    # #VERIFY: tests/unit/test_chunk_sets.py covers missing, null, string and
    # mixed values.
    seen: list[object] = []
    set_value = document.get("consent_on_file", _MISSING)
    if set_value is not _MISSING:
        seen.append(set_value)
    seen.extend(chunk.get("consent_on_file", False) for chunk in chunks)
    return bool(seen) and all(value is True for value in seen)


def _is_tax_return(document: JsonObject, chunks: list[JsonObject]) -> bool:
    for source in (document, *chunks):
        category = source.get("category")
        if (
            isinstance(category, str)
            and category.strip().casefold() == TAX_RETURN_CATEGORY.casefold()
        ):
            return True
        document_type = source.get("document_type")
        if (
            isinstance(document_type, str)
            and document_type.strip().casefold() in TAX_RETURN_DOCUMENT_TYPES
        ):
            return True
    return False


def _chunk_payload(
    document_id: str,
    document: dict[str, object],
    chunk: dict[str, object],
    index: int,
    resolved: tuple[str | None, bool],
) -> dict[str, object]:
    sha256, consent = resolved
    payload: dict[str, object] = {
        "document_id": document_id,
        "chunk_index": index,
    }
    for name in CHUNK_CONTRACT_FIELDS:
        payload[name] = chunk.get(name, document.get(name))
    for name in DOCUMENT_FIELDS:
        payload[name] = chunk.get(name, document.get(name))
    confidential = chunk.get("is_confidential", document.get("is_confidential"))
    # #CRITICAL: security: anything but a real boolean is stored as
    # confidential, so viewers never see it. #VERIFY:
    # tests/unit/test_chunk_sets.py::test_is_confidential_fails_closed.
    payload["is_confidential"] = (
        confidential if isinstance(confidential, bool) else True
    )
    payload["sha256"] = sha256
    payload["consent_on_file"] = consent
    return payload


def parse_chunk_set(raw: object, document_id: str) -> ChunkSet:
    """Check a decoded chunk set and resolve its document-level values.

    Args:
        raw (object): The decoded JSON document.
        document_id (str): Document ID taken from the file name.

    Returns:
        ChunkSet: The parsed set; chunks with blank text are left out.

    Raises:
        ChunkSetError: If the shape is wrong, an ID does not match the file
            name, or the chunks disagree on the document hash.
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

    sha256 = _resolve_sha256(document, chunks)
    consent = _resolve_consent(document, chunks)
    parsed = tuple(
        Chunk(
            index=position,
            text=cast("str", chunk.get("text", "")),
            payload=_chunk_payload(
                document_id, document, chunk, position, (sha256, consent)
            ),
        )
        for position, chunk in enumerate(chunks)
        if cast("str", chunk.get("text", "")).strip()
    )
    return ChunkSet(
        document_id=document_id,
        sha256=sha256,
        consent_on_file=consent,
        is_tax_return=_is_tax_return(document, chunks),
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
    except (OSError, UnicodeDecodeError, ValueError) as exc:
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
