# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Environment-driven settings for the family office portal.

Every setting without a default is required at startup; ``app.main`` exits
with status 1 when any is missing (see ``REQUIRED_ENV_VARS``). Settings are
read fresh on each ``load_settings()`` call so tests can patch the environment
without clearing a cache.
"""

from __future__ import annotations

from pydantic import (
    SecretStr,  # noqa: TC002  # pydantic resolves annotations at runtime
)
from pydantic_settings import BaseSettings, SettingsConfigDict

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "BACKEND_LLC_MANAGER_URL",
    "BACKEND_LLC_MANAGER_API_KEY",
    "BACKEND_PP_SECURITY_URL",
    "BACKEND_PP_SECURITY_API_KEY",
    "BACKEND_XERO_CRYPTO_URL",
    "BACKEND_XERO_CRYPTO_API_KEY",
    "AUTHENTIK_JWKS_URL",
    "AUTHENTIK_ISSUER",
    "AUTHENTIK_AUDIENCE",
    "SQLITE_PATH",
)


class Settings(BaseSettings):
    """Portal configuration loaded from environment variables.

    Attributes:
        model_config: Pydantic settings behavior (ignore
            unknown variables, case-insensitive names).
        backend_llc_manager_url (str): Base URL of the llc-manager API.
        backend_llc_manager_api_key (SecretStr): API key sent to llc-manager.
        backend_pp_security_url (str): Base URL of the pp-security-master API.
        backend_pp_security_api_key (SecretStr): API key sent to
            pp-security-master.
        backend_xero_crypto_url (str): Base URL of the xero-crypto API.
        backend_xero_crypto_api_key (SecretStr): API key sent to xero-crypto.
        authentik_jwks_url (str): JWKS URL of the Authentik proxy provider that
            signs ``X-authentik-jwt``.
        authentik_issuer (str): Expected ``iss`` claim of that JWT.
        authentik_audience (str): Expected ``aud`` claim (the provider's
            client ID).
        sqlite_path (str): Filesystem path of the SQLite cache database.
        fo_viewer_group (str): Authentik group granting the Viewer role.
        fo_admin_group (str): Authentik group granting the Admin role.
        backend_timeout_seconds (float): Timeout for outbound backend calls.
        jwks_cache_seconds (int): How long fetched JWKS keys are reused.
        display_timezone (str): IANA time zone used for "last updated"
            labels shown to primary users.
        scheduler_enabled (bool): Start the refresh scheduler at startup.
            Set false for local template work without backends.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    backend_llc_manager_url: str
    backend_llc_manager_api_key: SecretStr
    backend_pp_security_url: str
    backend_pp_security_api_key: SecretStr
    backend_xero_crypto_url: str
    backend_xero_crypto_api_key: SecretStr
    authentik_jwks_url: str
    authentik_issuer: str
    authentik_audience: str
    sqlite_path: str
    fo_viewer_group: str = "fo-viewer"
    fo_admin_group: str = "fo-admin"
    backend_timeout_seconds: float = 10.0
    jwks_cache_seconds: int = 600
    display_timezone: str = "UTC"
    scheduler_enabled: bool = True


def load_settings() -> Settings:
    """Build a ``Settings`` instance from the current environment.

    Returns:
        Settings: Validated portal settings.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from env
