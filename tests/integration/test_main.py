# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLC0415
"""Integration tests for ``app.main`` startup and lifespan.

Spec contract (``CLAUDE.md`` "Environment variables"):

* The authentication and SQLite variables are required at startup.
* The application must call ``sys.exit(1)`` if any are absent.
* Each backend is an optional ``BACKEND_<NAME>_URL`` plus
  ``BACKEND_<NAME>_API_KEY`` pair: a URL with a blank key stops startup, and
  an unset URL means the backend is not connected.
* No optional env vars without a documented default.

Phase 0 / Phase A ship only a minimal FastAPI instance with ``/health``; the
env-var fail-fast and static-mount behaviors land in Phase 1. The Phase 1
tests below skip until the section routes (e.g. ``/documents``) are mounted,
which we use as a proxy signal that Phase 1 startup is in place.
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest
from structlog.testing import capture_logs

if importlib.util.find_spec("app.main") is None:
    pytest.skip("app.main not implemented yet", allow_module_level=True)


REQUIRED_ENV_VARS = (
    "AUTHENTIK_JWT_SECRET",
    "AUTHENTIK_ISSUER",
    "AUTHENTIK_AUDIENCE",
    "SQLITE_PATH",
)


def _phase1_main_present() -> bool:
    """True iff ``app.main`` has Phase 1 section routes mounted.

    Used as a proxy for "Phase 1 startup is in place" -- when section routes
    are mounted, env-var fail-fast and static mount are also expected.

    # noqa
    """
    try:
        main = importlib.import_module("app.main")
    except SystemExit:
        # Phase 1 fail-fast already engaged but env not set in collection -- treat
        # as Phase 1 present.
        return True
    paths = {getattr(r, "path", "") for r in main.app.routes}
    return "/documents" in paths


phase1 = pytest.mark.skipif(
    not _phase1_main_present(),
    reason="Phase 1 section routes not yet mounted on app.main",
)


def test_app_attribute_is_a_fastapi_instance(
    portal_env: dict[str, str],
) -> None:
    """``app.main.app`` is a FastAPI instance after env-driven startup.

    Works in both Phase 0/A (no env validation) and Phase 1.

    # noqa
    """
    del portal_env
    from fastapi import FastAPI

    main = importlib.import_module("app.main")
    importlib.reload(main)
    assert isinstance(main.app, FastAPI)


@phase1
@pytest.mark.parametrize("missing", REQUIRED_ENV_VARS)
def test_missing_env_var_causes_startup_failure(
    portal_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    """Per ``CLAUDE.md``: a missing required env var must cause ``sys.exit(1)``.

    The variable's name, never its value, is printed to stderr.

    # noqa
    """
    del portal_env
    monkeypatch.delenv(missing, raising=False)

    main = importlib.import_module("app.main")

    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1
    assert missing in capsys.readouterr().err


@phase1
def test_static_files_mount_is_registered(
    portal_env: dict[str, str],
) -> None:
    """Per ``CLAUDE.md``: htmx.min.js and chart.umd.min.js are vendored under
    ``static/`` and must be served by the application (no CDN).

    # noqa
    """
    del portal_env
    main = importlib.import_module("app.main")
    importlib.reload(main)
    routes = [getattr(r, "path", "") for r in main.app.routes]
    assert any(path.startswith("/static") for path in routes)


def test_openapi_schema_builds(portal_env: dict[str, str]) -> None:
    """The OpenAPI document renders; every route annotation resolves at runtime.

    # noqa
    """
    del portal_env
    main = importlib.import_module("app.main")
    importlib.reload(main)
    schema = main.app.openapi()
    assert "/documents/search" in schema["paths"]


# --------------------------------------------------------------------------- #
# Backend URL and API key pairs
# --------------------------------------------------------------------------- #

BACKEND_PAIRS = (
    ("BACKEND_LLC_MANAGER_URL", "BACKEND_LLC_MANAGER_API_KEY"),
    ("BACKEND_PP_SECURITY_URL", "BACKEND_PP_SECURITY_API_KEY"),
    ("BACKEND_XERO_CRYPTO_URL", "BACKEND_XERO_CRYPTO_API_KEY"),
    ("BACKEND_DATA_INGESTOR_URL", "BACKEND_DATA_INGESTOR_API_KEY"),
)


def _load_main() -> None:
    importlib.reload(importlib.import_module("app.main"))


@pytest.mark.usefixtures("portal_env")
@pytest.mark.parametrize(("url_var", "key_var"), BACKEND_PAIRS)
@pytest.mark.parametrize("blank", ["unset", "", "   ", "\t\n"])
def test_backend_url_without_key_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url_var: str,
    key_var: str,
    blank: str,
) -> None:
    """A set URL with an unset, empty or whitespace key exits 1 naming the key.

    # noqa
    """
    monkeypatch.setenv(url_var, "http://backend.test")
    if blank == "unset":
        monkeypatch.delenv(key_var, raising=False)
    else:
        monkeypatch.setenv(key_var, blank)

    with pytest.raises(SystemExit) as exc_info:
        _load_main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert key_var in err
    assert url_var in err


def test_every_backend_without_key_is_named(
    portal_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When several backends are misconfigured the error names each key.

    # noqa
    """
    del portal_env
    for url_var, key_var in BACKEND_PAIRS:
        monkeypatch.setenv(url_var, "http://backend.test")
        monkeypatch.delenv(key_var, raising=False)

    with pytest.raises(SystemExit):
        _load_main()

    err = capsys.readouterr().err
    for _, key_var in BACKEND_PAIRS:
        assert key_var in err


@pytest.mark.parametrize(("url_var", "key_var"), BACKEND_PAIRS)
def test_backend_key_without_url_starts_and_logs_info(
    portal_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    url_var: str,
    key_var: str,
) -> None:
    """A key with no URL is allowed; startup logs one info line naming it.

    # noqa
    """
    del portal_env
    monkeypatch.delenv(url_var, raising=False)
    monkeypatch.setenv(key_var, "a-key-with-no-backend")

    with capture_logs() as logs:
        _load_main()

    notes = [e for e in logs if e["event"] == "backend_key_without_url"]
    assert len(notes) == 1
    assert notes[0]["log_level"] == "info"
    assert notes[0]["key_variable"] == key_var
    assert notes[0]["url_variable"] == url_var
    assert "a-key-with-no-backend" not in str(notes[0])


@pytest.mark.parametrize(("url_var", "key_var"), BACKEND_PAIRS)
def test_backend_with_neither_url_nor_key_starts_quietly(
    portal_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    url_var: str,
    key_var: str,
) -> None:
    """A backend with neither variable is simply not connected.

    # noqa
    """
    del portal_env
    monkeypatch.delenv(url_var, raising=False)
    monkeypatch.delenv(key_var, raising=False)

    with capture_logs() as logs:
        _load_main()

    assert not [e for e in logs if e["event"] == "backend_key_without_url"]


@pytest.mark.parametrize(("url_var", "key_var"), BACKEND_PAIRS)
def test_backend_with_url_and_key_starts(
    portal_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    url_var: str,
    key_var: str,
) -> None:
    """A URL with a real key is a valid connected backend.

    # noqa
    """
    del portal_env
    monkeypatch.setenv(url_var, "http://backend.test")
    monkeypatch.setenv(key_var, "a-real-key")

    _load_main()
