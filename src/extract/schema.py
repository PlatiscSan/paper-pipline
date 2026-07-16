"""Schema loading, hashing, JSON recovery, and validation."""

import hashlib
import json
import re
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
from paper_pipeline.errors import ErrorCode, PipelineError


def load_schema(path: Path) -> tuple[dict[str, Any], str]:
    if path.exists():
        raw = path.read_bytes()
    elif path.parent.name == "schemas" and path.name in {"catalysis.json", "generic.json"}:
        raw = resources.files("paper_pipeline").joinpath("schemas", path.name).read_bytes()
    else:
        raise PipelineError(ErrorCode.FILE_NOT_FOUND, f"Schema file not found: {path}")
    schema = json.loads(raw)
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema, hashlib.sha256(raw).hexdigest()


def parse_json_object(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(.*?)```", text, re.I | re.S))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            if start < 0:
                continue
            try:
                value, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                continue
        if isinstance(value, dict):
            return value
    raise PipelineError(ErrorCode.AI_INVALID_JSON, "AI response did not contain a JSON object")


def validate(data: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise PipelineError(ErrorCode.AI_SCHEMA_MISMATCH, exc.message) from exc
