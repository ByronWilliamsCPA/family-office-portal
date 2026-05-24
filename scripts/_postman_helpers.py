# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Low-level helpers for Postman collection generation.

These utilities are consumed exclusively by ``scripts.generate_postman``.
They are split into a separate module to keep per-file cyclomatic complexity
below the qlty threshold.
"""

from __future__ import annotations

import json
from typing import Any


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
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(cursor, dict) or part not in cursor:
            return {}
        cursor = cursor[part]
    return cursor if isinstance(cursor, dict) else {}


_PRIMITIVE_DEFAULTS: dict[str, Any] = {
    "integer": 0,
    "number": 0.0,
    "boolean": False,
}


def _synthesize_example(
    schema: dict[str, Any],
    spec: dict[str, Any],
    depth: int,
) -> Any:
    """Build a typed placeholder when the schema has no ``example`` field.

    Args:
        schema: Schema fragment with no inline example.
        spec: Full OpenAPI document for ``$ref`` resolution.
        depth: Current recursion depth.

    Returns:
        Any: Type-appropriate placeholder value.
    """
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            return {}
        return {
            name: _example_for_schema(sub, spec, depth + 1)
            for name, sub in props.items()
            if isinstance(sub, dict)
        }
    if schema_type == "array":
        items = schema.get("items", {})
        if isinstance(items, dict):
            return [_example_for_schema(items, spec, depth + 1)]
        return []
    return _PRIMITIVE_DEFAULTS.get(schema_type or "", "")


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
    return _synthesize_example(schema, spec, depth)


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


def _test_script(
    success_status: int,
    expects_json: bool,
    declares_422: bool,
) -> str:
    """Build the Postman test script for an operation.

    Args:
        success_status: The 2xx status code the route normally returns.
        expects_json: True when the success response declares JSON content.
        declares_422: True when the operation declares a 422 response (i.e.
            it accepts a request body or validated parameters).

    Returns:
        str: JavaScript test script for the Postman request.
    """
    allowed = [success_status, 422] if declares_422 else [success_status]
    allowed_js = ", ".join(str(code) for code in allowed)
    lines: list[str] = [
        "pm.test('status is in the expected range', function () {",
        f"    pm.expect([{allowed_js}]).to.include(pm.response.code);",
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
