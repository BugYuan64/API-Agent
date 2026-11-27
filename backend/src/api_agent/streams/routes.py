"""Endpoints that drive WebSocket test sessions and multi-round AI suites."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from api_agent.infrastructure.api import (
    ApiError,
    LimitParam,
    OffsetParam,
    Page,
    not_found,
    paginate,
)
from api_agent.infrastructure.history import sanitize_stream
from api_agent.infrastructure.targets import TargetError
from api_agent.streams.models import StreamSessionRecord
from api_agent.streams.repository import StreamRepository
from api_agent.streams.runner import StreamSuiteRunner
from api_agent.streams.schemas import (
    ActiveStreamItem,
    ActiveStreamsResponse,
    StreamEventPage,
    StreamEventResponse,
    StreamHistoryResponse,
    StreamReplayInputs,
    StreamSendRequest,
    StreamSessionResponse,
    StreamStart,
    StreamSubCaseListResponse,
    StreamSubCaseReplayResponse,
    StreamSubCaseSummary,
    StreamSuiteStatusResponse,
)
from api_agent.streams.service import StreamManager
from api_agent.streams.state import serialize_event, serialize_stream

router = APIRouter(prefix="/api/streams", tags=["streams"])


def _manager(request: Request) -> StreamManager:
    """从应用状态取出流会话管理器。

    实现：管理器在 lifespan 里创建（需要数据库），因此只能经 ``app.state`` 取，
    不能在模块级实例化。
    """
    return request.app.state.streams


def _repo(request: Request) -> StreamRepository:
    """从流管理器取出统一的仓储边界，避免路由层编写内联 SQL。"""
    manager = _manager(request)
    return getattr(manager, "repo", None) or manager.repository


def _get_stream_suites(request: Request) -> StreamSuiteRunner:
    """读取或懒缓存应用级多轮 WebSocket AI 套件运行器（Stage 2 接入 lifespan）。"""
    suites = getattr(request.app.state, "stream_suites", None)
    if suites is None:
        suites = StreamSuiteRunner(_manager(request))
        request.app.state.stream_suites = suites
    return suites


@router.post("", response_model=StreamSessionResponse, status_code=202)
async def start_stream(payload: StreamStart, request: Request, response: Response):
    """启动一次 WebSocket 长连接测试（异步受理，返回 202）。

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
    return StreamSessionResponse.model_validate(_live_state(stream, manager))


@router.get("", response_model=Page[StreamHistoryResponse])
async def list_sessions(
    request: Request,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
):
    """分页列出顶层历史长连接会话（自动排除分组 AI 套件的子用例行）。"""
    repo = _repo(request)
    records, total = await repo.list_sessions(limit=limit, offset=offset)
    counts = await repo.count_sub_cases_map([record.id for record in records])
    items = [_history_item(record, counts.get(record.id, 0)) for record in records]
    return paginate(StreamHistoryResponse, items, limit, offset, total)


@router.get("/active", response_model=ActiveStreamsResponse)
async def list_active_streams(request: Request):
    """列出当前正在后台运行的手动长连接与 AI 多轮测试套件，供跨页切回重连。"""
    manager = _manager(request)
    suites = _get_stream_suites(request)
    manual = [ActiveStreamItem.model_validate(item) for item in manager.active_sessions()]
    ai_suites = [ActiveStreamItem.model_validate(item) for item in suites.active_runs()]
    return ActiveStreamsResponse(
        items=[*ai_suites, *manual],
        manual_sessions=manual,
        ai_suites=ai_suites,
    )


