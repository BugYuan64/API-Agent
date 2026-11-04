"""Independent AI capabilities and a durable, cancellable orchestration API."""

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
from api_agent.llm.client import OpenAIClient
from api_agent.llm.service import LLMSettingService

router = APIRouter(prefix="/api/agent", tags=["AI agent"])
Session = Annotated[AsyncSession, Depends(session_for)]


async def agent_for(request: Request, session: Session) -> AgentService:
    settings = LLMSettingService(session)
    record = await settings.get_record()
    client = (
        await settings.get_client()
        if record is not None
        else OpenAIClient(settings=request.app.state.settings)
    )
    if not client.is_configured:
        raise ApiError(503, "model_not_configured", "请先配置并启用大模型服务")
    return AgentService(request.app.state.settings, client)


Agent = Annotated[AgentService, Depends(agent_for)]


def api_error(error: AgentError | PlanError):
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
    """契约 → 已通过本地范围校验的 ExecutionPlan，不发送目标请求。"""
    try:
        return await agent.generate(payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None


@router.post("/diagnose", response_model=Diagnosis)
async def diagnose(payload: DiagnoseRequest, agent: Agent):
    """保留断言事实，生成有证据引用的未证实根因假设。"""
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
    """按执行 ID 诊断：计划与结果一律取自该次执行的冻结记录。

    作用：切断“编辑器已改、历史未动”的证据错配——
    调用方只能提供契约，不能提交自己的计划，后端忽略任何额外 plan 字段。
    实现：旧记录缺冻结计划时返回 409 ``execution_plan_unavailable``，
    明确要求重新执行，而不是拿当前编辑器内容顶替。
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
    """返回经过单因素/重复/预算/重放权限过滤的下一轮计划，不自动执行。"""
    try:
        return await agent.adapt(payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None


@router.post("/runs", status_code=202)
async def start_run(payload: AgentRunRequest, request: Request, response: Response, agent: Agent):
    """创建后台闭环，立即返回 ID；通过详情/报告查询或主动取消。"""
    try:
        record = request.app.state.agents.start(agent, payload)
    except (AgentError, PlanError) as error:
        raise api_error(error) from None
    response.headers["Location"] = f"/api/agent/runs/{record['id']}"
    return record


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    try:
        return request.app.state.agents.store.read(run_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    try:
        return await request.app.state.agents.cancel(run_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None


@router.get("/runs/{run_id}/report", response_class=PlainTextResponse)
async def get_report(run_id: str, request: Request):
    try:
        report = request.app.state.agents.store.report(run_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Agent run") from None
    return PlainTextResponse(report, media_type="text/markdown")
