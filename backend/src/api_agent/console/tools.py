"""Page-scoped AI assistant tool definitions and handlers for the HTTP/SSE console."""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from api_agent.console.repository import ConsoleRepository
from api_agent.console.runner import ConsoleRunManager, build_case_link
from api_agent.console.schemas import ConsoleAiCaseSpec, ConsoleAiSuiteSpec
from api_agent.llm.schemas import FunctionDefinition, ToolDefinition

DEFAULT_COOLDOWN_MS = 500
MIN_COOLDOWN_MS = 0
MAX_COOLDOWN_MS = 60_000


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, numeric))


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


async def _invoke_hook(callback, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    outcome = callback(payload)
    if inspect.isawaitable(outcome):
        await outcome


RUN_CONSOLE_AI_SUITE_TOOL = ToolDefinition(
    type="function",
    function=FunctionDefinition(
        name="run_console_ai_suite",
        description=(
            "在接口测试页执行单轮或多轮 HTTP/SSE 测试用例套件（支持边界值、异常参数与多轮冷却），"
            "将全部子用例汇总保存为单条带「AI 测试」标注的历史记录，"
            "并返回汇总统计与可点击的子用例回放链接。"
            "仅在用户要求对接口执行实际测试、多轮回归或边界验证时调用。"
        ),
        parameters={
            "type": "object",
            "required": ["cases"],
            "properties": {
                "title": {
                    "type": "string",
                    "description": "测试套件标题（简洁概括本次测试目标）。",
                },
                "url": {
                    "type": "string",
                    "description": "默认目标 URL，单条用例可省略或以相对路径覆盖。",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
                    "description": "默认 HTTP 方法，缺省为 GET。",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "套件级默认请求头。",
                },
                "rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "循环执行轮数，默认 1。",
                },
                "cooldown_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 60000,
                    "description": "每轮/每条用例之间的冷却等待毫秒数，默认 500。",
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 60000,
                    "description": "单条请求超时毫秒数，默认 10000。",
                },
                "follow_redirects": {
                    "type": "boolean",
                    "description": "是否跟随 HTTP 重定向，默认 false。",
                },
                "cases": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "description": "待执行的测试用例列表。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "子用例名称或场景说明。"},
                            "method": {
                                "type": "string",
                                "enum": [
                                    "GET",
                                    "HEAD",
                                    "OPTIONS",
                                    "POST",
                                    "PUT",
                                    "PATCH",
                                    "DELETE",
                                ],
                            },
                            "url": {"type": "string", "description": "目标完整 URL 或相对路径。"},
                            "headers": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                            "body_mode": {
                                "type": "string",
                                "enum": ["none", "json", "text", "form"],
                            },
                            "body": {"type": "string", "description": "请求正文字符串。"},
                            "expected_status": {
                                "description": "期望 HTTP 状态码（单个整数或整数列表）。",
                                "oneOf": [
                                    {"type": "integer"},
                                    {"type": "array", "items": {"type": "integer"}},
                                ],
                            },
                            "expect_contains": {
                                "type": "string",
                                "description": "期望响应正文包含的关键子串。",
                            },
                        },
                    },
                },
            },
        },
    ),
)

READ_CONSOLE_SUB_CASE_TOOL = ToolDefinition(
    type="function",
    function=FunctionDefinition(
        name="read_console_sub_case",
        description=(
            "读取接口测试某条历史记录下指定子用例的脱敏只读执行快照"
            "（包含请求参数、响应头、响应预览、重定向链路、SSE 事件与断言摘要），"
            "用于深入排查具体子用例失败原因。"
        ),
        parameters={
            "type": "object",
            "required": ["record_id", "case_id"],
            "properties": {
                "record_id": {"type": "string", "description": "父历史记录 ID（history_id）。"},
                "case_id": {"type": "string", "description": "子用例 ID。"},
            },
        },
    ),
)

CONSOLE_TOOL_DEFINITIONS: list[ToolDefinition] = [
    RUN_CONSOLE_AI_SUITE_TOOL,
    READ_CONSOLE_SUB_CASE_TOOL,
]


