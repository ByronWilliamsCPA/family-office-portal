# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the optional retrieval settings.

Rules covered: everything is off when unset; a URL without its key (or the
embedding URL without a model) is an error naming the variable and never the
value; a value that cannot be parsed is an error naming the variable and
never the value; connections cannot be built with blank values or a timeout
that is not a finite positive number; keys never show in ``repr``; pages
learn about a bad setting through one warning, never an exception.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.retrieval import settings as settings_module
from app.retrieval.settings import (
    EmbeddingConnection,
    QdrantConnection,
    RetrievalConfigError,
    document_search_connected,
    load_retrieval_settings,
)

# Generated per run so no credential-shaped literal sits in the source.
USERINFO_SECRET = secrets.token_urlsafe(9)

RETRIEVAL_VARS = (
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBEDDING_MODEL",
    "EMBED_TIMEOUT_SECONDS",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "CHUNKS_DIR",
    "TAX_LAW_PATH",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Clean env and forget problems already reported."""
    for name in RETRIEVAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings_module, "_reported_problems", set())


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
    assert settings.tax_law_file() is None
    assert settings.search_connected() is False


def test_full_configuration_builds_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full configuration builds connections."""
    values = _set_all(monkeypatch)
    settings = load_retrieval_settings()
    embedding = settings.embedding_connection()
    qdrant = settings.qdrant_connection()
    assert embedding is not None
    assert embedding.base_url == "http://embed.test"  # trailing slash dropped
    assert embedding.api_key.get_secret_value() == values["EMBED_API_KEY"]
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
    settings = load_retrieval_settings()
    with pytest.raises(RetrievalConfigError, match="EMBEDDING_MODEL"):
        settings.embedding_connection()


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
        ("http://e", "k", "m", -1.0, "timeout"),
        ("http://e", "k", "m", float("nan"), "timeout"),
        ("http://e", "k", "m", float("inf"), "timeout"),
    ],
)
def test_embedding_connection_rejects_blank_values(
    base_url: str, key: str, model: str, timeout: float, message: str
) -> None:
    """Embedding connection rejects blank values."""
    api_key = SecretStr(key)
    with pytest.raises(RetrievalConfigError, match=message):
        EmbeddingConnection(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout,
        )


@pytest.mark.parametrize(
    ("url", "key", "message"), [(" ", "k", "URL"), ("http://q", "", "API key")]
)
def test_qdrant_connection_rejects_blank_values(
    url: str, key: str, message: str
) -> None:
    """Qdrant connection rejects blank values."""
    api_key = SecretStr(key)
    with pytest.raises(RetrievalConfigError, match=message):
        QdrantConnection(url=url, api_key=api_key)


def test_embedding_connection_rejects_a_non_ascii_key_without_echoing_it() -> None:
    """A key that cannot be an HTTP header value is a config error."""
    api_key = SecretStr("cl\u00e9-secret")
    with pytest.raises(RetrievalConfigError, match="ASCII") as caught:
        EmbeddingConnection(base_url="http://e", api_key=api_key, model="m")
    assert "secret" not in str(caught.value)


def test_qdrant_connection_rejects_a_non_ascii_key_without_echoing_it() -> None:
    """A key that cannot be an HTTP header value is a config error."""
    api_key = SecretStr("cl\u00e9-secret")
    with pytest.raises(RetrievalConfigError, match="ASCII") as caught:
        QdrantConnection(url="http://q", api_key=api_key)
    assert "secret" not in str(caught.value)


