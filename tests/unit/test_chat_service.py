# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for chat service helpers (app.chat.service)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from app.chat import instructions as instructions_module
from app.chat import service
from app.chat.client import ChatClient
from app.chat.instructions import (
    MAX_INSTRUCTIONS_BYTES,
    InstructionsError,
    instructions_readable,
    load_instructions,
)
from app.chat.service import chat_connection, chat_panel_state, check_question
from app.chat.settings import ChatConnection
from app.routes.chat import build_chat_client
from tests.unit.chat_fakes import SYNTHETIC_INSTRUCTIONS, random_key, write_instructions

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_BASE_URL", "LLM_API_KEY", "CHAT_INSTRUCTIONS_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CHAT_ENABLED_FOR", raising=False)


def test_instructions_readable(tmp_path: Path) -> None:
    """Only a non-empty regular file counts as readable."""
    assert instructions_readable(None) is False
    assert instructions_readable(tmp_path) is False
    assert instructions_readable(tmp_path / "missing.md") is False
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    assert instructions_readable(empty) is False
    assert instructions_readable(write_instructions(tmp_path)) is True


def test_instructions_unreadable_on_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission error while opening counts as unreadable."""

    def deny(path: Path) -> bool:
        raise PermissionError(str(path))

    monkeypatch.setattr(instructions_module, "_can_open", deny)
    assert instructions_readable(write_instructions(tmp_path)) is False


async def test_load_instructions_reads_text(tmp_path: Path) -> None:
    """The file's text is returned as is."""
    assert await load_instructions(write_instructions(tmp_path)) == (
        SYNTHETIC_INSTRUCTIONS
    )


async def test_load_instructions_refuses_unset_path() -> None:
    """An unset path is an error naming the variable."""
    with pytest.raises(InstructionsError, match="CHAT_INSTRUCTIONS_PATH"):
        await load_instructions(None)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xff\xfe\x00bad", "cannot be read"),
        (b"   \n", "empty or too large"),
        (b"x" * (MAX_INSTRUCTIONS_BYTES + 1), "empty or too large"),
    ],
)
async def test_load_instructions_refuses_bad_files(
    tmp_path: Path, content: bytes, message: str
) -> None:
    """Non-UTF-8, blank and oversized files are refused."""
    path = tmp_path / "bad.md"
    path.write_bytes(content)
    with pytest.raises(InstructionsError, match=message):
        await load_instructions(path)


async def test_load_instructions_refuses_missing_file(tmp_path: Path) -> None:
    """A missing file cannot be read."""
    with pytest.raises(InstructionsError, match="cannot be read"):
        await load_instructions(tmp_path / "missing.md")


def test_chat_connection_is_none_when_misconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL without its key is treated as not connected at request time."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    assert chat_connection() is None


async def test_panel_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Visible follows the flag; connected needs URL, key and instructions."""
    assert await chat_panel_state(is_admin=True) == {
        "visible": True,
        "connected": False,
    }
    assert await chat_panel_state(is_admin=False) == {
        "visible": False,
        "connected": False,
    }
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", random_key())
    monkeypatch.setenv("CHAT_INSTRUCTIONS_PATH", str(write_instructions(tmp_path)))
    assert await chat_panel_state(is_admin=True) == {
        "visible": True,
        "connected": True,
    }


async def test_panel_state_survives_a_bad_value_set_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that stops parsing mid-run turns chat off instead of failing Home."""
    monkeypatch.setenv("CHAT_ENABLED_FOR", "everyone")
    assert await chat_panel_state(is_admin=True) == {
        "visible": False,
        "connected": False,
    }


def test_chat_connection_is_none_for_an_unparsable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout that is not a number counts as not connected."""
    monkeypatch.setenv("LLM_BASE_URL", "http://chat.test")
    monkeypatch.setenv("LLM_API_KEY", random_key())
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "soon")
    assert chat_connection() is None


def test_check_question() -> None:
    """Blank and overlong questions get plain errors."""
    assert check_question("") == service.MSG_EMPTY
    assert check_question("x" * 1001) == service.MSG_TOO_LONG
    assert check_question("x" * 1000) is None


def test_default_chat_client_builder() -> None:
    """The route's builder returns a real client for the connection."""
    connection = ChatConnection(
        base_url="http://chat.test", api_key=SecretStr(random_key())
    )
    assert isinstance(build_chat_client(connection), ChatClient)
