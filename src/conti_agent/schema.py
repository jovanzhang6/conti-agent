from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import ToolValidationError


_ALLOWED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ToolValidationError(f"unsupported schema type: {expected}")


def validate_value(value: Any, schema: Mapping[str, Any], path: str = "arguments") -> None:
    expected = schema.get("type")
    if expected is not None:
        possibilities = expected if isinstance(expected, list) else [expected]
        unknown = set(possibilities) - _ALLOWED_TYPES
        if unknown:
            raise ToolValidationError(f"unsupported schema types: {sorted(unknown)}")
        if not any(_type_matches(value, kind) for kind in possibilities):
            raise ToolValidationError(f"{path} must be {' or '.join(possibilities)}")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise ToolValidationError(f"{path} must be one of {enum!r}")

    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ToolValidationError(f"{path}.{key} is required")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolValidationError(f"{path} keys must be strings")
            if key in properties:
                validate_value(item, properties[key], f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_value(item, schema["items"], f"{path}[{index}]")
