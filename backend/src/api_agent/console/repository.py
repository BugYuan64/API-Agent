"""SQLite persistence boundary for console HTTP requests, AI suites, and sub-cases."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select, update

from api_agent.console.models import ConsoleRequestRecord, ConsoleSubCaseRecord
from api_agent.console.service import ConsoleRequest, ConsoleResult
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.history import (
    PREVIEW_LIMIT,
    sanitize_console,
    sanitize_console_sub_case,
)
from api_agent.infrastructure.orm import as_utc, new_id
from api_agent.infrastructure.outcomes import outcome_for


def _serialize_sub_case(record: ConsoleSubCaseRecord) -> dict[str, Any]:
    """Serialize one sanitized sub-case record for read-only replay and tool access."""
    return {
        "id": record.id,
        "parent_id": record.parent_id,
        "round_index": record.round_index,
        "case_index": record.case_index,
        "label": record.label,
        "method": record.method,
        "url": record.url,
        "request_headers": dict(record.request_headers or {}),
        "body_mode": record.body_mode,
        "request_body": record.request_body,
        "ok": bool(record.ok),
        "status_code": record.status_code,
        "elapsed_ms": record.elapsed_ms,
        "error_kind": record.error_kind,
        "error_message": record.error_message,
        "response_headers": dict(record.response_headers or {}),
        "response_preview": record.response_preview,
        "response_bytes": record.response_bytes,
        "http_version": record.http_version,
        "response_reason": record.response_reason,
        "redirects": list(record.redirects_json) if isinstance(record.redirects_json, list) else [],
        "sse": record.sse_json if isinstance(record.sse_json, dict) else None,
        "assertion_summary": (
            record.assertion_summary if isinstance(record.assertion_summary, dict) else None
        ),
        "created_at": as_utc(record.created_at),
    }


def _serialize_summary(
    record: ConsoleRequestRecord,
    *,
    sub_case_count: int = 0,
) -> dict[str, Any]:
    """Serialize one console history list item without expanding request_body."""
    return {
        "id": record.id,
        "project_id": record.project_id,
        "method": record.method,
        "url": record.url,
        "request_headers": dict(record.request_headers or {}),
        "body_mode": record.body_mode,
        "body_preview": record.body_preview,
        "ok": bool(record.ok),
        "status_code": record.status_code,
        "elapsed_ms": record.elapsed_ms,
        "error_kind": record.error_kind,
        "error_message": record.error_message,
        "response_headers": dict(record.response_headers or {}),
        "response_preview": record.response_preview,
        "response_bytes": record.response_bytes,
        "source": record.source,
        "title": record.title,
        "badge": record.badge,
        "status": record.status,
        "summary": record.summary,
        "total_rounds": record.total_rounds,
        "cooldown_ms": record.cooldown_ms,
        "sub_case_count": sub_case_count,
        "http_version": record.http_version,
        "response_reason": record.response_reason,
        "created_at": as_utc(record.created_at),
    }


def _serialize_detail(
    record: ConsoleRequestRecord,
    sub_cases: list[ConsoleSubCaseRecord],
) -> dict[str, Any]:
    """Serialize full console history detail with replay body, SSE, redirects, and sub-cases."""
    return {
        **_serialize_summary(record, sub_case_count=len(sub_cases)),
        "request_body": record.request_body,
        "redirects": list(record.redirects_json) if isinstance(record.redirects_json, list) else [],
        "sse": record.sse_json if isinstance(record.sse_json, dict) else None,
        "sub_cases": [_serialize_sub_case(item) for item in sub_cases],
    }


class ConsoleRepository:
    """控制台请求、AI 多轮父记录与子用例快照的事务仓储。"""

    def __init__(self, database: Database):
        self.database = database

    async def create_run(
        self,
        *,
        method: str,
        url: str,
        body_mode: str = "none",
        request_headers: dict[str, Any] | None = None,
        request_body: str | None = None,
        source: str = "manual",
        title: str | None = None,
        badge: str | None = None,
        status: str = "running",
        total_rounds: int = 1,
        cooldown_ms: int = 500,
        project_id: str | None = None,
        record_id: str | None = None,
    ) -> dict[str, Any]:
        """创建并脱敏落库一条控制台父运行记录（支持手动单次与 AI 多轮套件）。"""
        async with self.database.session() as session:
            record = ConsoleRequestRecord(
                id=record_id or new_id(),
                project_id=project_id,
                method=method,
                url=url,
                request_headers=dict(request_headers or {}),
                body_mode=body_mode,
                body_preview=(request_body or "")[:PREVIEW_LIMIT] or None,
                request_body=request_body,
                ok=False,
                elapsed_ms=0,
                response_headers={},
                response_bytes=0,
                source=source,
                title=title[:200] if isinstance(title, str) else title,
                badge=badge[:40] if isinstance(badge, str) else badge,
                status=status,
                total_rounds=max(1, int(total_rounds)),
                cooldown_ms=max(0, int(cooldown_ms)),
            )
            sanitize_console(record, full_body=request_body)
            session.add(record)
            await session.commit()
            return _serialize_detail(record, [])

    async def append_sub_case(
        self,
        parent_id: str,
        case_data: dict[str, Any],
    ) -> dict[str, Any]:
        """在父记录下追加一条脱敏后的子用例执行快照。"""
        raw_body = case_data.get("request_body")
        full_response = case_data.get("full_response")
        if full_response is None:
            full_response = case_data.get("response_body")
        raw_preview = case_data.get("response_preview")
        if raw_preview is None and full_response is not None:
            raw_preview = (full_response or "")[:PREVIEW_LIMIT] or None
        elif isinstance(raw_preview, str):
            raw_preview = raw_preview[:PREVIEW_LIMIT] or None

        raw_elapsed = case_data.get("elapsed_ms")
        elapsed_int = int(round(raw_elapsed)) if raw_elapsed is not None else None

        redirects = case_data.get("redirects_json")
        if redirects is None and "redirects" in case_data:
            redirects = case_data.get("redirects")
        sse = case_data.get("sse_json")
        if sse is None and "sse" in case_data:
            sse = case_data.get("sse")

        raw_response_headers = case_data.get("response_headers")
        async with self.database.session() as session:
            sub_case = ConsoleSubCaseRecord(
                id=case_data.get("id") or new_id(),
                parent_id=parent_id,
                round_index=int(case_data.get("round_index", 1)),
                case_index=int(case_data.get("case_index", 0)),
                label=str(case_data.get("label") or "")[:200],
                method=str(case_data.get("method") or "GET")[:16],
                url=str(case_data.get("url") or "")[:2000],
                request_headers=dict(case_data.get("request_headers") or {}),
                body_mode=str(case_data.get("body_mode") or "none")[:20],
                request_body=raw_body,
                ok=bool(case_data.get("ok", False)),
                status_code=case_data.get("status_code"),
                elapsed_ms=elapsed_int,
                error_kind=case_data.get("error_kind"),
                error_message=case_data.get("error_message"),
                response_headers=(
                    dict(raw_response_headers) if raw_response_headers is not None else None
                ),
                response_preview=raw_preview,
                response_bytes=int(case_data.get("response_bytes") or 0),
                http_version=case_data.get("http_version"),
                response_reason=case_data.get("response_reason"),
                redirects_json=redirects,
                sse_json=sse,
                assertion_summary=case_data.get("assertion_summary"),
            )
            sanitize_console_sub_case(
                sub_case,
                full_body=raw_body,
                full_response=full_response if full_response is not None else raw_preview,
            )
            session.add(sub_case)
            await session.commit()
            return _serialize_sub_case(sub_case)

    async def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        ok: bool = False,
        status_code: int | None = None,
        elapsed_ms: float = 0,
        error_kind: str | None = None,
        error_message: str | None = None,
        response_headers: dict[str, Any] | None = None,
        response_preview: str | None = None,
        response_bytes: int = 0,
        http_version: str | None = None,
        response_reason: str | None = None,
        redirects_json: list[dict[str, Any]] | None = None,
        sse_json: dict[str, Any] | None = None,
        summary: str | None = None,
        full_body: str | None = None,
        full_response: str | None = None,
        url: str | None = None,
        request_headers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入终态、脱敏响应证据与汇总信息，返回完整详情字典。"""
        async with self.database.session() as session:
            record = await session.get(ConsoleRequestRecord, run_id)
            if record is None:
                raise KeyError(f"Console request {run_id} was not found")

            if url is not None:
                record.url = url
            if request_headers is not None:
                record.request_headers = dict(request_headers)

            record.status = status
            record.ok = bool(ok)
            record.status_code = status_code
            record.elapsed_ms = float(elapsed_ms)
            record.error_kind = error_kind[:60] if isinstance(error_kind, str) else error_kind
            record.error_message = (
                error_message[:300] if isinstance(error_message, str) else error_message
            )
            if response_headers is not None:
                record.response_headers = dict(response_headers)
            effective_preview = response_preview
            if effective_preview is None and full_response is not None:
                effective_preview = (full_response or "")[:PREVIEW_LIMIT] or None
            elif isinstance(effective_preview, str):
                effective_preview = effective_preview[:PREVIEW_LIMIT] or None
            record.response_preview = effective_preview
            record.response_bytes = int(response_bytes or 0)
            record.http_version = http_version
            record.response_reason = (
                response_reason[:120] if isinstance(response_reason, str) else response_reason
            )
            record.redirects_json = redirects_json
            record.sse_json = sse_json
            if summary is not None:
                record.summary = summary

            sanitize_console(
                record,
                full_body=full_body,
                full_response=(full_response if full_response is not None else response_preview),
            )
            await session.commit()
            sub_cases = await self._fetch_sub_cases(session, run_id)
            return _serialize_detail(record, sub_cases)

    async def save_single_result(
        self,
        payload: ConsoleRequest,
        result: ConsoleResult,
        *,
        source: str = "manual",
        title: str | None = None,
        badge: str | None = None,
        status: str = "completed",
        record_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """在单次 HTTP/SSE 请求完成后一次性脱敏落库（用于同步接口与兼容实时流）。"""
        response = result.response
        redirects_json = (
            [hop.model_dump(mode="json") for hop in response.redirects]
            if response and response.redirects
            else None
        )
        async with self.database.session() as session:
            record = ConsoleRequestRecord(
                id=record_id or new_id(),
                project_id=project_id,
                method=payload.method,
                url=result.request["url"],
                request_headers=dict(payload.headers),
                body_mode=payload.body_mode,
                body_preview=(payload.body or "")[:PREVIEW_LIMIT] or None,
                request_body=payload.body,
                ok=result.ok,
                status_code=response.status if response else None,
                elapsed_ms=result.elapsed_ms,
                error_kind=result.error_kind,
                error_message=(
                    result.error_message[:300]
                    if isinstance(result.error_message, str)
                    else result.error_message
                ),
                response_headers=dict(response.headers) if response else {},
                response_preview=(response.body or "")[:PREVIEW_LIMIT] if response else None,
                response_bytes=response.body_bytes if response else 0,
                source=source,
                title=title[:200] if isinstance(title, str) else title,
                badge=badge[:40] if isinstance(badge, str) else badge,
                status=status,
                total_rounds=1,
                cooldown_ms=500,
                http_version=response.http_version if response else None,
                response_reason=response.reason if response else None,
                redirects_json=redirects_json,
                sse_json=response.sse if response else None,
            )
            # 先用完整正文学习命名敏感字段，再擦除兄弟回显并截断预览。
            sanitize_console(
                record,
                full_body=payload.body,
                full_response=response.body if response else None,
            )
            session.add(record)
            await session.commit()
            return _serialize_detail(record, [])

    async def list_history(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """按创建时间倒序分页返回控制台历史摘要及总数，读取时补做幂等清洗。"""
        async with self.database.session() as session:
            records = list(
                (
                    await session.scalars(
                        select(ConsoleRequestRecord)
                        .order_by(
                            ConsoleRequestRecord.created_at.desc(),
                            ConsoleRequestRecord.id.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            total = await session.scalar(select(func.count()).select_from(ConsoleRequestRecord))
            counts: dict[str, int] = {}
            if records:
                ids = [item.id for item in records]
                rows = (
                    await session.execute(
                        select(ConsoleSubCaseRecord.parent_id, func.count(ConsoleSubCaseRecord.id))
                        .where(ConsoleSubCaseRecord.parent_id.in_(ids))
                        .group_by(ConsoleSubCaseRecord.parent_id)
                    )
                ).all()
                counts = {parent_id: int(count) for parent_id, count in rows}

            items = []
            for item in records:
                sanitize_console(item)
                items.append(_serialize_summary(item, sub_case_count=counts.get(item.id, 0)))
            return items, int(total or 0)

    async def get_detail(self, record_id: str) -> dict[str, Any] | None:
        """读取单条控制台历史详情及其按轮次/序号升序排列的子用例列表。"""
        async with self.database.session() as session:
            record = await session.get(ConsoleRequestRecord, record_id)
            if record is None:
                return None
            sanitize_console(record)
            sub_cases = await self._fetch_sub_cases(session, record_id)
            for sub in sub_cases:
                sanitize_console_sub_case(sub)
            return _serialize_detail(record, sub_cases)

    async def get_sub_case(self, record_id: str, case_id: str) -> dict[str, Any] | None:
        """按父记录 ID 与子用例 ID 读取单条子用例的完整脱敏只读回放快照。"""
        async with self.database.session() as session:
            sub = await session.scalar(
                select(ConsoleSubCaseRecord).where(
                    ConsoleSubCaseRecord.id == case_id,
                    ConsoleSubCaseRecord.parent_id == record_id,
                )
            )
            if sub is None:
                return None
            sanitize_console_sub_case(sub)
            return _serialize_sub_case(sub)

    async def delete(self, record_id: str) -> bool:
        """按主键删除一条控制台历史记录（级联删除其子用例），返回是否命中删除。"""
        async with self.database.session() as session:
            record = await session.get(ConsoleRequestRecord, record_id)
            if record is None:
                return False
            await session.execute(
                delete(ConsoleRequestRecord).where(ConsoleRequestRecord.id == record_id)
            )
            await session.commit()
            return True

    async def clear(self) -> int:
        """清空全部控制台历史记录（级联删除全部子用例），返回删除行数。"""
        async with self.database.session() as session:
            result = await session.execute(delete(ConsoleRequestRecord))
            await session.commit()
            return int(result.rowcount or 0)

    async def recover(self) -> int:
        """启动恢复：将上一进程遗留的 running/queued 记录标记为 interrupted。"""
        async with self.database.session() as session:
            result = await session.execute(
                update(ConsoleRequestRecord)
                .where(ConsoleRequestRecord.status.in_(["running", "queued"]))
                .values(
                    status="interrupted",
                    error_kind="recovered_after_restart",
                    error_message=outcome_for("interrupted").message,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def _fetch_sub_cases(
        self,
        session,
        parent_id: str,
    ) -> list[ConsoleSubCaseRecord]:
        return list(
            (
                await session.scalars(
                    select(ConsoleSubCaseRecord)
                    .where(ConsoleSubCaseRecord.parent_id == parent_id)
                    .order_by(
                        ConsoleSubCaseRecord.round_index.asc(),
                        ConsoleSubCaseRecord.case_index.asc(),
                        ConsoleSubCaseRecord.created_at.asc(),
                        ConsoleSubCaseRecord.id.asc(),
                    )
                )
            ).all()
        )