async def run_console_ai_suite(
    manager: ConsoleRunManager,
    args: dict[str, Any],
    *,
    on_case_done=None,
    on_run_bound=None,
    settings_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验参数、按冷却配置启动后台 AI 接口测试套件、回传绑定与子用例链接并返回总结数据。"""
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be a JSON object")

    ctx = settings_context if isinstance(settings_context, dict) else {}
    raw_cooldown = args.get("cooldown_ms")
    if raw_cooldown is None:
        raw_cooldown = (
            ctx.get("cooldown_ms") if ctx.get("cooldown_ms") is not None else ctx.get("cooldownMs")
        )
    cooldown_ms = _clamp_int(
        raw_cooldown,
        default=DEFAULT_COOLDOWN_MS,
        minimum=MIN_COOLDOWN_MS,
        maximum=MAX_COOLDOWN_MS,
    )

    normalized = dict(args)
    normalized["cooldown_ms"] = cooldown_ms
    if "rounds" in normalized and normalized["rounds"] is not None:
        normalized["rounds"] = _clamp_int(normalized["rounds"], default=1, minimum=1, maximum=20)
    if normalized.get("timeout_ms") is None:
        ctx_timeout = ctx.get("timeout_ms", ctx.get("timeoutMs"))
        if ctx_timeout is not None:
            normalized["timeout_ms"] = _clamp_int(
                ctx_timeout, default=10_000, minimum=100, maximum=60_000
            )
    if normalized.get("sse_duration_ms") is None:
        ctx_sse_dur = ctx.get("sse_duration_ms", ctx.get("sseDurationMs"))
        if ctx_sse_dur is not None:
            normalized["sse_duration_ms"] = _clamp_int(
                ctx_sse_dur, default=3000, minimum=100, maximum=30_000
            )
    if normalized.get("sse_max_events") is None:
        ctx_sse_max = ctx.get("sse_max_events", ctx.get("sseMaxEvents"))
        if ctx_sse_max is not None:
            normalized["sse_max_events"] = _clamp_int(
                ctx_sse_max, default=50, minimum=1, maximum=1000
            )
    if normalized.get("follow_redirects") is None:
        ctx_redir = ctx.get("follow_redirects", ctx.get("followRedirects"))
        if isinstance(ctx_redir, bool):
            normalized["follow_redirects"] = ctx_redir

    spec = ConsoleAiSuiteSpec.model_validate(normalized)
    run_info = await manager.start_ai_suite(spec, on_case_done=on_case_done)
    await _invoke_hook(
        on_run_bound,
        {
            "scope": "console",
            "run_id": run_info["id"],
            "history_id": run_info["id"],
            "title": run_info.get("title"),
            "badge": run_info.get("badge"),
            "source": run_info.get("source", "ai"),
            "status": run_info.get("status", "running"),
            "total_rounds": run_info.get("total_rounds", spec.rounds),
            "total_cases": run_info.get("total_cases", len(spec.cases)),
            "cooldown_ms": run_info.get("cooldown_ms", spec.cooldown_ms),
        },
    )

    detail = await manager.wait_for_completion(run_info["id"])
    sub_cases = detail.get("sub_cases", [])
    case_links = [build_case_link(detail["id"], sc) for sc in sub_cases]

    passed = 0
    failed = 0
    errors = 0
    for sc in sub_cases:
        assertion = sc.get("assertion_summary")
        if not sc.get("ok"):
            errors += 1
        elif isinstance(assertion, dict) and assertion.get("passed") is False:
            failed += 1
        elif (
            assertion is None
            and isinstance(sc.get("status_code"), int)
            and sc["status_code"] >= 400
        ):
            failed += 1
        else:
            passed += 1

    return _json_ready(
        {
            "ok": detail["ok"],
            "scope": "console",
            "run_id": detail["id"],
            "history_id": detail["id"],
            "source": detail["source"],
            "badge": detail["badge"],
            "title": detail["title"],
            "status": detail["status"],
            "total_rounds": detail["total_rounds"],
            "cooldown_ms": detail["cooldown_ms"],
            "elapsed_ms": detail["elapsed_ms"],
            "summary": detail["summary"],
            "stats": {
                "total": len(sub_cases),
                "passed": passed,
                "failed": failed,
                "error": errors,
            },
            "case_links": case_links,
            "cases_preview": [
                {
                    "case_id": sc["id"],
                    "round_index": sc["round_index"],
                    "case_index": sc["case_index"],
                    "label": sc["label"],
                    "method": sc["method"],
                    "url": sc["url"],
                    "ok": sc["ok"],
                    "status_code": sc["status_code"],
                    "elapsed_ms": sc["elapsed_ms"],
                    "error_kind": sc["error_kind"],
                    "assertion_summary": sc["assertion_summary"],
                    "response_preview": (sc.get("response_preview") or "")[:400] or None,
                }
                for sc in sub_cases[:50]
            ],
        }
    )


async def read_console_sub_case(
    repo: ConsoleRepository,
    record_id: str,
    case_id: str,
) -> dict[str, Any]:
    """读取单条脱敏子用例执行快照，供助手排查具体用例问题。"""
    sub_case = await repo.get_sub_case(record_id, case_id)
    if sub_case is None:
        return {
            "found": False,
            "error": "console_sub_case_not_found",
            "record_id": record_id,
            "case_id": case_id,
        }
    return _json_ready(
        {
            "found": True,
            "scope": "console",
            "history_id": record_id,
            "case_id": case_id,
            "case": sub_case,
            "case_link": build_case_link(record_id, sub_case),
        }
    )


__all__ = [
    "CONSOLE_TOOL_DEFINITIONS",
    "ConsoleAiCaseSpec",
    "ConsoleAiSuiteSpec",
    "READ_CONSOLE_SUB_CASE_TOOL",
    "RUN_CONSOLE_AI_SUITE_TOOL",
    "read_console_sub_case",
    "run_console_ai_suite",
]
