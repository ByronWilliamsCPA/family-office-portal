# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Read the chat instructions file, which is never part of this repository.

The path comes from ``CHAT_INSTRUCTIONS_PATH``. The file is read at question
time, so an edit takes effect on the next question.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from pathlib import Path

MAX_INSTRUCTIONS_BYTES = 64 * 1024


class InstructionsError(RuntimeError):
    """The instructions file is unset, unreadable, empty or too large."""


def instructions_readable(path: Path | None) -> bool:
    """Say whether the instructions file exists and can be read.

    Args:
        path (Path | None): Configured path, or None when unset.

    Returns:
        bool: True when the file is a readable, non-empty regular file.
    """
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0 and _can_open(path)
    except OSError:
        return False


def _can_open(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.read(1)
    return True


async def load_instructions(path: Path | None) -> str:
    """Read the instructions file.

    Args:
        path (Path | None): Configured path, or None when unset.

    Returns:
        str: The instructions text.

    Raises:
        InstructionsError: If the path is unset, the file cannot be read, is
            not UTF-8, is empty, or is over 64 KB.
    """
    if path is None:
        msg = "CHAT_INSTRUCTIONS_PATH is not set"
        raise InstructionsError(msg)
    try:
        async with await anyio.Path(path).open("rb") as handle:
            raw = await handle.read(MAX_INSTRUCTIONS_BYTES + 1)
    except OSError as exc:
        msg = "the chat instructions file cannot be read"
        raise InstructionsError(msg) from exc
    if len(raw) > MAX_INSTRUCTIONS_BYTES:
        msg = "the chat instructions file is empty or too large"
        raise InstructionsError(msg)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "the chat instructions file cannot be read"
        raise InstructionsError(msg) from exc
    if not text.strip():
        msg = "the chat instructions file is empty or too large"
        raise InstructionsError(msg)
    return text
