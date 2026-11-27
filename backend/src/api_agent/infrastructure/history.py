"""Shared sanitization for persisted request/stream history.

作用：控制台与长连接历史会把请求头、请求体预览、响应头与响应体预览写进 SQLite；
若只在写入时脱敏，旧行、或某条新路径漏改一处，凭据仍会留在库里。
因此这里提供**唯一入口**：写入前清洗、启动时迁移旧行、读取时再清洗一次。
实现要点：
- 同一条记录内先 ``learn()`` 全部明文字段，再统一 ``clean()``，
  这样“密码出现在请求体、又在响应回显里出现”这种兄弟字段泄漏也能被抹掉；
- 先学习**完整**正文再截断，避免敏感值跨过截断点后漏识别；
- 幂等：已替换成占位符的字段不再产生新的秘密，重复执行返回 0。
"""

import json
import re

from sqlalchemy import select

from api_agent.assistant.models import AssistantMessageRecord
from api_agent.assistant.thread_store import scrub_assistant_fields
from api_agent.console.models import ConsoleRequestRecord, ConsoleSubCaseRecord
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.logging import (
    REDACTED,
    SENSITIVE_KEYS,
    is_sensitive_key,
    normalize_key,
)
from api_agent.infrastructure.privacy import Context, is_sensitive_pointer
from api_agent.infrastructure.targets import redact_headers
from api_agent.llm.models import ModelTestRunRecord
from api_agent.streams.models import StreamSessionRecord

PREVIEW_LIMIT = 8 * 1024
# 截断后的 JSON 文本里 ``"password": "value"`` 这样的键值对（含转义引号）。
_LABELLED_PAIR = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.){4,})"')
_BEARER_SECRET = re.compile(r"\bBearer\s+([^\s,;\"']+)", re.IGNORECASE)
# 已脱敏的占位符不应再次被登记为"秘密"，否则重复清洗会改写占位符本身。
_PLACEHOLDERS = {REDACTED, "[redacted]", "[PHONE]", "[EMAIL]"}
# 历史接口对外使用的头占位符（与 ``redact_headers`` 契约一致，小写形态）。
HEADER_PLACEHOLDERS = {"[redacted]", REDACTED}
_HEADER_DICT_KEYS = frozenset({"headers", "request_headers", "response_headers"})


def _safe_headers(original, *, context: Context) -> dict:
    """先按键名脱敏，再按已学秘密清洗，最后还原多值形态。

    实现：``redact_headers`` 对敏感键会返回等长列表（多值头）；
    ``Context.clean`` 处理列表时会逐项清洗但值本身是字符串，
    因此这里以**原始记录**的列表性为准重建结果，避免
    ``Set-Cookie: ["a=..."]`` 被塌成字符串而破坏契约。
    """
    redacted = redact_headers(original or {})
    cleaned = context.clean(redacted)
    output = {}
    for name, value in redacted.items():
        result = cleaned.get(name, value)
        if isinstance(value, list):
            items = result if isinstance(result, list) else [result] * len(value)
            # 已经脱敏的占位符保持原样：历史契约用的是 ``[redacted]``，
            # 不能被下文清洗统一改写成 ``[REDACTED]``。
            output[name] = [
                original_item if str(original_item) in HEADER_PLACEHOLDERS else item
                for item, original_item in zip(items, value, strict=False)
            ]
        else:
            result = result[0] if isinstance(result, list) and result else result
            output[name] = value if str(value) in HEADER_PLACEHOLDERS else result
    return output


def _jsonable(value):
    """把预览文本解析成结构（能解析时），否则原样返回。"""
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if not stripped.startswith(("{", "[")):
        return value
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return value


def _sanitize_body(text: str | None, mode: str, context: Context) -> str | None:
    """先学习完整正文里的敏感值，再清洗并截成预览。

    实现：``Context.body`` 只在整段 JSON 可解析时按键学习；历史里的预览
    往往已被截断（9000 字符 → 8192），整段解析必然失败，命名敏感字段
    就学不到。因此这里在结构化解析失败时补一次**按键扫描**：
    用正则找出 ``"key": "value"`` 形式的敏感键值并学习，再按文本清洗。
    """
    if text is None:
        return None
    cleaned = context.body(text, mode)
    if cleaned is None:
        return None
    # 截断后的 JSON 无法整体解析，命名敏感值会漏学；补一轮键值扫描。
    for match in _LABELLED_PAIR.finditer(text):
        key, value = match.group(1), match.group(2)
        if is_sensitive_key(key) and len(value) >= 4:
            context.redactor.secrets = tuple(
                sorted({*context.redactor.secrets, value}, key=len, reverse=True)
            )
    if context.redactor.secrets:
        cleaned = context.text(cleaned)
    return cleaned or None


