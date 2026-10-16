# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""The Documents page groups documents by category or by entity.

Every name, id and title here is made up. The rules under test:

* ``/documents`` shows category folders by default and entity folders with
  ``?view=entity``; ``/documents/folders`` returns the same folders as an HTMX
  fragment.
* An entity folder is named after the cached entity, never its id. A document
  with no entity, or an entity that is not cached or has a blank name, goes in
  one "No entity on file" folder, listed last.
* A Viewer never sees a confidential document in either view, and an entity
  whose documents are all confidential gets no folder (its name is not
  revealed either). An Admin sees everything.
* Name search keeps working in both views.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

NO_ENTITY = "No entity on file"

# Rows of id, name, category, entity id and confidential flag.
DOCS = [
    ("doc-a1", "Operating Agreement.pdf", "LLCs", "ent-alder", 0),
    ("doc-a2", "Annual Report 2025.pdf", "Filings", "ent-alder", 0),
    ("doc-b1", "Trust Deed.pdf", "Trusts", "ent-birch", 0),
    ("doc-s1", "Sealed Memo.pdf", "LLCs", "ent-cedar", 1),
    ("doc-c1", "Cedar Lease.pdf", "Leases", "ent-cedar", 0),
    ("doc-n1", "Loose Receipt.pdf", "Receipts", None, 0),
    ("doc-u1", "Orphan Notice.pdf", "Filings", "ent-gone", 0),
    ("doc-e1", "Blank Name Notice.pdf", "Filings", "ent-blank", 0),
]

# Rows of id, name and type; ent-gone is deliberately absent and ent-blank has
# no name.
ENTITIES = [
    ("ent-alder", "Alder Holdings LLC", "LLC"),
    ("ent-birch", "Birch Family Trust", "trust"),
    ("ent-cedar", "Cedar Rentals LLC", "LLC"),
    ("ent-blank", "", "LLC"),
]


def _seed(path: Path, docs: list[tuple] = DOCS) -> None:
    """Write the synthetic entities and documents into the test database.

    Args:
        path: SQLite database path.
        docs: Document rows to insert.
    """
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, ?, ?)",
            [(*row, now) for row in ENTITIES],
        )
        conn.executemany(
            "INSERT INTO documents (id, name, category, entity_id, "
            "is_confidential, proxy_url, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (doc_id, name, category, entity, conf, f"/documents/{doc_id}", now)
                for doc_id, name, category, entity, conf in docs
            ],
        )
        conn.commit()


def _headings(html: str) -> list[str]:
    """Return the text of every folder heading, in page order."""
    found = re.findall(r"<h2[^>]*>(.*?)</h2>", html, flags=re.DOTALL)
    return [re.sub(r"<[^>]+>", "", item).strip() for item in found]


def _folder(html: str, heading: str) -> str:
    """Return the HTML of one folder section, from its heading to its end."""
    start = html.index(heading)
    end = html.find("</section>", start)
    return html[start:end]


# --------------------------------------------------------------------------- #
# Category view stays the default
# --------------------------------------------------------------------------- #


