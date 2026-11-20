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

from api_agent.console.models import ConsoleRequestRecord
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.logging import REDACTED, is_sensitive_key
from api_agent.infrastructure.privacy import Context
from api_agent.infrastructure.targets import redact_headers
from api_agent.streams.models import StreamSessionRecord

PREVIEW_LIMIT = 8 * 1024
# 截断后的 JSON 文本里 ``"password": "value"`` 这样的键值对（含转义引号）。
_LABELLED_PAIR = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.){4,})"')
# 已脱敏的占位符不应再次被登记为"秘密"，否则重复清洗会改写占位符本身。
_PLACEHOLDERS = {REDACTED, "[redacted]", "[PHONE]", "[EMAIL]"}
# 历史接口对外使用的头占位符（与 ``redact_headers`` 契约一致，小写形态）。
HEADER_PLACEHOLDERS = {"[redacted]", REDACTED}


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
        }
    )
    for name, value in (record.request_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    for name, value in (record.response_headers or {}).items():
        if is_sensitive_key(name):
            _learn_header_secret(context, value)
    context.body(body_source or "", record.body_mode or "json")
    if full_response:
        context.body(full_response, "json")
    if record.error_message:
        # 错误消息里常带目标回显或旧头值，先当作自由文本学习一遍。
        context.text(record.error_message)

    url = context.text(record.url) if record.url else record.url
    headers = _safe_headers(record.request_headers, context=context)
    body = _sanitize_body(body_source, record.body_mode or "json", context)
    response_headers = _safe_headers(record.response_headers, context=context)
    response_body = _sanitize_body(record.response_preview, "json", context)
    error_message = context.text(record.error_message) if record.error_message else None

    changed = False
    for field, value in (
        ("url", url),
        ("request_headers", headers),
        ("body_preview", body[:PREVIEW_LIMIT] if body else None),
        ("request_body", body if request_body is not None else None),
        ("response_headers", response_headers),
        ("response_preview", response_body[:PREVIEW_LIMIT] if response_body else None),
        ("error_message", error_message),
    ):
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


def sanitize_stream(record: StreamSessionRecord) -> bool:
    """清洗地址、头和预置消息，保护同一会话表单内的敏感值。"""
    context = Context()
    context.learn({"request_headers": record.request_headers or {}})
    scheduled = record.scheduled_messages
    if scheduled is not None:
        for message in scheduled:
            context.body(message)
        scheduled = [_sanitize_body(message, "json", context) or "" for message in scheduled]
    url = context.text(record.url) if record.url else record.url
    headers = _safe_headers(record.request_headers, context=context)
    error_message = context.text(record.error_message) if record.error_message else None

    changed = False
    for field, value in (
        ("url", url),
        ("request_headers", headers),
        ("scheduled_messages", scheduled),
        ("error_message", error_message),
    ):
        if getattr(record, field) != value:
            setattr(record, field, value)
            changed = True
    return changed


async def sanitize_history(database: Database) -> int:
    """启动时迁移：把库里仍在的历史行就地清洗一次，返回被改写的行数。

    实现：分别扫描两张历史表；每行改动后统一提交。
    幂等性由内容比较保证——已清洗的行再次执行不会产生新改动，因此返回 0。
    """
    changed = 0
    async with database.session() as session:
        for record in (await session.scalars(select(ConsoleRequestRecord))).all():
            if sanitize_console(record):
                changed += 1
        for stream in (await session.scalars(select(StreamSessionRecord))).all():
            if sanitize_stream(stream):
                changed += 1
        if changed:
            await session.commit()
    return changed


__all__ = [
    "PREVIEW_LIMIT",
    "REDACTED",
    "sanitize_console",
    "sanitize_history",
    "sanitize_stream",
]
