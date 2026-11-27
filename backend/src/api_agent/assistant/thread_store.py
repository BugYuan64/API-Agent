"""Async repository and payload scrubbing for page-scoped assistant chat threads."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.assistant.models import (
    VALID_PAGE_SCOPES,
    AssistantMessageRecord,
    AssistantThreadRecord,
)
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.logging import (
    REDACTED,
    SENSITIVE_KEYS,
    is_sensitive_key,
    normalize_key,
)
from api_agent.infrastructure.orm import as_utc, utc_now
from api_agent.infrastructure.privacy import Context, is_sensitive_pointer
from api_agent.infrastructure.targets import redact_headers

_HEADER_DICT_KEYS = frozenset({"headers", "request_headers", "response_headers"})
_LABELLED_PAIR = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"((?:[^"\\]|\\.){4,})"')
_BEARER_SECRET = re.compile(r"\bBearer\s+([^\s,;\"']+)", re.IGNORECASE)
_PLACEHOLDERS = frozenset({REDACTED, "[redacted]", "[PHONE]", "[EMAIL]"})
_HEADER_PLACEHOLDERS = frozenset({"[redacted]", REDACTED})


def _validate_scope(scope: str) -> str:
    """校验页面作用域，保证落库线程严格属于四个规范工作台之一。"""
    if scope not in VALID_PAGE_SCOPES:
        allowed = ", ".join(sorted(VALID_PAGE_SCOPES))
        raise ValueError(f"Unsupported assistant scope {scope!r}; expected one of: {allowed}")
    return scope


def _validate_history_id(history_id: str) -> str:
    """校验非空历史记录标识并限制列宽边界。"""
    cleaned = str(history_id).strip()
    if not cleaned:
        raise ValueError("history_id must be a non-empty string")
    if len(cleaned) > 64:
        raise ValueError("history_id exceeds maximum length of 64 characters")
    return cleaned


def _register_secret(context: Context, raw: str) -> None:
    """登记长度 >= 4 且非占位符的明文秘密，避免重复清洗破坏幂等性。"""
    if len(raw) >= 4 and raw not in _PLACEHOLDERS:
        context.redactor.secrets = tuple(
            sorted({*context.redactor.secrets, raw}, key=len, reverse=True)
        )


def _learn_nested_headers(node: Any, context: Context) -> None:
    """递归发现嵌套头字典中的敏感原值与 Bearer 令牌并登记到上下文，用于抹除兄弟回显。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in _HEADER_DICT_KEYS and isinstance(value, dict):
                for header_name, header_val in value.items():
                    if is_sensitive_key(header_name):
                        items = header_val if isinstance(header_val, list) else [header_val]
                        for item in items:
                            text = str(item)
                            _register_secret(context, text)
                            for match in _BEARER_SECRET.finditer(text):
                                _register_secret(context, match.group(1))
            else:
                _learn_nested_headers(value, context)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _learn_nested_headers(item, context)
    elif isinstance(node, str):
        for match in _BEARER_SECRET.finditer(node):
            _register_secret(context, match.group(1))


def _safe_headers(original: dict[str, Any], context: Context) -> dict[str, Any]:
    """按键名与已学秘密脱敏嵌套头字典，同时保留既有占位符与多值列表形状。"""
    redacted = redact_headers(original or {})
    cleaned = context.clean(redacted)
    output: dict[str, Any] = {}
    for name, value in redacted.items():
        result = cleaned.get(name, value)
        if isinstance(value, list):
            items = result if isinstance(result, list) else [result] * len(value)
            output[name] = [
                orig if str(orig) in _HEADER_PLACEHOLDERS else item
                for item, orig in zip(items, value, strict=False)
            ]
        else:
            result = result[0] if isinstance(result, list) and result else result
            output[name] = value if str(value) in _HEADER_PLACEHOLDERS else result
    return output


def _clean_structured(node: Any, context: Context) -> Any:
    """线性递归清洗结构化块数据，对嵌套头字典应用头脱敏规则，其余委托 ``Context.clean``。"""
    if isinstance(node, dict):
        if "pointer" in node and is_sensitive_pointer(node.get("pointer")):
            return context.clean(node)
        output: dict[str, Any] = {}
        for key, value in node.items():
            key_str = str(key)
            if key_str == "contract":
                output[key_str] = context.contract(value)
            elif key_str.lower() in _HEADER_DICT_KEYS and isinstance(value, dict):
                output[key_str] = _safe_headers(value, context)
            elif isinstance(value, (dict, list, tuple)):
                if normalize_key(key_str) in SENSITIVE_KEYS:
                    output[key_str] = REDACTED
                else:
                    output[key_str] = _clean_structured(value, context)
            else:
                output[key_str] = context.clean({key_str: value})[key_str]
        return output
    if isinstance(node, (list, tuple)):
        return [_clean_structured(item, context) for item in node]
    if isinstance(node, str):
        return context.text(node)
    return node


