from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

SUPPORTED_STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}
)
_ANNOTATION_KEYS = frozenset({"default", "examples", "title", "$schema"})
_SUPPORTED_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "multipleOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)
_JSON_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_MAX_PROPERTIES = 5_000
_MAX_DEPTH = 10
_MAX_TOTAL_NAME_AND_ENUM_CHARACTERS = 120_000
_MAX_ENUM_VALUES = 1_000
_MAX_LARGE_ENUM_CHARACTERS = 15_000


class OpenAISchemaError(ValueError):
    """A schema cannot be represented by OpenAI's strict JSON Schema subset."""


def compile_openai_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate one schema for strict Responses API output/tools."""

    compiled = copy.deepcopy(dict(schema))
    root_type = compiled.get("type")
    if root_type != "object":
        raise OpenAISchemaError("$: root schema must have type 'object'")
    if "anyOf" in compiled:
        raise OpenAISchemaError("$.anyOf: root anyOf is not supported")

    counters = {"properties": 0, "strings": 0, "enum_values": 0}
    _visit(compiled, "$", depth=1, counters=counters)
    if counters["properties"] > _MAX_PROPERTIES:
        raise OpenAISchemaError(
            f"$: schema has {counters['properties']} properties; maximum is {_MAX_PROPERTIES}"
        )
    if counters["strings"] > _MAX_TOTAL_NAME_AND_ENUM_CHARACTERS:
        raise OpenAISchemaError(
            "$: property/definition/enum strings exceed the 120000-character limit"
        )
    if counters["enum_values"] > _MAX_ENUM_VALUES:
        raise OpenAISchemaError(
            f"$: schema has {counters['enum_values']} enum values; maximum is {_MAX_ENUM_VALUES}"
        )
    return compiled


def _visit(node: Any, path: str, *, depth: int, counters: dict[str, int]) -> None:
    if not isinstance(node, dict):
        raise OpenAISchemaError(f"{path}: schema node must be an object")
    if depth > _MAX_DEPTH:
        raise OpenAISchemaError(f"{path}: schema nesting exceeds {_MAX_DEPTH} levels")

    for key in _ANNOTATION_KEYS:
        node.pop(key, None)
    unsupported = next((key for key in node if key not in _SUPPORTED_KEYWORDS), None)
    if unsupported is not None:
        raise OpenAISchemaError(f"{_path(path, unsupported)}: keyword is not supported")

    declared_type = node.get("type")
    if isinstance(declared_type, list):
        if not declared_type or len(set(declared_type)) != len(declared_type):
            raise OpenAISchemaError(f"{_path(path, 'type')}: type union is invalid")
        invalid_types = [value for value in declared_type if value not in _JSON_TYPES]
        if invalid_types:
            raise OpenAISchemaError(
                f"{_path(path, 'type')}: unsupported JSON type {invalid_types[0]!r}"
            )
    elif declared_type is not None and declared_type not in _JSON_TYPES:
        raise OpenAISchemaError(f"{_path(path, 'type')}: unsupported JSON type")

    string_format = node.get("format")
    if string_format is not None and string_format not in SUPPORTED_STRING_FORMATS:
        node.pop("format")

    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise OpenAISchemaError(f"{_path(path, 'enum')}: enum must be a non-empty array")
        counters["enum_values"] += len(enum)
        enum_characters = sum(len(value) for value in enum if isinstance(value, str))
        counters["strings"] += enum_characters
        if len(enum) > 250 and enum_characters > _MAX_LARGE_ENUM_CHARACTERS:
            raise OpenAISchemaError(
                f"{_path(path, 'enum')}: large enum strings exceed 15000 characters"
            )
    if "const" in node and isinstance(node["const"], str):
        counters["strings"] += len(node["const"])

    properties = node.get("properties")
    is_object = declared_type == "object" or isinstance(properties, dict)
    if is_object:
        if not isinstance(properties, dict):
            raise OpenAISchemaError(f"{_path(path, 'properties')}: object properties are required")
        counters["properties"] += len(properties)
        counters["strings"] += sum(len(str(name)) for name in properties)
        node["required"] = list(properties)
        node["additionalProperties"] = False
        for name, child in properties.items():
            _visit(
                child,
                _path(_path(path, "properties"), str(name)),
                depth=depth + 1,
                counters=counters,
            )

    if declared_type == "array" or "items" in node:
        items = node.get("items")
        if not isinstance(items, dict):
            raise OpenAISchemaError(f"{_path(path, 'items')}: array items schema is required")
        _visit(items, _path(path, "items"), depth=depth + 1, counters=counters)

    any_of = node.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise OpenAISchemaError(f"{_path(path, 'anyOf')}: anyOf must be a non-empty array")
        for index, child in enumerate(any_of):
            _visit(
                child,
                f"{_path(path, 'anyOf')}[{index}]",
                depth=depth + 1,
                counters=counters,
            )

    definitions = node.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, dict):
            raise OpenAISchemaError(f"{_path(path, '$defs')}: definitions must be an object")
        counters["strings"] += sum(len(str(name)) for name in definitions)
        for name, child in definitions.items():
            _visit(
                child,
                _path(_path(path, "$defs"), str(name)),
                depth=depth + 1,
                counters=counters,
            )

    reference = node.get("$ref")
    if reference is not None and (
        not isinstance(reference, str)
        or (reference != "#" and not reference.startswith("#/$defs/"))
    ):
        raise OpenAISchemaError(
            f"{_path(path, '$ref')}: only local root/$defs references are supported"
        )


def _path(parent: str, key: str) -> str:
    if key.replace("_", "a").isalnum() and not key[:1].isdigit():
        return f"{parent}.{key}"
    return f"{parent}[{key!r}]"
