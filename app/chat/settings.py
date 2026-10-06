# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Settings for the chat model and the chat instructions file.

Every chat setting name lives in this module, so renaming one is a single
change. All of them are optional: with the model URL or the instructions path unset,
or the instructions file unreadable, chat is off and the page says "not
connected". As with the backend pairs in ``app.config``, a URL without its key
stops the app at startup (``check_chat_settings``), and the connection object
below cannot be built without both, so no request to the model can go out
without its bearer key.

Variables (names only; values come from the deployment environment):

* ``LLM_BASE_URL`` and ``LLM_API_KEY``: the chat model service and its
  bearer key. No host or port is built into the code.
* ``LLM_MODEL`` (default empty): model name sent with each request. The
  service hosts one model and does not route on this field, so it is left
  out of the request when empty.
* ``LLM_TIMEOUT_SECONDS`` (default 30): the most one answer may take,
  counting the wait for a free model slot.
* ``CHAT_INSTRUCTIONS_PATH``: the chat instructions file used as the system
  prompt. The file is kept outside this repository; the portal reads it at
  question time and refuses to enable chat when it is unreadable.
* ``CHAT_ENABLED_FOR`` (default ``admin``): who sees chat. ``admin`` shows it
  to the Admin role only, ``all`` to Admins and Viewers, ``none`` to nobody.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

_UNSET = SecretStr("")

DEFAULT_TIMEOUT_SECONDS = 30.0

ChatAudience = Literal["admin", "all", "none"]


class ChatConfigError(ValueError):
    """Chat settings are set but cannot form a usable connection."""


@dataclass(frozen=True)
class ChatConnection:
    """Where and how to reach the chat model.

    Attributes:
        base_url (str): Service base URL, without a trailing slash.
        api_key (str): Bearer key; never shown in ``repr``.
        model (str): Model name, or empty to leave it out of requests.
        timeout_seconds (float): The most one answer may take.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject a blank URL or key and a non-positive timeout.

        Raises:
            ChatConfigError: If the URL or key is blank, or the timeout is
                not positive. The message never includes the key.
        """
        if not self.base_url.strip():
            msg = "the chat model needs a base URL"
            raise ChatConfigError(msg)
        if not self.api_key.strip():
            msg = "the chat model needs a non-blank API key"
            raise ChatConfigError(msg)
        if self.timeout_seconds <= 0:
            msg = "the chat timeout must be positive"
            raise ChatConfigError(msg)


class ChatSettings(BaseSettings):
    """Optional chat configuration read from the environment.

    Attributes:
        model_config: Pydantic settings behavior (ignore unknown variables,
            case-insensitive names).
        llm_base_url (str): Chat model base URL. Empty means off.
        llm_api_key (SecretStr): Chat model bearer key.
        llm_model (str): Model name sent with each request, or empty.
        llm_timeout_seconds (float): The most one answer may take.
        chat_instructions_path (str): The instructions file. Empty means off.
        chat_enabled_for (ChatAudience): Who sees chat.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    llm_base_url: str = ""
    llm_api_key: SecretStr = _UNSET
    llm_model: str = ""
    llm_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    chat_instructions_path: str = ""
    chat_enabled_for: ChatAudience = "admin"

    def connection(self) -> ChatConnection | None:
        """Return the model connection, or None when its URL is unset.

        Returns:
            ChatConnection | None: The connection, or None when off.

        Raises:
            ChatConfigError: If the URL is set but the key is blank, or the
                timeout is not positive. The message names the variable,
                never a value.
        """
        url = self.llm_base_url.strip()
        if not url:
            return None
        key = self.llm_api_key.get_secret_value().strip()
        if not key:
            msg = "LLM_API_KEY must be set when LLM_BASE_URL is set"
            raise ChatConfigError(msg)
        if self.llm_timeout_seconds <= 0:
            msg = "LLM_TIMEOUT_SECONDS must be positive"
            raise ChatConfigError(msg)
        return ChatConnection(
            base_url=url.rstrip("/"),
            api_key=key,
            model=self.llm_model.strip(),
            timeout_seconds=self.llm_timeout_seconds,
        )

    def instructions_file(self) -> Path | None:
        """Return the instructions file path, or None when unset.

        Returns:
            Path | None: The configured file, or None.
        """
        value = self.chat_instructions_path.strip()
        return Path(value) if value else None

    def allows(self, *, is_admin: bool) -> bool:
        """Say whether the feature flag shows chat to this role.

        Args:
            is_admin (bool): True for the Admin role.

        Returns:
            bool: True when chat should be offered to the caller.
        """
        if self.chat_enabled_for == "all":
            return True
        return self.chat_enabled_for == "admin" and is_admin


def load_chat_settings() -> ChatSettings:
    """Build ``ChatSettings`` from the current environment.

    Returns:
        ChatSettings: Settings read fresh on every call.
    """
    return ChatSettings()


def check_chat_settings() -> str | None:
    """Check the chat settings once at startup.

    Returns:
        str | None: A message naming the bad variables (never a value), or
        None when the settings are usable or simply unset.
    """
    try:
        load_chat_settings().connection()
    except ValidationError as exc:
        names = sorted({str(error["loc"][0]).upper() for error in exc.errors()})
        return f"invalid value for {', '.join(names)}"
    except ChatConfigError as exc:
        return str(exc)
    return None
