from __future__ import annotations

import pytest

from app.openai_schema import OpenAISchemaError, compile_openai_schema
from app.schemas import MusicProposal


def test_compiler_strips_uri_but_keeps_runtime_url_model_strict() -> None:
    compiled = compile_openai_schema(MusicProposal.model_json_schema())
    track = compiled["$defs"]["ProposalTrack"]
    source_url = track["properties"]["source_url"]

    assert set(track["required"]) == set(track["properties"])
    assert track["additionalProperties"] is False
    assert all(option.get("format") != "uri" for option in source_url["anyOf"])


def test_compiler_preserves_supported_format_and_local_references() -> None:
    schema = {
        "type": "object",
        "properties": {
            "when": {"type": "string", "format": "date-time", "default": "ignored"},
            "child": {"$ref": "#/$defs/Child"},
            "recursive": {"$ref": "#"},
        },
        "$defs": {
            "Child": {
                "type": "object",
                "properties": {"value": {"type": ["string", "null"]}},
            }
        },
    }

    compiled = compile_openai_schema(schema)

    assert compiled["properties"]["when"] == {"type": "string", "format": "date-time"}
    assert compiled["properties"]["child"] == {"$ref": "#/$defs/Child"}
    assert compiled["properties"]["recursive"] == {"$ref": "#"}
    assert compiled["$defs"]["Child"]["required"] == ["value"]


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"anyOf": [{"type": "object"}]}, "root schema"),
        (
            {"type": "object", "properties": {}, "allOf": [{"type": "object"}]},
            r"\$\.allOf",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "uniqueItems": True,
                    }
                },
            },
            r"\$\.properties\.values\.uniqueItems",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"$ref": "https://invalid.example/schema"}},
            },
            r"\$\.properties\.value\['\$ref'\]",
        ),
    ],
)
def test_compiler_rejects_unsupported_shapes(schema: dict[str, object], message: str) -> None:
    with pytest.raises(OpenAISchemaError, match=message):
        compile_openai_schema(schema)