def _learn_header_secret(context: Context, value) -> None:
    """把敏感头的原值登记为秘密，供随后抹掉同记录内的回显。

    实现：只登记长度 ≥ 4 的值。单字符/双字符"秘密"会把 URL 里的普通字母、
    自由文本里的常见词一并替换，既过度脱敏又破坏幂等（占位符本身再次被匹配）。
    已是占位符的值不重复登记，保证重复清洗返回“未改动”。
    """
    for item in value if isinstance(value, list) else [value]:
        text = str(item)
        if len(text) >= 4 and text not in _PLACEHOLDERS:
            context.redactor.secrets = tuple(
                sorted({*context.redactor.secrets, text}, key=len, reverse=True)
            )
        for match in _BEARER_SECRET.finditer(text):
            token = match.group(1)
            if len(token) >= 4 and token not in _PLACEHOLDERS:
                context.redactor.secrets = tuple(
                    sorted({*context.redactor.secrets, token}, key=len, reverse=True)
                )


def _learn_labelled_pairs(text: str | None, context: Context) -> None:
    """从可能被截断或混排的文本中扫描 ``"key": "value"`` 敏感值与 Bearer 令牌并登记。"""
    if not text:
        return
    for match in _LABELLED_PAIR.finditer(text):
        key, value = match.group(1), match.group(2)
        if is_sensitive_key(key) and len(value) >= 4 and value not in _PLACEHOLDERS:
            context.redactor.secrets = tuple(
                sorted({*context.redactor.secrets, value}, key=len, reverse=True)
            )
    for match in _BEARER_SECRET.finditer(text):
        token = match.group(1)
        if len(token) >= 4 and token not in _PLACEHOLDERS:
            context.redactor.secrets = tuple(
                sorted({*context.redactor.secrets, token}, key=len, reverse=True)
            )