@router.get("/suites/{suite_id}/live")
async def live_stream_suite(suite_id: str, request: Request):
    """可重连的多轮 WebSocket AI 测试套件 NDJSON 进度流；断线不取消后台任务。"""
    suites = _get_stream_suites(request)
    repo = _repo(request)
    if not suites.is_active(suite_id) and suite_id not in suites.live:
        record = await repo.get_session(suite_id)
        if record is None:
            raise not_found("Stream suite")
    return StreamingResponse(
        suites.observe(suite_id),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/suites/{suite_id}/cancel", response_model=StreamSuiteStatusResponse)
async def cancel_stream_suite(suite_id: str, request: Request):
    """显式取消一个后台运行中的多轮 WebSocket AI 测试套件。"""
    suites = _get_stream_suites(request)
    snapshot = await suites.cancel(suite_id)
    if snapshot is None:
        raise not_found("Stream suite")
    return StreamSuiteStatusResponse.model_validate(snapshot)


@router.get("/{session_id}/inputs", response_model=StreamReplayInputs)
async def read_inputs(session_id: str, request: Request):
    """Fetch the original handshake and scheduled messages independently of event replay."""
    repo = _repo(request)
    record = await repo.get_session(session_id)
    if record is None:
        raise not_found("Stream session")
    sanitize_stream(record)
    return StreamReplayInputs(
        url=record.url,
        request_headers=record.request_headers,
        subprotocols=(record.settings or {}).get("subprotocols", []),
        scheduled_messages=record.scheduled_messages,
    )


@router.get("/{session_id}/cases", response_model=StreamSubCaseListResponse)
async def list_session_sub_cases(session_id: str, request: Request):
    """列出分组 AI WebSocket 历史记录下的全部子用例摘要。"""
    repo = _repo(request)
    parent = await repo.get_session(session_id)
    if parent is None:
        raise not_found("Stream session")
    rows = await repo.list_sub_cases(session_id)
    items = [_sub_case_summary(row) for row in rows]
    return StreamSubCaseListResponse(session_id=session_id, items=items, total=len(items))


@router.get("/{session_id}/cases/{case_id}", response_model=StreamSubCaseReplayResponse)
async def read_session_sub_case(session_id: str, case_id: str, request: Request):
    """读取单条 WebSocket 子用例的完整只读回放快照（连接参数 + 会话状态 + 事件流）。"""
    repo = _repo(request)
    record = await repo.get_sub_case(session_id, case_id)
    if record is None:
        raise not_found("Stream sub-case")
    sanitize_stream(record)
    events = await repo.list_events(record.id, after=0, limit=2000)
    label = record.case_label or f"第 {record.round_index} 轮 · 用例 {record.case_index + 1}"
    return StreamSubCaseReplayResponse(
        session_id=session_id,
        case_id=record.id,
        round_index=record.round_index,
        case_index=record.case_index,
        case_label=record.case_label,
        label=label,
        readonly=True,
        inputs=StreamReplayInputs(
            url=record.url,
            request_headers=record.request_headers,
            subprotocols=(record.settings or {}).get("subprotocols", []),
            scheduled_messages=record.scheduled_messages,
        ),
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
            for row in events
        ],
    )


@router.get("/{session_id}", response_model=StreamEventPage)
async def read_session(
    session_id: str,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    wait_ms: Annotated[int, Query(ge=0, le=10_000)] = 0,
):
    """增量读取会话状态与事件（支持长轮询）。

    作用：界面用这一个接口就同时拿到“状态变了没”和“有没有新事件”，
    无需再维护第二条通道。
    实现：会话仍在内存时走管理器（可长轮询 ``wait_ms``）；
    已不在内存（进程重启、历史淘汰或分组 AI 套件）则经 ``StreamRepository`` 读取。
    """
    manager = _manager(request)
    stream, events, finished = await manager.read(session_id, after, wait_ms)
    if stream is not None:
        return StreamEventPage(
            session=StreamSessionResponse.model_validate(_live_state(stream, manager)),
            events=[
                StreamEventResponse.model_validate(serialize_event(e, session_id)) for e in events
            ],
            next_cursor=events[-1].sequence if events else after,
            finished=finished,
        )
    repo = _repo(request)
    record = await repo.get_session(session_id)
    if record is None:
        raise not_found("Stream session")
    rows = await repo.list_events(session_id, after=after, limit=501)
    has_more = len(rows) > 500
    rows = rows[:500]
    sub_case_count = await repo.count_sub_cases(session_id)
    suites = _get_stream_suites(request)
    suite_active = suites.is_active(session_id)
    return StreamEventPage(
        session=StreamSessionResponse.model_validate(
            _stored_state(record, sub_case_count=sub_case_count, suite_active=suite_active)
        ),
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
        finished=not suite_active,
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
    return StreamSessionResponse.model_validate(_live_state(stream, manager))


@router.post("/{session_id}/stop", response_model=StreamSessionResponse)
async def stop_session(session_id: str, request: Request):
    """请求结束一个会话。

    实现：``stop`` 只设置停止意图并取消任务；真正的状态落库在任务的
    ``finally`` 里完成，因此这里返回的可能是 ``cancelled`` 之前的瞬时状态。
    """
    manager = _manager(request)
    stream = await manager.stop(session_id)
    if stream is None:
        suites = _get_stream_suites(request)
        if suites.is_active(session_id):
            await suites.cancel(session_id)
        repo = _repo(request)
        record = await repo.get_session(session_id)
        if record is None:
            raise not_found("Stream session")
        sub_case_count = await repo.count_sub_cases(session_id)
        return StreamSessionResponse.model_validate(
            _stored_state(record, sub_case_count=sub_case_count)
        )
    return StreamSessionResponse.model_validate(_live_state(stream, manager))


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request):
    """删除一条长连接会话及其子用例、事件时间线和绑定的 AI 助手线程。

    实现：若目标是运行中的多轮 AI 套件则拒绝（409 ``stream_active``），避免孤儿后台写库；
    单条手动长连接保持“先 ``manager.stop`` 收尾再删除”的既有兼容行为；
    删除后若 ``assistant_threads`` 已就绪则一并清理 ``("websocket", session_id)`` 线程。
    """
    suites = _get_stream_suites(request)
    if suites.is_active(session_id):
        raise ApiError(409, "stream_active", "WebSocket AI suite is still active; cancel it first")

    manager = _manager(request)
    repo = _repo(request)
    stream = await manager.stop(session_id)
    deleted = await repo.delete(session_id)
    if not deleted and stream is None:
        raise not_found("Stream session")
    manager.forget(session_id)

    threads = getattr(request.app.state, "assistant_threads", None)
    if threads is not None:
        await threads.delete_thread("websocket", session_id)


