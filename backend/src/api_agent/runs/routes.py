"""Management endpoints for local simulation runs and their evidence."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import Page, PageInfo, not_found
from api_agent.infrastructure.database import session_for
from api_agent.runs.repository import RunRepository
from api_agent.runs.schemas import (
    BudgetEntryResponse,
    DispatchAttemptResponse,
    RunCreate,
    RunEventResponse,
    RunResponse,
    RunSnapshotResponse,
)
from api_agent.runs.service import RunService

router = APIRouter(prefix="/api", tags=["runs"])
Session = Annotated[AsyncSession, Depends(session_for)]


def service(session: AsyncSession, request: Request) -> RunService:
    """构造带配置上下文的运行服务。

    作用：运行记录需要如实记下当前配置的模型模式（``llm_mode``），
    因此从 ``app.state.settings`` 取值注入，而不是读全局变量。
    实现：把请求级 session 和 ``request.app.state.settings.llm_mode`` 一起传给 RunService。
    """
    return RunService(session, request.app.state.settings.llm_mode)


@router.post("/runs", response_model=RunResponse, status_code=202)
async def create_run(data: RunCreate, session: Session, request: Request, response: Response):
    """创建一次模拟运行（异步受理，返回 202）。

    实现：服务层只负责落库为 ``queued``，不在此处跑动作；
    响应头 ``Location`` 指向新资源，随后 ``worker.notify()`` 立即唤醒后台循环，
    让运行能马上被认领，而不必等下一次轮询。
    """
    run = await service(session, request).create(data)
    response.headers["Location"] = f"/api/runs/{run.id}"
    request.app.state.worker.notify()
    return run


@router.get("/runs", response_model=Page[RunResponse])
async def list_runs(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """分页列出全部运行记录。

    实现：直接用仓储查询（只读、无业务规则），
    再封装成统一的 ``{items, page}`` 结构；
    分页参数在此处用 ``Query`` 约束，越界会在进入函数前被拒绝。
    """
    runs, total = await RunRepository(session).list(limit, offset)
    return Page[RunResponse](
        items=[RunResponse.model_validate(run) for run in runs],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, session: Session, request: Request):
    """查询单个运行的最新状态。

    实现：交给服务层；运行不存在时返回 404。
    该接口是前端轮询进度的主要入口（会一并返回 ``current_action``/``action_count``）。
    """
    return await service(session, request).get(run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(run_id: str, session: Session, request: Request):
    """请求取消一次运行。

    实现：服务层根据当前状态决定“直接取消”还是“转入 cancelling”，
    随后 ``notify`` 唤醒 worker 尽快走到安全点收尾；
    返回值是取消请求处理后的运行状态，不一定是终态。
    """
    run = await service(session, request).cancel(run_id)
    request.app.state.worker.notify()
    return run


@router.get("/runs/{run_id}/snapshot", response_model=RunSnapshotResponse)
async def get_snapshot(run_id: str, session: Session, request: Request):
    """查询运行创建时冻结的动作快照。

    实现：服务层先确认运行存在，再按 ``snapshot_id`` 取不可变快照，
    用于复现这次运行的动作序列、资源上限与随机种子。
    """
    return await service(session, request).get_snapshot(run_id)


@router.get("/runs/{run_id}/events", response_model=Page[RunEventResponse])
async def list_events(
    run_id: str,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """分页读取运行事件流（按 sequence 升序的审计轨迹）。

    实现：先确认运行存在（否则 404，避免“空事件列表”被误解为“还没开始”），
    再按序号分页；每页条目由 ``RunEventResponse`` 裁剪成对外字段。
    """
    repository = RunRepository(session)
    if await repository.get(run_id) is None:
        raise not_found("Run")
    items, total = await repository.list_events(run_id, limit, offset)
    return Page[RunEventResponse](
        items=[RunEventResponse.model_validate(item) for item in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.get("/runs/{run_id}/budget", response_model=list[BudgetEntryResponse])
async def list_budget(run_id: str, session: Session):
    """列出运行的完整预算账本（append-only 原始条目）。

    实现：先校验运行存在，再原样返回所有账本行，不做聚合——
    聚合后的“已预留/已消耗/已释放”是派生视图，保留原始条目便于审计对账。
    """
    repository = RunRepository(session)
    if await repository.get(run_id) is None:
        raise not_found("Run")
    return [
        BudgetEntryResponse.model_validate(item) for item in await repository.list_budget(run_id)
    ]


@router.get("/runs/{run_id}/attempts", response_model=list[DispatchAttemptResponse])
async def list_attempts(run_id: str, session: Session):
    """列出运行的每次派发尝试及其结果类型。

    实现：先校验运行存在，再按动作序号返回；
    通过 ``status``/``error_kind`` 可以区分“正常完成、模拟失败、被跳过、进程中断”四种结局。
    """
    repository = RunRepository(session)
    if await repository.get(run_id) is None:
        raise not_found("Run")
    return [
        DispatchAttemptResponse.model_validate(item)
        for item in await repository.list_attempts(run_id)
    ]