def _learn_nested_headers(node, context: Context) -> None:
    """递归收集嵌套 JSON 中敏感头原值与 Bearer 令牌，供随后清除同记录内兄弟回显。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in _HEADER_DICT_KEYS and isinstance(value, dict):
                for header_name, header_value in value.items():
                    if is_sensitive_key(header_name):
                        _learn_header_secret(context, header_value)
            else:
                _learn_nested_headers(value, context)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _learn_nested_headers(item, context)
    elif isinstance(node, str):
        _learn_labelled_pairs(node, context)


def _sanitize_json_payload(node, context: Context):
    """线性递归清洗 JSON 结构，兼容嵌套头字典并保留布尔/数值元数据。"""
    if isinstance(node, dict):
        if "pointer" in node and is_sensitive_pointer(node.get("pointer")):
            return context.clean(node)
        output = {}
        for key, value in node.items():
            key_str = str(key)
            if key_str == "contract":
                output[key_str] = context.contract(value)
            elif key_str.lower() in _HEADER_DICT_KEYS and isinstance(value, dict):
                output[key_str] = _safe_headers(value, context=context)
            elif isinstance(value, (dict, list, tuple)):
                if normalize_key(key_str) in SENSITIVE_KEYS:
                    output[key_str] = REDACTED
                else:
                    output[key_str] = _sanitize_json_payload(value, context)
            else:
                output[key_str] = context.clean({key_str: value})[key_str]
        return output
    if isinstance(node, (list, tuple)):
        return [_sanitize_json_payload(item, context) for item in node]
    if isinstance(node, str):
        return context.text(node)
    return node


def sanitize_console(
    record: ConsoleRequestRecord,
    *,
    full_body: str | None = None,
    full_response: str | None = None,
) -> bool:
    """就地清洗一条控制台历史，返回是否有字段被改写。

    参数：``full_body``/``full_response`` 是**截断前**的完整正文。
    预览会被切到 8 KiB，命名敏感字段（如 ``password``）可能正好落在切点之后；
    只用预览学习就会漏掉它，进而漏掉它在响应里的兄弟回显。
    迁移旧行时没有完整原文，只能退化为“按预览学习”。
    """
    context = Context()
    request_body = full_body if full_body is not None else record.request_body
    body_source = request_body if request_body is not None else record.body_preview
    # 第一遍：学习全部明文字段中的敏感值（含 URL 查询串、请求头、响应体、错误消息）。
    # 敏感头的原值必须在 redact 之前学习，否则错误消息里对它的回显无从识别。
    context.learn(
        {
            "request_headers": record.request_headers or {},
            "response_headers": record.response_headers or {},
            "body": _jsonable(body_source),
            "response": _jsonable(
                full_response if full_response is not None else record.response_preview
            ),
            "error_message": record.error_message,
            "title": record.title,
            "summary": record.summary,
            "redirects_json": record.redirects_json,
            "sse_json": record.sse_json,
        }
    )
    for name, value in (record.request_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    for name, value in (record.response_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    _learn_nested_headers(record.redirects_json, context)
    _learn_nested_headers(record.sse_json, context)
    context.body(body_source or "", record.body_mode or "json")
    if full_response:
        context.body(full_response, "json")
    if record.error_message:
        # 错误消息里常带目标回显或旧头值，先当作自由文本学习一遍。
        context.text(record.error_message)
    for text_field in (record.title, record.summary, record.error_message):
        _learn_labelled_pairs(text_field, context)

    url = context.text(record.url) if record.url else record.url
    headers = _safe_headers(record.request_headers, context=context)
    body = _sanitize_body(body_source, record.body_mode or "json", context)
    response_headers = _safe_headers(record.response_headers, context=context)
    response_body = _sanitize_body(record.response_preview, "json", context)
    error_message = context.text(record.error_message) if record.error_message else None
    title = context.text(record.title) if record.title else record.title
    summary = context.text(record.summary) if record.summary else record.summary
    redirects_json = (
        _sanitize_json_payload(record.redirects_json, context)
        if record.redirects_json is not None
        else None
    )
    sse_json = (
        _sanitize_json_payload(record.sse_json, context) if record.sse_json is not None else None
    )

    changed = False
    for field, value in (
        ("url", url),
        ("request_headers", headers),
        ("body_preview", body[:PREVIEW_LIMIT] if body else None),
        ("request_body", body if request_body is not None else None),
        ("response_headers", response_headers),
        ("response_preview", response_body[:PREVIEW_LIMIT] if response_body else None),
        ("error_message", error_message),
        ("title", title),
        ("summary", summary),
        ("redirects_json", redirects_json),
        ("sse_json", sse_json),
    ):
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


def sanitize_console_sub_case(
    record: ConsoleSubCaseRecord,
    *,
    full_body: str | None = None,
    full_response: str | None = None,
) -> bool:
    """就地清洗一条 AI 多轮控制台子用例快照，返回是否有字段被改写。"""
    context = Context()
    body_source = full_body if full_body is not None else record.request_body
    response_source = full_response if full_response is not None else record.response_preview
    context.learn(
        {
            "label": record.label,
            "request_headers": record.request_headers or {},
            "response_headers": record.response_headers or {},
            "body": _jsonable(body_source),
            "response": _jsonable(response_source),
            "error_message": record.error_message,
            "redirects_json": record.redirects_json,
            "sse_json": record.sse_json,
            "assertion_summary": record.assertion_summary,
        }
    )
    for name, value in (record.request_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    for name, value in (record.response_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    _learn_nested_headers(record.redirects_json, context)
    _learn_nested_headers(record.sse_json, context)
    _learn_nested_headers(record.assertion_summary, context)
    context.body(body_source or "", record.body_mode or "json")
    if full_response:
        context.body(full_response, "json")
    for text_field in (record.label, record.error_message):
        _learn_labelled_pairs(text_field, context)

    label = context.text(record.label) if record.label else record.label
    url = context.text(record.url) if record.url else record.url
    request_headers = _safe_headers(record.request_headers, context=context)
    cleaned_body = _sanitize_body(body_source, record.body_mode or "json", context)
    response_headers = (
        _safe_headers(record.response_headers, context=context)
        if record.response_headers is not None
        else None
    )
    cleaned_response = _sanitize_body(record.response_preview, "json", context)
    error_message = context.text(record.error_message) if record.error_message else None
    redirects_json = (
        _sanitize_json_payload(record.redirects_json, context)
        if record.redirects_json is not None
        else None
    )
    sse_json = (
        _sanitize_json_payload(record.sse_json, context) if record.sse_json is not None else None
    )
    assertion_summary = (
        _sanitize_json_payload(record.assertion_summary, context)
        if record.assertion_summary is not None
        else None
    )

    changed = False
    for field, value in (
        ("label", label),
        ("url", url),
        ("request_headers", request_headers),
        ("request_body", cleaned_body if body_source is not None else None),
        ("response_headers", response_headers),
        ("response_preview", cleaned_response[:PREVIEW_LIMIT] if cleaned_response else None),
        ("error_message", error_message),
        ("redirects_json", redirects_json),
        ("sse_json", sse_json),
        ("assertion_summary", assertion_summary),
    ):
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


def sanitize_stream(record: StreamSessionRecord) -> bool:
    """清洗地址、头和预置消息，保护同一会话表单内的敏感值。"""
    context = Context()
    context.learn(
        {
            "request_headers": record.request_headers or {},
            "title": record.title,
            "case_label": record.case_label,
            "summary": record.summary,
            "error_message": record.error_message,
        }
    )
    for name, value in (record.request_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    for text_field in (record.title, record.case_label, record.summary, record.error_message):
        _learn_labelled_pairs(text_field, context)
    scheduled = record.scheduled_messages
    if scheduled is not None:
        for message in scheduled:
            context.body(message)
        scheduled = [_sanitize_body(message, "json", context) or "" for message in scheduled]
    url = context.text(record.url) if record.url else record.url
    headers = _safe_headers(record.request_headers, context=context)
    error_message = context.text(record.error_message) if record.error_message else None
    title = context.text(record.title) if record.title else record.title
    case_label = context.text(record.case_label) if record.case_label else record.case_label
    summary = context.text(record.summary) if record.summary else record.summary

    changed = False
    for field, value in (
        ("url", url),
        ("request_headers", headers),
        ("scheduled_messages", scheduled),
        ("error_message", error_message),
        ("title", title),
        ("case_label", case_label),
        ("summary", summary),
    ):
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


def sanitize_model_test_run(record: ModelTestRunRecord) -> bool:
    """就地清洗模型测试任务记录中的地址、错误消息、快照与总结字段。"""
    context = Context()
    context.learn(
        {
            "title": record.title,
            "base_url": record.base_url,
            "error_message": record.error_message,
            "summary_text": record.summary_text,
            "discover_meta": record.discover_meta,
            "models_snapshot": record.models_snapshot,
            "rounds_json": record.rounds_json,
            "sub_cases_json": record.sub_cases_json,
            "summary_json": record.summary_json,
        }
    )
    for payload in (
        record.discover_meta,
        record.models_snapshot,
        record.rounds_json,
        record.sub_cases_json,
        record.summary_json,
    ):
        _learn_nested_headers(payload, context)
    for text_field in (record.title, record.error_message, record.summary_text):
        _learn_labelled_pairs(text_field, context)

    title = context.text(record.title) if record.title else record.title
    base_url = context.text(record.base_url) if record.base_url else record.base_url
    error_message = context.text(record.error_message) if record.error_message else None
    summary_text = context.text(record.summary_text) if record.summary_text else record.summary_text
    discover_meta = (
        _sanitize_json_payload(record.discover_meta, context)
        if record.discover_meta is not None
        else None
    )
    models_snapshot = (
        _sanitize_json_payload(record.models_snapshot, context)
        if record.models_snapshot is not None
        else []
    )
    rounds_json = (
        _sanitize_json_payload(record.rounds_json, context)
        if record.rounds_json is not None
        else []
    )
    sub_cases_json = (
        _sanitize_json_payload(record.sub_cases_json, context)
        if record.sub_cases_json is not None
        else []
    )
    summary_json = (
        _sanitize_json_payload(record.summary_json, context)
        if record.summary_json is not None
        else None
    )

    changed = False
    for field, value in (
        ("title", title),
        ("base_url", base_url),
        ("error_message", error_message),
        ("summary_text", summary_text),
        ("discover_meta", discover_meta),
        ("models_snapshot", models_snapshot),
        ("rounds_json", rounds_json),
        ("sub_cases_json", sub_cases_json),
        ("summary_json", summary_json),
    ):
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


def sanitize_assistant_message(record: AssistantMessageRecord) -> bool:
    """就地清洗 AI 助手消息正文与结构化块，保持与写库脱敏一致且严格幂等。"""
    cleaned_content, cleaned_blocks, _ = scrub_assistant_fields(
        record.content,
        record.blocks_json if isinstance(record.blocks_json, list) else [],
    )
    changed = False
    if record.content != cleaned_content:
        record.content = cleaned_content
        changed = True
    if record.blocks_json != cleaned_blocks:
        record.blocks_json = cleaned_blocks
        changed = True
    return changed


async def sanitize_history(database: Database) -> int:
    """启动时迁移：把库里仍在的历史行就地清洗一次，返回被改写的行数。

    实现：分别扫描控制台历史、子用例快照、长连接会话、模型测试任务及助手消息表；
    每行改动后统一提交。幂等性由内容比较保证——已清洗的行再次执行不会产生新改动，返回 0。
    """
    changed = 0
    async with database.session() as session:
        for record in (await session.scalars(select(ConsoleRequestRecord))).all():
            if sanitize_console(record):
                changed += 1
        for sub_case in (await session.scalars(select(ConsoleSubCaseRecord))).all():
            if sanitize_console_sub_case(sub_case):
                changed += 1
        for stream in (await session.scalars(select(StreamSessionRecord))).all():
            if sanitize_stream(stream):
                changed += 1
        for run in (await session.scalars(select(ModelTestRunRecord))).all():
            if sanitize_model_test_run(run):
                changed += 1
        for message in (await session.scalars(select(AssistantMessageRecord))).all():
            if sanitize_assistant_message(message):
                changed += 1
        if changed:
            await session.commit()
    return changed


__all__ = [
    "PREVIEW_LIMIT",
    "REDACTED",
    "sanitize_assistant_message",
    "sanitize_console",
    "sanitize_console_sub_case",
    "sanitize_history",
    "sanitize_model_test_run",
    "sanitize_stream",
]
