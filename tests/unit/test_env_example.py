# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Guard tests for the ``.env.example`` file at the repository root.

Rules covered:

* the names documented in ``.env.example`` (active lines and commented-out
  ``# NAME=`` lines) are exactly the environment variables the application
  reads, so a new setting cannot ship undocumented and a removed one cannot
  linger;
* every name ending in ``_KEY`` or ``_SECRET`` has an empty value, so a real
  credential can never be committed through the example file;
* copying the file enables nothing: no backend, chat or document search.

The application's names are discovered, not listed by hand:

* each ``pydantic`` settings class contributes its field names, upper-cased
  (``Settings``, ``ChatSettings``, ``RetrievalSettings``), because the
  settings are read case-insensitively with no prefix or alias;
* ``app/middleware/authentik.py`` reads its variables through a plain
  mapping, so its module-level and literal string constants that look like
  ``AUTHENTIK_*`` or ``FO_*`` names are collected from its syntax tree;
* every literal ``os.environ.get``, ``os.environ[...]`` and ``os.getenv``
  call under ``app/`` must name a documented variable, which catches a new
  direct read in any module.

#ASSUME: a future setting is added as a field on one of the three settings
classes or as a literal ``AUTHENTIK_*``/``FO_*`` name in the Authentik
middleware. #VERIFY: if a fourth settings class or a new prefix appears, extend
``_application_names`` so this test still sees it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.chat.settings import ChatSettings, load_chat_settings
from app.config import Settings, load_settings
from app.retrieval.settings import RetrievalSettings, load_retrieval_settings

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
APP_DIR = REPO_ROOT / "app"
AUTHENTIK_MODULE = APP_DIR / "middleware" / "authentik.py"

# NAME=value or "# NAME=value": the commented form marks a setting that is
# documented but switched off. Prose comments never start a line with
# ``NAME=``, so they do not match.
_ENTRY = re.compile(r"^(?P<off>#\s*)?(?P<name>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
_AUTH_NAME = re.compile(r"(?:AUTHENTIK|FO)_[A-Z0-9_]+")
_SECRET_SUFFIXES = ("_KEY", "_SECRET")


def _parse_env_example() -> list[tuple[str, str, bool]]:
    """Read ``.env.example`` into (name, value, active) entries.

    Returns:
        list[tuple[str, str, bool]]: One entry per ``NAME=value`` line, in file
        order; ``active`` is False for a commented-out line.
    """
    entries: list[tuple[str, str, bool]] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _ENTRY.match(line.rstrip())
        if match:
            entries.append(
                (match["name"], match["value"].strip(), match["off"] is None)
            )
    return entries


def _documented_names() -> set[str]:
    """Return the names documented in ``.env.example``, active or not.

    Returns:
        set[str]: Variable names.
    """
    return {name for name, _, _ in _parse_env_example()}


def _authentik_names() -> set[str]:
    """Collect the variable names the Authentik middleware reads.

    Returns:
        set[str]: String constants in the module that are entirely an
        ``AUTHENTIK_*`` or ``FO_*`` name.
    """
    tree = ast.parse(AUTHENTIK_MODULE.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _AUTH_NAME.fullmatch(node.value)
    }


def _application_names() -> set[str]:
    """Return every environment variable name the application reads.

    Returns:
        set[str]: Variable names from the settings classes and the Authentik
        middleware.
    """
    names: set[str] = set()
    for settings_class in (Settings, ChatSettings, RetrievalSettings):
        names.update(field.upper() for field in settings_class.model_fields)
    names.update(_authentik_names())
    return names


def _literal_environ_reads() -> set[str]:
    """Find variable names read directly from ``os.environ`` under ``app/``.

    Returns:
        set[str]: Literal names passed to ``os.environ.get``, ``os.getenv``
        or used as ``os.environ[...]`` subscripts.
    """
    found: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            literal: ast.expr | None = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.args
                and (ast.unparse(node.func) in {"os.environ.get", "os.getenv"})
            ):
                literal = node.args[0]
            elif (
                isinstance(node, ast.Subscript)
                and ast.unparse(node.value) == "os.environ"
            ):
                literal = node.slice
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                found.add(literal.value)
    return found


def test_example_file_exists_and_is_not_empty() -> None:
    """The example file the README tells people to copy must exist."""
    assert ENV_EXAMPLE.is_file()
    assert _parse_env_example()


def test_every_name_is_documented_once() -> None:
    """A name must not be listed twice, active or commented out."""
    names = [name for name, _, _ in _parse_env_example()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == []


def test_documented_names_match_the_names_the_application_reads() -> None:
    """Adding or removing a setting without updating the file must fail."""
    documented = _documented_names()
    read = _application_names()
    assert sorted(read - documented) == [], "read by the app but not documented"
    assert sorted(documented - read) == [], "documented but never read by the app"


def test_direct_environ_reads_are_documented() -> None:
    """A new literal ``os.environ`` read in any module must be documented."""
    assert sorted(_literal_environ_reads() - _documented_names()) == []


def test_application_name_discovery_finds_every_group() -> None:
    """Discovery must see each source, so a broken scan cannot pass silently."""
    names = _application_names()
    for expected in (
        "SQLITE_PATH",
        "AUTHENTIK_JWT_SECRET",
        "FO_ADMIN_GROUP",
        "BACKEND_LLC_MANAGER_API_KEY",
        "BALANCE_INTAKE_API_KEY",
        "LLM_BASE_URL",
        "QDRANT_API_KEY",
    ):
        assert expected in names
    assert "SQLITE_PATH" in _literal_environ_reads()


def test_secret_variables_have_empty_values() -> None:
    """No name ending in _KEY or _SECRET may carry a value in the example."""
    secrets_seen = [
        (name, value)
        for name, value, _ in _parse_env_example()
        if name.endswith(_SECRET_SUFFIXES)
    ]
    assert secrets_seen, "expected at least one secret variable to be documented"
    assert [name for name, value in secrets_seen if value] == []


def test_copying_the_file_enables_no_optional_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only the active lines applied, every optional feature is off.

    Args:
        monkeypatch: Pytest fixture used to set and clear the environment.
    """
    for name in _application_names():
        monkeypatch.delenv(name, raising=False)
    for name, value, active in _parse_env_example():
        if active:
            monkeypatch.setenv(name, value)

    settings = load_settings()
    assert not any(
        settings.is_connected(name)
        for name in ("llc_manager", "pp_security", "xero_crypto", "data_ingestor")
    )
    assert settings.balance_intake_enabled() is False

    assert load_chat_settings().connection() is None
    retrieval = load_retrieval_settings()
    assert retrieval.embedding_connection() is None
    assert retrieval.qdrant_connection() is None
    assert retrieval.search_connected() is False
