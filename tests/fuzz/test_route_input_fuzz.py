# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Fuzz tests for route input parsing and validation.

Exercises FastAPI path/query parameter parsing and Pydantic request-body
validation with adversarial and randomly generated inputs via Hypothesis.
Satisfies OpenSSF Silver badge criterion 6.1 (dynamic analysis with many
diverse inputs beyond mutation testing); see the organization's standards
manifest, checks OSSF-011 / OSSF-013 (no copy of the manifest lives in this
repository).

The routes exercised here are Phase 0 placeholders that discard their inputs
after FastAPI/Pydantic validation runs (``_ = document_id`` etc.). These
tests assert the contract that must hold in every phase regardless of what
the handler body eventually does: validated input never produces an
unhandled exception (HTTP 5xx) reaching the ASGI boundary, and the
validation boundary itself (``min_length``, ``Literal`` membership, JSON body
shape) rejects invalid input with 422 rather than silently accepting it.

The shared ``client`` fixture sends a valid Admin ``X-authentik-jwt`` token
(see ``tests/conftest.py``), so every request passes the Authentik middleware
and the status codes asserted here come from the route handlers. Fail-closed
auth behaviour is covered separately in ``tests/unit/test_middleware.py``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import status
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from httpx import AsyncClient

# CI runs against the ASGI transport, which can vary in latency under a
# loaded runner; `deadline=None` avoids flaking on timing rather than on the
# input itself. `derandomize=True` under CI makes a given commit's fuzz run
# reproducible across retries, at the cost of losing new-shrink coverage
# between runs; local development keeps randomized exploration.
settings.register_profile("ci", deadline=None, derandomize=True)
settings.register_profile("dev", deadline=None)
settings.load_profile("ci" if os.environ.get("CI") else "dev")

# The `client` fixture is reused across every Hypothesis example within a
# single test; each example issues an independent, stateless request against
# the ASGI app, so the "function scoped fixture reused across examples"
# health check does not indicate a real problem here. #ASSUME this holds
# only while the Phase 0 handlers are pure, side-effect-free placeholders
# that ignore their input; #VERIFY this suppression is still warranted once
# Phase 1 wires the handlers to the shared SQLite cache (a stateful
# dependency the fixture-reuse health check is designed to catch).
_SUPPRESS = (HealthCheck.function_scoped_fixture,)
_MAX_EXAMPLES = 100

# Adversarial payloads known to break naive input handling: path traversal,
# null-byte truncation, injection markers, oversized input, and non-ASCII
# text. Mixed into the generated strategies via `st.sampled_from` so every
# run covers these specific shapes in addition to purely random text.
_ADVERSARIAL_EXAMPLES = (
    "../../../../etc/passwd",
    "..%2f..%2f..%2fetc%2fpasswd",
    "'; DROP TABLE documents; --",
    "<script>alert(1)</script>",
    "A" * 5000,
    "\u0000",
    "\u202e",  # right-to-left override control character
    "😀🔥💀" * 10,
    "\r\nSet-Cookie: evil=1",
)


def _adversarial_text(*, max_size: int = 200) -> st.SearchStrategy[str]:
    """Return a strategy mixing random text with known-adversarial payloads.

    Args:
        max_size (int): Upper bound on the length of purely random examples.

    Returns:
        st.SearchStrategy[str]: Combined strategy of random and adversarial
            text.
    """
    return st.one_of(
        st.text(min_size=0, max_size=max_size),
        st.sampled_from(_ADVERSARIAL_EXAMPLES),
    )


def _path_segment_text() -> st.SearchStrategy[str]:
    """Return adversarial text restricted to a single URL path segment.

    Excludes the empty string, ``/``, NUL, ``.``, and ``..``: a literal
    ``/`` changes the number of path segments rather than the segment's
    content, an empty segment collapses the URL to a different route
    entirely, NUL is rejected by the HTTP client itself before the request
    reaches the app, and ``.``/``..`` are dot segments that httpx normalizes
    away when it joins the request path onto the client's base URL, so a
    generated ``.`` or ``..`` would silently miss the intended route instead
    of exercising it. Everything else (including the samples in
    ``_ADVERSARIAL_EXAMPLES``) is percent-encoded via ``quote(..., safe="")``
    at call sites, matching how a real HTTP client builds a request path.

    This strategy does not cover raw, slash-separated path-traversal
    sequences such as ``../../../../etc/passwd``: the raw ``/`` characters
    are filtered out because they would change the segment count rather
    than stay within one segment. The percent-encoded traversal sample
    (``..%2f..%2f..%2fetc%2fpasswd``) does survive this filter, since it
    contains no raw ``/``, and is exercised as ordinary single-segment
    content.

    Returns:
        st.SearchStrategy[str]: Strategy yielding single-segment path text.
    """
    return _adversarial_text().filter(
        lambda s: (
            len(s) > 0 and s not in {".", ".."} and "/" not in s and "\x00" not in s
        )
    )


