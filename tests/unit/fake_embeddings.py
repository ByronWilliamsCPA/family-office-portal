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
        self.raise_error: httpx.HTTPError | None = None

    def connection(self, key: str | None = None) -> EmbeddingConnection:
        """Return a connection to this fake, optionally with another key."""
        return EmbeddingConnection(
            base_url=BASE_URL, api_key=key or self.key, model=MODEL
        )

    def transport(self) -> httpx.MockTransport:
        """Return the transport that routes requests to this fake."""
        return httpx.MockTransport(self._handle)

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
        if self.fail_with:
            # A real server may echo input in its error; the client must not.
            return httpx.Response(self.fail_with, json={"error": body["input"]})
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
