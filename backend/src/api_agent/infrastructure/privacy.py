"""Shared redaction at model, artifact and report boundaries."""

import json
import re
from urllib.parse import parse_qsl, urlencode

from api_agent.infrastructure.json_pointer import is_sensitive
from api_agent.infrastructure.logging import (
    ASSIGNED_CREDENTIAL,
    REDACTED,
    SENSITIVE_KEYS,
    RedactingJsonFormatter,
    normalize_key,
)

SCHEMA_KEYS = {"type", "$ref", "properties", "allOf", "anyOf", "oneOf"}
SCHEMA_SAMPLES = {"default", "example", "examples", "enum", "const"}
SCHEMA_METADATA = {"type", "$ref", "$schema", "$id", "required", "format", "pattern"}
ASSERTION_VALUES = {"expected", "actual", "value"}
PHONE = re.compile(r"(?<![\d])1[3-9]\d{9}(?![\d])")
KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\b")
EMAIL = re.compile(
    r"(?<![A-Za-z0-9_.+-])[A-Za-z0-9_.+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,24}(?![A-Za-z0-9-])"
)
TEMPLATE_ONLY_SECRET = re.compile(
    r"^(?:(?:Bearer|Basic|Token)\s+)?\{\{[A-Za-z_][A-Za-z0-9_]{0,63}\}\}$",
    re.IGNORECASE,
)


def is_sensitive_pointer(pointer) -> bool:
    """任何敏感属性段都保护其后代（委托 ``json_pointer.is_sensitive``）。"""
    return is_sensitive(pointer)


def _parse_json(value):
    """尝试解码对象或数组文本；非法或过深 JSON 保留原值供文本清洗兜底。"""
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


class Context:
    """在模型、历史和工件边界学习敏感值并生成脱敏副本；不负责目标授权。"""

    def __init__(self, secrets=()):
        """用已知秘密初始化上下文清洗器，后续 learn 可补充敏感值。"""
        self.redactor = RedactingJsonFormatter(secrets=secrets)

    def text(self, value: str) -> str:
        """清洗已知秘密、密钥形态、手机号与邮箱，返回文本副本。"""
        value = self.redactor.redact_text(value)
        if "sk-" in value:
            value = KEY.sub(REDACTED, value)
        if "eyJ" in value:
            value = JWT.sub(REDACTED, value)
        value = PHONE.sub("[PHONE]", value)
        if "@" in value:
            value = EMAIL.sub("[EMAIL]", value)
        return value

    def learn(self, value):
        """Learn labelled/schema/pointer secrets before scrubbing unlabelled echoes."""
        secrets = set(self.redactor.secrets)

        def collect(node):
            """收集敏感字段中的字符串，忽略短值与已有占位符，避免污染后续替换规则。"""
            if isinstance(node, str) and len(node) >= 4:
                if node not in {REDACTED, "[PHONE]", "[EMAIL]"}:
                    secrets.add(node)
            elif isinstance(node, dict):
                for item in node.values():
                    collect(item)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    collect(item)

        def visit(node, sensitive_schema=False):
            """识别契约、Schema 样例及敏感 Pointer，区分结构约束和秘密内容后登记。"""
            if isinstance(node, dict):
                sensitive_assertion = is_sensitive_pointer(node.get("pointer"))
                for key, item in node.items():
                    normalized = normalize_key(key)
                    if key == "contract":
                        visit(_parse_json(item))
                    elif (sensitive_schema and key in SCHEMA_SAMPLES) or (
                        sensitive_assertion and key in ASSERTION_VALUES
                    ):
                        collect(item)
                    elif normalized in SENSITIVE_KEYS:
                        if isinstance(item, dict) and SCHEMA_KEYS & item.keys():
                            visit(item, True)
                        elif isinstance(item, str) and TEMPLATE_ONLY_SECRET.fullmatch(item):
                            continue
                        else:
                            collect(item)
                    else:
                        visit(item, sensitive_schema)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    visit(item, sensitive_schema)
            elif isinstance(node, str):
                for match in ASSIGNED_CREDENTIAL.finditer(node):
                    assigned = re.split(r"[:=]\s*", match[0], maxsplit=1)[1]
                    if assigned.startswith('"'):
                        try:
                            assigned = json.loads(assigned)
                        except ValueError:
                            assigned = assigned.strip('"')
                    else:
                        assigned = assigned.strip("'")
                    collect(assigned)

        visit(value)
        self.redactor.secrets = tuple(sorted(secrets, key=len, reverse=True))

    def assertion_value(self, pointer, value):
        """Scrub assertion scalars even when their credential only appears in the pointer."""
        return REDACTED if is_sensitive_pointer(pointer) else self.clean(value)

    def clean(self, value, *, schema=False, sensitive_schema=False):
        """递归生成安全副本；保留 Schema 元数据与 Pointer 结构，遮蔽敏感字段和断言值。"""
        if isinstance(value, dict):
            sensitive_assertion = is_sensitive_pointer(value.get("pointer"))
            if sensitive_assertion:
                self.learn(value)
            output = {}
            for key, item in value.items():
                if (schema and key in SCHEMA_METADATA) or (
                    key == "pointer" and isinstance(item, str) and item.startswith("/")
                ):
                    output[key] = item
                    continue
                if key == "contract":
                    output[key] = self.contract(item)
                    continue
                if (sensitive_schema and key in SCHEMA_SAMPLES) or (
                    sensitive_assertion and key in ASSERTION_VALUES
                ):
                    output[key] = REDACTED
                    continue
                normalized = normalize_key(key)
                is_schema = schema and isinstance(item, dict) and bool(SCHEMA_KEYS & item.keys())
                if normalized in SENSITIVE_KEYS and not is_schema:
                    if isinstance(item, str) and TEMPLATE_ONLY_SECRET.fullmatch(item):
                        output[str(key)] = item
                    else:
                        output[str(key)] = REDACTED
                else:
                    output[str(key)] = self.clean(
                        item,
                        schema=schema,
                        sensitive_schema=sensitive_schema
                        or (normalized in SENSITIVE_KEYS and is_schema),
                    )
            return output
        if isinstance(value, (list, tuple)):
            return [
                self.clean(item, schema=schema, sensitive_schema=sensitive_schema) for item in value
            ]
        if isinstance(value, str):
            return self.text(value)
        return value

    def contract(self, value):
        """解析并学习契约秘密后保留 Schema 语义清洗；原输入为 JSON 文本时仍返回文本。"""
        parsed = _parse_json(value)
        self.learn(parsed)
        cleaned = self.clean(parsed, schema=True)
        if isinstance(value, str) and parsed is not value:
            return json.dumps(cleaned, ensure_ascii=False)
        return cleaned

    def body(self, value: str | None, mode: str = "json") -> str | None:
        """Decode structured body credentials before cleaning their sibling echoes."""
        if value is None:
            return None
        if mode == "form":
            pairs = parse_qsl(value, keep_blank_values=True)
            for key, item in pairs:
                self.learn({key: item})
            return urlencode([(key, self.clean({key: item})[key]) for key, item in pairs])
        parsed = _parse_json(value) if mode == "json" else value
        self.learn(parsed)
        if parsed is not value:
            return json.dumps(self.clean(parsed), ensure_ascii=False)
        return self.text(value)

    def dumps(self, value) -> str:
        """学习敏感值后序列化脱敏副本，拒绝非有限数字，不在此执行文件写入。"""
        self.learn(value)
        return json.dumps(self.clean(value), ensure_ascii=False, allow_nan=False)
