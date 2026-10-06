# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Build synthetic chunk-set documents for tests. All values are made up."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

DOC_A = "0b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8"
DOC_B = "1c2d3e4f-5061-4b72-9c83-a4b5c6d7e8f9"
DOC_TAX = "2d3e4f50-6172-4c83-ad94-b5c6d7e8f90a"
ENTITY = "11111111-1111-4111-8111-111111111111"


def chunk_set(
    document_id: str = DOC_A,
    *,
    texts: tuple[str, ...] = ("alpha beta", "gamma delta"),
    set_consent: object = None,
    **overrides: object,
) -> dict[str, Any]:
    """Return a chunk-set dict shaped like the pipeline's output.

    ``overrides`` replace fields on every chunk (``consent`` is short for
    ``consent_on_file``); a value of ``...`` removes the field.
    ``set_consent`` adds a set-level ``consent_on_file`` when not None.
    """
    if "consent" in overrides:
        overrides["consent_on_file"] = overrides.pop("consent")
    chunks: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        chunk: dict[str, Any] = {
            "chunk_id": f"chunk-{document_id}-{index}",
            "document_id": document_id,
            "trace_id": f"trace-{document_id}",
            "text": text,
            "page_range": [index + 1, index + 1],
            "section_hierarchy": ["Synthetic Report", f"Part {index}"],
            "trust_score": None,
            "ocr_engine_provenance": None,
            "chunk_strategy": "hybrid",
            "token_count": len(text.split()),
            "source_track": "document",
            "hallucination_risk": None,
            "entity_id": ENTITY,
            "document_type": "operating_agreement",
            "category": "LLCs",
            "is_confidential": False,
            "title": "Synthetic Report",
            "document_date": "2024-01-31",
            "sha256": "a" * 64,
            "consent_on_file": False,
        }
        for key, value in overrides.items():
            if value is ...:
                chunk.pop(key, None)
            else:
                chunk[key] = value
        chunks.append(chunk)
    result: dict[str, Any] = {
        "schema_version": "1.1",
        "document_id": document_id,
        "trace_id": f"trace-{document_id}",
        "source_track": "document",
        "chunk_strategy": "hybrid",
        "total_chunks": len(chunks),
        "chunks": chunks,
    }
    if set_consent is not None:
        result["consent_on_file"] = set_consent
    return result


def write_chunk_set(directory: Path, data: dict[str, Any]) -> Path:
    """Write ``data`` as ``<document_id>.json`` in ``directory``."""
    path = directory / f"{data['document_id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path
