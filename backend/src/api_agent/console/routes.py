"""Endpoints for console HTTP requests, background runs, sub-case replay, and history."""

import json
from contextlib import aclosing

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from api_agent.console.runner import ConsoleRunManager
from api_agent.console.schemas import (
    ConsoleActiveRunsResponse,
    ConsoleHistoryDetail,
    ConsoleHistoryResponse,
    ConsoleRunSummary,
    ConsoleSubCaseResponse,
)
from api_agent.console.service import ConsoleRequest, ConsoleResult
from api_agent.infrastructure.api import (
    ApiError,
    LimitParam,
    OffsetParam,
    Page,
    error_response,
    not_found,
    paginate,
)
from api_agent.infrastructure.targets import TargetError

router = APIRouter(prefix="/api/console", tags=["console"])


def _get_console_runs(request: Request) -> ConsoleRunManager:
    """获取应用级 ConsoleRunManager；若 lifespan 尚未注入则按需懒创建并缓存。"""
    runs = getattr(request.app.state, "console_runs", None)
    if runs is None:
        runs = ConsoleRunManager(request.app.state.database, request.app.state.settings)
        request.app.state.console_runs = runs
    return runs


def ndjson_line(kind: str, **payload) -> bytes:
    """Frame one management-stream message independently of TCP chunk boundaries."""
    return (json.dumps({"type": kind, **payload}, ensure_ascii=False) + "\n").encode()


class ConsoleStreamingResponse(StreamingResponse):
    """Close the observer iterator when sending a frame fails or the browser disconnects."""

    async def stream_response(self, send) -> None:
        async with aclosing(self.body_iterator):
            await super().stream_response(send)


