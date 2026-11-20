"""Endpoints for console HTTP requests and their saved history."""

import asyncio
import json
import logging
from contextlib import aclosing
from typing import Annotated

from anyio import CancelScope
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.console.models import ConsoleRequestRecord
from api_agent.console.schemas import ConsoleHistoryDetail, ConsoleHistoryResponse
from api_agent.console.service import ConsoleRequest, ConsoleResult, send
from api_agent.console.sse import SseEvent
from api_agent.infrastructure.api import (
    ApiError,
    LimitParam,
    OffsetParam,
    Page,
    error_response,
    not_found,
    paginate,
)
from api_agent.infrastructure.database import session_for
from api_agent.infrastructure.history import PREVIEW_LIMIT, sanitize_console
from api_agent.infrastructure.targets import TargetError

router = APIRouter(prefix="/api/console", tags=["console"])
Session = Annotated[AsyncSession, Depends(session_for)]


def ndjson_line(kind: str, **payload) -> bytes:
    """Frame one management-stream message independently of TCP chunk boundaries."""
    return (json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n").encode()


class ConsoleStreamingResponse(StreamingResponse):
    """Close the producer when sending a frame fails or the browser disconnects."""

    async def stream_response(self, send) -> None:
        async with aclosing(self.body_iterator):
            await super().stream_response(send)


@router.post(
    "/requests/live",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def stream_request(payload: ConsoleRequest, request: Request):
    """Send one target request and forward SSE events before committing its final history."""

    async def frames():
        messages: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)

        async def publish(event: SseEvent | None) -> None:
            line = (
                ndjson_line("sse_start") if event is None else ndjson_line("sse_event", event=event)
            )
            await messages.put(line)

        async def execute() -> bytes:
            try:
                result = await send(request.app.state.settings, payload, on_sse=publish)
                async with request.app.state.database.session() as session:
                    record = await save_history(session, payload, result)
                result.history_id = record.id
                return ndjson_line("result", result=result.model_dump(mode="json"))
            except TargetError as error:
                status, code, message = 400, str(error), "Target rejected by local target policy"
            except Exception:
                logging.getLogger("api_agent").error("Live console request could not finish")
                status, code, message = 500, "internal_error", "Internal server error"
            envelope = error_response(request, status, code, message)
            return ndjson_line("error", status=status, **json.loads(envelope.body))

        worker = asyncio.create_task(execute())
        next_message = asyncio.create_task(messages.get())
        try:
            while True:
                await asyncio.wait((worker, next_message), return_when=asyncio.FIRST_COMPLETED)
                if next_message.done():
                    yield next_message.result()
                    next_message = asyncio.create_task(messages.get())
                elif worker.done():
                    if not messages.empty():
                        yield await next_message
                    break
            yield worker.result()
        finally:
            next_message.cancel()
            worker.cancel()
            with CancelScope(shield=True):
                await asyncio.gather(next_message, worker, return_exceptions=True)

    return ConsoleStreamingResponse(
        frames(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/requests",
    response_model=ConsoleResult,
    responses={
        413: {"model": ConsoleResult, "description": "Target response exceeded the read limit"},
        502: {"model": ConsoleResult, "description": "Target transport or decoding failed"},
        504: {"model": ConsoleResult, "description": "Target request deadline exceeded"},
    },
)
async def send_request(
    payload: ConsoleRequest, session: Session, request: Request, response: Response
):
    """发出一条受控 HTTP 请求，并把它记入历史。

    作用：这是界面上「发送」按钮的落点，也是唯一由界面触发真实请求的入口。
    实现：``send`` 内部完成回环范围裁定与超时控制；
    无论成功还是传输失败都落一条历史（失败时也记录，否则“为什么没回包”无从追溯）。
    落库前把头脱敏、把正文裁成预览，避免历史库成为凭据的第二份副本。
    """
    try:
        result = await send(request.app.state.settings, payload)
    except TargetError as error:
        # 范围裁定失败是“请求被本机策略拒绝”，不是服务端故障，因此返回 400 并带稳定错误码。
        raise ApiError(400, str(error), "Target rejected by local target policy") from None
    record = await save_history(session, payload, result)
    result.history_id = record.id
    # 仅本机传输失败改变外层 HTTP；目标 4xx/5xx 仍是收到真实响应的正常结果。
    if not result.ok:
        response.status_code = result.result_status or 500
    return result


async def save_history(session: AsyncSession, payload: ConsoleRequest, result: ConsoleResult):
    """Commit one scrubbed history record after the shared HTTP engine finishes."""
    record = ConsoleRequestRecord(
        method=payload.method,
        url=result.request["url"],
        request_headers=payload.headers,
        body_mode=payload.body_mode,
        body_preview=(payload.body or "")[:PREVIEW_LIMIT] or None,
        ok=result.ok,
        status_code=result.response.status if result.response else None,
        elapsed_ms=result.elapsed_ms,
        error_kind=result.error_kind,
        error_message=result.error_message,
        response_headers=result.response.headers if result.response else {},
        response_preview=(result.response.body or "")[:PREVIEW_LIMIT] if result.response else None,
        response_bytes=result.response.body_bytes if result.response else 0,
    )
    # 统一清洗入口：先用**完整**正文学习密码/JWT（截断可能切掉命名敏感字段），
    # 再抹掉同记录内的兄弟回显并截成预览。
    # 即时响应对用户仍然原样返回（上面 result 未被修改），只有持久化副本被清洗。
    sanitize_console(
        record,
        full_body=payload.body,
        full_response=result.response.body if result.response else None,
    )
    session.add(record)
    await session.commit()
    return record


@router.get("/history", response_model=Page[ConsoleHistoryResponse])
async def list_history(
    session: Session,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
):
    """分页列出历史请求，最新的在前。

    实现：按 ``(created_at, id)`` 倒序并计数，
    这样界面打开就能看到刚发的那条，翻页顺序也稳定。
    读取时再清洗一次：旧版本写入的行、或迁移尚未跑到的行，
    也不会把凭据原样吐出到接口响应里。
    """
    from sqlalchemy import func

    items = (
        await session.scalars(
            select(ConsoleRequestRecord)
            .order_by(ConsoleRequestRecord.created_at.desc(), ConsoleRequestRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    for item in items:
        sanitize_console(item)
    total = await session.scalar(select(func.count()).select_from(ConsoleRequestRecord)) or 0
    return paginate(ConsoleHistoryResponse, items, limit, offset, total)


@router.get("/history/{record_id}", response_model=ConsoleHistoryDetail)
async def read_history(record_id: str, session: Session):
    """Read one scrubbed replay input without expanding the history list payload."""
    record = await session.get(ConsoleRequestRecord, record_id)
    if record is None:
        raise not_found("Console request")
    sanitize_console(record)
    return record


@router.delete("/history/{record_id}", status_code=204)
async def delete_history(record_id: str, session: Session):
    """删除一条历史记录。

    实现：先确认存在（否则 404，避免“删掉了”和“本来就没有”被混为一谈），
    再按主键删除并提交；返回 204 无正文。
    """
    record = await session.get(ConsoleRequestRecord, record_id)
    if record is None:
        raise not_found("Console request")
    await session.execute(delete(ConsoleRequestRecord).where(ConsoleRequestRecord.id == record_id))
    await session.commit()


@router.delete("/history", status_code=204)
async def clear_history(session: Session):
    """清空全部历史请求。

    实现：一次性删除全表；这是用户显式动作，因此不做二次确认（由界面负责）。
    """
    await session.execute(delete(ConsoleRequestRecord))
    await session.commit()
