# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Unit tests for the backend URL and API key pairs in ``app.config``.

Rules covered:

* a URL with an unset, empty or whitespace key is an error naming the key;
* a key with no URL is allowed and logged at info level;
* a backend with no URL is not connected;
* a ``BackendConnection`` cannot exist without both a URL and a key.
"""

from __future__ import annotations

import secrets

import pytest
from structlog.testing import capture_logs

from app.config import (
    BACKENDS,
    BackendConfigError,
    BackendConnection,
    BackendSpec,
    check_backends,
    load_settings,
)

BACKEND_IDS = [spec.name for spec in BACKENDS]
BLANK_KEYS = ["", " ", "   ", "\t", "\n"]


@pytest.fixture(autouse=True)
def _clean_backend_env(  # pyright: ignore[reportUnusedFunction]
    portal_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start every test with no backend variables set."""
    del portal_env
    for spec in BACKENDS:
        monkeypatch.delenv(spec.url_var, raising=False)
        monkeypatch.delenv(spec.key_var, raising=False)


def test_registry_lists_the_four_backends() -> None:
    """The three existing names are kept and data-ingestor is added."""
    assert {spec.url_var for spec in BACKENDS} == {
        "BACKEND_LLC_MANAGER_URL",
        "BACKEND_PP_SECURITY_URL",
        "BACKEND_XERO_CRYPTO_URL",
        "BACKEND_DATA_INGESTOR_URL",
    }
    assert {spec.key_var for spec in BACKENDS} == {
        "BACKEND_LLC_MANAGER_API_KEY",
        "BACKEND_PP_SECURITY_API_KEY",
        "BACKEND_XERO_CRYPTO_API_KEY",
        "BACKEND_DATA_INGESTOR_API_KEY",
    }


@pytest.mark.parametrize("spec", BACKENDS, ids=BACKEND_IDS)
def test_backend_is_not_connected_by_default(spec: BackendSpec) -> None:
    """With no variables set every backend is not connected."""
    settings = load_settings()
    assert settings.is_connected(spec.name) is False
    assert settings.backend_connection(spec.name) is None
    check_backends(settings)


@pytest.mark.parametrize("spec", BACKENDS, ids=BACKEND_IDS)
@pytest.mark.parametrize("blank", [None, *BLANK_KEYS])
def test_url_without_usable_key_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
    spec: BackendSpec,
    blank: str | None,
) -> None:
    """A URL with an unset, empty or whitespace key fails and names the key."""
    monkeypatch.setenv(spec.url_var, "http://backend.test")
    if blank is not None:
        monkeypatch.setenv(spec.key_var, blank)
    settings = load_settings()

    with pytest.raises(BackendConfigError, match=spec.key_var):
        check_backends(settings)
    with pytest.raises(BackendConfigError, match=spec.key_var):
        settings.backend_connection(spec.name)


@pytest.mark.parametrize("spec", BACKENDS, ids=BACKEND_IDS)
def test_url_with_key_is_connected(
    monkeypatch: pytest.MonkeyPatch,
    spec: BackendSpec,
) -> None:
    """A URL plus a key passes validation and yields a connection."""
    monkeypatch.setenv(spec.url_var, "  http://backend.test/  ")
    key = secrets.token_urlsafe(16)
    monkeypatch.setenv(spec.key_var, f"  {key}\n")
    settings = load_settings()

    check_backends(settings)
    connection = settings.backend_connection(spec.name)

    assert settings.is_connected(spec.name) is True
    assert connection is not None
    assert connection.url == "http://backend.test/"
    assert connection.api_key == key


@pytest.mark.parametrize("spec", BACKENDS, ids=BACKEND_IDS)
@pytest.mark.parametrize("blank_url", ["", "   "])
def test_blank_url_means_not_connected(
    monkeypatch: pytest.MonkeyPatch,
    spec: BackendSpec,
    blank_url: str,
) -> None:
    """An empty or whitespace URL is the same as an unset URL."""
    monkeypatch.setenv(spec.url_var, blank_url)
    monkeypatch.setenv(spec.key_var, "some-key")
    settings = load_settings()

    assert settings.is_connected(spec.name) is False
    assert settings.backend_connection(spec.name) is None


@pytest.mark.parametrize("spec", BACKENDS, ids=BACKEND_IDS)
def test_key_without_url_is_allowed_and_logged_at_info(
    monkeypatch: pytest.MonkeyPatch,
    spec: BackendSpec,
) -> None:
    """A key with no URL passes and logs one info line naming both variables."""
    monkeypatch.setenv(spec.key_var, "key-without-a-backend")
    settings = load_settings()

    with capture_logs() as logs:
        check_backends(settings)

    assert len(logs) == 1
    assert logs[0]["log_level"] == "info"
    assert logs[0]["key_variable"] == spec.key_var
    assert logs[0]["url_variable"] == spec.url_var
    assert "key-without-a-backend" not in str(logs[0])
    assert settings.backend_connection(spec.name) is None


def test_all_misconfigured_keys_are_named_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One error lists every backend whose URL has no usable key."""
    for spec in BACKENDS:
        monkeypatch.setenv(spec.url_var, "http://backend.test")
    with pytest.raises(BackendConfigError) as exc_info:
        check_backends(load_settings())
    for spec in BACKENDS:
        assert spec.key_var in str(exc_info.value)


def test_error_never_contains_a_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error names variables only, even when another key is set."""
    monkeypatch.setenv("BACKEND_LLC_MANAGER_URL", "http://backend.test")
    monkeypatch.setenv("BACKEND_PP_SECURITY_URL", "http://backend.test")
    monkeypatch.setenv("BACKEND_PP_SECURITY_API_KEY", "super-secret-value")
    with pytest.raises(BackendConfigError) as exc_info:
        check_backends(load_settings())
    assert "super-secret-value" not in str(exc_info.value)


@pytest.mark.parametrize("blank", BLANK_KEYS)
def test_connection_cannot_be_built_without_a_key(blank: str) -> None:
    """A ``BackendConnection`` with a blank key is impossible to construct."""
    with pytest.raises(BackendConfigError):
        BackendConnection(label="llc-manager", url="http://backend.test", api_key=blank)


@pytest.mark.parametrize("blank", ["", "  "])
def test_connection_cannot_be_built_without_a_url(blank: str) -> None:
    """A ``BackendConnection`` with a blank URL is impossible to construct."""
    with pytest.raises(BackendConfigError):
        BackendConnection(
            label="llc-manager", url=blank, api_key=secrets.token_urlsafe(16)
        )


def test_connection_repr_hides_the_key() -> None:
    """The key never appears in the repr, so logs cannot leak it."""
    key = secrets.token_urlsafe(16)
    connection = BackendConnection(
        label="llc-manager", url="http://backend.test", api_key=key
    )
    assert key not in repr(connection)
