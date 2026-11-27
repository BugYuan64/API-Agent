"""Transactional stream evidence writes and repository queries."""

import logging
from datetime import datetime

from sqlalchemy import delete, func, select, update

from api_agent.infrastructure.database import Database
from api_agent.infrastructure.history import sanitize_stream
from api_agent.infrastructure.orm import utc_now
from api_agent.streams.models import StreamEventRecord, StreamSessionRecord
from api_agent.streams.schemas import StreamStart
from api_agent.streams.state import ActiveStream

logger = logging.getLogger("api_agent")

TERMINAL_SUITE_STATUSES = frozenset({"closed", "completed", "error", "cancelled", "interrupted"})


class StreamRepository:
    """长连接证据的事务存储边界，依赖独立数据库会话，不持有 socket 或裁定终态。"""

    def __init__(self, database: Database):
        """保存所属 Database，每次创建、恢复或写入使用短事务。"""
        self.database = database

    async def create(
        self,
        stream: ActiveStream,
        payload: StreamStart,
        origin: str,
        *,
        source: str = "manual",
        title: str | None = None,
        badge: str | None = None,
        parent_session_id: str | None = None,
        round_index: int = 1,
        case_index: int = 0,
        case_label: str | None = None,
        cooldown_ms: int = 500,
        total_rounds: int = 1,
        summary: str | None = None,
    ) -> None:
        """连接前提交初始会话和脱敏头；失败抛出，由管理器释放预留名额。"""
        async with self.database.session() as session:
            record = StreamSessionRecord(
                id=stream.id,
                project_id=payload.project_id,
                kind=payload.kind,
                url=stream.url,
                request_headers=dict(payload.headers),
                scheduled_messages=list(payload.send) if payload.send is not None else None,
                status=stream.status,
                settings={
                    "origin": origin,
                    "max_events": payload.max_events,
                    "max_duration_ms": payload.max_duration_ms,
                    "connect_timeout_ms": payload.connect_timeout_ms,
                    "ping_interval_ms": payload.ping_interval_ms,
                    "subprotocols": list(payload.subprotocols),
                    "scheduled_sends": len(payload.send),
                },
                source=source,
                title=title,
                badge=badge,
                parent_session_id=parent_session_id,
                round_index=round_index,
                case_index=case_index,
                case_label=case_label,
                cooldown_ms=cooldown_ms,
                total_rounds=total_rounds,
                summary=summary,
            )
            sanitize_stream(record)
            session.add(record)
            await session.commit()

    async def create_parent_suite(
        self,
        *,
        session_id: str,
        url: str,
        origin: str,
        headers: dict[str, str] | None = None,
        subprotocols: list[str] | None = None,
        project_id: str | None = None,
        source: str = "ai",
        title: str | None = None,
        badge: str | None = "AI 测试",
        cooldown_ms: int = 500,
        total_rounds: int = 1,
        total_cases: int = 1,
        cases_per_round: int = 1,
        summary: str | None = None,
    ) -> StreamSessionRecord:
        """创建一条顶层分组 AI WebSocket 测试套件记录。"""
        async with self.database.session() as session:
            record = StreamSessionRecord(
                id=session_id,
                project_id=project_id,
                kind="websocket",
                url=url,
                request_headers=dict(headers or {}),
                scheduled_messages=None,
                status="running",
                opened_at=utc_now(),
                settings={
                    "origin": origin,
                    "subprotocols": list(subprotocols or []),
                    "total_cases": total_cases,
                    "rounds": total_rounds,
                    "cases_per_round": cases_per_round,
                    "suite": True,
                },
                source=source,
                title=title,
                badge=badge,
                parent_session_id=None,
                round_index=1,
                case_index=0,
                case_label=None,
                cooldown_ms=cooldown_ms,
                total_rounds=total_rounds,
                summary=summary,
            )
            sanitize_stream(record)
            session.add(record)
            await session.commit()
            return record

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        include_children: bool = False,
    ) -> tuple[list[StreamSessionRecord], int]:
        """分页列出长连接历史会话；默认隐藏子用例行以免污染顶层历史。"""
        async with self.database.session() as session:
            items_query = select(StreamSessionRecord)
            count_query = select(func.count()).select_from(StreamSessionRecord)
            if not include_children:
                items_query = items_query.where(StreamSessionRecord.parent_session_id.is_(None))
                count_query = count_query.where(StreamSessionRecord.parent_session_id.is_(None))
            items = list(
                (
                    await session.scalars(
                        items_query.order_by(
                            StreamSessionRecord.created_at.desc(),
                            StreamSessionRecord.id.desc(),
                        )
                        .limit(limit)
                        .offset(offset)
                    )
                ).all()
            )
            total = int(await session.scalar(count_query) or 0)
            return items, total

    async def count_sub_cases_map(self, parent_ids: list[str]) -> dict[str, int]:
        """批量统计若干顶层会话各自拥有的子用例数量，避免列表接口产生 N+1 查询。"""
        if not parent_ids:
            return {}
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    select(
                        StreamSessionRecord.parent_session_id,
                        func.count(StreamSessionRecord.id),
                    )
                    .where(StreamSessionRecord.parent_session_id.in_(parent_ids))
                    .group_by(StreamSessionRecord.parent_session_id)
                )
            ).all()
            return {str(parent_id): int(count) for parent_id, count in rows if parent_id}

    async def count_sub_cases(self, parent_session_id: str) -> int:
        """统计指定顶层会话的子用例总数。"""
        async with self.database.session() as session:
            total = await session.scalar(
                select(func.count())
                .select_from(StreamSessionRecord)
                .where(StreamSessionRecord.parent_session_id == parent_session_id)
            )
            return int(total or 0)

    async def get_session(self, session_id: str) -> StreamSessionRecord | None:
        """按主键读取单条会话记录（顶层或子用例）。"""
        async with self.database.session() as session:
            return await session.get(StreamSessionRecord, session_id)

    async def get_sub_case(
        self, parent_session_id: str, case_id: str
    ) -> StreamSessionRecord | None:
        """按父会话 ID 与子用例 ID 精确读取归属该分组的子用例记录。"""
        async with self.database.session() as session:
            return await session.scalar(
                select(StreamSessionRecord).where(
                    StreamSessionRecord.id == case_id,
                    StreamSessionRecord.parent_session_id == parent_session_id,
                )
            )

    async def list_sub_cases(self, parent_session_id: str) -> list[StreamSessionRecord]:
        """按轮次、用例序号与创建时间升序列出分组套件的全部子用例。"""
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(StreamSessionRecord)
                        .where(StreamSessionRecord.parent_session_id == parent_session_id)
                        .order_by(
                            StreamSessionRecord.round_index.asc(),
                            StreamSessionRecord.case_index.asc(),
                            StreamSessionRecord.created_at.asc(),
                            StreamSessionRecord.id.asc(),
                        )
                    )
                ).all()
            )

    async def list_events(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> list[StreamEventRecord]:
        """按游标 ``sequence > after`` 升序读取会话的事件时间线。"""
        async with self.database.session() as session:
            return list(
                (
                    await session.scalars(
                        select(StreamEventRecord)
                        .where(
                            StreamEventRecord.session_id == session_id,
                            StreamEventRecord.sequence > after,
                        )
                        .order_by(StreamEventRecord.sequence.asc())
                        .limit(limit)
                    )
                ).all()
            )

    async def update_parent_suite(
        self,
        session_id: str,
        *,
        status: str,
        summary: str | None = None,
        stop_reason: str | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
        event_count: int | None = None,
        byte_count: int | None = None,
        sent_count: int | None = None,
        total_rounds: int | None = None,
        closed_at: datetime | None = None,
    ) -> StreamSessionRecord | None:
        """更新分组 AI 套件的汇总状态、统计计数与脱敏后的总结正文。"""
        async with self.database.session() as session:
            record = await session.get(StreamSessionRecord, session_id)
            if record is None:
                return None
            record.status = status
            if summary is not None:
                record.summary = summary
            if stop_reason is not None:
                record.stop_reason = stop_reason
            if error_kind is not None:
                record.error_kind = error_kind
            if error_message is not None:
                record.error_message = error_message[:300]
            if event_count is not None:
                record.event_count = event_count
            if byte_count is not None:
                record.byte_count = byte_count
            if sent_count is not None:
                record.sent_count = sent_count
            if total_rounds is not None:
                record.total_rounds = total_rounds
            if closed_at is not None:
                record.closed_at = closed_at
            elif status in TERMINAL_SUITE_STATUSES and record.closed_at is None:
                record.closed_at = utc_now()
            sanitize_stream(record)
            await session.commit()
            return record

    async def update_sub_case_verdict(
        self,
        case_id: str,
        *,
        case_status: str,
        notes: list[str] | None = None,
    ) -> None:
        """持久化子用例的断言裁定结果与说明，防止缓存淘汰或服务重启后丢失失败状态。"""
        async with self.database.session() as session:
            record = await session.get(StreamSessionRecord, case_id)
            if record is None:
                return
            next_settings = dict(record.settings or {})
            next_settings["case_status"] = case_status
            if notes:
                next_settings["case_notes"] = list(notes)
            else:
                next_settings.pop("case_notes", None)
            record.settings = next_settings
            if notes:
                record.summary = "；".join(notes)[:500]
            sanitize_stream(record)
            await session.commit()

    async def delete(self, session_id: str) -> bool:
        """删除一条会话记录并级联清理其子用例与事件时间线；未命中返回 False。"""
        async with self.database.session() as session:
            existing = await session.get(StreamSessionRecord, session_id)
            if existing is None:
                return False
            # 除 SQLite ON DELETE CASCADE 外显式删除子行，确保即使测试引擎未开外键也无残留。
            await session.execute(
                delete(StreamSessionRecord).where(
                    StreamSessionRecord.parent_session_id == session_id
                )
            )
            result = await session.execute(
                delete(StreamSessionRecord).where(StreamSessionRecord.id == session_id)
            )
            await session.commit()
            return bool(result.rowcount)

    async def recover(self) -> int:
        """关闭上一进程遗留的 connecting/open 会话与 running/queued 套件行并返回总行数。"""
        now = utc_now()
        async with self.database.session() as session:
            streams_result = await session.execute(
                update(StreamSessionRecord)
                .where(StreamSessionRecord.status.in_(["connecting", "open"]))
                .values(status="closed", stop_reason="recovered_after_restart", closed_at=now)
            )
            suites_result = await session.execute(
                update(StreamSessionRecord)
                .where(StreamSessionRecord.status.in_(["running", "queued"]))
                .values(
                    status="interrupted",
                    stop_reason="recovered_after_restart",
                    closed_at=now,
                )
            )
            await session.commit()
            return int(streams_result.rowcount or 0) + int(suites_result.rowcount or 0)

    async def flush(self, stream: ActiveStream, *, final: bool = False) -> None:
        # Serialize commits, not event capture. A send/receive can append while
        # SQLite awaits IO; only the committed prefix may be removed afterward.
        """在锁内提交事件与状态快照，成功才删除已提交前缀；失败保留有界待写队列并告警，最终持续失败不保证断电恢复。"""
        async with stream.flush_lock:
            if not stream.pending and not final:
                return
            pending = list(stream.pending)
            values = {
                name: getattr(stream, name)
                for name in (
                    "status",
                    "stop_reason",
                    "error_kind",
                    "error_message",
                    "http_status",
                    "content_type",
                    "subprotocol",
                    "close_code",
                    "close_reason",
                    "event_count",
                    "byte_count",
                    "sent_count",
                    "truncated_count",
                    "opened_at",
                    "closed_at",
                )
            }
            try:
                async with self.database.session() as session:
                    session.add_all(
                        [
                            StreamEventRecord(
                                session_id=stream.id,
                                sequence=event.sequence,
                                direction=event.direction,
                                kind=event.kind,
                                event_name=event.event_name,
                                data=event.data,
                                data_bytes=event.data_bytes,
                                truncated=event.truncated,
                                elapsed_ms=event.elapsed_ms,
                            )
                            for event in pending
                        ]
                    )
                    await session.execute(
                        update(StreamSessionRecord)
                        .where(StreamSessionRecord.id == stream.id)
                        .values(**values)
                    )
                    await session.commit()
            except Exception:  # A transient failure leaves the bounded queue for retry.
                logger.warning("Stream evidence could not be persisted", exc_info=True)
                return
            del stream.pending[: len(pending)]