BAD_URLS = [
    "ftp://host.test",
    "host.test:6333",
    "http://",
    "http://host.test:99999",
    "http://host.test:bad",
    "http://[host.test",
    "ht\ntp://host.test:6333",
    "http://host.\ttest:6333",
    "http://host.test:6333\x00",
    "http://host.test\x7f:6333",
    f"http://user:{USERINFO_SECRET}@host.test:99999",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_qdrant_connection_rejects_a_malformed_url_without_echoing_it(
    url: str,
) -> None:
    """A URL that is not http or https with a host and port names the variable."""
    with pytest.raises(RetrievalConfigError, match="QDRANT_URL") as caught:
        QdrantConnection(url=url, api_key=SecretStr("k"))
    assert url not in str(caught.value)
    assert USERINFO_SECRET not in str(caught.value)


@pytest.mark.parametrize("url", [*BAD_URLS, "embed.test/v1"])
def test_embedding_connection_rejects_a_malformed_url_without_echoing_it(
    url: str,
) -> None:
    """A malformed embedding base URL names the variable, never its value."""
    with pytest.raises(RetrievalConfigError, match="EMBED_BASE_URL") as caught:
        EmbeddingConnection(base_url=url, api_key=SecretStr("k"), model="m")
    assert url not in str(caught.value)
    assert USERINFO_SECRET not in str(caught.value)


@pytest.mark.parametrize(
    "url", ["http://q", "https://q.test:6333", "http://q.test:6333/prefix"]
)
def test_qdrant_connection_accepts_http_and_https_urls(url: str) -> None:
    """Valid http and https URLs, with or without a port or path, are kept."""
    assert QdrantConnection(url=url, api_key=SecretStr("k")).url == url


def test_a_malformed_url_in_the_environment_is_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad URL makes search not connected instead of failing a later client."""
    _set_all(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "ftp://qdrant.test")
    with pytest.raises(RetrievalConfigError, match="QDRANT_URL"):
        load_retrieval_settings().qdrant_connection()
    assert load_retrieval_settings().search_connected() is False


@pytest.mark.parametrize("name", ["QDRANT_URL", "EMBED_BASE_URL"])
def test_a_control_character_inside_a_url_in_the_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """``urlsplit`` drops a newline, so the value must be refused before it."""
    _set_all(monkeypatch)
    monkeypatch.setenv(name, "ht\ntp://host.test:6333")
    settings = load_retrieval_settings()
    with pytest.raises(RetrievalConfigError, match=name) as caught:
        settings.qdrant_connection()
        settings.embedding_connection()
    assert "host.test" not in str(caught.value)
    assert settings.search_connected() is False


def test_surrounding_whitespace_on_a_url_in_the_environment_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing newline from an env file is trimmed, not treated as corruption."""
    _set_all(monkeypatch)
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333\n")
    connection = load_retrieval_settings().qdrant_connection()
    assert connection is not None
    assert connection.url == "http://qdrant.test:6333"


@pytest.mark.parametrize("value", ["abc", "", "nan", "inf", "0", "-1"])
def test_bad_timeout_names_the_variable_not_the_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A timeout that is not a finite positive number names the variable."""
    _set_all(monkeypatch)
    monkeypatch.setenv("EMBED_TIMEOUT_SECONDS", value)
    with pytest.raises(RetrievalConfigError) as caught:
        load_retrieval_settings()
    assert str(caught.value) == "invalid value for EMBED_TIMEOUT_SECONDS"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_require_search_settings_raises_on_a_url_without_its_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raising check reports a URL without its key."""
    _set_all(monkeypatch)
    monkeypatch.delenv("QDRANT_API_KEY")
    settings = load_retrieval_settings()
    assert settings.search_connected() is False
    with pytest.raises(RetrievalConfigError, match="QDRANT_API_KEY"):
        settings.require_search_settings()


def test_document_search_connected_when_fully_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pages see search as connected when both services are set."""
    _set_all(monkeypatch)
    with capture_logs() as logs:
        assert document_search_connected() is True
    assert logs == []


def test_document_search_is_quietly_off_when_unset() -> None:
    """Unset services are simply off, with no warning."""
    with capture_logs() as logs:
        assert document_search_connected() is False
    assert logs == []


def test_bad_setting_is_reported_once_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad value logs one warning naming the variable, then stays quiet."""
    _set_all(monkeypatch)
    monkeypatch.setenv("EMBED_TIMEOUT_SECONDS", "soon")
    with capture_logs() as logs:
        assert document_search_connected() is False
        assert document_search_connected() is False
    assert logs == [
        {
            "event": "document_search_misconfigured",
            "reason": "invalid value for EMBED_TIMEOUT_SECONDS",
            "log_level": "warning",
        }
    ]
    assert "soon" not in repr(logs)


def test_tax_law_path_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knowledge-base path comes from TAX_LAW_PATH; blank means off."""
    monkeypatch.setenv("TAX_LAW_PATH", " /data/knowledge/kb.json ")
    assert load_retrieval_settings().tax_law_file() == Path("/data/knowledge/kb.json")
    monkeypatch.setenv("TAX_LAW_PATH", "   ")
    assert load_retrieval_settings().tax_law_file() is None
