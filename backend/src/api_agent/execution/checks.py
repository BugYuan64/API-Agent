"""JSON comparisons and bounded observations shared by execution adapters."""

import json


def json_type(value) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in {int, float}:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def json_equal(actual, expected) -> bool:
    if json_type(actual) != json_type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            json_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            json_equal(a, b) for a, b in zip(actual, expected, strict=True)
        )
    return actual == expected


def bounded_observation(value, pointer, context) -> dict:
    safe = context.assertion_value(pointer, value)
    encoded = json.dumps(safe, ensure_ascii=True, allow_nan=False)
    truncated = len(encoded.encode()) > 4096
    return {
        "actual": None if truncated else safe,
        "actual_present": True,
        "actual_type": json_type(value),
        "actual_truncated": truncated,
    }