def scrub_assistant_fields(
    content: str,
    blocks: list[Any] | None = None,
    *,
    title: str | None = None,
) -> tuple[str, list[Any], str | None]:
    """在写库和历史扫描边界统一脱敏助手消息正文、结构化块及标题。"""
    raw_content = content if isinstance(content, str) else str(content or "")
    raw_blocks = list(blocks) if isinstance(blocks, (list, tuple)) else []
    raw_title = title if title is None else str(title)

    context = Context()
    context.learn({"content": raw_content, "blocks": raw_blocks, "title": raw_title})
    _learn_nested_headers(raw_blocks, context)
    for text_field in (raw_content, raw_title):
        if text_field:
            for match in _BEARER_SECRET.finditer(text_field):
                _register_secret(context, match.group(1))
    for match in _LABELLED_PAIR.finditer(raw_content):
        key, value = match.group(1), match.group(2)
        if is_sensitive_key(key):
            _register_secret(context, value)

    cleaned_content = context.text(raw_content) if raw_content else ""
    cleaned_blocks = _clean_structured(raw_blocks, context)
    cleaned_title = context.text(raw_title) if raw_title else raw_title
    return cleaned_content, cleaned_blocks, cleaned_title


def _serialize_message(record: AssistantMessageRecord) -> dict[str, Any]:
    created_at = as_utc(record.created_at)
    return {
        "id": record.id,
        "role": record.role,
        "content": record.content,
        "blocks": list(record.blocks_json) if isinstance(record.blocks_json, list) else [],
        "run_id": record.run_id,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


def _serialize_thread(
    thread: AssistantThreadRecord,
    messages: list[AssistantMessageRecord],
) -> dict[str, Any]:
    created_at = as_utc(thread.created_at)
    updated_at = as_utc(thread.updated_at)
    return {
        "id": thread.id,
        "scope": thread.scope,
        "history_id": thread.history_id,
        "title": thread.title,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
        "messages": [_serialize_message(msg) for msg in messages],
    }


class AssistantThreadRepository:
    """页面隔离 AI 助手对话线程的异步持久化仓储。"""

    def __init__(self, database: Database):
        self.database = database

    async def _fetch_thread_row(
        self, session: AsyncSession, scope: str, history_id: str
    ) -> AssistantThreadRecord | None:
        return await session.scalar(
            select(AssistantThreadRecord).where(
                AssistantThreadRecord.scope == scope,
                AssistantThreadRecord.history_id == history_id,
            )
        )

    async def _fetch_messages(
        self, session: AsyncSession, thread_id: str
    ) -> list[AssistantMessageRecord]:
        return list(
            (
                await session.scalars(
                    select(AssistantMessageRecord)
                    .where(AssistantMessageRecord.thread_id == thread_id)
                    .order_by(
                        AssistantMessageRecord.created_at.asc(),
                        AssistantMessageRecord.id.asc(),
                    )
                )
            ).all()
        )

    async def _ensure_thread_row(
        self,
        session: AsyncSession,
        scope: str,
        history_id: str,
        title: str | None = None,
    ) -> tuple[AssistantThreadRecord, bool]:
        """获取或创建线程行，并发唯一键冲突时回滚并重读；返回 ``(row, dirty)``。"""
        clean_title: str | None = None
        if title is not None:
            _, _, scrubbed = scrub_assistant_fields("", None, title=title)
            clean_title = scrubbed[:200] if scrubbed else None

        existing = await self._fetch_thread_row(session, scope, history_id)
        if existing is not None:
            if clean_title and not existing.title:
                existing.title = clean_title
                existing.updated_at = utc_now()
                return existing, True
            return existing, False

        thread = AssistantThreadRecord(
            scope=scope,
            history_id=history_id,
            title=clean_title,
        )
        session.add(thread)
        try:
            await session.flush()
            return thread, True
        except IntegrityError:
            await session.rollback()
            existing = await self._fetch_thread_row(session, scope, history_id)
            if existing is None:
                raise
            if clean_title and not existing.title:
                existing.title = clean_title
                existing.updated_at = utc_now()
                return existing, True
            return existing, False

    async def get_or_create_thread(
        self,
        scope: str,
        history_id: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        """按 ``(scope, history_id)`` 获取已有线程或新建空线程，返回含消息列表的字典。"""
        valid_scope = _validate_scope(scope)
        valid_history_id = _validate_history_id(history_id)
        async with self.database.session() as session:
            thread, dirty = await self._ensure_thread_row(
                session, valid_scope, valid_history_id, title=title
            )
            if dirty:
                await session.commit()
            messages = await self._fetch_messages(session, thread.id)
            return _serialize_thread(thread, messages)

    async def get_thread(self, scope: str, history_id: str) -> dict[str, Any] | None:
        """读取指定历史记录绑定的助手线程及按时间升序排列的消息，不存在时返回 ``None``。"""
        valid_scope = _validate_scope(scope)
        valid_history_id = _validate_history_id(history_id)
        async with self.database.session() as session:
            thread = await self._fetch_thread_row(session, valid_scope, valid_history_id)
            if thread is None:
                return None
            messages = await self._fetch_messages(session, thread.id)
            return _serialize_thread(thread, messages)

    async def append_message(
        self,
        scope: str,
        history_id: str,
        role: str,
        content: str,
        *,
        blocks: list[Any] | None = None,
        run_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """向线程追加一条脱敏后的消息；若线程尚不存在则自动创建。"""
        valid_scope = _validate_scope(scope)
        valid_history_id = _validate_history_id(history_id)
        clean_role = str(role or "").strip()[:20]
        if not clean_role:
            raise ValueError("role must be a non-empty string")
        clean_content, clean_blocks, clean_title = scrub_assistant_fields(
            content, blocks, title=title
        )
        clean_run_id = str(run_id).strip()[:64] if run_id is not None else None
        if clean_run_id == "":
            clean_run_id = None

        async with self.database.session() as session:
            thread, _ = await self._ensure_thread_row(
                session, valid_scope, valid_history_id, title=clean_title
            )
            message = AssistantMessageRecord(
                thread_id=thread.id,
                role=clean_role,
                content=clean_content,
                blocks_json=clean_blocks,
                run_id=clean_run_id,
            )
            thread.updated_at = utc_now()
            session.add(message)
            await session.commit()
            return {
                **_serialize_message(message),
                "thread_id": thread.id,
            }

    async def replace_or_migrate_messages(
        self,
        scope: str,
        history_id: str,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """当线程为空时将旧版 localStorage 消息脱敏迁移入库；已有消息默认不覆盖。

        设置 ``force=True`` 可显式替换该线程的全部消息。
        """
        valid_scope = _validate_scope(scope)
        valid_history_id = _validate_history_id(history_id)
        async with self.database.session() as session:
            thread, dirty = await self._ensure_thread_row(
                session, valid_scope, valid_history_id, title=title
            )
            existing_count = (
                await session.scalar(
                    select(func.count())
                    .select_from(AssistantMessageRecord)
                    .where(AssistantMessageRecord.thread_id == thread.id)
                )
                or 0
            )
            if existing_count > 0 and not force:
                if dirty:
                    await session.commit()
                existing_messages = await self._fetch_messages(session, thread.id)
                return _serialize_thread(thread, existing_messages)

            if force and existing_count > 0:
                await session.execute(
                    delete(AssistantMessageRecord).where(
                        AssistantMessageRecord.thread_id == thread.id
                    )
                )
                dirty = True

            for item in messages or []:
                if not isinstance(item, dict):
                    continue
                item_role = str(item.get("role") or "assistant").strip()[:20] or "assistant"
                raw_content = item.get("content", "")
                raw_blocks = item.get("blocks")
                if raw_blocks is None:
                    raw_blocks = item.get("blocks_json")
                clean_content, clean_blocks, _ = scrub_assistant_fields(
                    raw_content if isinstance(raw_content, str) else str(raw_content or ""),
                    raw_blocks if isinstance(raw_blocks, list) else [],
                )
                raw_run_id = item.get("run_id")
                clean_run_id = str(raw_run_id).strip()[:64] if raw_run_id is not None else None
                if clean_run_id == "":
                    clean_run_id = None
                session.add(
                    AssistantMessageRecord(
                        thread_id=thread.id,
                        role=item_role,
                        content=clean_content,
                        blocks_json=clean_blocks,
                        run_id=clean_run_id,
                    )
                )
                dirty = True

            if dirty:
                thread.updated_at = utc_now()
                await session.commit()
            stored_messages = await self._fetch_messages(session, thread.id)
            return _serialize_thread(thread, stored_messages)

    async def delete_thread(self, scope: str, history_id: str) -> bool:
        """删除单条历史记录绑定的助手线程（级联删除其全部消息），返回是否命中删除。"""
        valid_scope = _validate_scope(scope)
        valid_history_id = _validate_history_id(history_id)
        async with self.database.session() as session:
            result = await session.execute(
                delete(AssistantThreadRecord).where(
                    AssistantThreadRecord.scope == valid_scope,
                    AssistantThreadRecord.history_id == valid_history_id,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def clear_scope(self, scope: str) -> int:
        """清空某个页面作用域下的全部助手线程及消息，返回删除的线程数量。"""
        valid_scope = _validate_scope(scope)
        async with self.database.session() as session:
            result = await session.execute(
                delete(AssistantThreadRecord).where(AssistantThreadRecord.scope == valid_scope)
            )
            await session.commit()
            return int(result.rowcount or 0)