async def test_default_view_is_category_folders(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """Without ``view`` the page lists category folders, as before."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents", headers=viewer_headers)
    assert response.status_code == 200
    assert _headings(response.text) == [
        "Filings",
        "LLCs",
        "Leases",
        "Receipts",
        "Trusts",
    ]
    assert "Alder Holdings LLC" not in _headings(response.text)


async def test_view_switch_offers_both_views_and_marks_current(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Two plain links switch the view, with an HTMX upgrade and no-JS fallback."""
    async with client as ac:
        category = await ac.get("/documents", headers=viewer_headers)
        entity = await ac.get("/documents?view=entity", headers=viewer_headers)
    for page in (category, entity):
        assert 'href="/documents?view=category"' in page.text
        assert 'href="/documents?view=entity"' in page.text
        assert 'hx-get="/documents/folders?view=entity"' in page.text
        assert 'hx-push-url="/documents?view=entity"' in page.text
    assert re.search(
        r'href="/documents\?view=category"[^>]*aria-current="page"', category.text
    )
    assert re.search(
        r'href="/documents\?view=entity"[^>]*aria-current="page"', entity.text
    )
    assert 'aria-current="page">By entity' not in category.text


async def test_unknown_view_is_rejected(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Only the two named views are accepted."""
    async with client as ac:
        page = await ac.get("/documents?view=bogus", headers=viewer_headers)
        fragment = await ac.get("/documents/folders?view=bogus", headers=viewer_headers)
    assert page.status_code == 422
    assert fragment.status_code == 422


# --------------------------------------------------------------------------- #
# Entity view
# --------------------------------------------------------------------------- #


async def test_entity_view_groups_documents_under_entity_names(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """Each entity folder lists exactly its own documents, named folders first."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    assert response.status_code == 200
    assert _headings(response.text) == [
        "Alder Holdings LLC",
        "Birch Family Trust",
        "Cedar Rentals LLC",
        NO_ENTITY,
    ]
    alder = _folder(response.text, "Alder Holdings LLC")
    assert "Operating Agreement.pdf" in alder
    assert "Annual Report 2025.pdf" in alder
    assert "Trust Deed.pdf" not in alder
    assert "Trust Deed.pdf" in _folder(response.text, "Birch Family Trust")


async def test_entity_folder_rows_show_category_and_link_to_entity(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """A row names its category; the folder heading links to the entity page."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    alder = _folder(response.text, "Alder Holdings LLC")
    assert "LLCs" in alder
    assert "Filings" in alder
    assert 'href="/entities/ent-alder"' in response.text
    assert 'href="/documents/doc-a1/download"' in alder


async def test_documents_without_a_usable_entity_share_one_folder(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """No entity, an uncached entity and a blank-named entity share one folder.

    The folder comes last and never shows an id as its name.
    """
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    headings = _headings(response.text)
    assert headings.count(NO_ENTITY) == 1
    assert headings[-1] == NO_ENTITY
    unlinked = _folder(response.text, NO_ENTITY)
    for name in (
        "Loose Receipt.pdf",
        "Orphan Notice.pdf",
        "Blank Name Notice.pdf",
    ):
        assert name in unlinked
    assert "ent-gone" not in response.text
    assert "ent-blank" not in response.text
    assert 'href="/entities/ent-gone"' not in response.text
    assert 'href="/entities/ent-blank"' not in response.text


async def test_entities_with_the_same_name_keep_separate_folders(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """Two entities with one name are two folders, told apart by identity."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.executemany(
            "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, ?, ?)",
            [("e1", "Twin LLC", "LLC", now), ("e2", "Twin LLC", "LLC", now)],
        )
        conn.executemany(
            "INSERT INTO documents (id, name, category, entity_id, "
            "is_confidential, proxy_url, fetched_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            [
                ("d1", "First.pdf", "LLCs", "e1", "/documents/d1", now),
                ("d2", "Second.pdf", "LLCs", "e2", "/documents/d2", now),
            ],
        )
        conn.commit()
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    assert _headings(response.text) == ["Twin LLC", "Twin LLC"]


async def test_folders_fragment_matches_the_page_folders(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """The HTMX fragment holds the folders for the requested view, no page chrome."""
    _seed(tmp_db_path)
    async with client as ac:
        entity = await ac.get("/documents/folders?view=entity", headers=viewer_headers)
        category = await ac.get(
            "/documents/folders?view=category", headers=viewer_headers
        )
    for fragment in (entity, category):
        assert fragment.status_code == 200
        assert fragment.headers["content-type"].startswith("text/html")
        assert "<html" not in fragment.text
        assert "Find a document by name" not in fragment.text
    assert "Alder Holdings LLC" in _headings(entity.text)
    assert "Alder Holdings LLC" not in _headings(category.text)
    assert "Filings" in _headings(category.text)


async def test_folders_fragment_defaults_to_category(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """The fragment, like the page, defaults to category folders."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents/folders", headers=viewer_headers)
    assert "Filings" in _headings(response.text)


async def test_empty_cache_says_no_documents_in_both_views(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """With nothing cached both views say so instead of rendering empty folders."""
    async with client as ac:
        category = await ac.get("/documents", headers=viewer_headers)
        entity = await ac.get("/documents?view=entity", headers=viewer_headers)
    for page in (category, entity):
        assert "No documents have been added yet." in page.text
        assert _headings(page.text) == []


async def test_folders_fragment_says_not_connected_without_the_backend(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the document service unset the fragment says so, as the page does."""
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        response = await ac.get("/documents/folders?view=entity")
    assert response.status_code == 200
    assert "Not connected yet" in response.text


# --------------------------------------------------------------------------- #
# Confidential documents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/documents",
        "/documents?view=category",
        "/documents?view=entity",
        "/documents/folders",
        "/documents/folders?view=category",
        "/documents/folders?view=entity",
        "/documents?view=entity&q=memo",
        "/documents?q=memo",
        "/documents/search?q=memo",
    ],
)
async def test_viewer_never_sees_a_confidential_document(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path, path: str
) -> None:
    """No view, fragment or search shows a Viewer a confidential document."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get(path, headers=viewer_headers)
    assert response.status_code == 200
    assert "Sealed Memo.pdf" not in response.text
    assert "doc-s1" not in response.text


async def test_viewer_entity_view_hides_an_entity_with_only_confidential_documents(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """An entity folder exists for a Viewer only through a visible document."""
    _seed(
        tmp_db_path,
        [
            ("doc-a1", "Operating Agreement.pdf", "LLCs", "ent-alder", 0),
            ("doc-s1", "Sealed Memo.pdf", "LLCs", "ent-cedar", 1),
            ("doc-s2", "Sealed Letter.pdf", "Filings", "ent-cedar", 1),
        ],
    )
    async with client as ac:
        page = await ac.get("/documents?view=entity", headers=viewer_headers)
        fragment = await ac.get(
            "/documents/folders?view=entity", headers=viewer_headers
        )
    for response in (page, fragment):
        assert _headings(response.text) == ["Alder Holdings LLC"]
        assert "Cedar Rentals LLC" not in response.text
        assert "ent-cedar" not in response.text


async def test_viewer_does_not_see_confidential_documents_in_a_shared_folder(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """A visible document keeps its folder; the hidden one beside it is absent."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    cedar = _folder(response.text, "Cedar Rentals LLC")
    assert "Cedar Lease.pdf" in cedar
    assert "Sealed Memo.pdf" not in cedar


@pytest.mark.parametrize(
    "path", ["/documents?view=entity", "/documents/folders?view=entity"]
)
async def test_admin_entity_view_includes_confidential_documents(
    client: AsyncClient, admin_headers: dict[str, str], tmp_db_path: Path, path: str
) -> None:
    """An Admin sees confidential documents in their entity folder."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get(path, headers=admin_headers)
    assert "Sealed Memo.pdf" in _folder(response.text, "Cedar Rentals LLC")


async def test_admin_category_view_includes_confidential_documents(
    client: AsyncClient, admin_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """An Admin sees confidential documents in the category view too."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents?view=category", headers=admin_headers)
    assert "Sealed Memo.pdf" in _folder(response.text, "LLCs")


