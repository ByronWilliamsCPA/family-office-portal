# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for chat settings (app.chat.settings)."""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.chat.settings import (
    DEFAULT_TIMEOUT_SECONDS,
    ChatConfigError,
    ChatConnection,
    ChatSettings,
    check_chat_settings,
)
from tests.unit.chat_fakes import random_key

USERINFO_SECRET = secrets.token_urlsafe(9)

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
    assert connection.api_key.get_secret_value() == key
    assert connection.model == "qwen"
    assert connection.timeout_seconds == 12.5
    assert key not in repr(connection)


def test_url_without_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL with a blank key names the key variable."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", "   ")
    settings = ChatSettings()
    with pytest.raises(ChatConfigError, match="LLM_API_KEY"):
        settings.connection()


def test_non_positive_timeout_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero timeout is refused."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", random_key())
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0")
    settings = ChatSettings()
    with pytest.raises(ChatConfigError, match="LLM_TIMEOUT_SECONDS"):
        settings.connection()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_url": " ", "api_key": SecretStr("k")}, "base URL"),
        ({"base_url": "http://x", "api_key": SecretStr(" ")}, "API key"),
        (
            {
                "base_url": "http://x",
                "api_key": SecretStr("k"),
                "timeout_seconds": 0,
            },
            "finite number above zero",
        ),
        (
            {
                "base_url": "http://x",
                "api_key": SecretStr("k"),
                "timeout_seconds": float("nan"),
            },
            "finite number above zero",
        ),
        (
            {
                "base_url": "http://x",
                "api_key": SecretStr("k"),
                "timeout_seconds": float("inf"),
            },
            "finite number above zero",
        ),
        (
            {"base_url": "http://x", "api_key": SecretStr("k\u00e9")},
            "ASCII",
        ),
    ],
)
def test_connection_rejects_bad_values(kwargs: dict[str, object], message: str) -> None:
    """The connection object cannot be built from unusable values."""
    with pytest.raises(ChatConfigError, match=message):
        ChatConnection(**kwargs)


BAD_URLS = [
    "chat.test/v1",
    "ftp://chat.test",
    "http://",
    "http://chat.test:99999",
    "http://chat.test:bad",
    "http://[chat.test",
    "ht\ntp://chat.test:8000",
    "http://chat.test\n",
    "http://chat.\ttest:8000",
    "http://chat.test:8000\x00",
    f"http://user:{USERINFO_SECRET}@chat.test:99999",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_connection_rejects_a_malformed_url_without_echoing_it(url: str) -> None:
    """A URL that is not http or https with a host and port names the variable."""
    api_key = SecretStr("k")
    with pytest.raises(ChatConfigError, match="LLM_BASE_URL") as caught:
        ChatConnection(base_url=url, api_key=api_key)
    assert url not in str(caught.value)
    assert USERINFO_SECRET not in str(caught.value)


ENV_BAD_URLS = [
    u for u in BAD_URLS if not u.endswith(("\n", "\x00")) and "user:" not in u
]


@pytest.mark.parametrize("url", ENV_BAD_URLS)
def test_bad_url_in_the_environment_is_a_startup_problem(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """The startup check reports a bad URL by variable name only."""
    monkeypatch.setenv("LLM_BASE_URL", url)
    monkeypatch.setenv("LLM_API_KEY", random_key())
    problem = check_chat_settings()
    assert problem is not None
    assert "LLM_BASE_URL" in problem
    assert url.strip() not in problem


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_unusable_timeout_in_the_environment_is_a_startup_problem(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """NaN and infinity do not pass as a positive timeout."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", random_key())
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", value)
    assert check_chat_settings() == (
        "LLM_TIMEOUT_SECONDS must be a finite number above zero"
    )


def test_unparsable_timeout_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout that is not a number is a validation error naming the variable."""
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "soon")
    assert check_chat_settings() == "invalid value for LLM_TIMEOUT_SECONDS"
    with pytest.raises(ValidationError):
        ChatSettings()


def test_non_ascii_key_in_the_environment_is_a_startup_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key with a non-ASCII character is refused without echoing it."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", "caf\u00e9-key")
    problem = check_chat_settings()
    assert problem is not None
    assert "ASCII" in problem
    assert "caf" not in problem


def test_connection_is_hidden_and_settings_are_frozen() -> None:
    """The key is a SecretStr and settings cannot be changed after loading."""
    connection = ChatConnection(base_url="http://x", api_key=SecretStr("k"))
    assert "k" not in repr(connection).replace("base_url", "")
    settings = ChatSettings()
    with pytest.raises(ValidationError):
        settings.chat_enabled_for = "all"


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
