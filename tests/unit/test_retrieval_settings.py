# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the optional retrieval settings.

Rules covered: everything is off when unset; a URL without its key (or the
embedding URL without a model) is an error naming the variable and never the
value; connections cannot be built with blank values; keys never show in
``repr``.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from app.retrieval.settings import (
    EmbeddingConnection,
    QdrantConnection,
    RetrievalConfigError,
    load_retrieval_settings,
)

RETRIEVAL_VARS = (
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBEDDING_MODEL",
    "EMBED_TIMEOUT_SECONDS",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "CHUNKS_DIR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Clean env."""
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_all(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Set all."""
    values = {
        "EMBED_BASE_URL": "http://embed.test/",
        "EMBED_API_KEY": secrets.token_urlsafe(24),
        "EMBEDDING_MODEL": "test-embed-model",
        "QDRANT_URL": "http://qdrant.test:6333",
        "QDRANT_API_KEY": secrets.token_urlsafe(24),
        "CHUNKS_DIR": "/data/chunks",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_everything_is_off_when_unset() -> None:
    """Everything is off when unset."""
    settings = load_retrieval_settings()
    assert settings.embedding_connection() is None
    assert settings.qdrant_connection() is None
    assert settings.chunks_path() is None
    assert settings.search_connected() is False


def test_full_configuration_builds_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full configuration builds connections."""
    values = _set_all(monkeypatch)
    settings = load_retrieval_settings()
    embedding = settings.embedding_connection()
    qdrant = settings.qdrant_connection()
    assert embedding is not None
    assert embedding.base_url == "http://embed.test"  # trailing slash dropped
    assert embedding.api_key == values["EMBED_API_KEY"]
    assert embedding.model == "test-embed-model"
    assert embedding.timeout_seconds == 60.0
    assert qdrant is not None
    assert qdrant.url == values["QDRANT_URL"]
    assert settings.chunks_path() == Path("/data/chunks")
    assert settings.search_connected() is True


def test_keys_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys never appear in repr."""
    values = _set_all(monkeypatch)
    settings = load_retrieval_settings()
    shown = repr(settings) + repr(settings.embedding_connection())
    shown += repr(settings.qdrant_connection())
    assert values["EMBED_API_KEY"] not in shown
    assert values["QDRANT_API_KEY"] not in shown


@pytest.mark.parametrize("blank", ["", "   "])
def test_embed_url_without_key_is_an_error(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Embed url without key is an error."""
    _set_all(monkeypatch)
    monkeypatch.setenv("EMBED_API_KEY", blank)
    settings = load_retrieval_settings()
    with pytest.raises(RetrievalConfigError, match="EMBED_API_KEY"):
        settings.embedding_connection()
    assert settings.search_connected() is False


def test_embed_url_without_model_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Embed url without model is an error."""
    _set_all(monkeypatch)
    monkeypatch.delenv("EMBEDDING_MODEL")
    with pytest.raises(RetrievalConfigError, match="EMBEDDING_MODEL"):
        load_retrieval_settings().embedding_connection()


def test_qdrant_url_without_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Qdrant url without key is an error."""
    values = _set_all(monkeypatch)
    monkeypatch.delenv("QDRANT_API_KEY")
    settings = load_retrieval_settings()
    with pytest.raises(RetrievalConfigError, match="QDRANT_API_KEY") as excinfo:
        settings.qdrant_connection()
    assert values["EMBED_API_KEY"] not in str(excinfo.value)
    assert settings.search_connected() is False


def test_keys_without_urls_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys without urls are off."""
    monkeypatch.setenv("EMBED_API_KEY", secrets.token_urlsafe(24))
    monkeypatch.setenv("QDRANT_API_KEY", secrets.token_urlsafe(24))
    settings = load_retrieval_settings()
    assert settings.embedding_connection() is None
    assert settings.qdrant_connection() is None


def test_timeout_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout is read from the environment."""
    _set_all(monkeypatch)
    monkeypatch.setenv("EMBED_TIMEOUT_SECONDS", "15")
    embedding = load_retrieval_settings().embedding_connection()
    assert embedding is not None
    assert embedding.timeout_seconds == 15.0


@pytest.mark.parametrize(
    ("base_url", "key", "model", "timeout", "message"),
    [
        (" ", "k", "m", 1.0, "base URL"),
        ("http://e", " ", "m", 1.0, "API key"),
        ("http://e", "k", " ", 1.0, "model"),
        ("http://e", "k", "m", 0.0, "timeout"),
    ],
)
def test_embedding_connection_rejects_blank_values(
    base_url: str, key: str, model: str, timeout: float, message: str
) -> None:
    """Embedding connection rejects blank values."""
    with pytest.raises(RetrievalConfigError, match=message):
        EmbeddingConnection(
            base_url=base_url, api_key=key, model=model, timeout_seconds=timeout
        )


@pytest.mark.parametrize(
    ("url", "key", "message"), [(" ", "k", "URL"), ("http://q", "", "API key")]
)
def test_qdrant_connection_rejects_blank_values(
    url: str, key: str, message: str
) -> None:
    """Qdrant connection rejects blank values."""
    with pytest.raises(RetrievalConfigError, match=message):
        QdrantConnection(url=url, api_key=key)
