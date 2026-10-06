# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Pages learn whether document search is connected.

Search needs both the embedding service and Qdrant; with either unset, or a
URL without its key, templates see ``connected.document_search`` as false and
can say "not connected yet".
"""

from __future__ import annotations

import secrets

import pytest
from starlette.requests import Request

from app import templating

RETRIEVAL_VARS = (
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBEDDING_MODEL",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "CHUNKS_DIR",
)


@pytest.fixture
def captured(
    portal_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    """Clear retrieval settings and capture the context ``render`` builds."""
    del portal_env
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, object] = {}

    def fake_response(
        _request: Request, _name: str, context: dict[str, object], **_: object
    ) -> str:
        """Record the template context instead of rendering."""
        seen.update(context)
        return "rendered"

    monkeypatch.setattr(templating.templates, "TemplateResponse", fake_response)
    return seen


def _render() -> None:
    """Render the home page through the shared helper."""
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    templating.render(request, "pages/home.html", section="home")


def _connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set both the embedding service and Qdrant settings."""
    monkeypatch.setenv("EMBED_BASE_URL", "http://embed.test")
    monkeypatch.setenv("EMBED_API_KEY", secrets.token_urlsafe(16))
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embed-model")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")
    monkeypatch.setenv("QDRANT_API_KEY", secrets.token_urlsafe(16))


def test_search_is_not_connected_by_default(captured: dict[str, object]) -> None:
    """Search is not connected by default."""
    _render()
    connected = captured["connected"]
    assert isinstance(connected, dict)
    assert connected["document_search"] is False
    assert connected["llc_manager"] is True  # backend flags are unchanged


def test_search_is_connected_when_both_services_are_set(
    captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search is connected when both services are set."""
    _connect(monkeypatch)
    _render()
    connected = captured["connected"]
    assert isinstance(connected, dict)
    assert connected["document_search"] is True


def test_url_without_key_shows_not_connected(
    captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Url without key shows not connected."""
    _connect(monkeypatch)
    monkeypatch.delenv("QDRANT_API_KEY")
    _render()
    connected = captured["connected"]
    assert isinstance(connected, dict)
    assert connected["document_search"] is False
