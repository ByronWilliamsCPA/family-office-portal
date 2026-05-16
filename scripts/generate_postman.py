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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OPENAPI_PATH: Path = REPO_ROOT / "docs" / "api" / "openapi.json"
OUTPUT_PATH: Path = REPO_ROOT / "docs" / "api" / "postman-collection.json"

METHODS: tuple[str, ...] = ("get", "post", "put", "patch", "delete", "options", "head")

PRE_REQUEST_SCRIPT: str = (
    "if (!pm.variables.get('baseUrl')) { "
    "pm.variables.set('baseUrl', 'http://localhost:8000'); "
    "}"
)


def _resolve_ref(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``$ref`` pointer into a schema fragment.

    Args:
        ref: Reference of the form ``#/components/schemas/Name``.
        spec: Full OpenAPI document.

    Returns:
        dict[str, Any]: The referenced schema fragment, or ``{}`` if not found.
    """
    if not ref.startswith("#/"):
        return {}
    cursor: Any = spec
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(cursor, dict) or part not in cursor:
            return {}
        cursor = cursor[part]
    return cursor if isinstance(cursor, dict) else {}


def _example_for_schema(
    schema: dict[str, Any],
    spec: dict[str, Any],
    depth: int = 0,
) -> Any:
    """Build a minimal JSON example value for ``schema``.

    Prefers ``example``/``examples`` when present; otherwise synthesises a
    placeholder appropriate to the declared type. Recursion is capped to
    avoid pathological self-referential schemas.

    Args:
        schema: The schema fragment to materialise.
        spec: Full OpenAPI document (needed for ``$ref`` resolution).
        depth: Current recursion depth.

    Returns:
        Any: Example JSON-compatible value.
    """
    if depth > 4:
        return None
    if "$ref" in schema:
        return _example_for_schema(_resolve_ref(schema["$ref"], spec), spec, depth + 1)
    if "example" in schema:
        return schema["example"]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        result: dict[str, Any] = {}
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for name, sub in props.items():
                if isinstance(sub, dict):
                    result[name] = _example_for_schema(sub, spec, depth + 1)
        return result
    if schema_type == "array":
        items = schema.get("items", {})
        if isinstance(items, dict):
            return [_example_for_schema(items, spec, depth + 1)]
        return []
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0.0
    if schema_type == "boolean":
        return False
    return ""


def _success_status(responses: dict[str, Any]) -> int:
    """Return the first 2xx status code declared by ``responses``.

    Args:
        responses: ``responses`` object from an OpenAPI operation.

    Returns:
        int: A 2xx status code; defaults to 200 when none is declared.
    """
    for code in responses:
        if isinstance(code, str) and code.startswith("2") and code.isdigit():
            return int(code)
    return 200


def _path_to_postman(path: str) -> tuple[str, list[str]]:
    """Convert an OpenAPI path template to Postman url and path segments.

    Args:
        path: Path template such as ``/entities/{entity_id}``.

    Returns:
        tuple[str, list[str]]: Postman raw URL form and the segment list.
    """
    segments: list[str] = []
    for raw in path.strip("/").split("/"):
        if not raw:
            continue
        if raw.startswith("{") and raw.endswith("}"):
            segments.append(":" + raw[1:-1])
        else:
            segments.append(raw)
    raw_url = "{{baseUrl}}/" + "/".join(segments) if segments else "{{baseUrl}}/"
    return raw_url, segments


def _path_variables(
    segments: list[str],
    operation: dict[str, Any],
) -> list[dict[str, str]]:
    """Build Postman ``variable`` entries for path parameter segments.

    Path variables get a non-empty default so that the request URL routes to
    the intended handler under Newman (an empty value collapses adjacent
    slashes and causes 404s on stub routes).

    Args:
        segments: Postman path segments (parameters prefixed with ``:``).
        operation: OpenAPI operation object, used to pick up declared examples.

    Returns:
        list[dict[str, str]]: One entry per parameter segment.
    """
    declared: dict[str, str] = {}
    for param in operation.get("parameters", []) or []:
        if not isinstance(param, dict) or param.get("in") != "path":
            continue
        name = param.get("name")
        if not isinstance(name, str):
            continue
        schema = param.get("schema", {})
        example = ""
        if isinstance(schema, dict):
            raw_example = schema.get("example")
            if raw_example is not None:
                example = str(raw_example)
        declared[name] = example or "example"

    out: list[dict[str, str]] = []
    for seg in segments:
        if seg.startswith(":"):
            key = seg[1:]
            out.append({"key": key, "value": declared.get(key, "example")})
    return out


def _build_request_body(
    operation: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a Postman ``body`` block for a JSON request body, if declared.

    Args:
        operation: OpenAPI operation object.
        spec: Full OpenAPI document.

    Returns:
        dict[str, Any] | None: Postman body block, or ``None`` if no JSON body.
    """
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return None
    content = request_body.get("content", {})
    if not isinstance(content, dict):
        return None
    json_block = content.get("application/json")
    if not isinstance(json_block, dict):
        return None
    schema = json_block.get("schema", {})
    example = _example_for_schema(schema, spec)
    return {
        "mode": "raw",
        "raw": json.dumps(example, indent=2),
        "options": {"raw": {"language": "json"}},
    }


def _query_parameters(operation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Postman query parameter stubs for the operation.

    Args:
        operation: OpenAPI operation object.

    Returns:
        list[dict[str, Any]]: Query parameter entries.
    """
    out: list[dict[str, Any]] = []
    for param in operation.get("parameters", []) or []:
        if not isinstance(param, dict):
            continue
        if param.get("in") != "query":
            continue
        schema = param.get("schema", {})
        example = schema.get("example", "") if isinstance(schema, dict) else ""
        required = bool(param.get("required", False))
        value = str(example) if example != "" else ("example" if required else "")
        out.append(
            {
                "key": param.get("name", ""),
                "value": value,
                "description": param.get("description", ""),
                "disabled": not required,
            },
        )
    return out


def _test_script(success_status: int, expects_json: bool) -> str:
    """Build the Postman test script for an operation.

    Args:
        success_status: The 2xx status code the route normally returns.
        expects_json: True when the success response declares JSON content.

    Returns:
        str: JavaScript test script for the Postman request.
    """
    lines: list[str] = [
        "pm.test('status is in the expected range', function () {",
        f"    pm.expect([{success_status}, 422]).to.include(pm.response.code);",
        "});",
    ]
    if expects_json:
        lines.extend(
            [
                "pm.test('response body is valid JSON', function () {",
                "    pm.response.to.have.jsonBody();",
                "});",
            ],
        )
    return "\n".join(lines)


def _operation_returns_json(operation: dict[str, Any]) -> bool:
    """Return True when the operation's success response declares JSON content.

    Args:
        operation: OpenAPI operation object.

    Returns:
        bool: True when any 2xx response has an ``application/json`` content type.
    """
    responses = operation.get("responses", {})
    if not isinstance(responses, dict):
        return False
    for code, body in responses.items():
        if not (isinstance(code, str) and code.startswith("2")):
            continue
        if not isinstance(body, dict):
            continue
        content = body.get("content", {})
        if isinstance(content, dict) and "application/json" in content:
            return True
    return False


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
