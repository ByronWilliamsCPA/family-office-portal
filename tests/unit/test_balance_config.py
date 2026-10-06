# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the optional balance intake key setting.

The key is read from ``BALANCE_INTAKE_API_KEY``. Unset or blank means the
intake is disabled. A set key must be at least 32 characters of printable
ASCII and must differ from the sign-in secret, which is checked at startup;
the value never appears in an error, a ``repr`` or a log. All keys here are
generated inside the tests.
"""

from __future__ import annotations

import importlib
import secrets

import pytest

from app.config import (
    MIN_INTAKE_KEY_LENGTH,
    BackendConfigError,
    check_balance_intake,
    load_settings,
)


@pytest.fixture(autouse=True)
def _clean_env(  # pyright: ignore[reportUnusedFunction]
    portal_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start with no intake key set."""
    del portal_env
    monkeypatch.delenv("BALANCE_INTAKE_API_KEY", raising=False)


def _reload_main() -> None:
    """Re-run ``app.main`` at import, which runs the startup checks."""
    importlib.reload(importlib.import_module("app.main"))


def test_unset_key_disables_the_intake() -> None:
    """No variable: disabled, empty key, startup check passes."""
    settings = load_settings()
    assert settings.balance_intake_key() == ""
    assert settings.balance_intake_enabled() is False
    check_balance_intake(settings)


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n"])
def test_blank_key_disables_the_intake(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """An empty or whitespace key is the same as unset."""
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", blank)
    settings = load_settings()
    assert settings.balance_intake_enabled() is False
    check_balance_intake(settings)


def test_strong_key_enables_the_intake_and_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long key enables the intake; surrounding whitespace is dropped."""
    key = secrets.token_urlsafe(MIN_INTAKE_KEY_LENGTH)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", f" {key}\n")
    settings = load_settings()
    assert settings.balance_intake_key() == key
    assert settings.balance_intake_enabled() is True
    check_balance_intake(settings)


def test_short_key_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short key fails the startup check; the message names only the variable."""
    short = secrets.token_hex(8)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", short)
    settings = load_settings()
    with pytest.raises(BackendConfigError) as exc_info:
        check_balance_intake(settings)
    message = str(exc_info.value)
    assert "BALANCE_INTAKE_API_KEY" in message
    assert str(MIN_INTAKE_KEY_LENGTH) in message
    assert short not in message


def test_key_length_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """31 characters fail and 32 pass."""
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", "k" * (MIN_INTAKE_KEY_LENGTH - 1))
    too_short = load_settings()
    with pytest.raises(BackendConfigError):
        check_balance_intake(too_short)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", "k" * MIN_INTAKE_KEY_LENGTH)
    check_balance_intake(load_settings())


def test_settings_repr_hides_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither ``repr`` nor ``str`` of the settings shows the key."""
    key = secrets.token_urlsafe(MIN_INTAKE_KEY_LENGTH)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", key)
    settings = load_settings()
    assert key not in repr(settings)
    assert key not in str(settings)
    assert key not in repr(settings.balance_intake_api_key)


def test_app_refuses_to_start_with_a_short_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A short key stops startup with exit 1 and no value in the message."""
    short = secrets.token_hex(4)
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", short)
    with pytest.raises(SystemExit) as exc_info:
        _reload_main()
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "BALANCE_INTAKE_API_KEY" in err
    assert short not in err


def test_app_starts_without_the_key() -> None:
    """The setting is optional: the app loads when it is absent."""
    _reload_main()


@pytest.mark.parametrize(
    "suffix", ["é", "☃", "\x7f", "\t"], ids=["latin", "symbol", "del", "tab"]
)
def test_non_printable_ascii_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    """A key a header could never carry intact stops startup, unechoed."""
    key = secrets.token_hex(MIN_INTAKE_KEY_LENGTH) + suffix + "k"
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", key)
    settings = load_settings()
    with pytest.raises(BackendConfigError) as exc_info:
        check_balance_intake(settings)
    message = str(exc_info.value)
    assert "printable ASCII" in message
    assert key not in message


def test_key_equal_to_the_sign_in_secret_is_refused(
    monkeypatch: pytest.MonkeyPatch, portal_env: dict[str, str]
) -> None:
    """Reusing the sign-in secret as the intake key stops startup."""
    secret = portal_env["AUTHENTIK_JWT_SECRET"]
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", secret)
    settings = load_settings()
    with pytest.raises(BackendConfigError) as exc_info:
        check_balance_intake(settings, jwt_secret=secret)
    message = str(exc_info.value)
    assert "AUTHENTIK_JWT_SECRET" in message
    assert secret not in message
    check_balance_intake(settings, jwt_secret=secret + "x")


def test_app_refuses_to_start_when_the_key_reuses_the_sign_in_secret(
    monkeypatch: pytest.MonkeyPatch,
    portal_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Startup passes the sign-in secret to the check."""
    secret = portal_env["AUTHENTIK_JWT_SECRET"]
    monkeypatch.setenv("BALANCE_INTAKE_API_KEY", secret)
    with pytest.raises(SystemExit) as exc_info:
        _reload_main()
    assert exc_info.value.code == 1
    assert secret not in capsys.readouterr().err
