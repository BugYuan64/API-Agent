"""Endpoints for console HTTP requests and their saved history."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.console.models import ConsoleRequestRecord
from api_agent.console.schemas import ConsoleHistoryResponse
from api_agent.console.service import ConsoleRequest, ConsoleResult, send
from api_agent.infrastructure.api import ApiError, Page, PageInfo, not_found
from api_agent.infrastructure.database import session_for
from api_agent.infrastructure.targets import TargetError, redact_headers

router = APIRouter(prefix="/api/console", tags=["console"])
Session = Annotated[AsyncSession, Depends(session_for)]

PREVIEW_LIMIT = 8 * 1024


@router.post("/requests", response_model=ConsoleResult)
async def send_request(payload: ConsoleRequest, session: Session, request: Request):
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
    record = ConsoleRequestRecord(
        method=payload.method,
        url=result.request["url"],
        request_headers=redact_headers(payload.headers),
        body_mode=payload.body_mode,
        body_preview=(payload.body or "")[:PREVIEW_LIMIT] or None,
        ok=result.ok,
        status_code=result.response.status if result.response else None,
        elapsed_ms=result.elapsed_ms,
        error_kind=result.error_kind,
        error_message=result.error_message,
        response_headers=redact_headers(result.response.headers) if result.response else {},
        response_preview=(result.response.body or "")[:PREVIEW_LIMIT] if result.response else None,
        response_bytes=result.response.body_bytes if result.response else 0,
    )
    session.add(record)
    await session.commit()
    return result


@router.get("/history", response_model=Page[ConsoleHistoryResponse])
async def list_history(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """分页列出历史请求，最新的在前。

    实现：按 ``(created_at, id)`` 倒序并计数，
    这样界面打开就能看到刚发的那条，翻页顺序也稳定。
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
    total = await session.scalar(select(func.count()).select_from(ConsoleRequestRecord)) or 0
    return Page[ConsoleHistoryResponse](
        items=[ConsoleHistoryResponse.model_validate(item) for item in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


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