@given(document_id=_path_segment_text())
@settings(suppress_health_check=_SUPPRESS, max_examples=_MAX_EXAMPLES)
async def test_document_preview_path_param_never_crashes(
    client: AsyncClient, document_id: str
) -> None:
    """Any opaque ``document_id`` must route and return 200.

    The Phase 0 handler (``app/routes/documents.py::document_preview``)
    accepts any single path segment and discards it (``_ = document_id``)
    without an existence check, so every routable identifier returns 200;
    there is no code path that legitimately 404s here yet. Phase 1 may
    introduce a real existence check once ``app/cache.py`` exists.

    Args:
        client (AsyncClient): ASGI-wired HTTPX client (see ``conftest.py``).
        document_id (str): Hypothesis-generated adversarial path segment.
    """
    encoded = quote(document_id, safe="")
    response = await client.get(f"/documents/{encoded}/preview")
    assert response.status_code == status.HTTP_200_OK


@given(document_id=_path_segment_text())
@settings(suppress_health_check=_SUPPRESS, max_examples=_MAX_EXAMPLES)
async def test_document_download_path_param_never_crashes(
    client: AsyncClient, document_id: str
) -> None:
    """Any opaque ``document_id`` must route and return 200.

    The Phase 0 handler (``app/routes/documents.py::document_download``)
    accepts any single path segment and discards it (``_ = document_id``)
    without an existence check, so every routable identifier returns 200;
    there is no code path that legitimately 404s here yet. Phase 1 may
    introduce a real existence check once ``app/cache.py`` exists.

    Args:
        client (AsyncClient): ASGI-wired HTTPX client (see ``conftest.py``).
        document_id (str): Hypothesis-generated adversarial path segment.
    """
    encoded = quote(document_id, safe="")
    response = await client.get(f"/documents/{encoded}/download")
    assert response.status_code == status.HTTP_200_OK


@given(entity_id=_path_segment_text())
@settings(suppress_health_check=_SUPPRESS, max_examples=_MAX_EXAMPLES)
async def test_entity_detail_path_param_never_crashes(
    client: AsyncClient, entity_id: str
) -> None:
    """Any opaque ``entity_id`` must route and return 200.

    The Phase 0 handler (``app/routes/entities.py::entity_detail``) accepts
    any single path segment and discards it (``_ = entity_id``) without an
    existence check, so every routable identifier returns 200; there is no
    code path that legitimately 404s here yet. Phase 1 may introduce a real
    existence check once ``app/cache.py`` exists.

    Args:
        client (AsyncClient): ASGI-wired HTTPX client (see ``conftest.py``).
        entity_id (str): Hypothesis-generated adversarial path segment.
    """
    encoded = quote(entity_id, safe="")
    response = await client.get(f"/entities/{encoded}")
    assert response.status_code == status.HTTP_200_OK


@given(query=_adversarial_text(max_size=500))
@settings(suppress_health_check=_SUPPRESS, max_examples=_MAX_EXAMPLES)
async def test_document_search_query_validation_boundary(
    client: AsyncClient, query: str
) -> None:
    """``q`` enforces ``min_length=1``: empty always 422, non-empty always 200.

    This asserts the validation boundary itself, not just the absence of a
    crash: the purpose of ``Query(min_length=1)`` is to reject empty input,
    and a fuzz test that only checked "no 500" would miss a regression that
    silently disabled the constraint.

    Args:
        client (AsyncClient): ASGI-wired HTTPX client (see ``conftest.py``).
        query (str): Hypothesis-generated free-text search query.
    """
    response = await client.get("/documents/search", params={"q": query})
    if len(query) == 0:
        assert response.status_code == 422
    else:
        assert response.status_code == 200


@given(service=_path_segment_text())
@settings(suppress_health_check=_SUPPRESS, max_examples=_MAX_EXAMPLES)
async def test_admin_refresh_rejects_unknown_service(
    client: AsyncClient, service: str
) -> None:
    """A ``service`` value outside the four known backends is rejected.

    ``service`` is typed ``Literal["entities", "holdings", "positions",
    "documents"]``; FastAPI must 422 on anything else rather than passing an
    arbitrary string through to the (future) scheduler dispatch.

    Args:
        client (AsyncClient): ASGI-wired HTTPX client (see ``conftest.py``).
        service (str): Hypothesis-generated candidate service identifier.
    """
    known_services = {"entities", "holdings", "positions", "documents"}
    encoded = quote(service, safe="")
    response = await client.post(f"/admin/refresh/{encoded}", json={"force": False})
    if service in known_services:
        assert response.status_code == 202
    else:
        assert response.status_code == 422


@given(
    body=st.dictionaries(
        keys=st.text(max_size=20),
        values=st.one_of(
            st.booleans(),
            st.text(max_size=50),
            st.integers(),
            st.none(),
        ),
        max_size=10,
    )
)
@settings(suppress_health_check=_SUPPRESS, max_examples=_MAX_EXAMPLES)
async def test_admin_refresh_body_never_crashes(
    client: AsyncClient, body: dict[str, object]
) -> None:
    """Arbitrary JSON request bodies must not raise a server error.

    ``RefreshTriggerRequest`` has a single optional ``force: bool`` field;
    Pydantic must reject or coerce anything else without an unhandled
    exception reaching the ASGI layer.

    Args:
        client (AsyncClient): ASGI-wired HTTPX client (see ``conftest.py``).
        body (dict[str, object]): Hypothesis-generated arbitrary JSON object.
    """
    response = await client.post("/admin/refresh/entities", json=body)
    assert response.status_code < 500
