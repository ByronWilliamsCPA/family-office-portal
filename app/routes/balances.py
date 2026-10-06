# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Balance intake: ``POST /api/v1/balances``.

A collector service delivers every account balance row in one request. The
route is the only HTTP handler that writes to SQLite; the write itself lives
in ``app.scheduler`` so it shares the process-wide write lock, and it runs in
a worker thread so the blocking database work never stalls the event loop.

Authentication is a shared key, not a signed-in identity: the sign-in
middleware lets exactly this method and path through, and this handler checks
``X-API-Key`` before it reads the body. With no key configured the endpoint is
disabled and answers 404.

Nothing here logs a key, an amount, an account id or an entity id; log lines
carry counts, provider prefixes and error classes only.
"""

from __future__ import annotations

import hmac
import sqlite3
from typing import Any

import structlog
from fastapi import APIRouter, Request, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

from app.config import load_settings
from app.middleware.authentik import INTAKE_PATH
from app.models import (
    BalanceDelivery,
    BalanceReceipt,
    validation_problems,
)
from app.scheduler import store_balance_delivery

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["balances"])

# Largest body accepted, in bytes. Sized so every delivery the model accepts
# fits: 2000 rows that each use every field at its longest, with a 200
# character name written entirely as JSON ``\u`` escape pairs and indented by
# two spaces, come to about 5.5 MiB (about 2.8 KB a row). Typical rows are
# about 400 bytes, so a real delivery is well under 1 MiB.
# ``tests/unit/test_balance_intake.py`` checks that worst case fits.
MAX_BODY_BYTES = 6 * 1024 * 1024

# Status codes spelled as numbers: Starlette renamed the 413 and 422 constants
# between releases, and the project supports a range of them.
_TOO_LARGE = 413
_UNPROCESSABLE = 422


def _delivery_schema() -> dict[str, Any]:
    """Build a self-contained JSON schema for the request body.

    Returns:
        dict[str, Any]: The delivery schema with the row schema inlined, so it
        does not depend on a components section.
    """
    schema = BalanceDelivery.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})
    schema["properties"]["items"]["items"] = defs["BalanceRow"]
    return schema


_REQUEST_BODY: dict[str, Any] = {
    "required": True,
    "content": {"application/json": {"schema": _delivery_schema()}},
}


def _detail(
    status_code: int,
    detail: str,
    errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    """Build a JSON error response with a fixed ``detail`` message.

    Args:
        status_code (int): HTTP status code.
        detail (str): Fixed message; never contains submitted text.
        errors (list[dict[str, str]] | None): Field paths and error codes for
            a validation failure, added as ``errors`` when given.

    Returns:
        JSONResponse: The error body.
    """
    body: dict[str, object] = {"detail": detail}
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status_code)


def _key_matches(request: Request, expected: str) -> bool:
    """Compare the ``X-API-Key`` header with the configured key in constant time.

    Args:
        request (Request): Current request.
        expected (str): The configured key.

    Returns:
        bool: True when the header equals the key.
    """
    provided = request.headers.get("x-api-key", "")
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def _read_body(request: Request) -> bytes | None:
    """Read the request body, stopping once it passes ``MAX_BODY_BYTES``.

    Args:
        request (Request): Current request.

    Returns:
        bytes | None: The body, or None when it is larger than the limit.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


@router.post(
    INTAKE_PATH,
    summary="Deliver account balances",
    status_code=status.HTTP_200_OK,
    response_model=BalanceReceipt,
    openapi_extra={"requestBody": _REQUEST_BODY},
    responses={
        401: {"description": "Missing or wrong X-API-Key."},
        404: {"description": "The intake is disabled (no key configured)."},
        413: {"description": "Body too large."},
        422: {"description": "Body failed validation; nothing was stored."},
        503: {"description": "The database write failed; nothing was changed."},
    },
)
async def receive_balances(request: Request) -> Response:
    """Replace stored balances with the rows in one delivery.

    Authentication: a shared key in ``X-API-Key``, not a sign-in identity. A
    valid sign-in does not authorise this route. When no key is configured the
    route answers 404 and never accepts a request.

    Rows replace the stored rows of each provider (the account id prefix) that
    appears in the delivery; a provider with no rows keeps its last values.
    Everything is stored in one transaction, then today's daily snapshot is
    written in the same transaction.

    #CRITICAL: security: the key is checked before the body is read or parsed.
    #VERIFY: tests/unit/test_balance_intake.py sends a missing or wrong key
    with a body over ``MAX_BODY_BYTES`` (declared and streamed) and expects
    401, not 413, and checks the key is compared with ``hmac.compare_digest``.

    Args:
        request (Request): Current request; the body is read here so the key is
            checked first.

    Returns:
        Response: 200 with a receipt, or a JSON error (401, 404, 413, 422,
        503) with a ``detail`` string and no submitted values.
    """
    expected = load_settings().balance_intake_key()
    if not expected:
        logger.info("balance_intake_denied", reason="disabled")
        return _detail(status.HTTP_404_NOT_FOUND, "Not found")
    if not _key_matches(request, expected):
        logger.info("balance_intake_denied", reason="bad_key")
        return _detail(status.HTTP_401_UNAUTHORIZED, "A valid API key is required.")
    raw = await _read_body(request)
    if raw is None:
        logger.info("balance_intake_rejected", reason="too_large")
        return _detail(_TOO_LARGE, "Request body is too large.")
    try:
        delivery = BalanceDelivery.model_validate_json(raw)
    except ValidationError as exc:
        problems = validation_problems(
            exc.errors(include_input=False, include_url=False, include_context=False)
        )
        logger.info("balance_intake_rejected", reason="invalid", problems=len(problems))
        return _detail(
            _UNPROCESSABLE,
            "The delivery was not accepted and nothing was stored.",
            errors=problems,
        )
    try:
        providers = await run_in_threadpool(store_balance_delivery, delivery)
    except sqlite3.Error:
        return _detail(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Balances could not be stored."
        )
    receipt = BalanceReceipt(accepted=len(delivery.items), providers=providers)
    return JSONResponse(receipt.model_dump())
