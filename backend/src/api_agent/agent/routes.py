"""暴露独立 Agent 能力与可查询、可取消的后台运行接口。

依赖 FastAPI、模型配置、执行历史和应用级任务管理器；负责取依赖、
校验历史可用性及错误码映射，业务校验和闭环执行交给服务与运行时。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.agent.schemas import (
    Adaptation,
    AdaptRequest,
    AgentRunRequest,
    DiagnoseExecutionRequest,
    DiagnoseRequest,
    Diagnosis,
    GenerateRequest,
    Generation,
)
from api_agent.agent.service import AgentError, AgentService
from api_agent.execution.models import ExecutionRecord
from api_agent.execution.schemas import ExecutionPlan, RunResult
from api_agent.execution.service import PlanError
from api_agent.infrastructure.api import ApiError, not_found
from api_agent.infrastructure.database import session_for
from api_agent.infrastructure.outcomes import result_fields
from api_agent.llm.service import LLMSettingService

router = APIRouter(prefix="/api/agent", tags=["AI agent"])
Session = Annotated[AsyncSession, Depends(session_for)]


async def agent_for(request: Request, session: Session) -> AgentService:
    """Use the assistant model for chat, summaries and existing agent routes."""
    return await configured_agent(request, session)


async def generation_agent_for(request: Request, session: Session) -> AgentService:
    """Use the model configured for document and test generation."""
    return await configured_agent(request, session, for_generation=True)


async def configured_agent(
    request: Request, session: AsyncSession, *, for_generation: bool = False
) -> AgentService:
    """Resolve the effective client once and reject unconfigured agents."""
    settings = request.app.state.settings
    client = await LLMSettingService(session, settings).get_client(for_generation=for_generation)
    if not client.is_configured:
        raise ApiError(503, "model_not_configured", "请先配置并启用大模型服务")
    return AgentService(settings, client)


Agent = Annotated[AgentService, Depends(agent_for)]


def api_error(error: AgentError | PlanError):
    """将 Agent/计划稳定错误码映射为 HTTP 状态和统一提示，返回 ApiError。"""
    code = str(error)
    if code == "agent_capacity_exceeded":
        status = 429
    elif code in {"model_not_configured", "model_budget_exhausted"}:
        status = 503
    elif code == "model_timeout":
        status = 504
    elif code.startswith("model_") or code == "invalid_model_output":
        status = 502
    else:
        status = 400
    return ApiError(status, code, "AI 操作未完成；请按错误码检查配置、输入或预算")


@router.post("/generate", response_model=Generation)
async def generate(payload: GenerateRequest, agent: Agent):
    """由契约生成通过本地校验的计划，不发送目标请求；领域错误映射为 HTTP 错误。"""
    try:
        return await agent.generate(payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None


@router.post("/diagnose", response_model=Diagnosis)
async def diagnose(payload: DiagnoseRequest, agent: Agent):
    """由计划和结果生成证据及未证实假设；对应校验或模型失败映射为 HTTP 错误。"""
    try:
        return await agent.diagnose(payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None


@router.post("/executions/{execution_id}/diagnose", response_model=Diagnosis)
async def diagnose_execution(
    execution_id: str,
    payload: DiagnoseExecutionRequest,
    agent: Agent,
    session: Session,
):
    """按执行 ID 诊断，计划与结果取自该次执行的冻结记录。

    调用方只提供契约，额外 plan 字段由输入模型拒绝为 422；记录不存在
    返回 404，缺失或不兼容的冻结计划/结果返回 409，模型错误按领域码映射。
    """
    record = await session.get(ExecutionRecord, execution_id)
    if record is None:
        raise not_found("Execution")
    if record.plan is None:
        raise ApiError(
            409,
            "execution_plan_unavailable",
            "该执行记录未冻结原计划（旧版本记录），请重新执行后再诊断",
        )
    try:
        plan = ExecutionPlan.model_validate(record.plan)
    except ValidationError:
        raise ApiError(
            409,
            "execution_plan_unavailable",
            "冻结的计划无法通过当前结构校验，请重新执行后再诊断",
        ) from None
    try:
        run_result = RunResult.model_validate(
            {
                "status": record.status,
                "error_kind": record.error_kind,
                "engine": record.engine,
                "duration_ms": record.duration_ms,
                "reserved_requests": record.reserved_requests,
                "observed_attempts": record.observed_attempts,
                "unknown_requests": record.unknown_requests,
                "executions": record.executions,
            }
        )
    except ValidationError:
        raise ApiError(
            409, "execution_result_unavailable", "该执行记录的结果结构不完整，请重新执行"
        ) from None
    request = DiagnoseRequest(contract=payload.contract, plan=plan, result=run_result)
    try:
        return await agent.diagnose(request)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None


@router.post("/adapt", response_model=Adaptation)
async def adapt(payload: AdaptRequest, agent: Agent):
    """返回经范围、单因素、去重、预算与重放权限过滤的补测计划；不自动执行。

    计划校验或模型失败映射为统一 HTTP 错误。
    """
    try:
        return await agent.adapt(payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None


@router.post("/runs", status_code=202)
async def start_run(payload: AgentRunRequest, request: Request, response: Response, agent: Agent):
    """接受闭环请求，返回初始记录及查询 Location，由后台继续运行。

    容量、引擎或目标校验失败按领域码返回 HTTP 错误。
    """
    try:
        record = request.app.state.agents.start(agent, payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None
    response.headers["Location"] = f"/api/agent/runs/{record['id']}"
    return result_fields(record)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    """按 ID 返回持久化运行记录；ID 非法或文件不存在返回 404。"""
    try:
        return result_fields(request.app.state.agents.store.read(run_id))
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    """取消本进程中的指定运行并返回记录；ID 非法或不存在返回 404。"""
    try:
        return result_fields(await request.app.state.agents.cancel(run_id))
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None


@router.get("/runs/{run_id}/report", response_class=PlainTextResponse)
async def get_report(run_id: str, request: Request):
    """按 ID 返回 Markdown 纯文本报告；ID 非法或不存在返回 404。"""
    try:
        report = request.app.state.agents.store.report(run_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None
    return PlainTextResponse(report, media_type="text/markdown")


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, request: Request):
    """删除一次编排运行的证据文件。

    作用：让编排历史可以清理，而不是只能越积越多。
    实现：先读一次记录确认它已到终态——``queued``/``running`` 的运行仍有后台任务
    在写同一个文件，此时删除会与写入竞争，也会让用户误以为任务被取消了；
    这种情况返回 409 并要求先取消。终态才真的删文件，不存在或 ID 非法返回 404。
    """
    store = request.app.state.agents.store
    try:
        record = store.read(run_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None
    if record.get("status") in {"queued", "running"}:
        raise ApiError(409, "agent_run_active", "Cancel the run before deleting it")
    if not store.delete(run_id):
        raise not_found("Agent run")
