# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for answer and citation rendering (app.chat.render)."""

from __future__ import annotations

from app.chat.render import (
    LINK_REMOVED,
    Citation,
    answer_paragraphs,
    citations,
    preview_url,
)
from tests.unit.chat_fakes import doc_result, tax_result


def test_markdown_image_is_dropped() -> None:
    """A markdown image leaves no trace of its address."""
    out = answer_paragraphs("Before ![chart](https://evil.example/x.png) after")
    assert out == ("Before  after",)


def test_markdown_link_keeps_only_its_words() -> None:
    """A markdown link becomes its text."""
    out = answer_paragraphs("See [the deed](https://evil.example/deed) now.")
    assert out == ("See the deed now.",)


def test_bare_urls_are_replaced() -> None:
    """Web addresses and script URLs are replaced with a note."""
    out = answer_paragraphs(
        "Visit https://evil.example/a?b=c or www.evil.example or javascript:alert(1)"
    )
    assert out == (f"Visit {LINK_REMOVED} or {LINK_REMOVED} or {LINK_REMOVED}",)


def test_reference_style_link_definitions_are_removed() -> None:
    """A reference link definition line is dropped."""
    out = answer_paragraphs("Text [x][1].\n\n[1]: https://evil.example")
    assert out == ("Text [x][1].",)


def test_paragraphs_split_on_blank_lines() -> None:
    """Blank lines separate paragraphs; single newlines stay inside one."""
    out = answer_paragraphs("One\nline two\n\n\nThree\n   \n")
    assert out == ("One\nline two", "Three")


def test_html_is_left_for_the_template_to_escape() -> None:
    """HTML text is kept as text (autoescaping makes it harmless)."""
    assert answer_paragraphs("<b>bold</b>") == ("<b>bold</b>",)


def test_preview_url_points_at_the_cited_page() -> None:
    """A document citation links to its preview at the first cited page."""
    result = doc_result("x", document_id="doc 1/a", page_start=7, page_end=8)
    assert preview_url(result.citation) == "/documents/doc%201%2Fa/preview#page=7"


def test_preview_url_without_page_or_id() -> None:
    """No page gives no fragment; no id gives no link."""
    no_page = doc_result("x", page_start=None, page_end=None)
    assert preview_url(no_page.citation) == "/documents/doc-1/preview"
    no_id = doc_result("x", document_id="")
    assert preview_url(no_id.citation) is None


def test_citations_link_documents_and_label_tax_law() -> None:
    """Documents get a preview link; tax law gets its number and title only."""
    out = citations([doc_result("a"), doc_result("b"), tax_result("c")])
    assert out == (
        Citation(
            label="Operating Agreement, page 3", href="/documents/doc-1/preview#page=3"
        ),
        Citation(label='Tax law reference 4.4, "Gifts"', href=None),
    )