@router.post(
    "/requests/live",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def stream_request(payload: ConsoleRequest, request: Request):
    """Send one target request in background and stream SSE/result frames over NDJSON.

    断开客户端连接仅关闭本观察流，不会取消后台 HTTP/SSE 请求，结果仍会完成落库并回收资源。
    """
    runs = _get_console_runs(request)
    try:
        run_info, live = await runs.start_single_with_live(payload, defer_persist=True)
    except TargetError as error:
        envelope = error_response(
            request, 400, str(error), "Target rejected by local target policy"
        )
        error_frame = ndjson_line("error", status=400, **json.loads(envelope.body))

        async def rejected():
            yield error_frame

        return ConsoleStreamingResponse(
            rejected(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
    except ValueError as error:
        envelope = error_response(request, 429, str(error), "Too many active console runs")
        error_frame = ndjson_line("error", status=429, **json.loads(envelope.body))

        async def limited():
            yield error_frame

        return ConsoleStreamingResponse(
            limited(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return ConsoleStreamingResponse(
        runs.observe(run_info["id"], live=live, request_for_error=request),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Console-Run-Id": run_info["id"],
        },
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
async def send_request(payload: ConsoleRequest, request: Request, response: Response):
    """发出一条受控 HTTP 请求，并把它记入历史。

    作用：这是界面上「发送」按钮的同步落点。
    实现：通过 ``ConsoleRunManager`` 共用的 ``HttpTestClient`` 执行范围裁定与超时控制；
    无论成功还是传输失败都通过 ``ConsoleRepository`` 脱敏落库。
    """
    runs = _get_console_runs(request)
    try:
        result = await runs.http.execute(payload)
    except TargetError as error:
        # 范围裁定失败是“请求被本机策略拒绝”，不是服务端故障，因此返回 400 并带稳定错误码。
        raise ApiError(400, str(error), "Target rejected by local target policy") from None
    record = await runs.repo.save_single_result(payload, result)
    result.history_id = record["id"]
    # 仅本机传输失败改变外层 HTTP；目标 4xx/5xx 仍是收到真实响应的正常结果。
    if not result.ok:
        response.status_code = result.result_status or 500
    return result


@router.post("/runs", status_code=202, response_model=ConsoleRunSummary)
async def start_background_run(payload: ConsoleRequest, request: Request):
    """在后台启动单次受控 HTTP/SSE 请求，立即返回 202 与运行摘要，支持切页不断连。"""
    runs = _get_console_runs(request)
    try:
        summary = await runs.start_single(payload)
    except TargetError as error:
        raise ApiError(400, str(error), "Target rejected by local target policy") from None
    except ValueError as error:
        raise ApiError(429, str(error), "Too many active console runs") from None
    return ConsoleRunSummary.model_validate(summary)


@router.get("/runs/active", response_model=ConsoleActiveRunsResponse)
async def list_active_runs(request: Request):
    """列出当前仍在后台运行的控制台请求与 AI 套件，供跨页返回时自动重连。"""
    runs = _get_console_runs(request)
    return ConsoleActiveRunsResponse(
        items=[ConsoleRunSummary.model_validate(item) for item in runs.active_runs()]
    )


@router.get(
    "/runs/{run_id}/live",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def live_run(run_id: str, request: Request):
    """以可重连 NDJSON 流订阅后台控制台运行的实时帧；断开不取消后台任务。"""
    runs = _get_console_runs(request)
    if run_id not in runs.live and await runs.repo.get_detail(run_id) is None:
        raise not_found("Console run")
    return ConsoleStreamingResponse(
        runs.observe(run_id, request_for_error=request),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel", response_model=ConsoleHistoryDetail)
async def cancel_run(run_id: str, request: Request):
    """显式取消指定后台控制台运行并返回其终态记录。"""
    runs = _get_console_runs(request)
    detail = await runs.cancel(run_id)
    if detail is None:
        raise not_found("Console run")
    return ConsoleHistoryDetail.model_validate(detail)


@router.get("/history", response_model=Page[ConsoleHistoryResponse])
async def list_history(
    request: Request,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
):
    """分页列出历史请求，最新的在前。

    实现：委托 ``ConsoleRepository.list_history`` 按 ``(created_at, id)`` 倒序并计数，
    同时包含来源、标注、状态、轮次及子用例数量，读取时补做幂等清洗。
    """
    runs = _get_console_runs(request)
    items, total = await runs.repo.list_history(limit=limit, offset=offset)
    return paginate(ConsoleHistoryResponse, items, limit, offset, total)


@router.get("/history/{record_id}", response_model=ConsoleHistoryDetail)
async def read_history(record_id: str, request: Request):
    """Read one scrubbed replay record including redirects, SSE snapshot, and ordered sub-cases."""
    runs = _get_console_runs(request)
    detail = await runs.repo.get_detail(record_id)
    if detail is None:
        raise not_found("Console request")
    return ConsoleHistoryDetail.model_validate(detail)


@router.get("/history/{record_id}/cases/{case_id}", response_model=ConsoleSubCaseResponse)
async def read_sub_case(record_id: str, case_id: str, request: Request):
    """读取单条 AI 多轮子用例的完整脱敏只读回放快照。"""
    runs = _get_console_runs(request)
    sub_case = await runs.repo.get_sub_case(record_id, case_id)
    if sub_case is None:
        raise not_found("Console sub-case")
    return ConsoleSubCaseResponse.model_validate(sub_case)


@router.delete("/history/{record_id}", status_code=204)
async def delete_history(record_id: str, request: Request):
    """删除一条历史记录及其关联的子用例与助手线程；运行中记录返回 409 拒绝删除。"""
    runs = _get_console_runs(request)
    if record_id in runs.active:
        raise ApiError(409, "console_run_active", "请先取消正在运行的控制台测试")
    deleted = await runs.repo.delete(record_id)
    if not deleted:
        raise not_found("Console request")
    threads = getattr(request.app.state, "assistant_threads", None)
    if threads is not None:
        await threads.delete_thread("console", record_id)


@router.delete("/history", status_code=204)
async def clear_history(request: Request):
    """清空全部历史请求及其关联的控制台助手线程；存在活跃运行时拒绝清空。"""
    runs = _get_console_runs(request)
    if runs.active:
        raise ApiError(409, "console_run_active", "存在正在运行的控制台测试，请先取消后再清空历史")
    await runs.repo.clear()
    threads = getattr(request.app.state, "assistant_threads", None)
    if threads is not None:
        await threads.clear_scope("console")