# --------------------------------------------------------------------------- #
# Name search keeps working
# --------------------------------------------------------------------------- #


async def test_search_form_keeps_the_current_view(
    client: AsyncClient, viewer_headers: dict[str, str]
) -> None:
    """Submitting the search without JavaScript returns to the same view."""
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    assert re.search(
        r'<input[^>]*type="hidden"[^>]*name="view"[^>]*value="entity"', response.text
    )


async def test_search_in_entity_view_lists_matches_and_keeps_folders(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """A name search shows its matches above the entity folders."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get(
            "/documents?view=entity&q=lease", headers=viewer_headers
        )
    assert response.status_code == 200
    results = response.text[: response.text.index("<section")]
    assert "Cedar Lease.pdf" in results
    assert "Trust Deed.pdf" not in results
    assert "Alder Holdings LLC" in _headings(response.text)


async def test_search_with_no_match_still_says_so_in_entity_view(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """The no-match sentence is unchanged in the entity view."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents?view=entity&q=zzzz", headers=viewer_headers)
    assert 'No documents match "zzzz".' in response.text


async def test_search_partial_still_works(
    client: AsyncClient, viewer_headers: dict[str, str], tmp_db_path: Path
) -> None:
    """The HTMX search fragment is untouched by the folder views."""
    _seed(tmp_db_path)
    async with client as ac:
        response = await ac.get("/documents/search?q=trust", headers=viewer_headers)
    assert response.status_code == 200
    assert "Trust Deed.pdf" in response.text
    assert "<html" not in response.text


# --------------------------------------------------------------------------- #
# Entity links and the Viewer's not-connected note
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entity_id", ["a/b", "../admin", "x?y=1#z", "50%/off"])
async def test_entity_link_escapes_the_whole_id(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    tmp_db_path: Path,
    entity_id: str,
) -> None:
    """A slash, dot segment or other URL character in an id stays one segment."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, ?, ?)",
            (entity_id, "Odd Id LLC", "LLC", now),
        )
        conn.execute(
            "INSERT INTO documents (id, name, category, entity_id, "
            "is_confidential, proxy_url, fetched_at) VALUES "
            "('d1', 'Odd.pdf', 'LLCs', ?, 0, '/documents/d1', ?)",
            (entity_id, now),
        )
        conn.commit()
    async with client as ac:
        response = await ac.get("/documents?view=entity", headers=viewer_headers)
    expected = quote(entity_id, safe="")
    assert f'href="/entities/{expected}"' in response.text
    assert f'href="/entities/{entity_id}"' not in response.text


async def test_viewer_fragment_says_not_connected_without_the_backend(
    client: AsyncClient,
    viewer_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Viewer gets the same plain note when the document service is unset."""
    monkeypatch.delenv("BACKEND_LLC_MANAGER_URL")
    async with client as ac:
        response = await ac.get(
            "/documents/folders?view=entity", headers=viewer_headers
        )
    assert response.status_code == 200
    assert "Not connected yet" in response.text
