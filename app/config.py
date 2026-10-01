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

Each backend is an optional pair: ``BACKEND_<NAME>_URL`` and
``BACKEND_<NAME>_API_KEY``. A backend whose URL is unset is "not connected":
its refresh jobs skip and its pages say so. A backend whose URL is set must
also have a non-blank key, and ``check_backends`` enforces that at startup.
``BackendConnection`` can only be built with both a URL and a key, so no
outbound request to a backend can be made without its key header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import structlog
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)

_NO_API_KEY = SecretStr("")


class BackendConfigError(ValueError):
    """A backend URL and API key do not form a usable pair."""


@dataclass(frozen=True)
class BackendSpec:
    """Static description of one backend and its environment variables.

    Attributes:
        name (str): Settings key, for example ``llc_manager``.
        label (str): Service name used in logs and docs.
        url_var (str): Environment variable holding the base URL.
        key_var (str): Environment variable holding the API key.
    """

    name: str
    label: str
    url_var: str
    key_var: str


BACKENDS: tuple[BackendSpec, ...] = (
    BackendSpec(
        "llc_manager",
        "llc-manager",
        "BACKEND_LLC_MANAGER_URL",
        "BACKEND_LLC_MANAGER_API_KEY",
    ),
    BackendSpec(
        "pp_security",
        "pp-security-master",
        "BACKEND_PP_SECURITY_URL",
        "BACKEND_PP_SECURITY_API_KEY",
    ),
    BackendSpec(
        "xero_crypto",
        "xero-crypto",
        "BACKEND_XERO_CRYPTO_URL",
        "BACKEND_XERO_CRYPTO_API_KEY",
    ),
    BackendSpec(
        "data_ingestor",
        "data-ingestor",
        "BACKEND_DATA_INGESTOR_URL",
        "BACKEND_DATA_INGESTOR_API_KEY",
    ),
)

_SPECS_BY_NAME: dict[str, BackendSpec] = {spec.name: spec for spec in BACKENDS}


@dataclass(frozen=True)
class BackendConnection:
    """A backend URL together with the API key every request must carry.

    Construction fails when either value is blank, so code that holds a
    connection can always send the key header.

    Attributes:
        label (str): Service name used in logs.
        url (str): Base URL, without surrounding whitespace.
        api_key (str): API key sent as ``X-API-Key``; never shown in ``repr``.
    """

    label: str
    url: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject a blank URL or key.

        Raises:
            BackendConfigError: If the URL or the key is empty or whitespace.
        """
        if not self.url.strip():
            msg = f"{self.label} needs a base URL"
            raise BackendConfigError(msg)
        if not self.api_key.strip():
            msg = f"{self.label} needs a non-blank API key"
            raise BackendConfigError(msg)


class Settings(BaseSettings):
    """Portal configuration loaded from environment variables.

    Attributes:
        model_config: Pydantic settings behavior (ignore
            unknown variables, case-insensitive names).
        backend_llc_manager_url (str): Base URL of the llc-manager API.
            Empty means not connected.
        backend_llc_manager_api_key (SecretStr): API key sent to llc-manager
            as ``X-API-Key``. Required when the URL is set.
        backend_pp_security_url (str): Base URL of the pp-security-master API.
            Empty means not connected.
        backend_pp_security_api_key (SecretStr): API key sent to
            pp-security-master as ``X-API-Key``. Required when the URL is set.
        backend_xero_crypto_url (str): Base URL of the xero-crypto API.
            Empty means not connected.
        backend_xero_crypto_api_key (SecretStr): API key sent to xero-crypto
            as ``X-API-Key``. Required when the URL is set.
        backend_data_ingestor_url (str): Base URL of the data-ingestor API.
            Empty means not connected. No client calls it yet.
        backend_data_ingestor_api_key (SecretStr): API key for data-ingestor.
            Required when the URL is set.
        sqlite_path (str): Filesystem path of the SQLite cache database.
        backend_timeout_seconds (float): Timeout for outbound backend calls.
        display_timezone (str): IANA time zone used for "last updated"
            labels shown to primary users.
        scheduler_enabled (bool): Start the refresh scheduler at startup.
            Set false for local template work without backends.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    backend_llc_manager_url: str = ""
    backend_llc_manager_api_key: SecretStr = _NO_API_KEY
    backend_pp_security_url: str = ""
    backend_pp_security_api_key: SecretStr = _NO_API_KEY
    backend_xero_crypto_url: str = ""
    backend_xero_crypto_api_key: SecretStr = _NO_API_KEY
    backend_data_ingestor_url: str = ""
    backend_data_ingestor_api_key: SecretStr = _NO_API_KEY
    sqlite_path: str
    backend_timeout_seconds: float = 10.0
    display_timezone: str = "UTC"
    scheduler_enabled: bool = True

    def backend_url(self, name: str) -> str:
        """Return the stripped base URL of one backend, or empty when unset.

        Args:
            name (str): Backend settings key, for example ``llc_manager``.

        Returns:
            str: Base URL without surrounding whitespace; empty when unset.
        """
        return cast("str", getattr(self, f"backend_{name}_url")).strip()

    def backend_key(self, name: str) -> str:
        """Return the stripped API key of one backend, or empty when unset.

        Args:
            name (str): Backend settings key, for example ``llc_manager``.

        Returns:
            str: API key without surrounding whitespace; empty when unset.
        """
        secret = cast("SecretStr", getattr(self, f"backend_{name}_api_key"))
        return secret.get_secret_value().strip()

    def is_connected(self, name: str) -> bool:
        """Say whether a backend has a URL, which is what "connected" means.

        Args:
            name (str): Backend settings key, for example ``llc_manager``.

        Returns:
            bool: True when the backend URL is set.
        """
        return bool(self.backend_url(name))

    def backend_connection(self, name: str) -> BackendConnection | None:
        """Return the connection for one backend, or None when not connected.

        Args:
            name (str): Backend settings key, for example ``llc_manager``.

        Returns:
            BackendConnection | None: URL plus key, or None when the URL is
            unset.

        Raises:
            BackendConfigError: If the URL is set but the key is blank. The
                message names the key variable.
        """
        spec = _SPECS_BY_NAME[name]
        url = self.backend_url(name)
        if not url:
            return None
        key = self.backend_key(name)
        if not key:
            msg = f"{spec.key_var} must be set when {spec.url_var} is set"
            raise BackendConfigError(msg)
        return BackendConnection(label=spec.label, url=url, api_key=key)


def load_settings() -> Settings:
    """Build a ``Settings`` instance from the current environment.

    Returns:
        Settings: Validated portal settings.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from env


def check_backends(settings: Settings) -> None:
    """Validate every backend URL and key pair at startup.

    A URL without a usable key is an error. A key without a URL is allowed
    and logged at info level, because the backend is simply not connected.

    Args:
        settings (Settings): Settings to check.

    Raises:
        BackendConfigError: If any URL is set while its key is unset, empty
            or whitespace. The message names every such key variable.
    """
    problems: list[str] = []
    for spec in BACKENDS:
        has_url = settings.is_connected(spec.name)
        has_key = bool(settings.backend_key(spec.name))
        if has_url and not has_key:
            problems.append(f"{spec.key_var} must be set when {spec.url_var} is set")
        elif has_key and not has_url:
            logger.info(
                "backend_key_without_url",
                key_variable=spec.key_var,
                url_variable=spec.url_var,
                detail=f"{spec.label} is not connected: {spec.url_var} is unset",
            )
    if problems:
        msg = "; ".join(problems)
        raise BackendConfigError(msg)
