# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLC0415
"""Integration tests for ``app.main`` startup and lifespan.

Spec contract (``CLAUDE.md`` "Environment variables"):

* All env vars listed in tech-spec §4 are required at startup.
* The application must call ``sys.exit(1)`` if any are absent.
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

if importlib.util.find_spec("app.main") is None:
    pytest.skip("app.main not implemented yet", allow_module_level=True)


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
    cf_env: dict[str, str],
) -> None:
    """``app.main.app`` is a FastAPI instance after env-driven startup.

    Works in both Phase 0/A (no env validation) and Phase 1.

    # noqa
    """
    del cf_env
    from fastapi import FastAPI

    main = importlib.import_module("app.main")
    importlib.reload(main)
    assert isinstance(main.app, FastAPI)


@phase1
@pytest.mark.parametrize("missing", REQUIRED_ENV_VARS)
def test_missing_env_var_causes_startup_failure(
    cf_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """Per ``CLAUDE.md``: a missing required env var must cause ``sys.exit(1)``.

    # noqa
    """
    del cf_env
    monkeypatch.delenv(missing, raising=False)

    main = importlib.import_module("app.main")

    with pytest.raises(SystemExit) as exc_info:
        importlib.reload(main)
    assert exc_info.value.code == 1


@phase1
def test_static_files_mount_is_registered(
    cf_env: dict[str, str],
) -> None:
    """Per ``CLAUDE.md``: htmx.min.js and chart.umd.min.js are vendored under
    ``static/`` and must be served by the application (no CDN).

    # noqa
    """
    del cf_env
    main = importlib.import_module("app.main")
    importlib.reload(main)
    routes = [getattr(r, "path", "") for r in main.app.routes]
    assert any(path.startswith("/static") for path in routes)
