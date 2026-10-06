# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Settings for embeddings, Qdrant and the chunk-set directory.

Every retrieval setting name lives in this module, so renaming one is a single
change. All of them are optional: with none set, document search and the
indexer are off and pages say "not connected". Like the backend pairs in
``app.config``, a URL without its key is a configuration error, and the
connection objects below cannot be built without both, so no request to the
embedding service or Qdrant can go out without its key.

Variables (names only; values come from the deployment environment):

* ``EMBED_BASE_URL`` and ``EMBED_API_KEY``: the embedding service and its
  bearer key.
* ``EMBEDDING_MODEL``: model name sent with each embedding request and stored
  on every point.
* ``EMBED_TIMEOUT_SECONDS`` (default 60): timeout for one embedding request.
* ``QDRANT_URL`` and ``QDRANT_API_KEY``: the vector database and its key.
* ``CHUNKS_DIR``: read-only directory of chunk-set files, used only by the
  indexer command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_UNSET = SecretStr("")


class RetrievalConfigError(ValueError):
    """Retrieval settings are set but cannot form a usable connection."""


@dataclass(frozen=True)
class EmbeddingConnection:
    """Where and how to reach the embedding service.

    Attributes:
        base_url (str): Service base URL, without a trailing slash.
        api_key (str): Bearer key; never shown in ``repr``.
        model (str): Model name sent with each request.
        timeout_seconds (float): Timeout for one request.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        """Reject blank values and a non-positive timeout.

        Raises:
            RetrievalConfigError: If the URL, key or model is blank, or the
                timeout is not positive. The message never includes the key.
        """
        if not self.base_url.strip():
            msg = "the embedding service needs a base URL"
            raise RetrievalConfigError(msg)
        if not self.api_key.strip():
            msg = "the embedding service needs a non-blank API key"
            raise RetrievalConfigError(msg)
        if not self.model.strip():
            msg = "the embedding service needs a model name"
            raise RetrievalConfigError(msg)
        if self.timeout_seconds <= 0:
            msg = "the embedding timeout must be positive"
            raise RetrievalConfigError(msg)


@dataclass(frozen=True)
class QdrantConnection:
    """Where and how to reach Qdrant.

    Attributes:
        url (str): Qdrant base URL.
        api_key (str): Qdrant API key; never shown in ``repr``.
    """

    url: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject a blank URL or key.

        Raises:
            RetrievalConfigError: If the URL or the key is blank.
        """
        if not self.url.strip():
            msg = "Qdrant needs a URL"
            raise RetrievalConfigError(msg)
        if not self.api_key.strip():
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
        embed_timeout_seconds (float): Timeout for one embedding request.
        qdrant_url (str): Qdrant base URL. Empty means off.
        qdrant_api_key (SecretStr): Qdrant API key.
        chunks_dir (str): Chunk-set directory read by the indexer.
    """

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    embed_base_url: str = ""
    embed_api_key: SecretStr = _UNSET
    embedding_model: str = ""
    embed_timeout_seconds: float = 60.0
    qdrant_url: str = ""
    qdrant_api_key: SecretStr = _UNSET
    chunks_dir: str = ""

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
            api_key=key,
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
        return QdrantConnection(url=url, api_key=key)

    def chunks_path(self) -> Path | None:
        """Return the chunk-set directory, or None when unset.

        Returns:
            Path | None: The configured directory, or None.
        """
        value = self.chunks_dir.strip()
        return Path(value) if value else None

    def search_connected(self) -> bool:
        """Say whether document search has everything it needs.

        A misconfigured pair (a URL without its key) counts as not connected
        here; the indexer command reports the error itself.

        Returns:
            bool: True when both the embedding service and Qdrant are usable.
        """
        try:
            return (
                self.embedding_connection() is not None
                and self.qdrant_connection() is not None
            )
        except RetrievalConfigError:
            return False


def load_retrieval_settings() -> RetrievalSettings:
    """Build ``RetrievalSettings`` from the current environment.

    Returns:
        RetrievalSettings: Settings read fresh on every call.
    """
    return RetrievalSettings()
