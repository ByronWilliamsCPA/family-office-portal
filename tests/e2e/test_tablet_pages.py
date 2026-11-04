# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Every page, viewed in a real browser at tablet size (1024x768 landscape).

The primary users read the portal on tablets, so each page is loaded in
Chromium with the compiled Tailwind stylesheet and checked for: a clean load,
no sideways scrolling, large enough tap targets, unclipped text, a sound
heading outline, labels and landmarks, readability with JavaScript off, and no
serious accessibility violations. Journey tests cover the back button and the
two-click rule. Opt-in: run with ``pytest -m e2e --no-cov`` (README.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from axe_playwright_python.sync_playwright import Axe
from playwright.sync_api import expect

from tests.e2e.conftest import LONG_ENTITY_NAME, SCREENSHOT_DIR

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.e2e.conftest import ConsolePage, PortalServer

pytestmark = pytest.mark.e2e

NAV_LABELS = ["Home", "Documents", "Finances", "Portfolio", "Entities"]
MIN_TARGET_PX = 44


@dataclass(frozen=True)
class PageSpec:
    """One page under test.

    Attributes:
        slug (str): Short name used in test ids and screenshot file names.
        path (str): URL path and query.
        h1 (str): Expected main heading text.
        texts (tuple[str, ...]): Content that must be readable, also with
            JavaScript off.
        admin (bool): Whether the page needs an Admin token.
    """

    slug: str
    path: str
    h1: str
    texts: tuple[str, ...]
    admin: bool = False


PAGES: tuple[PageSpec, ...] = (
    PageSpec(
        "home",
        "/",
        "Welcome",
        ("What we own today", "$1,685,000", "Coming up", "Example Family Trust"),
    ),
    PageSpec(
        "documents-folders",
        "/documents",
        "Documents",
        ("Find a document by name", "Legal", "Example Operating Agreement.pdf"),
    ),
    PageSpec(
        "documents-by-entity",
        "/documents?view=entity",
        "Documents",
        ("Example Holdings LLC", "Example Trust Agreement.pdf"),
    ),
    PageSpec(
        "documents-search",
        "/documents?q=Policy",
        "Documents",
        ("Example Insurance Policy.pdf",),
    ),
    PageSpec(
        "finances",
        "/finances",
        "Finances",
        ("Total of our accounts", "Digital currency", "Example Coin"),
    ),
    PageSpec(
        "portfolio",
        "/portfolio",
        "Portfolio",
        ("Example Total Market Fund", "Example Bond Fund"),
    ),
    PageSpec(
        "entities",
        "/entities",
        "Entities",
        ("Example Holdings LLC", LONG_ENTITY_NAME),
    ),
    PageSpec(
        "entity-detail",
        "/entities/ent-alpha",
        "Example Holdings LLC",
        ("Example Operating Agreement.pdf", "Back to all entities"),
    ),
    PageSpec(
        "entity-detail-long-names",
        "/entities/ent-long",
        LONG_ENTITY_NAME,
        ("Example Annual Statement With A Very Long File Name",),
    ),
    PageSpec(
        "manual-marks",
        "/admin/manual-marks",
        "Accounts valued by hand",
        ("Example Retirement", "Example Savings"),
        admin=True,
    ),
)

PAGE_PARAMS = [pytest.param(spec, id=spec.slug) for spec in PAGES]


def _script(name: str) -> str:
    """Read one browser script from ``tests/e2e/js``.

    Args:
        name (str): File name without ``.js``.

    Returns:
        str: The script text, a function expression for ``page.evaluate``.
    """
    return (Path(__file__).parent / "js" / f"{name}.js").read_text(encoding="utf-8")


def _load(
    open_page: Callable[..., ConsolePage],
    portal: PortalServer,
    spec: PageSpec,
    *,
    javascript: bool = True,
    expand: bool = False,
) -> ConsolePage:
    """Open a page at tablet size and wait for it to finish loading.

    Args:
        open_page (Callable[..., ConsolePage]): Page factory fixture.
        portal (PortalServer): The running app.
        spec (PageSpec): Page to visit.
        javascript (bool): False loads it with JavaScript off.
        expand (bool): Open every ``<details>`` so hidden tables are measured.

    Returns:
        ConsolePage: The loaded page with its console log.
    """
    cp = open_page(admin=spec.admin, javascript=javascript)
    response = cp.page.goto(f"{portal.base_url}{spec.path}", wait_until="load")
    assert response is not None
    assert response.status == 200, f"{spec.path} answered {response.status}"
    if expand and javascript:
        cp.page.evaluate(
            "document.querySelectorAll('details').forEach(d => { d.open = true; })"
        )
    return cp


# --------------------------------------------------------------------------- #
# Page scripts (run inside the browser)
# --------------------------------------------------------------------------- #

# Visible text that spills out of the box that should hold it, or is cut off.
_CLIPPED_TEXT_JS = _script("clipped_text")

# Visible links, buttons and fields with their size, skipping links that sit
# inside a sentence (WCAG 2.5.8 "inline" exception).
_TARGETS_JS = _script("targets")

_HEADINGS_JS = _script("headings")

_ACCESSIBILITY_BASICS_JS = _script("basics")


# --------------------------------------------------------------------------- #
# Per-page checks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_page_loads_without_console_errors(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """The page answers 200 and the browser logs no console error."""
    cp = _load(open_page, portal, spec)
    assert cp.errors == []


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_no_horizontal_scrolling(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """The page is never wider than the 1024 pixel screen."""
    cp = _load(open_page, portal, spec, expand=True)
    widths = cp.page.evaluate(
        "({doc: document.documentElement.scrollWidth, "
        "body: document.body.scrollWidth, view: window.innerWidth})"
    )
    assert widths["view"] == 1024
    assert widths["doc"] <= widths["view"], widths
    assert widths["body"] <= widths["view"], widths


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_navigation_links_are_visible_and_large(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """Five navigation links show, each at least 44 by 44 pixels."""
    cp = _load(open_page, portal, spec)
    links = cp.page.locator('nav[aria-label="Main"] a')
    assert links.all_inner_texts() == NAV_LABELS
    for index in range(links.count()):
        link = links.nth(index)
        assert link.is_visible()
        box = link.bounding_box()
        assert box is not None
        assert box["width"] >= MIN_TARGET_PX, (NAV_LABELS[index], box)
        assert box["height"] >= MIN_TARGET_PX, (NAV_LABELS[index], box)


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_primary_controls_are_at_least_44_pixels(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """Every link, button and field outside a sentence is 44 pixels or more.

    Links inside a sentence are exempt (WCAG 2.5.8 inline exception).
    """
    cp = _load(open_page, portal, spec, expand=True)
    targets: list[dict[str, Any]] = cp.page.evaluate(_TARGETS_JS)
    assert targets, "no interactive controls found"
    small = [
        f"{t['what']} is {t['width']}x{t['height']}"
        for t in targets
        if min(t["width"], t["height"]) < MIN_TARGET_PX
    ]
    assert "\n".join(small) == ""


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_text_is_not_clipped(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """No visible text overflows or is cut off by the box that holds it."""
    cp = _load(open_page, portal, spec, expand=True)
    assert cp.page.evaluate(_CLIPPED_TEXT_JS) == []


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_one_main_heading_and_no_skipped_levels(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """There is exactly one h1, it names the page, and levels never skip."""
    cp = _load(open_page, portal, spec)
    headings: list[dict[str, Any]] = cp.page.evaluate(_HEADINGS_JS)
    h1s = [h for h in headings if h["level"] == 1]
    assert [h["text"] for h in h1s] == [spec.h1]
    previous = 0
    for heading in headings:
        assert heading["level"] <= previous + 1, (
            f"{heading['text']!r} jumps from h{previous} to h{heading['level']}"
        )
        previous = heading["level"]


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_labels_alt_text_landmarks_and_title(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """Images have alt text, fields have labels, and the page has landmarks."""
    cp = _load(open_page, portal, spec)
    facts: dict[str, Any] = cp.page.evaluate(_ACCESSIBILITY_BASICS_JS)
    assert facts["problems"] == []
    assert facts["mains"] == 1
    assert facts["navs"] >= 1
    assert facts["banners"] == 1
    assert facts["lang"] == "en"
    assert facts["title"], "the page needs a title"


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_content_is_readable_with_javascript_off(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """With JavaScript off, the main content text is still on the page."""
    cp = _load(open_page, portal, spec, javascript=False)
    expect(cp.page.locator("h1")).to_have_text(spec.h1)
    main_text = cp.page.inner_text("main")
    for text in spec.texts:
        assert text in main_text, f"missing with JavaScript off: {text!r}"


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_no_serious_accessibility_violations(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """axe-core finds no serious or critical violation (colour contrast included).

    Nothing is excluded: every rule runs against the compiled stylesheet.
    """
    cp = _load(open_page, portal, spec, expand=True)
    results = Axe().run(cp.page)
    serious = [
        f"{v['id']} ({v['impact']}): {v['help']} -> "
        + "; ".join(node["html"][:80] for node in v["nodes"][:3])
        for v in results.response["violations"]
        if v["impact"] in {"serious", "critical"}
    ]
    assert serious == []


@pytest.mark.parametrize("spec", PAGE_PARAMS)
def test_full_page_screenshot_is_saved(
    open_page: Callable[..., ConsolePage], portal: PortalServer, spec: PageSpec
) -> None:
    """A full-page picture is saved, for a person to look at, never committed."""
    cp = _load(open_page, portal, spec)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    target = SCREENSHOT_DIR / f"{spec.slug}.png"
    cp.page.screenshot(path=str(target), full_page=True)
    assert target.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Journeys: back button, two clicks, search
# --------------------------------------------------------------------------- #


def _nav(cp: ConsolePage, label: str) -> None:
    """Click one main navigation link.

    Args:
        cp (ConsolePage): The page.
        label (str): Link text, for example ``Documents``.
    """
    cp.page.locator('nav[aria-label="Main"]').get_by_role("link", name=label).click()


def test_back_button_after_a_navigation_link(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """Home, then Documents, then Back lands on Home with its content."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/")
    _nav(cp, "Documents")
    expect(cp.page.locator("h1")).to_have_text("Documents")
    cp.page.go_back()
    expect(cp.page).to_have_url(f"{portal.base_url}/")
    expect(cp.page.locator("h1")).to_have_text("Welcome")
    expect(cp.page.locator("main")).to_contain_text("What we own today")


def test_back_button_after_opening_an_entity(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """Entities, then one entity, then Back lands on the entity list."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/entities")
    cp.page.get_by_role("link", name="Example Family Trust").click()
    expect(cp.page.locator("h1")).to_have_text("Example Family Trust")
    cp.page.go_back()
    expect(cp.page).to_have_url(f"{portal.base_url}/entities")
    expect(cp.page.locator("main")).to_contain_text("Example Holdings LLC")


def test_back_button_after_switching_the_document_view(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """Switching to "By entity" adds a history step; Back returns to categories."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/documents")
    cp.page.get_by_role("link", name="By entity").click()
    expect(cp.page).to_have_url(re.compile(r"/documents\?view=entity$"))
    expect(cp.page.locator("#folders")).to_contain_text("Example Trust Agreement.pdf")
    cp.page.go_back()
    expect(cp.page).to_have_url(f"{portal.base_url}/documents")
    expect(cp.page.locator("main")).to_contain_text("Example Operating Agreement.pdf")
    expect(cp.page.get_by_role("link", name="By category")).to_have_attribute(
        "aria-current", "page"
    )


def test_typing_a_search_does_not_trap_the_back_button(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """A live search changes no history, so one Back leaves the Documents page."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/")
    _nav(cp, "Documents")
    cp.page.get_by_label("Find a document by name").press_sequentially("Policy")
    expect(cp.page.locator("#search-results")).to_contain_text(
        "Example Insurance Policy.pdf"
    )
    cp.page.go_back()
    expect(cp.page).to_have_url(f"{portal.base_url}/")
    expect(cp.page.locator("h1")).to_have_text("Welcome")


def test_search_form_works_with_javascript_off(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """With JavaScript off, the search box still finds documents (full reload)."""
    cp = open_page(javascript=False)
    cp.page.goto(f"{portal.base_url}/documents")
    box = cp.page.get_by_label("Find a document by name")
    box.fill("Policy")
    box.press("Enter")
    expect(cp.page).to_have_url(re.compile(r"/documents\?q=Policy"))
    expect(cp.page.locator("#search-results")).to_contain_text(
        "Example Insurance Policy.pdf"
    )


def test_every_section_is_one_click_from_home(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """Each navigation link on Home opens a page with its own main heading."""
    for label in NAV_LABELS[1:]:
        cp = open_page()
        cp.page.goto(f"{portal.base_url}/")
        _nav(cp, label)
        expect(cp.page.locator("h1")).to_have_text(label)


def test_document_lists_are_within_two_clicks_of_home(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """One click gives the category list; a second gives the entity list."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/")
    _nav(cp, "Documents")  # click 1: the document list, grouped by category
    folders = cp.page.locator("#folders")
    for name in (
        "Example Operating Agreement.pdf",
        "Example Insurance Policy.pdf",
        "Example Trust Agreement.pdf",
    ):
        expect(folders.get_by_role("link", name=name)).to_be_visible()
    cp.page.get_by_role("link", name="By entity").click()  # click 2
    expect(folders).to_contain_text("Example Holdings LLC")
    expect(
        folders.get_by_role("link", name="Example Trust Agreement.pdf")
    ).to_be_visible()


def test_entity_detail_is_within_two_clicks_of_home(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """Home, Entities (click 1), then an entity (click 2) shows its details."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/")
    _nav(cp, "Entities")
    cp.page.get_by_role("link", name="Example Holdings LLC").click()
    expect(cp.page.locator("h1")).to_have_text("Example Holdings LLC")
    expect(cp.page.locator("main")).to_contain_text("Example Operating Agreement.pdf")


def test_entity_detail_is_one_click_from_the_coming_up_list(
    open_page: Callable[..., ConsolePage], portal: PortalServer
) -> None:
    """The Coming up list on Home links straight to an entity."""
    cp = open_page()
    cp.page.goto(f"{portal.base_url}/")
    cp.page.get_by_role("link", name="Example Family Trust").click()
    expect(cp.page.locator("h1")).to_have_text("Example Family Trust")
