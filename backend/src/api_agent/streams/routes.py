"""Endpoints that drive SSE and WebSocket test sessions."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import ApiError, Page, PageInfo, not_found
from api_agent.infrastructure.database import session_for
from api_agent.infrastructure.targets import TargetError
from api_agent.streams.models import StreamEventRecord, StreamSessionRecord
from api_agent.streams.schemas import (
    StreamEventPage,
    StreamEventResponse,
    StreamHistoryResponse,
    StreamSendRequest,
    StreamSessionResponse,
)
from api_agent.streams.service import StreamStart, serialize_event, serialize_stream

router = APIRouter(prefix="/api/streams", tags=["streams"])
Session = Annotated[AsyncSession, Depends(session_for)]


def _manager(request: Request):
    """从应用状态取出流会话管理器。

    实现：管理器在 lifespan 里创建（需要数据库），因此只能经 ``app.state`` 取，
    不能在模块级实例化。
    """
    return request.app.state.streams


@router.post("", response_model=StreamSessionResponse, status_code=202)
async def start_stream(payload: StreamStart, request: Request, response: Response):
    """启动一次 SSE 或 WebSocket 长连接测试（异步受理，返回 202）。

    作用：连接可能持续很久，接口不能同步等待，因此立刻返回会话 ID，
    真正的握手与读取交给后台任务。
    实现：``manager.start`` 完成范围裁定与预写会话行；
    策略拒绝（非回环、不允许的 origin、并发超限）统一转 400 带固定错误码。
    """
    manager = _manager(request)
    try:
        stream = await manager.start(payload, request.app.state.settings.target_allowed_origins)
    except TargetError as error:
        raise ApiError(400, str(error), "Target rejected by local target policy") from None
    except ValueError as error:
        raise ApiError(400, str(error), "Stream session rejected") from None
    response.headers["Location"] = f"/api/streams/{stream.id}"
    return StreamSessionResponse.model_validate(serialize_stream(stream))


@router.get("", response_model=Page[StreamHistoryResponse])
async def list_sessions(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """分页列出历史长连接会话（进程重启后依然可用）。

    实现：直接查 ``stream_sessions`` 并按创建时间倒序；
    标题栏显示的状态来自最后一次落库的快照。
    """
    from sqlalchemy import func

    items = (
        await session.scalars(
            select(StreamSessionRecord)
            .order_by(StreamSessionRecord.created_at.desc(), StreamSessionRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(StreamSessionRecord)) or 0
    return Page[StreamHistoryResponse](
        items=[StreamHistoryResponse.model_validate(item) for item in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.get("/{session_id}", response_model=StreamEventPage)
async def read_session(
    session_id: str,
    session: Session,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    wait_ms: Annotated[int, Query(ge=0, le=10_000)] = 0,
):
    """增量读取会话状态与事件（支持长轮询）。

    作用：界面用这一个接口就同时拿到“状态变了没”和“有没有新事件”，
    无需再维护第二条通道。
    实现：会话仍在内存时走管理器（可长轮询 ``wait_ms``）；
    已不在内存（进程重启或历史淘汰）则回落到数据库，按 ``sequence > after`` 取，
    并明确标记 ``finished=True``——数据库里的会话一定已经结束。
    """
    manager = _manager(request)
    stream, events, finished = await manager.read(session_id, after, wait_ms)
    if stream is not None:
        return StreamEventPage(
            session=StreamSessionResponse.model_validate(serialize_stream(stream)),
            events=[
                StreamEventResponse.model_validate(serialize_event(e, session_id)) for e in events
            ],
            next_cursor=events[-1].sequence if events else after,
            finished=finished,
        )
    record = await session.get(StreamSessionRecord, session_id)
    if record is None:
        raise not_found("Stream session")
    rows = (
        await session.scalars(
            select(StreamEventRecord)
            .where(
                StreamEventRecord.session_id == session_id,
                StreamEventRecord.sequence > after,
            )
            .order_by(StreamEventRecord.sequence)
            .limit(501)
        )
    ).all()
    has_more = len(rows) > 500
    rows = rows[:500]
    return StreamEventPage(
        session=StreamSessionResponse.model_validate(_stored_state(record)),
        events=[
            StreamEventResponse(
                session_id=row.session_id,
                sequence=row.sequence,
                direction=row.direction,
                kind=row.kind,
                event_name=row.event_name,
                data=row.data,
                data_bytes=row.data_bytes,
                truncated=row.truncated,
                elapsed_ms=row.elapsed_ms,
            )
            for row in rows
        ],
        next_cursor=rows[-1].sequence if rows else after,
        finished=True,
        has_more=has_more,
    )


@router.post("/{session_id}/send", response_model=StreamSessionResponse)
async def send_message(session_id: str, payload: StreamSendRequest, request: Request):
    """向一个运行中的 WebSocket 会话发送文本消息。

    实现：只有 ``kind=websocket`` 且状态为 ``open`` 时允许；
    发送成功后本地也记一条 ``direction=out`` 事件，时间线因此能对齐“发→收”。
    """
    manager = _manager(request)
    try:
        await manager.send_text(session_id, payload.message)
    except ValueError as error:
        code = str(error)
        status = 404 if code == "stream_not_found" else 409
        raise ApiError(status, code, "Message could not be sent on this session") from None
    stream = manager.find(session_id)
    return StreamSessionResponse.model_validate(serialize_stream(stream))


@router.post("/{session_id}/stop", response_model=StreamSessionResponse)
async def stop_session(session_id: str, request: Request):
    """请求结束一个会话。

    实现：``stop`` 只设置停止意图并取消任务；真正的状态落库在任务的
    ``finally`` 里完成，因此这里返回的可能是 ``cancelled`` 之前的瞬时状态。
    """
    manager = _manager(request)
    stream = await manager.stop(session_id)
    if stream is None:
        raise not_found("Stream session")
    return StreamSessionResponse.model_validate(serialize_stream(stream))


def _stored_state(record: StreamSessionRecord) -> dict:
    """把数据库里的会话行转成与内存会话一致的序列化形状。

    实现：数据库行没有 ``finished`` 派生字段，这里按状态补上；
    终态以外的记录（进程崩溃留下的 open 行）视为已结束，
    因为它们不可能再有新的写入者。
    """
    terminal = record.status in {"closed", "error", "cancelled"}
    return {
        "id": record.id,
        "kind": record.kind,
        "url": record.url,
        "status": record.status if terminal else "closed",
        "finished": True,
        "stop_reason": record.stop_reason or (None if terminal else "recovered_after_restart"),
        "error_kind": record.error_kind,
        "error_message": record.error_message,
        "http_status": record.http_status,
        "content_type": record.content_type,
        "subprotocol": record.subprotocol,
        "close_code": record.close_code,
        "close_reason": record.close_reason,
        "event_count": record.event_count,
        "byte_count": record.byte_count,
        "sent_count": record.sent_count,
        "truncated_count": record.truncated_count,
        "opened_at": record.opened_at,
        "closed_at": record.closed_at,
    }
