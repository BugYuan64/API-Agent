"""JSON comparisons and bounded observations shared by execution adapters."""

import json


def json_type(value) -> str:
    """返回 JSON 类型名；先识别 bool，避免把它当作 Python 整数。"""
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
    """递归比较 JSON 类型与内容；对象键集合、数组长度或任一子项不符即返回 False。"""
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
    """按 Pointer 脱敏并限制证据大小；返回安全值、存在性、原始类型和截断标记。"""
    safe = context.assertion_value(pointer, value)
    encoded = json.dumps(safe, ensure_ascii=True, allow_nan=False)
    truncated = len(encoded.encode()) > 4096
    return {
        "actual": None if truncated else safe,
        "actual_present": True,
        "actual_type": json_type(value),
        "actual_truncated": truncated,
    }
