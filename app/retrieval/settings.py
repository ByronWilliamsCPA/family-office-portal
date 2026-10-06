# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Settings for embeddings, Qdrant and the chunk-set directory.

Every retrieval setting name lives in this module, so renaming one is a single
change. All of them are optional: with none set, document search and the
indexer are off and pages say "not connected". Like the backend pairs in
``app.config``, a URL without its key is a configuration error, and the
connection objects below cannot be built without both, so no request to the
embedding service or Qdrant can go out without its key. Keys stay
``SecretStr`` on the connection objects and are unwrapped only where the
request header or client is built.

Variables (names only; values come from the deployment environment):

* ``EMBED_BASE_URL`` and ``EMBED_API_KEY``: the embedding service and its
  bearer key. The base URL stops before ``/v1``; the client adds
  ``/v1/embeddings`` itself.
* ``EMBEDDING_MODEL``: model name sent with each embedding request and stored
  on every point.
* ``EMBED_TIMEOUT_SECONDS`` (default 60): timeout for one embedding request,
  a finite number above zero.
* ``QDRANT_URL`` and ``QDRANT_API_KEY``: the vector database and its key.
* ``CHUNKS_DIR``: read-only directory of chunk-set files, used only by the
  indexer command.
* ``TAX_LAW_PATH``: the tax-law knowledge-base JSON file, used only by the
  tax-law indexer command.

A value that cannot be parsed (for example a non-numeric timeout) makes
``load_retrieval_settings`` raise ``RetrievalConfigError`` naming the variable,
never its value. So does a URL that is not http or https with a host and a
valid port, which the connection objects reject before any client is built.
Unlike the backend pairs in ``app.config``, these settings are not checked at
startup: the web process treats a bad value as "not connected" and logs a
warning, and the indexer command exits 2.

#ASSUME: security: the embedding service and Qdrant are reached over a
private network, so an ``http://`` URL does not expose the keys. #VERIFY:
both URLs resolve to hosts on the private network, or use ``https://``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)

_UNSET = SecretStr("")

# Problems already logged by ``document_search_connected``, so a bad setting
# is reported once per process rather than on every page render.
_reported_problems: set[str] = set()


class RetrievalConfigError(ValueError):
    """Retrieval settings are set but cannot form a usable connection."""


def _require_http_url(url: str, name: str) -> None:
    """Check that a URL is http or https with a host and a usable port.

    The client libraries raise on a malformed URL with a message that can
    echo it, userinfo included, so the check happens here and the error names
    only the variable.

    Args:
        url (str): The configured URL.
        name (str): The variable it came from, for the error message.

    Raises:
        RetrievalConfigError: If the scheme is not http or https, the host is
            missing, or the port is not a number from 0 to 65535. The message
            never includes the URL.
    """
    try:
        parts = urlsplit(url.strip())
        usable = parts.scheme in {"http", "https"} and bool(parts.hostname)
        _ = parts.port  # raises ValueError for a port that is not 0-65535
    except ValueError:
        usable = False
    if not usable:
        msg = f"{name} must be an http or https URL with a host and a valid port"
        raise RetrievalConfigError(msg)


@dataclass(frozen=True)
class EmbeddingConnection:
    """Where and how to reach the embedding service.

    Attributes:
        base_url (str): Service base URL, without a trailing slash.
        api_key (SecretStr): Bearer key; never shown in ``repr``.
        model (str): Model name sent with each request.
        timeout_seconds (float): Timeout for one request.
    """

    base_url: str
    api_key: SecretStr
    model: str
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        """Reject blank values and a timeout that is not a positive number.

        Raises:
            RetrievalConfigError: If the URL, key or model is blank, the URL
                is not an http or https URL with a host and a valid port, or
                the timeout is not a finite number above zero. The message
                never includes the URL or the key.
        """
        if not self.base_url.strip():
            msg = "the embedding service needs a base URL"
            raise RetrievalConfigError(msg)
        _require_http_url(self.base_url, "EMBED_BASE_URL")
        if not self.api_key.get_secret_value().strip():
            msg = "the embedding service needs a non-blank API key"
            raise RetrievalConfigError(msg)
        if not self.model.strip():
            msg = "the embedding service needs a model name"
            raise RetrievalConfigError(msg)
        if not (math.isfinite(self.timeout_seconds) and self.timeout_seconds > 0):
            msg = "the embedding timeout must be a finite number above zero"
            raise RetrievalConfigError(msg)


@dataclass(frozen=True)
class QdrantConnection:
    """Where and how to reach Qdrant.

    Attributes:
        url (str): Qdrant base URL.
        api_key (SecretStr): Qdrant API key; never shown in ``repr``.
    """

    url: str
    api_key: SecretStr

    def __post_init__(self) -> None:
        """Reject a blank or malformed URL and a blank key.

        Raises:
            RetrievalConfigError: If the URL is blank or is not an http or
                https URL with a host and a valid port, or the key is blank.
                The message never includes the URL or the key.
        """
        if not self.url.strip():
            msg = "Qdrant needs a URL"
            raise RetrievalConfigError(msg)
        _require_http_url(self.url, "QDRANT_URL")
        if not self.api_key.get_secret_value().strip():
            msg = "Qdrant needs a non-blank API key"
            raise RetrievalConfigError(msg)


