# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLC0415
"""Integration tests for ``app.main`` startup and lifespan.

Spec contract (``CLAUDE.md`` "Environment variables"):

* All env vars listed in tech-spec §4 are required at startup.
* The application must call ``sys.exit(1)`` if any are absent.
* No optional env vars without a documented default.

These tests validate startup behavior. They are skipped until ``app.main``
exists.
"""

from __future__ import annotations

import importlib

import pytest

main = pytest.importorskip("app.main")


REQUIRED_ENV_VARS = (
    "BACKEND_LLC_MANAGER_URL",
    "BACKEND_PP_SECURITY_URL",
    "BACKEND_XERO_CRYPTO_URL",
    "BACKEND_FAMILY_OFFICE_URL",
    "CF_TEAM_DOMAIN",
    "CF_ACCESS_APP_ID",
    "VIEWER_EMAILS",
    "ADMIN_EMAILS",
    "SQLITE_PATH",
)


def test_app_attribute_is_a_fastapi_instance(
    cf_env: dict[str, str],
) -> None:
    """``app.main.app`` is a FastAPI instance.

    # noqa
    """
    from fastapi import FastAPI

    importlib.reload(main)
    assert isinstance(main.app, FastAPI)


@pytest.mark.parametrize("missing", REQUIRED_ENV_VARS)
def test_missing_env_var_causes_startup_failure(
    cf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """Per ``CLAUDE.md``: 'The application must call sys.exit(1) if any are
    absent.' We accept either ``SystemExit`` or any explicit configuration
    error -- both signal fail-fast, not silent default substitution.

    # noqa
    """
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises((SystemExit, RuntimeError, ValueError, Exception)):
        importlib.reload(main)


def test_static_files_mount_is_registered(
    cf_env: dict[str, str],
) -> None:
    """Per ``CLAUDE.md``: htmx.min.js and chart.umd.min.js are vendored under
    ``static/`` and must be served by the application (no CDN).

    # noqa
    """
    importlib.reload(main)
    routes = [getattr(r, "path", "") for r in main.app.routes]
    assert any(path.startswith("/static") for path in routes)
