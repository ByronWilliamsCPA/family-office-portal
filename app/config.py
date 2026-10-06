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

The balance intake endpoint (``POST /api/v1/balances``) is the one inbound
machine route. Its shared key is read from ``BALANCE_INTAKE_API_KEY``; unset or
blank means the endpoint is disabled and answers 404, and a key shorter than
``MIN_INTAKE_KEY_LENGTH``, one that is not printable ASCII, or one equal to
``AUTHENTIK_JWT_SECRET`` stops startup. The key is a ``SecretStr`` and is
never logged or echoed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)

_NO_API_KEY = SecretStr("")

# Shortest accepted balance intake key; matches the sign-in secret floor.
MIN_INTAKE_KEY_LENGTH = 32


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
        balance_intake_api_key (SecretStr): Shared key a collector sends as
            ``X-API-Key`` to ``POST /api/v1/balances``. Unset or blank
            disables the endpoint. Never shown in ``repr``.
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
    balance_intake_api_key: SecretStr = _NO_API_KEY

    def balance_intake_key(self) -> str:
        """Return the stripped balance intake key, or empty when unset.

        Returns:
            str: The key without surrounding whitespace; empty when unset.
        """
        return self.balance_intake_api_key.get_secret_value().strip()

    def balance_intake_enabled(self) -> bool:
        """Say whether the balance intake endpoint has a key and so is on.

        Returns:
            bool: True when a non-blank key is set.
        """
        return bool(self.balance_intake_key())

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


# Zone names already reported as unusable, so each is warned about only once.
_WARNED_ZONES: set[str] = set()


def display_zone(settings: Settings | None = None) -> ZoneInfo | timezone:
    """Return the configured display time zone, falling back to UTC.

    ``ZoneInfo`` raises ``ZoneInfoNotFoundError`` for an unknown name,
    ``ValueError`` for a malformed one, and an ``OSError`` such as
    ``IsADirectoryError`` for a name like ``America`` when the ``tzdata``
    package is installed. All three fall back to UTC with one warning per
    name. Settings are loaded outside that guard, so a settings error is never
    mistaken for a bad zone name.

    Args:
        settings (Settings | None): Settings to read; loaded when omitted.

    Returns:
        ZoneInfo | timezone: The display zone, or UTC when it is unusable.
    """
    name = (settings or load_settings()).display_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if name not in _WARNED_ZONES:
            _WARNED_ZONES.add(name)
            logger.warning(
                "display_timezone_unusable",
                variable="DISPLAY_TIMEZONE",
                fallback="UTC",
            )
        return timezone.utc


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


def check_balance_intake(settings: Settings, *, jwt_secret: str = "") -> None:
    """Validate the optional balance intake key at startup.

    An unset or blank key is fine: the endpoint is then disabled. A key that
    is set must be at least ``MIN_INTAKE_KEY_LENGTH`` characters, printable
    ASCII only (HTTP header values are decoded as Latin-1, so any other
    character could never match), and different from the sign-in secret.

    Args:
        settings (Settings): Settings to check.
        jwt_secret (str): ``AUTHENTIK_JWT_SECRET``; the intake key must not
            reuse it, because that secret also signs sign-in tokens.

    Raises:
        BackendConfigError: If the key is set but too short, not printable
            ASCII, or equal to the sign-in secret. The message names the
            variable and never includes the value.
    """
    key = settings.balance_intake_key()
    if not key:
        return
    if len(key) < MIN_INTAKE_KEY_LENGTH:
        msg = (
            "BALANCE_INTAKE_API_KEY must be at least "
            f"{MIN_INTAKE_KEY_LENGTH} characters"
        )
        raise BackendConfigError(msg)
    if not (key.isascii() and key.isprintable()):
        msg = "BALANCE_INTAKE_API_KEY must be printable ASCII"
        raise BackendConfigError(msg)
    if jwt_secret and key == jwt_secret.strip():
        msg = "BALANCE_INTAKE_API_KEY must differ from AUTHENTIK_JWT_SECRET"
        raise BackendConfigError(msg)
