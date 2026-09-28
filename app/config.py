# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Environment-driven settings for the family office portal.

The startup-required variables are listed once, in ``app.main``
(``_REQUIRED_ENV_VARS``), which exits with status 1 when any is missing. This
module only reads the values the cache, scheduler, and templates need; every
setting it declares without a default is also on that list, and every other
setting has a documented default. Authentication settings are read by
``app.middleware.authentik.AuthentikSettings``, not here. Settings are read
fresh on each ``load_settings()`` call so tests can patch the environment
without clearing a cache.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_NO_API_KEY = SecretStr("")


class Settings(BaseSettings):
    """Portal configuration loaded from environment variables.

    Attributes:
        model_config: Pydantic settings behavior (ignore
            unknown variables, case-insensitive names).
        backend_llc_manager_url (str): Base URL of the llc-manager API.
        backend_llc_manager_api_key (SecretStr): Optional API key sent to
            llc-manager as ``X-API-Key``. Defaults to empty; unset sends no
            key header.
        backend_pp_security_url (str): Base URL of the pp-security-master API.
        backend_pp_security_api_key (SecretStr): Optional API key sent to
            pp-security-master as ``X-API-Key``. Defaults to empty; unset
            sends no key header.
        backend_xero_crypto_url (str): Base URL of the xero-crypto API.
        backend_xero_crypto_api_key (SecretStr): Optional API key sent to
            xero-crypto as ``X-API-Key``. Defaults to empty; unset sends no
            key header.
        sqlite_path (str): Filesystem path of the SQLite cache database.
        backend_timeout_seconds (float): Timeout for outbound backend calls.
        display_timezone (str): IANA time zone used for "last updated"
            labels shown to primary users.
        scheduler_enabled (bool): Start the refresh scheduler at startup.
            Set false for local template work without backends.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    backend_llc_manager_url: str
    backend_llc_manager_api_key: SecretStr = _NO_API_KEY
    backend_pp_security_url: str
    backend_pp_security_api_key: SecretStr = _NO_API_KEY
    backend_xero_crypto_url: str
    backend_xero_crypto_api_key: SecretStr = _NO_API_KEY
    sqlite_path: str
    backend_timeout_seconds: float = 10.0
    display_timezone: str = "UTC"
    scheduler_enabled: bool = True


def load_settings() -> Settings:
    """Build a ``Settings`` instance from the current environment.

    Returns:
        Settings: Validated portal settings.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from env
