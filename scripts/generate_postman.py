# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Generate ``docs/api/postman-collection.json`` from the exported OpenAPI spec.

The output is a Postman Collection v2.1 document. Every operation in the spec
becomes one request that:

* uses ``{{baseUrl}}`` for the host and ``{{apiKey}}`` for an
  ``X-Api-Key`` header (the portal itself authenticates via Cloudflare Access
  in production; the collection mirrors a generic backend auth pattern so
  Newman can be wired into CI without leaking real Cloudflare tokens),
* carries a minimal example body when the operation declares a JSON request
  body,
* asserts that the response status is in the expected range and that the
  body parses as JSON when the operation declares an ``application/json``
  response.

A collection-level pre-request script seeds ``baseUrl`` to
``http://localhost:8000`` when the variable is unset, so Newman runs against
the locally booted FastAPI app without further configuration.

Run from the repo root::

    uv run python -m scripts.generate_postman

Low-level schema and URL helpers live in ``scripts._postman_helpers``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._postman_helpers import (
    _build_request_body,
    _operation_returns_json,
    _path_to_postman,
    _path_variables,
    _query_parameters,
    _success_status,
    _test_script,
)

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OPENAPI_PATH: Path = REPO_ROOT / "docs" / "api" / "openapi.json"
OUTPUT_PATH: Path = REPO_ROOT / "docs" / "api" / "postman-collection.json"

METHODS: tuple[str, ...] = ("get", "post", "put", "patch", "delete", "options", "head")

PRE_REQUEST_SCRIPT: str = (
    "if (!pm.variables.get('baseUrl')) { "
    "pm.variables.set('baseUrl', 'http://localhost:8000'); "
    "}"
)


def _build_item(
    path: str,
    method: str,
    operation: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Convert one OpenAPI operation into one Postman collection item.

    Args:
        path: OpenAPI path template.
        method: HTTP method in lowercase.
        operation: OpenAPI operation object.
        spec: Full OpenAPI document.

    Returns:
        dict[str, Any]: Postman v2.1 item.
    """
    raw_url, segments = _path_to_postman(path)
    success_status = _success_status(operation.get("responses", {}))
    body_block = _build_request_body(operation, spec)
    headers: list[dict[str, str]] = [
        {"key": "X-Api-Key", "value": "{{apiKey}}", "type": "text"},
    ]
    if body_block is not None:
        headers.append(
            {"key": "Content-Type", "value": "application/json", "type": "text"},
        )

    url_block: dict[str, Any] = {
        "raw": raw_url,
        "host": ["{{baseUrl}}"],
        "path": segments,
    }
    query_params = _query_parameters(operation)
    if query_params:
        url_block["query"] = query_params
    path_vars = _path_variables(segments, operation)
    if path_vars:
        url_block["variable"] = path_vars

    request_block: dict[str, Any] = {
        "method": method.upper(),
        "header": headers,
        "url": url_block,
        "description": operation.get("description") or operation.get("summary", ""),
    }
    if body_block is not None:
        request_block["body"] = body_block

    item: dict[str, Any] = {
        "name": operation.get("summary") or f"{method.upper()} {path}",
        "request": request_block,
        "response": [],
        "event": [
            {
                "listen": "test",
                "script": {
                    "type": "text/javascript",
                    "exec": _test_script(
                        success_status,
                        _operation_returns_json(operation),
                        "422" in (operation.get("responses") or {}),
                    ).split("\n"),
                },
            },
        ],
    }
    return item


def _group_items_by_tag(
    spec: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build top-level Postman folders grouped by OpenAPI tag.

    Args:
        spec: Full OpenAPI document.

    Returns:
        list[dict[str, Any]]: Folder items, one per tag.
    """
    folders: dict[str, list[dict[str, Any]]] = {}
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return []
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, operation in ops.items():
            if method not in METHODS or not isinstance(operation, dict):
                continue
            tags = operation.get("tags") or ["default"]
            tag = tags[0] if isinstance(tags, list) and tags else "default"
            folders.setdefault(tag, []).append(
                _build_item(path, method, operation, spec),
            )
    return [{"name": tag, "item": items} for tag, items in sorted(folders.items())]


def generate() -> Path:
    """Read the OpenAPI spec and write the Postman collection.

    Returns:
        Path: Absolute path of the written collection.
    """
    with OPENAPI_PATH.open("r", encoding="utf-8") as fh:
        spec: dict[str, Any] = json.load(fh)

    info = spec.get("info", {})
    collection: dict[str, Any] = {
        "info": {
            "name": info.get("title", "Family Office Portal API"),
            "description": info.get("description", ""),
            "schema": (
                "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
            ),
            "_postman_id": "family-office-portal-collection",
        },
        "variable": [
            {"key": "baseUrl", "value": "http://localhost:8000", "type": "string"},
            {"key": "apiKey", "value": "", "type": "string"},
        ],
        "event": [
            {
                "listen": "prerequest",
                "script": {
                    "type": "text/javascript",
                    "exec": PRE_REQUEST_SCRIPT.split("\n"),
                },
            },
        ],
        "item": _group_items_by_tag(spec),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(collection, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return OUTPUT_PATH


if __name__ == "__main__":
    written: Path = generate()
    print(f"wrote {written.relative_to(REPO_ROOT)}")
