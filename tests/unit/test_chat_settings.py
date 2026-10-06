# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for chat settings (app.chat.settings)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.chat.settings import (
    DEFAULT_TIMEOUT_SECONDS,
    ChatConfigError,
    ChatConnection,
    ChatSettings,
    check_chat_settings,
)
from tests.unit.chat_fakes import random_key

CHAT_VARS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "CHAT_INSTRUCTIONS_PATH",
    "CHAT_ENABLED_FOR",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CHAT_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_off_and_admin_only() -> None:
    """With nothing set, chat is off, the timeout is 30 s, and admin-only."""
    settings = ChatSettings()
    assert settings.connection() is None
    assert settings.instructions_file() is None
    assert settings.llm_timeout_seconds == DEFAULT_TIMEOUT_SECONDS == 30.0
    assert settings.chat_enabled_for == "admin"


def test_connection_is_built_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL, key, model and timeout come from the environment."""
    key = random_key()
    monkeypatch.setenv("LLM_BASE_URL", " http://chat.test/ ")
    monkeypatch.setenv("LLM_API_KEY", key)
    monkeypatch.setenv("LLM_MODEL", " qwen ")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    connection = ChatSettings().connection()
    assert connection is not None
    assert connection.base_url == "http://chat.test"
    assert connection.api_key == key
    assert connection.model == "qwen"
    assert connection.timeout_seconds == 12.5
    assert key not in repr(connection)


def test_url_without_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL with a blank key names the key variable."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", "   ")
    with pytest.raises(ChatConfigError, match="LLM_API_KEY"):
        ChatSettings().connection()


def test_non_positive_timeout_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero timeout is refused."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", random_key())
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0")
    with pytest.raises(ChatConfigError, match="LLM_TIMEOUT_SECONDS"):
        ChatSettings().connection()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_url": " ", "api_key": "k"}, "base URL"),
        ({"base_url": "http://x", "api_key": " "}, "API key"),
        ({"base_url": "http://x", "api_key": "k", "timeout_seconds": 0}, "positive"),
    ],
)
def test_connection_rejects_blank_values(
    kwargs: dict[str, object], message: str
) -> None:
    """The connection object cannot be built without a URL and key."""
    with pytest.raises(ChatConfigError, match=message):
        ChatConnection(**kwargs)


def test_instructions_file_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instructions path is returned as a Path."""
    monkeypatch.setenv("CHAT_INSTRUCTIONS_PATH", " /run/chat/instructions.md ")
    assert ChatSettings().instructions_file() == Path("/run/chat/instructions.md")


@pytest.mark.parametrize(
    ("audience", "admin", "viewer"),
    [("admin", True, False), ("all", True, True), ("none", False, False)],
)
def test_feature_flag_controls_who_sees_chat(
    audience: str, *, admin: bool, viewer: bool
) -> None:
    """CHAT_ENABLED_FOR selects the roles that see chat."""
    settings = ChatSettings(chat_enabled_for=audience)
    assert settings.allows(is_admin=True) is admin
    assert settings.allows(is_admin=False) is viewer


def test_check_passes_when_unset() -> None:
    """No chat settings is a valid (off) configuration."""
    assert check_chat_settings() is None


def test_check_reports_url_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup check names the missing key variable."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    problem = check_chat_settings()
    assert problem is not None
    assert "LLM_API_KEY" in problem


def test_check_reports_bad_values_by_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad flag value is named by variable, never echoed."""
    monkeypatch.setenv("CHAT_ENABLED_FOR", "everyone-secret-value")
    problem = check_chat_settings()
    assert problem == "invalid value for CHAT_ENABLED_FOR"


def test_secret_is_not_in_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key never appears in the settings repr."""
    key = random_key()
    monkeypatch.setenv("LLM_API_KEY", key)
    settings = ChatSettings()
    assert isinstance(settings.llm_api_key, SecretStr)
    assert key not in repr(settings)
