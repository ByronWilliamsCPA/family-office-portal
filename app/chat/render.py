# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Turn a model answer and its search results into what the page shows.

The answer is shown as plain text: no markdown is rendered, markdown images
are dropped, markdown links keep only their words, reference-style link
definitions (``[1]: https://...``) are removed, and bare web addresses are
replaced. Jinja's autoescaping then turns any HTML the model wrote into
visible text. The only links on the answer are the portal's own citation
links, built here from search results, never from model text.

#CRITICAL: security: a model answer cannot place a live link, image or HTML
on the page. #VERIFY: tests/integration/test_chat_route.py
::test_model_links_images_and_html_are_not_rendered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.chat.prompt import clean_label, kept_results, passage_label
from app.document_files import document_url
from app.retrieval.search import DocumentCitation

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.retrieval.search import SearchResult

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# A reference-style link definition, ``[label]: target``, whose target looks
# like an address. A line such as ``[Important]: file Form 1065`` is answer
# text and stays. ``[ \t]`` rather than ``\s`` keeps the match from running
# across blank lines.
_REF_LINK_DEF = re.compile(
    r"^[ \t]*\[[^\]]+\]:[ \t]*<?"
    r"(?:[a-z][a-z0-9+.-]*://|(?:javascript|data|mailto|file|tel):|www\.|/)"
    r".*$",
    re.MULTILINE | re.IGNORECASE,
)
_URL = re.compile(r"\b(?:https?|ftp|file|data|javascript):\S+", re.IGNORECASE)
_WWW = re.compile(r"\bwww\.\S+", re.IGNORECASE)
_BLANK_LINES = re.compile(r"\n\s*\n")
LINK_REMOVED = "[link removed]"


@dataclass(frozen=True)
class Citation:
    """One source shown under an answer.

    Attributes:
        label (str): Title and pages, or tax-law reference and title.
        href (str | None): Portal preview link for a document, else None.
    """

    label: str
    href: str | None


def answer_paragraphs(answer: str) -> tuple[str, ...]:
    """Strip links and images from an answer and split it into paragraphs.

    Args:
        answer (str): Model answer text.

    Returns:
        tuple[str, ...]: Plain-text paragraphs, still unescaped (the template
        escapes them).
    """
    text = _MD_IMAGE.sub("", answer)
    text = _MD_LINK.sub(r"\1", text)
    text = _REF_LINK_DEF.sub("", text)
    text = _URL.sub(LINK_REMOVED, text)
    text = _WWW.sub(LINK_REMOVED, text)
    paragraphs = (p.strip() for p in _BLANK_LINES.split(text))
    return tuple(p for p in paragraphs if p)


def preview_url(citation: DocumentCitation) -> str | None:
    """Return the portal's preview link for a cited document.

    Args:
        citation (DocumentCitation): A family-document citation.

    Returns:
        str | None: ``/documents/<id>/preview``, with ``#page=N`` when the
        page is a positive whole number; None when the document id is
        missing.
    """
    if not citation.document_id:
        return None
    return document_url(citation.document_id, "preview", citation.page_start)


def citations(results: Sequence[SearchResult]) -> tuple[Citation, ...]:
    """Build the source list shown under an answer, without duplicates.

    Only passages the model was shown are listed (the same ones the prompt
    keeps), so every source here had its text in the prompt. The list is the
    passages searched for the answer, not a claim about which ones the
    answer used.

    Args:
        results (Sequence[SearchResult]): Search results, best first.

    Returns:
        tuple[Citation, ...]: One entry per distinct label and link.
    """
    seen: set[tuple[str, str | None]] = set()
    out: list[Citation] = []
    for result in kept_results(results):
        href = (
            preview_url(result.citation)
            if isinstance(result.citation, DocumentCitation)
            else None
        )
        label = clean_label(passage_label(result))
        if (label, href) in seen:
            continue
        seen.add((label, href))
        out.append(Citation(label=label, href=href))
    return tuple(out)
