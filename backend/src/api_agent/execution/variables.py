"""受控变量提取与模板解析：供计划执行双引擎与智能群测复用。

安全边界：
- 仅支持简单命名占位符 `{{var_name}}`（不支持表达式、函数或嵌套模板）；
- 路径段替换后强制二次校验 `PATH_SEGMENT`，阻止通过上游返回值触发路径穿越或查询注入；
- 请求头替换后强制校验无控制字符与长度上限，阻止 CRLF 头注入；
- 敏感变量自动脱敏，绝不将提取到的令牌/密码明文写入结果摘要。
"""

import re
from typing import Any

from api_agent.infrastructure.json_pointer import is_sensitive as is_sensitive_pointer
from api_agent.infrastructure.json_pointer import resolve as evaluate_pointer
from api_agent.infrastructure.logging import REDACTED, is_sensitive_key

VAR_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
VAR_NAME_RE = re.compile(VAR_NAME_PATTERN)
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]{0,63})\}\}")
EXACT_PLACEHOLDER_RE = re.compile(r"^\{\{([A-Za-z_][A-Za-z0-9_]{0,63})\}\}$")
CONCRETE_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9_~-]+(?:\.[A-Za-z0-9_~-]+)*")

MAX_EXTRACTED_VALUE_LEN = 4096


def find_placeholders(value: Any) -> tuple[set[str], bool]:
    """递归扫描任意 JSON 结构中的 `{{var}}` 占位符，返回 `(变量名集合, 是否存在非法花括号语法)`。"""
    names: set[str] = set()
    invalid = False
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            if "{{" in node or "}}" in node:
                stripped = PLACEHOLDER_RE.sub("", node)
                if "{{" in stripped or "}}" in stripped:
                    invalid = True
                for match in PLACEHOLDER_RE.finditer(node):
                    names.add(match.group(1))
        elif isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str) and ("{{" in key or "}}" in key):
                    invalid = True
                pending.append(item)
        elif isinstance(node, (list, tuple)):
            pending.extend(node)
    return names, invalid


def scalar_to_text(value: Any) -> str:
    """将提取到的标量安全转换为用于 URL/Header/字符串插值的文本。"""
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def resolve_string(
    text: str,
    variables: dict[str, Any],
    *,
    preserve_scalar: bool = False,
) -> tuple[Any, list[str]]:
    """按变量表替换字符串中的 `{{var}}`，返回 `(替换结果, 缺失变量列表)`。"""
    if "{{" not in text:
        return text, []
    missing: list[str] = []
    if preserve_scalar:
        exact = EXACT_PLACEHOLDER_RE.fullmatch(text)
        if exact:
            name = exact.group(1)
            if name not in variables:
                return text, [name]
            return variables[name], []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            if name not in missing:
                missing.append(name)
            return match.group(0)
        return scalar_to_text(variables[name])

    resolved = PLACEHOLDER_RE.sub(replace, text)
    return resolved, missing


def resolve_json_value(value: Any, variables: dict[str, Any]) -> tuple[Any, list[str]]:
    """递归解析 JSON 请求体或断言期望值中的 `{{var}}`，整串精确占位保留原始标量类型。"""
    if isinstance(value, str):
        return resolve_string(value, variables, preserve_scalar=True)
    if isinstance(value, list):
        missing: list[str] = []
        items = []
        for entry in value:
            resolved, item_missing = resolve_json_value(entry, variables)
            items.append(resolved)
            for name in item_missing:
                if name not in missing:
                    missing.append(name)
        return items, missing
    if isinstance(value, dict):
        missing = []
        result = {}
        for key, entry in value.items():
            resolved, item_missing = resolve_json_value(entry, variables)
            result[key] = resolved
            for name in item_missing:
                if name not in missing:
                    missing.append(name)
        return result, missing
    return value, []


