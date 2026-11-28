"""Page-scoped AI tool definitions and pure async handlers for WebSocket testing."""

from __future__ import annotations

import inspect
from typing import Any

from api_agent.infrastructure.history import sanitize_stream
from api_agent.infrastructure.orm import as_utc
from api_agent.infrastructure.outcomes import outcome_for
from api_agent.llm.schemas import FunctionDefinition, ToolDefinition
from api_agent.streams.repository import StreamRepository
from api_agent.streams.runner import DEFAULT_AI_BADGE, StreamSuiteRunner
from api_agent.streams.schemas import (
    StreamAiCaseSpec,
    StreamAiSuiteSpec,
    StreamEventResponse,
    StreamReplayInputs,
    effective_sub_case_status,
    sub_case_notes,
)

DEFAULT_COOLDOWN_MS = 500
MIN_COOLDOWN_MS = 0
MAX_COOLDOWN_MS = 60_000

WEBSOCKET_TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="run_websocket_ai_suite",
            description=(
                "在长连接（WebSocket）工作台运行多轮自动化 WebSocket 测试套件。"
                "支持配置目标 ws:// 或 wss:// URL、请求头、子协议、多条测试用例"
                "（含预发送消息、观察时长、期望收到的文本片段或最小事件数）、"
                "轮数 rounds 以及每轮/用例冷却间隔 cooldown_ms（默认 500ms）。"
                "所有子用例归档在单条带「AI 测试」徽标的历史下，返回总结与可点击回放链接。"
            ),
            parameters={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "目标 WebSocket 地址（支持 ws://、wss://、http:// 或 https://）。",
                    },
                    "title": {
                        "type": "string",
                        "description": "本次 AI 测试套件的简明标题（展示在历史列表中）。",
                    },
                    "headers": {
                        "type": "object",
                        "description": "所有子用例默认共用的握手请求头（键值对）。",
                        "additionalProperties": {"type": "string"},
                    },
                    "subprotocols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "所有子用例默认共用的 WebSocket 子协议列表。",
                    },
                    "rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "循环执行轮数，默认 1，范围 1..20。",
                    },
                    "cooldown_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 60000,
                        "description": "每轮/用例之间的冷却间隔（毫秒），默认 500，范围 0..60000。",
                    },
                    "cases": {
                        "type": "array",
                        "description": "要执行的 WebSocket 测试用例列表。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "子用例名称（用于链接按钮与回放标题）。",
                                },
                                "url": {
                                    "type": "string",
                                    "description": "可选：覆盖套件默认 URL 的子用例目标地址。",
                                },
                                "headers": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                    "description": "可选：该子用例附加或覆盖的握手请求头。",
                                },
                                "subprotocols": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "可选：覆盖该子用例的子协议列表。",
                                },
                                "send": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "握手成功后按序发送的文本消息列表。",
                                },
                                "send_interval_ms": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 60000,
                                    "description": "多条发送消息之间的间隔（毫秒），默认 200。",
                                },
                                "max_events": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 2000,
                                    "description": "单条子用例最多采集的事件条数，默认 50。",
                                },
                                "max_duration_ms": {
                                    "type": "integer",
                                    "minimum": 1000,
                                    "maximum": 900000,
                                    "description": "单条子用例最大观察时长（毫秒），默认 5000。",
                                },
                                "connect_timeout_ms": {
                                    "type": "integer",
                                    "minimum": 100,
                                    "maximum": 60000,
                                    "description": "握手超时（毫秒），默认 10000。",
                                },
                                "expect_contains": {
                                    "type": "string",
                                    "description": "可选断言：期望收到的入站事件中包含该子串。",
                                },
                                "expect_min_events": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 2000,
                                    "description": "可选断言：期望至少收到的入站消息帧数。",
                                },
                                "expect_close_code": {
                                    "type": "integer",
                                    "minimum": 1000,
                                    "maximum": 4999,
                                    "description": "可选断言：期望服务端返回的 WebSocket 关闭码。",
                                },
                                "expect_handshake_reject": {
                                    "type": "boolean",
                                    "description": (
                                        "可选断言：为 true 表示期望握手被目标拒绝"
                                        "（用于异常/鉴权边界测试）。"
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="read_websocket_sub_case",
            description=(
                "读取已执行的 WebSocket AI 测试套件中指定子用例的完整只读回放快照"
                "（包含脱敏后的连接参数、预置发送消息、握手/关闭状态与收发事件时间线），"
                "用于深入排查失败原因或回答用户追问。"
            ),
            parameters={
                "type": "object",
                "required": ["parent_id", "case_id"],
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "顶层分组 AI WebSocket 历史记录 ID。",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "目标子用例 ID。",
                    },
                },
            },
        ),
    ),
]


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """将模型传入的整数参数安全收敛至合法区间，非数值回落默认值。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _resolve_cooldown_ms(args: dict[str, Any], settings_context: dict[str, Any] | None) -> int:
    """解析冷却时间：优先采用显式参数，否则继承页面设置，统一收敛到 0..60000ms。"""
    raw = args.get("cooldown_ms")
    if raw is None and isinstance(settings_context, dict):
        raw = settings_context.get("cooldown_ms")
        if raw is None:
            raw = settings_context.get("cooldownMs")
    return _clamp_int(raw, DEFAULT_COOLDOWN_MS, MIN_COOLDOWN_MS, MAX_COOLDOWN_MS)


def _normalize_str_dict(raw: Any) -> dict[str, str]:
    """把潜在请求头映射规范为 ``dict[str, str]``。"""
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(val) for key, val in raw.items() if str(key).strip()}


def _normalize_str_list(raw: Any) -> list[str]:
    """把潜在字符串数组规范为 ``list[str]``。"""
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item is not None]


def _build_case_spec(raw: Any, index: int) -> StreamAiCaseSpec:
    """将松散工具参数规范为强校验的 ``StreamAiCaseSpec``。"""
    item = raw if isinstance(raw, dict) else {}
    label = str(item.get("label") or item.get("name") or f"用例 {index + 1}").strip()[:200]
    if not label:
        label = f"用例 {index + 1}"
    raw_send = item.get("send") if "send" in item else item.get("messages")
    send = _normalize_str_list(raw_send)[:20]
    raw_url = item.get("url")
    url = str(raw_url).strip() if isinstance(raw_url, str) and raw_url.strip() else None

    expect_contains = item.get("expect_contains")
    if expect_contains is not None:
        expect_contains = str(expect_contains)[:500] or None

    expect_min_events = (
        _clamp_int(item.get("expect_min_events"), 0, 0, 2000)
        if item.get("expect_min_events") is not None
        else None
    )
    expect_close_code = (
        _clamp_int(item.get("expect_close_code"), 1000, 1000, 4999)
        if item.get("expect_close_code") is not None
        else None
    )

    return StreamAiCaseSpec(
        label=label,
        url=url,
        headers=_normalize_str_dict(item.get("headers")),
        subprotocols=_normalize_str_list(item.get("subprotocols"))[:5],
        send=send,
        send_interval_ms=_clamp_int(item.get("send_interval_ms"), 200, 0, 60_000),
        max_events=_clamp_int(item.get("max_events"), 50, 1, 2000),
        max_duration_ms=_clamp_int(item.get("max_duration_ms"), 5_000, 1000, 900_000),
        connect_timeout_ms=_clamp_int(item.get("connect_timeout_ms"), 10_000, 100, 60_000),
        ping_interval_ms=_clamp_int(item.get("ping_interval_ms"), 20_000, 0, 300_000),
        expect_contains=expect_contains,
        expect_min_events=expect_min_events,
        expect_close_code=expect_close_code,
        expect_handshake_reject=bool(item.get("expect_handshake_reject", False)),
    )


def _parse_suite_spec(
    args: dict[str, Any], settings_context: dict[str, Any] | None = None
) -> StreamAiSuiteSpec:
    """将 AI 工具调用参数解析并归一化为 ``StreamAiSuiteSpec``。"""
    payload = args if isinstance(args, dict) else {}
    url = str(payload.get("url") or "").strip()
    if not url and isinstance(settings_context, dict):
        url = str(settings_context.get("url") or "").strip()
    if not url:
        raise ValueError("url is required for run_websocket_ai_suite")

    raw_cases = payload.get("cases")
    if isinstance(raw_cases, list) and raw_cases:
        cases = [_build_case_spec(item, idx) for idx, item in enumerate(raw_cases[:50])]
    else:
        fallback_send = _normalize_str_list(payload.get("send") or payload.get("messages"))[:20]
        cases = [
            StreamAiCaseSpec(
                label="连通性探测",
                send=fallback_send,
                max_events=_clamp_int(payload.get("max_events"), 50, 1, 2000),
                max_duration_ms=_clamp_int(payload.get("max_duration_ms"), 5_000, 1000, 900_000),
            )
        ]

    raw_title = payload.get("title")
    title = (
        str(raw_title).strip()[:200] if isinstance(raw_title, str) and raw_title.strip() else None
    )
    raw_badge = payload.get("badge")
    badge = (
        str(raw_badge).strip()[:40]
        if isinstance(raw_badge, str) and raw_badge.strip()
        else DEFAULT_AI_BADGE
    )
    raw_project_id = payload.get("project_id")
    project_id = (
        str(raw_project_id).strip()
        if isinstance(raw_project_id, str) and raw_project_id.strip()
        else None
    )

    return StreamAiSuiteSpec(
        url=url,
        title=title,
        headers=_normalize_str_dict(payload.get("headers")),
        subprotocols=_normalize_str_list(payload.get("subprotocols"))[:5],
        cases=cases,
        rounds=_clamp_int(payload.get("rounds"), 1, 1, 20),
        cooldown_ms=_resolve_cooldown_ms(payload, settings_context),
        project_id=project_id,
        badge=badge,
    )


async def _emit(callback, payload: dict[str, Any]) -> None:
    """触发可选同步/异步回调。"""
    if callback is None:
        return
    outcome = callback(payload)
    if inspect.isawaitable(outcome):
        await outcome


async def run_websocket_ai_suite(
    runner: StreamSuiteRunner,
    args: dict[str, Any],
    *,
    settings_context: dict[str, Any] | None = None,
    on_case_done=None,
    on_run_bound=None,
) -> dict[str, Any]:
    """纯异步工具处理器：启动多轮 WebSocket AI 测试套件并等待完成。

    流程：规范化参数（冷却默认 500ms、范围 0..60000ms）→ 立即启动后台套件并触发
    ``on_run_bound`` 通知调用方绑定历史 ID → 等待所有轮次完成并返回结构化摘要与
    规范 ``case_links``（``list[CaseLinkBlock]``）。
    """
    spec = _parse_suite_spec(args, settings_context=settings_context)
    started = await runner.start_ai_suite(spec, on_case_done=on_case_done)
    await _emit(
        on_run_bound,
        {
            "scope": "websocket",
            "history_id": started["id"],
            "run_id": started["id"],
            "title": started.get("title"),
            "badge": started.get("badge"),
            "status": started.get("status"),
            "total_rounds": started.get("total_rounds"),
            "total_cases": started.get("total_cases"),
            "cooldown_ms": started.get("cooldown_ms"),
        },
    )
    final = await runner.wait_for_completion(started["id"])
    return {
        "scope": "websocket",
        "history_id": final["id"],
        "run_id": final["id"],
        "title": final.get("title"),
        "badge": final.get("badge"),
        "source": "ai",
        "status": final.get("status"),
        "url": final.get("url"),
        "total_rounds": final.get("total_rounds"),
        "cooldown_ms": final.get("cooldown_ms"),
        "total_cases": final.get("total_cases"),
        "completed_cases": final.get("completed_cases"),
        "passed_cases": final.get("passed_cases", 0),
        "failed_cases": final.get("failed_cases", 0),
        "error_cases": final.get("error_cases", 0),
        "summary": final.get("summary"),
        "error_kind": final.get("error_kind"),
        "error_message": final.get("error_message"),
        "case_links": list(final.get("case_links") or []),
    }


async def read_websocket_sub_case(
    repo: StreamRepository,
    parent_id: str,
    case_id: str,
) -> dict[str, Any]:
    """纯异步工具处理器：按父套件 ID 与子用例 ID 读取脱敏后的完整只读快照。"""
    record = await repo.get_sub_case(parent_id, case_id)
    if record is None:
        return {
            "found": False,
            "error": "websocket_sub_case_not_found",
            "parent_id": parent_id,
            "case_id": case_id,
        }
    sanitize_stream(record)
    events = await repo.list_events(record.id, after=0, limit=500)
    label = record.case_label or f"第 {record.round_index} 轮 · 用例 {record.case_index + 1}"
    effective_status = effective_sub_case_status(record)
    notes = sub_case_notes(record)
    outcome = outcome_for(effective_status, record.error_kind, stop_reason=record.stop_reason)
    inputs = StreamReplayInputs(
        url=record.url,
        request_headers=record.request_headers or {},
        subprotocols=(record.settings or {}).get("subprotocols", []),
        scheduled_messages=record.scheduled_messages,
    )
    return {
        "found": True,
        "scope": "websocket",
        "parent_id": parent_id,
        "case_id": record.id,
        "round_index": record.round_index,
        "case_index": record.case_index,
        "case_label": record.case_label,
        "label": label,
        "readonly": True,
        "status": effective_status,
        "transport_status": record.status,
        "result_code": outcome.code,
        "result_status": outcome.status,
        "notes": notes,
        "inputs": inputs.model_dump(mode="json"),
        "session": {
            "id": record.id,
            "kind": record.kind,
            "url": record.url,
            "status": effective_status,
            "transport_status": record.status,
            "result_code": outcome.code,
            "result_status": outcome.status,
            "stop_reason": record.stop_reason,
            "error_kind": record.error_kind,
            "error_message": record.error_message,
            "http_status": record.http_status,
            "subprotocol": record.subprotocol,
            "close_code": record.close_code,
            "close_reason": record.close_reason,
            "event_count": record.event_count,
            "byte_count": record.byte_count,
            "sent_count": record.sent_count,
            "truncated_count": record.truncated_count,
            "opened_at": as_utc(record.opened_at).isoformat() if record.opened_at else None,
            "closed_at": as_utc(record.closed_at).isoformat() if record.closed_at else None,
        },
        "events": [
            StreamEventResponse(
                session_id=row.session_id,
                sequence=row.sequence,
                direction=row.direction,
                kind=row.kind,
                event_name=row.event_name,
                data=row.data,
                data_bytes=row.data_bytes,
                truncated=row.truncated,
                elapsed_ms=row.elapsed_ms,
            ).model_dump(mode="json")
            for row in events
        ],
    }


__all__ = [
    "StreamAiCaseSpec",
    "StreamAiSuiteSpec",
    "WEBSOCKET_TOOL_DEFINITIONS",
    "read_websocket_sub_case",
    "run_websocket_ai_suite",
]