def _live_state(stream, manager: StreamManager, *, sub_case_count: int = 0) -> dict:
    """把内存中的活跃会话与附加元数据合成为与持久化一致的序列化形状。"""
    meta = manager.session_meta(stream.id)
    return {
        **serialize_stream(stream),
        "source": meta.get("source", "manual"),
        "title": meta.get("title"),
        "badge": meta.get("badge"),
        "parent_session_id": meta.get("parent_session_id"),
        "round_index": meta.get("round_index", 1),
        "case_index": meta.get("case_index", 0),
        "case_label": meta.get("case_label"),
        "cooldown_ms": meta.get("cooldown_ms", 500),
        "total_rounds": meta.get("total_rounds", 1),
        "summary": None,
        "sub_case_count": sub_case_count,
    }


def _stored_state(
    record: StreamSessionRecord,
    *,
    sub_case_count: int = 0,
    suite_active: bool = False,
) -> dict:
    """把数据库里的会话行转成与内存会话一致的序列化形状。

    实现：数据库行没有 ``finished`` 派生字段，这里按状态补上；
    未在活跃套件中且非终态的记录（进程崩溃遗留行）视为已结束。
    """
    terminal = record.status in {"closed", "completed", "error", "cancelled", "interrupted"}
    if suite_active:
        status = record.status
        finished = False
        stop_reason = record.stop_reason
    elif terminal:
        status = record.status
        finished = True
        stop_reason = record.stop_reason
    else:
        status = "interrupted" if record.status in {"running", "queued"} else "closed"
        finished = True
        stop_reason = record.stop_reason or "recovered_after_restart"
    return {
        "id": record.id,
        "kind": record.kind,
        "url": record.url,
        "status": status,
        "finished": finished,
        "stop_reason": stop_reason,
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
        "source": record.source,
        "title": record.title,
        "badge": record.badge,
        "parent_session_id": record.parent_session_id,
        "round_index": record.round_index,
        "case_index": record.case_index,
        "case_label": record.case_label,
        "cooldown_ms": record.cooldown_ms,
        "total_rounds": record.total_rounds,
        "summary": record.summary,
        "sub_case_count": sub_case_count,
    }


def _history_item(record: StreamSessionRecord, sub_case_count: int) -> StreamHistoryResponse:
    """将数据库会话行投影为包含分组标签与子用例计数的历史响应模型。"""
    return StreamHistoryResponse(
        id=record.id,
        kind=record.kind,
        url=record.url,
        request_headers=record.request_headers,
        status=record.status,
        stop_reason=record.stop_reason,
        error_kind=record.error_kind,
        error_message=record.error_message,
        http_status=record.http_status,
        content_type=record.content_type,
        subprotocol=record.subprotocol,
        close_code=record.close_code,
        close_reason=record.close_reason,
        event_count=record.event_count,
        byte_count=record.byte_count,
        sent_count=record.sent_count,
        truncated_count=record.truncated_count,
        settings=record.settings,
        opened_at=record.opened_at,
        closed_at=record.closed_at,
        created_at=record.created_at,
        source=record.source,
        title=record.title,
        badge=record.badge,
        parent_session_id=record.parent_session_id,
        round_index=record.round_index,
        case_index=record.case_index,
        case_label=record.case_label,
        cooldown_ms=record.cooldown_ms,
        total_rounds=record.total_rounds,
        summary=record.summary,
        sub_case_count=sub_case_count,
    )


def _sub_case_summary(record: StreamSessionRecord) -> StreamSubCaseSummary:
    """将子会话 ORM 行投影为前端列表可用的子用例摘要（优先采用已持久化的断言裁定状态）。"""
    label = record.case_label or f"第 {record.round_index} 轮 · 用例 {record.case_index + 1}"
    stored_status = (record.settings or {}).get("case_status")
    effective_status = (
        stored_status
        if isinstance(stored_status, str)
        and stored_status in {"passed", "failed", "error", "cancelled", "interrupted"}
        else record.status
    )
    return StreamSubCaseSummary(
        id=record.id,
        parent_session_id=record.parent_session_id or "",
        round_index=record.round_index,
        case_index=record.case_index,
        case_label=record.case_label,
        label=label,
        url=record.url,
        status=effective_status,
        stop_reason=record.stop_reason,
        error_kind=record.error_kind,
        error_message=record.error_message,
        http_status=record.http_status,
        subprotocol=record.subprotocol,
        close_code=record.close_code,
        close_reason=record.close_reason,
        event_count=record.event_count,
        byte_count=record.byte_count,
        sent_count=record.sent_count,
        truncated_count=record.truncated_count,
        opened_at=record.opened_at,
        closed_at=record.closed_at,
        created_at=record.created_at,
    )
