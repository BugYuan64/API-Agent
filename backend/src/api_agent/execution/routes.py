"""Endpoints that run plans through the existing Postman execution engine."""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.execution.api_schemas import ExecutionHistoryResponse, ExecutionRunRequest
from api_agent.execution.models import ExecutionRecord
from api_agent.execution.schemas import ExecutionPlan
from api_agent.execution.service import ExecutionService, PlanError
from api_agent.infrastructure.api import (
    ApiError,
    LimitParam,
    OffsetParam,
    Page,
    not_found,
    paginate,
)
from api_agent.infrastructure.database import session_for
from api_agent.infrastructure.privacy import Context
from api_agent.projects.models import Project

router = APIRouter(prefix="/api/executions", tags=["executions"])
Session = Annotated[AsyncSession, Depends(session_for)]


def freeze_plan(plan: ExecutionPlan) -> tuple[dict, str]:
    """把计划固化成（脱敏副本, SHA-256 指纹）。

    作用：诊断必须依据“实际执行的那份计划”。计划里可能带 ``auth_ref``
    引用的变量名与负向用例的样例值，因此先过一遍统一脱敏再入库。
    实现：脱敏后按 ``sort_keys`` 规范化序列化再取哈希——
    相同内容得到相同指纹，且不受键顺序影响。
    """
    clean = Context().clean(plan.model_dump(mode="json"))
    encoded = json.dumps(clean, sort_keys=True, ensure_ascii=True).encode()
    return clean, hashlib.sha256(encoded).hexdigest()


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
        plan = service.parse(payload.plan)
        driver = service.resolve_engine(payload.engine)
    except PlanError as error:
        raise ApiError(400, str(error), "Execution plan was rejected") from None
    if payload.project_id is not None and await session.get(Project, payload.project_id) is None:
        raise not_found("Project")
    # 复用已校验的计划，避免对同一载荷二次序列化与全量扫描。
    result = await service.run_plan(plan, driver)
    # 冻结本次真正执行的那份脱敏计划：后续诊断只能依据它，
    # 避免“编辑器已改、历史未动”造成证据错配。
    frozen_plan, plan_digest = freeze_plan(plan)
    record = ExecutionRecord(
        project_id=payload.project_id,
        label=payload.label,
        engine=result.get("engine", "native"),
        status=result["status"],
        error_kind=result.get("error_kind"),
        duration_ms=float(result.get("duration_ms", 0)),
        reserved_requests=int(result.get("reserved_requests", 0)),
        observed_attempts=int(result.get("observed_attempts", 0)),
        unknown_requests=int(result.get("unknown_requests", 0)),
        executions=result.get("executions", []),
        plan=frozen_plan,
        plan_sha256=plan_digest,
    )
    session.add(record)
    await session.commit()
    response.headers["Location"] = f"/api/executions/{record.id}"
    return ExecutionHistoryResponse.model_validate(record)


@router.get("", response_model=Page[ExecutionHistoryResponse])
async def list_executions(
    session: Session,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
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
    return paginate(ExecutionHistoryResponse, items, limit, offset, total)


@router.get("/engine/status")
async def engine_status(request: Request, engine: str | None = None):
    """探测指定执行驱动的就绪状态（不登录、不发目标请求）。

    作用：让界面在“发送”之前就能提示环境问题，
    而不是等用户点完才报一个 ``cli_unavailable``。
    实现：``engine`` 缺省时探测原生驱动；传 ``postman`` 则实际拉起 CLI 执行
    ``--version``。任何驱动都只在本地探测，不访问被测目标。
    """
    return await _service(request).engine_status(engine)


@router.get("/engine/list")
async def list_engines(request: Request):
    """列出全部可用执行驱动及其当前就绪状态。

    作用：界面据此渲染“执行引擎”选择器，并标出哪个驱动在本机不可用。
    实现：逐个调用驱动的 ``status()``；单个驱动内部异常不影响其他驱动的报告。
    """
    service = _service(request)
    items = []
    for name, driver in service.engines.items():
        try:
            status = await driver.status()
        except Exception:  # noqa: BLE001 - 单个驱动异常不能拖垮整个列表
            status = {"available": False, "error_kind": "status_check_failed"}
        items.append({"id": name, **status, "engine": name})
    return {"items": items, "default": "native"}


@router.get("/{execution_id}", response_model=ExecutionHistoryResponse)
async def get_execution(execution_id: str, session: Session):
    """按 ID 查询一次执行的完整记录。

    实现：直接 ``session.get``；不存在时 404。
    """
    record = await session.get(ExecutionRecord, execution_id)
    if record is None:
        raise not_found("Execution")
    return ExecutionHistoryResponse.model_validate(record)
