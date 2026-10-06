# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""A fake OpenAI-compatible embeddings service on an ``httpx.MockTransport``.

Vectors are deterministic bag-of-words hashes, so texts that share words get
similar vectors and no model is needed. The key is generated per instance,
never a literal.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from typing import Any

import httpx
from pydantic import SecretStr

from app.retrieval.settings import EmbeddingConnection

DIMENSIONS = 1024
MODEL = "test-embed-model"
BASE_URL = "http://embed.test"


def fake_vector(text: str, dimensions: int = DIMENSIONS) -> list[float]:
    """Return a normalized bag-of-words vector for ``text``."""
    vector = [0.0] * dimensions
    for word in text.lower().split():
        bucket = int(hashlib.sha256(word.encode()).hexdigest(), 16) % dimensions
        vector[bucket] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class FakeEmbeddingService:
    """Records requests and answers like the embedding service."""

    def __init__(self) -> None:
        self.key = secrets.token_urlsafe(24)
        self.requests: list[dict[str, Any]] = []
        self.dimensions = DIMENSIONS
        self.fail_with: int | None = None
        self.fail_after = 0  # successful requests before ``fail_with`` applies
        self.fail_limit: int | None = None  # failures before answering again
        self.retry_after: str | None = None
        self.raise_error: httpx.HTTPError | None = None
        self.failures = 0

    def connection(self, key: str | None = None) -> EmbeddingConnection:
        """Return a connection to this fake, optionally with another key."""
        return EmbeddingConnection(
            base_url=BASE_URL, api_key=SecretStr(key or self.key), model=MODEL
        )

    def transport(self) -> httpx.MockTransport:
        """Return the transport that routes requests to this fake."""
        return httpx.MockTransport(self._handle)

    def _should_fail(self) -> bool:
        """Say whether the current request gets ``fail_with``."""
        if not self.fail_with or len(self.requests) <= self.fail_after:
            return False
        return self.fail_limit is None or self.failures < self.fail_limit

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request like the embedding service would."""
        if self.raise_error is not None:
            raise self.raise_error
        body = json.loads(request.content)
        self.requests.append(
            {
                "path": request.url.path,
                "auth": request.headers.get("Authorization"),
                "body": body,
            }
        )
        if request.headers.get("Authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if self._should_fail():
            self.failures += 1
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            # A real server may echo input in its error; the client must not.
            return httpx.Response(
                self.fail_with or 500, json={"error": body["input"]}, headers=headers
            )
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [
            {
                "object": "embedding",
                "index": i,
                "embedding": fake_vector(text)[: self.dimensions],
            }
            for i, text in enumerate(inputs)
        ]
        return httpx.Response(
            200, json={"object": "list", "model": body.get("model"), "data": data}
        )
