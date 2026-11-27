"""REST and reconnectable NDJSON live endpoints for the page-scoped AI assistant."""

from __future__ import annotations

from contextlib import aclosing

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from api_agent.assistant.models import VALID_PAGE_SCOPES, PageScope
from api_agent.assistant.runner import AssistantRunManager
from api_agent.assistant.schemas import (
    AssistantActiveRunItem,
    AssistantActiveRunsResponse,
    AssistantRunCancelResponse,
    AssistantRunStartRequest,
    AssistantRunStartResponse,
    AssistantThreadMigrateRequest,
    AssistantThreadResponse,
)
from api_agent.assistant.thread_store import AssistantThreadRepository
from api_agent.infrastructure.api import ApiError, not_found

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

MAX_ASSISTANT_TOTAL_CHARS = 64_000


class AssistantStreamingResponse(StreamingResponse):
    """Ensure the observer generator closes cleanly when a client disconnects."""

    async def stream_response(self, send) -> None:
        async with aclosing(self.body_iterator):
            await super().stream_response(send)


def _get_assistant_runs(request: Request) -> AssistantRunManager:
    runs = getattr(request.app.state, "assistant_runs", None)
    if runs is None:
        raise ApiError(503, "assistant_unavailable", "AI 助手服务尚未初始化")
    return runs


def _get_assistant_threads(request: Request) -> AssistantThreadRepository:
    threads = getattr(request.app.state, "assistant_threads", None)
    if threads is None:
        threads = AssistantThreadRepository(request.app.state.database)
        request.app.state.assistant_threads = threads
    return threads


def _validate_scope(scope: str) -> PageScope:
    if scope not in VALID_PAGE_SCOPES:
        allowed = ", ".join(sorted(VALID_PAGE_SCOPES))
        raise ApiError(
            400,
            "invalid_scope",
            f"Unsupported assistant scope {scope!r}; expected one of: {allowed}",
        )
    return scope  # type: ignore[return-value]


@router.post("/runs", status_code=202, response_model=AssistantRunStartResponse)
async def start_assistant_run(
    payload: AssistantRunStartRequest,
    request: Request,
    response: Response,
) -> AssistantRunStartResponse:
    """在后台启动页面隔离的 AI 助手多跳运行并立即返回 202 与 run_id。"""
    if (
        payload.messages[-1].role != "user"
        or sum(len(item.content) for item in payload.messages) > MAX_ASSISTANT_TOTAL_CHARS
    ):
        raise ApiError(422, "invalid_assistant_messages", "请发送有效问题，或缩短对话内容")

    runs = _get_assistant_runs(request)
    try:
        started = await runs.start_run(payload)
    except ValueError as error:
        if str(error) == "assistant_run_limit":
            raise ApiError(
                429, "assistant_run_limit", "当前并发运行的助手任务过多，请稍后再试"
            ) from None
        raise ApiError(400, "invalid_assistant_request", str(error)) from None

    response.headers["Location"] = f"/api/assistant/runs/{started['run_id']}/live"
    return AssistantRunStartResponse.model_validate(started)


@router.get("/runs/active", response_model=AssistantActiveRunsResponse)
async def list_active_assistant_runs(
    request: Request,
    scope: str | None = Query(default=None),
) -> AssistantActiveRunsResponse:
    """查询当前仍在后台运行的助手任务（可按页面作用域过滤）。"""
    valid_scope = _validate_scope(scope) if scope is not None else None
    runs = _get_assistant_runs(request)
    items = [
        AssistantActiveRunItem.model_validate(item) for item in runs.active_runs(scope=valid_scope)
    ]
    return AssistantActiveRunsResponse(items=items)


@router.get(
    "/runs/{run_id}/live",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def live_assistant_run(run_id: str, request: Request) -> StreamingResponse:
    """以可无损重连的 NDJSON 流订阅后台助手运行；客户端断开绝不取消后台任务。"""
    runs = _get_assistant_runs(request)
    if run_id not in runs.live:
        raise not_found("Assistant run")
    return AssistantStreamingResponse(
        runs.observe(run_id),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel", response_model=AssistantRunCancelResponse)
async def cancel_assistant_run(run_id: str, request: Request) -> AssistantRunCancelResponse:
    """显式取消后台助手任务，并将取消信号传播到当前活跃的子测试任务。"""
    runs = _get_assistant_runs(request)
    result = await runs.cancel(run_id)
    if result is None:
        raise not_found("Assistant run")
    return AssistantRunCancelResponse.model_validate(result)


@router.get("/threads/{scope}/{history_id}", response_model=AssistantThreadResponse)
async def get_assistant_thread(
    scope: str,
    history_id: str,
    request: Request,
) -> AssistantThreadResponse:
    """读取绑定到 ``(scope, history_id)`` 的助手消息线程及当前活跃运行 ID。"""
    valid_scope = _validate_scope(scope)
    threads = _get_assistant_threads(request)
    runs = getattr(request.app.state, "assistant_runs", None)
    try:
        thread = await threads.get_thread(valid_scope, history_id)
    except ValueError as error:
        raise ApiError(400, "invalid_history_id", str(error)) from None

    active_run_id = runs.active_run_for_history(valid_scope, history_id) if runs else None
    if thread is None:
        return AssistantThreadResponse(
            id=None,
            scope=valid_scope,
            history_id=history_id.strip(),
            title=None,
            created_at=None,
            updated_at=None,
            active_run_id=active_run_id,
            messages=[],
        )
    return AssistantThreadResponse.model_validate({**thread, "active_run_id": active_run_id})


@router.put("/threads/{scope}/{history_id}/migrate", response_model=AssistantThreadResponse)
async def migrate_assistant_thread(
    scope: str,
    history_id: str,
    payload: AssistantThreadMigrateRequest,
    request: Request,
) -> AssistantThreadResponse:
    """当目标线程为空时，将旧版客户端缓存消息幂等脱敏迁移入库（绝不覆盖已有记录）。"""
    valid_scope = _validate_scope(scope)
    threads = _get_assistant_threads(request)
    runs = getattr(request.app.state, "assistant_runs", None)
    try:
        thread = await threads.replace_or_migrate_messages(
            valid_scope,
            history_id,
            [item.model_dump() for item in payload.messages],
            title=payload.title,
            force=False,
        )
    except ValueError as error:
        raise ApiError(400, "invalid_history_id", str(error)) from None

    active_run_id = runs.active_run_for_history(valid_scope, history_id) if runs else None
    return AssistantThreadResponse.model_validate({**thread, "active_run_id": active_run_id})


@router.delete("/threads/{scope}/{history_id}", status_code=204)
async def delete_assistant_thread(
    scope: str,
    history_id: str,
    request: Request,
) -> Response:
    """删除绑定到指定历史记录的助手线程及其全部持久化消息。"""
    valid_scope = _validate_scope(scope)
    threads = _get_assistant_threads(request)
    try:
        await threads.delete_thread(valid_scope, history_id)
    except ValueError as error:
        raise ApiError(400, "invalid_history_id", str(error)) from None
    return Response(status_code=204)