class RetrievalSettings(BaseSettings):
    """Optional retrieval configuration read from the environment.

    Attributes:
        model_config: Pydantic settings behavior (ignore unknown variables,
            case-insensitive names).
        embed_base_url (str): Embedding service base URL. Empty means off.
        embed_api_key (SecretStr): Embedding service bearer key.
        embedding_model (str): Embedding model name.
        embed_timeout_seconds (float): Timeout for one embedding request; a
            finite number above zero.
        qdrant_url (str): Qdrant base URL. Empty means off.
        qdrant_api_key (SecretStr): Qdrant API key.
        chunks_dir (str): Chunk-set directory read by the indexer.
        tax_law_path (str): Knowledge-base file read by the tax-law indexer.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    embed_base_url: str = ""
    embed_api_key: SecretStr = _UNSET
    embedding_model: str = ""
    embed_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    qdrant_url: str = ""
    qdrant_api_key: SecretStr = _UNSET
    chunks_dir: str = ""
    tax_law_path: str = ""

    def embedding_connection(self) -> EmbeddingConnection | None:
        """Return the embedding connection, or None when its URL is unset.

        Returns:
            EmbeddingConnection | None: The connection, or None when off.

        Raises:
            RetrievalConfigError: If the URL is set but the key or model is
                blank. The message names the variable, never a value.
        """
        url = self.embed_base_url.strip()
        if not url:
            return None
        key = self.embed_api_key.get_secret_value().strip()
        if not key:
            msg = "EMBED_API_KEY must be set when EMBED_BASE_URL is set"
            raise RetrievalConfigError(msg)
        model = self.embedding_model.strip()
        if not model:
            msg = "EMBEDDING_MODEL must be set when EMBED_BASE_URL is set"
            raise RetrievalConfigError(msg)
        return EmbeddingConnection(
            base_url=url.rstrip("/"),
            api_key=SecretStr(key),
            model=model,
            timeout_seconds=self.embed_timeout_seconds,
        )

    def qdrant_connection(self) -> QdrantConnection | None:
        """Return the Qdrant connection, or None when its URL is unset.

        Returns:
            QdrantConnection | None: The connection, or None when off.

        Raises:
            RetrievalConfigError: If the URL is set but the key is blank.
        """
        url = self.qdrant_url.strip()
        if not url:
            return None
        key = self.qdrant_api_key.get_secret_value().strip()
        if not key:
            msg = "QDRANT_API_KEY must be set when QDRANT_URL is set"
            raise RetrievalConfigError(msg)
        return QdrantConnection(url=url, api_key=SecretStr(key))

    def chunks_path(self) -> Path | None:
        """Return the chunk-set directory, or None when unset.

        Returns:
            Path | None: The configured directory, or None.
        """
        value = self.chunks_dir.strip()
        return Path(value) if value else None

    def tax_law_file(self) -> Path | None:
        """Return the tax-law knowledge-base file, or None when unset.

        Returns:
            Path | None: The configured file, or None.
        """
        value = self.tax_law_path.strip()
        return Path(value) if value else None

    def search_connected(self) -> bool:
        """Say whether document search has everything it needs.

        A misconfigured pair (a URL without its key) counts as not connected
        here; ``document_search_connected`` logs the reason, and the indexer
        command reports it itself.

        Returns:
            bool: True when both the embedding service and Qdrant are usable.
        """
        try:
            return self.require_search_settings()
        except RetrievalConfigError:
            return False

    def require_search_settings(self) -> bool:
        """Say whether search is configured, raising on a misconfigured pair.

        The connection builders raise ``RetrievalConfigError`` when a URL is
        set without its key or model; it is not caught here.

        Returns:
            bool: True when both the embedding service and Qdrant are set;
            False when either URL is unset.
        """
        return (
            self.embedding_connection() is not None
            and self.qdrant_connection() is not None
        )


def load_retrieval_settings() -> RetrievalSettings:
    """Build ``RetrievalSettings`` from the current environment.

    Returns:
        RetrievalSettings: Settings read fresh on every call.

    Raises:
        RetrievalConfigError: If a variable holds a value that cannot be
            parsed. The message names the variable, never its value, and the
            validation error (which carries the value) is not chained.
    """
    try:
        return RetrievalSettings()
    except ValidationError as exc:
        names = sorted(
            {str(error["loc"][0]).upper() for error in exc.errors() if error["loc"]}
        )
        msg = f"invalid value for {', '.join(names) or 'a retrieval setting'}"
    raise RetrievalConfigError(msg) from None


def document_search_connected() -> bool:
    """Say whether pages may offer document search, never raising.

    Unset services, a URL without its key, and a value that cannot be parsed
    all count as not connected, so a page says "not connected" instead of
    failing. A configuration problem is logged once per process by variable
    name; values are never logged.

    Returns:
        bool: True when both the embedding service and Qdrant are usable.
    """
    try:
        return load_retrieval_settings().require_search_settings()
    except RetrievalConfigError as exc:
        problem = str(exc)
    if problem not in _reported_problems:
        _reported_problems.add(problem)
        logger.warning("document_search_misconfigured", reason=problem)
    return False
