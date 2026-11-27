"""模型测试页面专属 AI 工具定义与纯异步处理器（作用域 ``model_testing``）。

职责边界：
- 仅暴露模型测试域的 3 个受控工具（模型列表拉取、AI 多轮能力探测套件、子用例只读回放）；
- 所有输入参数经显式范围夹紧（如 ``cooldown_ms`` 默认 500ms、范围 ``0..60000``）；
- 绝不在工具返回体或回放链接中暴露任何明文 API Key。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.privacy import Context
from api_agent.llm.schemas import FunctionDefinition, ToolDefinition
from api_agent.llm.testing import discover_models
from api_agent.llm.testing_manager import (
    DEFAULT_AI_BADGE,
    ModelTestingConfigError,
    ModelTestManager,
    ModelTestRepository,
    build_case_link_block,
    resolve_testing_credentials,
)
from api_agent.llm.testing_schemas import (
    ModelProfile,
    ModelProtocol,
    ModelTestAiSuiteSpec,
    ProbeCheckName,
)

__all__ = [
    "MODEL_TESTING_TOOL_DEFINITIONS",
    "ModelTestAiSuiteSpec",
    "discover_model_testing_models",
    "read_model_testing_sub_case",
    "run_model_testing_ai_suite",
]

_VALID_PROTOCOLS: frozenset[str] = frozenset({"auto", "openai", "anthropic", "gemini"})
_VALID_CHECKS: frozenset[str] = frozenset(
    {
        "availability",
        "tool_calling",
        "input_modalities",
        "output_modalities",
        "streaming",
        "structured_output",
    }
)
_DEFAULT_CHECKS: list[ProbeCheckName] = ["availability", "tool_calling", "input_modalities"]


MODEL_TESTING_TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="discover_model_testing_models",
            description=(
                "拉取指定大模型服务 Base URL 下的可用模型清单与初始能力画像（类型、模态、元数据）。"
                "当用户需要先查看服务提供哪些模型、确认可用模型 ID 或检查协议时调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "description": "目标大模型服务 Base URL；留空时使用系统已配置地址。",
                    },
                    "use_saved_key": {
                        "type": "boolean",
                        "description": "同源时是否复用系统已保存密钥（跨源自动阻止），默认 true。",
                        "default": True,
                    },
                    "protocol": {
                        "type": "string",
                        "enum": ["auto", "openai", "anthropic", "gemini"],
                        "description": "目标协议，默认 auto 自动识别。",
                        "default": "auto",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "单次拉取超时时间（秒，2~120），默认 20。",
                        "default": 20,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回给助手审阅的最大模型条目数（1~200），默认 100。",
                        "default": 100,
                    },
                },
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="run_model_testing_ai_suite",
            description=(
                "针对选定模型列表执行单轮或多轮能力探测（可用性、工具调用、流式响应、结构化输出、"
                "输入/输出模态），支持轮间冷却 cooldown_ms（默认 500ms），合并保存为一条带"
                "「AI 测试」标注的模型测试历史记录并返回每个子用例的可点击回放链接。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "base_url": {
                        "type": "string",
                        "description": "目标大模型服务 Base URL；留空时使用系统已配置地址。",
                    },
                    "use_saved_key": {
                        "type": "boolean",
                        "description": "同源时是否复用系统已保存密钥（跨源自动阻止），默认 true。",
                        "default": True,
                    },
                    "protocol": {
                        "type": "string",
                        "enum": ["auto", "openai", "anthropic", "gemini"],
                        "description": "目标协议，默认 auto。",
                        "default": "auto",
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 50,
                        "description": "待探测的模型 ID 列表。",
                    },
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "availability",
                                "tool_calling",
                                "input_modalities",
                                "output_modalities",
                                "streaming",
                                "structured_output",
                            ],
                        },
                        "description": (
                            "要执行的探测维度列表，默认 "
                            "availability/tool_calling/input_modalities。"
                        ),
                    },
                    "rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 1,
                        "description": "循环探测轮次（1~20），默认 1。",
                    },
                    "cooldown_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 60000,
                        "default": 500,
                        "description": "每轮探测之间的冷却间隔（毫秒，0~60000），默认 500。",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 3,
                        "maximum": 120,
                        "default": 30,
                        "description": "单次探测超时时间（秒，3~120），默认 30。",
                    },
                    "concurrency": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 4,
                        "description": "单轮内最大并发探测数（1~10），默认 4。",
                    },
                    "title": {
                        "type": "string",
                        "description": "可选任务标题，便于在模型测试历史中识别。",
                    },
                },
                "required": ["models"],
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="read_model_testing_sub_case",
            description=(
                "读取指定模型测试历史记录中单个子用例（如 r1:gpt-4o）的完整只读回放快照，"
                "包含解析端点、探测配置、延迟/TTFT 与能力详情。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "模型测试历史记录 ID。",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "子用例标识（例如 r1:gpt-4o）。",
                    },
                },
                "required": ["run_id", "case_id"],
            },
        ),
    ),
]


def _clamp_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _clamp_float(value: Any, *, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _normalize_protocol(raw: Any) -> ModelProtocol:
    text = str(raw or "auto").strip().lower()
    if text in _VALID_PROTOCOLS:
        return text  # type: ignore[return-value]
    return "auto"


def _normalize_checks(raw: Any) -> list[ProbeCheckName]:
    if not isinstance(raw, list):
        return list(_DEFAULT_CHECKS)
    cleaned: list[ProbeCheckName] = []
    for item in raw:
        name = str(item or "").strip()
        if name in _VALID_CHECKS and name not in cleaned:
            cleaned.append(name)  # type: ignore[arg-type]
    return cleaned or list(_DEFAULT_CHECKS)


def _normalize_models(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw_list = [raw]
    elif isinstance(raw, list):
        raw_list = raw
    else:
        return []
    cleaned: list[str] = []
    for item in raw_list:
        if isinstance(item, str):
            mid = item.strip()
            if mid and mid not in cleaned:
                cleaned.append(mid)
    return cleaned[:50]


def _parse_discovered_profiles(raw: Any) -> list[ModelProfile] | None:
    if not isinstance(raw, list) or not raw:
        return None
    profiles: list[ModelProfile] = []
    for item in raw:
        if isinstance(item, ModelProfile):
            profiles.append(item)
        elif isinstance(item, dict):
            try:
                profiles.append(ModelProfile.model_validate(item))
            except Exception:  # noqa: BLE001
                continue
    return profiles or None


async def _invoke_hook(callback: Callable[..., Any] | None, payload: Any) -> None:
    if callback is None:
        return
    outcome = callback(payload)
    if inspect.isawaitable(outcome):
        await outcome


async def discover_model_testing_models(
    session: AsyncSession,
    settings: Settings,
    args: dict[str, Any],
) -> dict[str, Any]:
    """AI 工具处理器：解析同源/临时凭证并拉取目标服务的模型清单（脱敏、有界）。"""
    safe_args = args if isinstance(args, dict) else {}
    raw_base_url = safe_args.get("base_url")
    raw_api_key = safe_args.get("api_key")
    use_saved_key = bool(safe_args.get("use_saved_key", True))
    protocol = _normalize_protocol(safe_args.get("protocol"))
    timeout_seconds = _clamp_float(
        safe_args.get("timeout_seconds"), default=20.0, low=2.0, high=120.0
    )
    limit = _clamp_int(safe_args.get("limit"), default=100, low=1, high=200)

    resolved_api_key = ""
    try:
        (
            resolved_base_url,
            resolved_api_key,
            used_saved_key,
            has_custom_api_key,
        ) = await resolve_testing_credentials(
            session,
            settings,
            base_url=raw_base_url if isinstance(raw_base_url, str) else None,
            api_key=raw_api_key if isinstance(raw_api_key, str) else None,
            use_saved_key=use_saved_key,
        )
    except ModelTestingConfigError as exc:
        return {
            "ok": False,
            "error": exc.code,
            "message": exc.message,
            "models": [],
        }

    try:
        response = await discover_models(
            resolved_base_url,
            resolved_api_key,
            protocol=protocol,
            timeout_seconds=timeout_seconds,
            trust_env=bool(getattr(settings, "trust_system_proxy", False)),
        )
        secrets = (resolved_api_key,) if len(resolved_api_key) >= 4 else ()
        ctx = Context(secrets=secrets)
        dumped = response.model_dump(mode="json")
        models = dumped.get("models") or []
        truncated = len(models) > limit
        payload: dict[str, Any] = {
            "ok": dumped["ok"],
            "protocol": dumped["protocol"],
            "base_url": dumped["base_url"],
            "http_status": dumped["http_status"],
            "latency_ms": dumped["latency_ms"],
            "count": dumped["count"],
            "message": dumped["message"],
            "used_saved_key": used_saved_key,
            "has_custom_api_key": has_custom_api_key,
            "truncated": truncated,
            "models": models[:limit],
        }
        return ctx.clean(payload)
    finally:
        resolved_api_key = ""


async def run_model_testing_ai_suite(
    manager: ModelTestManager,
    session: AsyncSession,
    args: dict[str, Any],
    *,
    on_case_done: Callable[[dict[str, Any]], Any] | None = None,
    on_run_bound: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """AI 工具处理器：启动多轮模型探测套件、绑定历史记录、推送子用例链接并返回汇总。"""
    safe_args = args if isinstance(args, dict) else {}
    models = _normalize_models(safe_args.get("models"))
    if not models:
        return {
            "ok": False,
            "error": "invalid_models",
            "message": "至少需要指定一个有效模型 ID",
            "case_links": [],
        }

    cooldown_ms = _clamp_int(safe_args.get("cooldown_ms"), default=500, low=0, high=60_000)
    rounds = _clamp_int(safe_args.get("rounds"), default=1, low=1, high=20)
    concurrency = _clamp_int(safe_args.get("concurrency"), default=4, low=1, high=10)
    timeout_seconds = _clamp_float(
        safe_args.get("timeout_seconds"), default=30.0, low=3.0, high=120.0
    )
    protocol = _normalize_protocol(safe_args.get("protocol"))
    checks = _normalize_checks(safe_args.get("checks"))
    raw_title = safe_args.get("title")
    title = raw_title.strip()[:200] if isinstance(raw_title, str) and raw_title.strip() else None
    raw_badge = safe_args.get("badge")
    badge = (
        raw_badge.strip()[:40]
        if isinstance(raw_badge, str) and raw_badge.strip()
        else DEFAULT_AI_BADGE
    )
    raw_base_url = safe_args.get("base_url")
    raw_api_key = safe_args.get("api_key")
    discover_meta = (
        safe_args.get("discover_meta") if isinstance(safe_args.get("discover_meta"), dict) else None
    )

    spec = ModelTestAiSuiteSpec(
        base_url=raw_base_url if isinstance(raw_base_url, str) else None,
        api_key=raw_api_key if isinstance(raw_api_key, str) else None,
        use_saved_key=bool(safe_args.get("use_saved_key", True)),
        protocol=protocol,
        models=models,
        checks=checks,
        rounds=rounds,
        cooldown_ms=cooldown_ms,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        title=title,
        badge=badge,
        discover_meta=discover_meta,
        discovered_profiles=_parse_discovered_profiles(safe_args.get("discovered_profiles")),
    )

    emitted_links: list[dict[str, Any]] = []

    async def _handle_case_done(case_link: dict[str, Any]) -> None:
        emitted_links.append(case_link)
        await _invoke_hook(on_case_done, case_link)

    try:
        initial = await manager.start_ai_multi_round_run(
            session,
            spec,
            on_case_done=_handle_case_done,
        )
    except ModelTestingConfigError as exc:
        return {
            "ok": False,
            "error": exc.code,
            "message": exc.message,
            "case_links": [],
        }

    run_id = initial["id"]
    await _invoke_hook(
        on_run_bound,
        {
            "scope": "model_testing",
            "history_id": run_id,
            "run_id": run_id,
            "title": initial.get("title"),
            "source": initial.get("source", "ai"),
            "badge": initial.get("badge") or DEFAULT_AI_BADGE,
            "status": initial.get("status", "running"),
            "total_rounds": initial.get("total_rounds", rounds),
            "cooldown_ms": initial.get("cooldown_ms", cooldown_ms),
        },
    )

    final_run = await manager.wait_for_completion(run_id)

    # 按持久化子用例顺序组装完整去重的 CaseLinkBlock 列表。
    case_links: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    for sc in final_run.get("sub_cases") or []:
        if not isinstance(sc, dict):
            continue
        cid = str(sc.get("case_id") or "")
        if not cid or cid in seen_case_ids:
            continue
        seen_case_ids.add(cid)
        case_links.append(
            build_case_link_block(
                run_id=run_id,
                case_id=cid,
                round_index=int(sc.get("round_index") or 1),
                label=str(sc.get("label") or cid),
                status=str(sc.get("status") or "unknown"),
            )
        )
    for link in emitted_links:
        cid = str(link.get("case_id") or "")
        if cid and cid not in seen_case_ids:
            seen_case_ids.add(cid)
            case_links.append(link)

    compact_models = [
        {
            "id": item.get("id"),
            "model_type": item.get("model_type"),
            "availability": (item.get("availability") or {}).get("status"),
            "latency_ms": (item.get("availability") or {}).get("latency_ms"),
            "tool_calling": (item.get("tool_calling") or {}).get("status"),
            "streaming": (item.get("streaming") or {}).get("status"),
            "ttft_ms": (item.get("streaming") or {}).get("ttft_ms"),
            "structured_output": (item.get("structured_output") or {}).get("status"),
        }
        for item in (final_run.get("models_snapshot") or [])
        if isinstance(item, dict)
    ]

    return {
        "ok": final_run.get("status") == "completed",
        "scope": "model_testing",
        "run_id": run_id,
        "history_id": run_id,
        "title": final_run.get("title"),
        "source": final_run.get("source"),
        "badge": final_run.get("badge"),
        "status": final_run.get("status"),
        "base_url": final_run.get("base_url"),
        "resolved_protocol": final_run.get("resolved_protocol"),
        "total_rounds": final_run.get("total_rounds"),
        "completed_rounds": final_run.get("completed_rounds"),
        "cooldown_ms": final_run.get("cooldown_ms"),
        "summary": final_run.get("summary"),
        "summary_text": final_run.get("summary_text"),
        "error_message": final_run.get("error_message"),
        "models": compact_models,
        "case_links": case_links,
    }


async def read_model_testing_sub_case(
    repo: ModelTestRepository,
    run_id: str,
    case_id: str,
) -> dict[str, Any]:
    """AI 工具处理器：只读查询指定模型测试任务中单个子用例的完整回放快照。"""
    clean_run_id = str(run_id or "").strip()
    clean_case_id = str(case_id or "").strip()
    if not clean_run_id or not clean_case_id:
        return {
            "ok": False,
            "error": "invalid_arguments",
            "message": "run_id 与 case_id 均不能为空",
            "run_id": clean_run_id,
            "case_id": clean_case_id,
        }
    run = await repo.get_run(clean_run_id)
    if run is None:
        return {
            "ok": False,
            "error": "not_found",
            "message": f"未找到模型测试任务 {clean_run_id}",
            "run_id": clean_run_id,
            "case_id": clean_case_id,
        }
    sub_case = await repo.get_sub_case(clean_run_id, clean_case_id)
    if sub_case is None:
        return {
            "ok": False,
            "error": "not_found",
            "message": f"未找到模型测试子用例 {clean_case_id}",
            "run_id": clean_run_id,
            "case_id": clean_case_id,
        }
    return {
        "ok": True,
        "scope": "model_testing",
        "run_id": clean_run_id,
        "history_id": clean_run_id,
        "run_title": run.get("title"),
        "source": run.get("source"),
        "badge": run.get("badge"),
        "sub_case": sub_case,
    }
