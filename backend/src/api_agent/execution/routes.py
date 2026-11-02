"""Endpoints that run plans through the existing Postman execution engine."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.execution.api_schemas import ExecutionHistoryResponse, ExecutionRunRequest
from api_agent.execution.models import ExecutionRecord
from api_agent.execution.service import ExecutionService, PlanError
from api_agent.infrastructure.api import ApiError, Page, PageInfo, not_found
from api_agent.infrastructure.database import session_for
from api_agent.projects.models import Project

router = APIRouter(prefix="/api/executions", tags=["executions"])
Session = Annotated[AsyncSession, Depends(session_for)]


def _service(request: Request) -> ExecutionService:
    """按应用配置构造执行服务。

    实现：``ExecutionService`` 需要 settings 来定位 CLI 与允许范围，
    因此在请求期从 ``app.state.settings`` 取值构造，而不是在启动时单例化——
    后者会让测试里替换配置变得困难。
    """
    return ExecutionService(request.app.state.settings)


@router.post("", response_model=ExecutionHistoryResponse, status_code=201)
async def run_plan(
    payload: ExecutionRunRequest, session: Session, request: Request, response: Response
):
    """执行一份受控计划并把结果落库。

    作用：这是执行引擎的界面入口，让“写计划 → 立刻运行 → 看断言”在同一页完成。
    实现：先独立调用 ``service.parse`` 做一次校验——``ExecutionService.run``
    会把握住的 ``PlanError`` 转成 ``{"status": "error", "error_kind": ...}`` 返回，
    直接用它就无法区分“计划根本没被接受”和“计划跑了但目标没通过”，
    而这两种情况对界面意味着完全不同的后续动作（改计划 vs 查目标）。
    因此这里显式先校验：被拒绝返回 400 且不落库；校验通过后的任何执行结果
    （包括连接失败、CLI 不可用）都作为真实证据 201 落库。
    """
    service = _service(request)
    try:
        service.parse(payload.plan)
    except PlanError as error:
        raise ApiError(400, str(error), "Execution plan was rejected") from None
    if payload.project_id is not None and await session.get(Project, payload.project_id) is None:
        raise not_found("Project")
    result = await service.run(payload.plan)
    record = ExecutionRecord(
        project_id=payload.project_id,
        label=payload.label,
        engine=result.get("engine", "postman-cli"),
        status=result["status"],
        error_kind=result.get("error_kind"),
        duration_ms=float(result.get("duration_ms", 0)),
        reserved_requests=int(result.get("reserved_requests", 0)),
        observed_attempts=int(result.get("observed_attempts", 0)),
        unknown_requests=int(result.get("unknown_requests", 0)),
        executions=result.get("executions", []),
    )
    session.add(record)
    await session.commit()
    response.headers["Location"] = f"/api/executions/{record.id}"
    return ExecutionHistoryResponse.model_validate(record)


@router.get("", response_model=Page[ExecutionHistoryResponse])
async def list_executions(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """分页列出历史执行（最新的在前）。

    实现：摘要即详情——``executions`` 一并返回，界面无需为展开断言再发一次请求；
    单份计划最多 50 个请求、每个最多 20 条断言，因此整页体积可控。
    """
    items = (
        await session.scalars(
            select(ExecutionRecord)
            .order_by(ExecutionRecord.created_at.desc(), ExecutionRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(ExecutionRecord)) or 0
    return Page[ExecutionHistoryResponse](
        items=[ExecutionHistoryResponse.model_validate(item) for item in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.get("/engine/status")
async def engine_status(request: Request):
    """探测 Postman CLI 是否可用（不登录、不发目标请求）。

    作用：让界面在“发送”之前就能提示环境问题，
    而不是等用户点完才报一个 ``cli_unavailable``。
    """
    return await _service(request).runner.status()


@router.get("/{execution_id}", response_model=ExecutionHistoryResponse)
async def get_execution(execution_id: str, session: Session):
    """按 ID 查询一次执行的完整记录。

    实现：直接 ``session.get``；不存在时 404。
    """
    record = await session.get(ExecutionRecord, execution_id)
    if record is None:
        raise not_found("Execution")
    return ExecutionHistoryResponse.model_validate(record)