def is_safe_resolved_path(path: str) -> bool:
    """校验替换后的绝对路径（不含 query）依然是合法、未编码、无穿越的路径。"""
    if not path.startswith("/"):
        return False
    if path == "/":
        return True
    segments = path[1:].removesuffix("/").split("/")
    return all(CONCRETE_PATH_SEGMENT_RE.fullmatch(part) for part in segments)


def is_safe_header_value(value: str) -> bool:
    """校验替换后的请求头值不含 CR/LF/控制字符且未超长。"""
    if len(value) > MAX_EXTRACTED_VALUE_LEN:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _lookup_header(
    headers: dict[str, list[str]] | Any, header_name: str
) -> tuple[bool, str | None]:
    target = header_name.lower()
    if hasattr(headers, "items"):
        for key, raw in headers.items():
            if str(key).lower() == target:
                if isinstance(raw, list):
                    return (True, str(raw[0])) if raw else (False, None)
                return True, str(raw)
    return False, None


def is_rule_sensitive(rule: Any) -> bool:
    """判断提取规则是否应视为敏感凭据并自动脱敏。"""
    if getattr(rule, "sensitive", False):
        return True
    if is_sensitive_key(getattr(rule, "name", "")):
        return True
    pointer = getattr(rule, "pointer", "") or ""
    if pointer and is_sensitive_pointer(pointer):
        return True
    header_name = getattr(rule, "header_name", "") or ""
    if header_name and is_sensitive_key(header_name):
        return True
    return False


def extract_from_response(
    rules: list[Any],
    *,
    status_code: int | None,
    headers: dict[str, list[str]] | Any,
    has_parsed_json: bool,
    parsed_json: Any,
    omit_values: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], set[str], bool]:
    """按规则从单次 HTTP 响应中提取变量。

    返回：
    - `extracted_vars`: 成功提取的原始运行时变量（供后续步骤注入）；
    - `records`: 脱敏后的提取记录列表（写入结果证据）；
    - `sensitive_secrets`: 提取出的敏感字符串值（供隐私上下文学习并清洗）；
    - `required_failed`: 是否有 `required=True` 的规则提取失败。
    """
    extracted_vars: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    sensitive_secrets: set[str] = set()
    required_failed = False

    for rule in rules:
        sensitive = is_rule_sensitive(rule)
        source = rule.source
        found = False
        raw_value: Any = None
        message: str | None = None

        if source == "status":
            if status_code is not None:
                found = True
                raw_value = status_code
            else:
                message = "response status unavailable"
        elif source == "header":
            found, raw_value = _lookup_header(headers or {}, rule.header_name)
            if not found:
                message = f"response header '{rule.header_name}' not found"
        elif source == "json":
            if not has_parsed_json:
                message = "response body is not valid JSON"
            else:
                found, raw_value = evaluate_pointer(parsed_json, rule.pointer)
                if not found:
                    message = f"JSON Pointer '{rule.pointer}' not found"

        if found:
            if raw_value is None or isinstance(raw_value, (dict, list)):
                found = False
                message = "extracted value must be a non-null scalar"
            else:
                text_val = scalar_to_text(raw_value)
                if len(text_val) > MAX_EXTRACTED_VALUE_LEN:
                    found = False
                    message = "extracted value exceeds length limit"

        if found:
            extracted_vars[rule.name] = raw_value
            if sensitive:
                text_val = scalar_to_text(raw_value)
                if len(text_val) >= 4:
                    sensitive_secrets.add(text_val)
            display_value: Any = REDACTED if (sensitive or omit_values) else raw_value
            records.append(
                {
                    "name": rule.name,
                    "source": source,
                    "ok": True,
                    "sensitive": sensitive,
                    "value": display_value,
                    "message": None,
                }
            )
        else:
            if rule.required:
                required_failed = True
            records.append(
                {
                    "name": rule.name,
                    "source": source,
                    "ok": False,
                    "sensitive": sensitive,
                    "value": None,
                    "message": message or "extraction failed",
                }
            )

    return extracted_vars, records, sensitive_secrets, required_failed
