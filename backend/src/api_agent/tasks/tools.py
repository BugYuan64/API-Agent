"""Page-scoped AI assistant tools for Smart Group Testing (scope ``"tasks"``)."""

from __future__ import annotations

import inspect
import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api_agent.agent.service import AgentService
from api_agent.infrastructure.targets import redact_headers
from api_agent.llm.schemas import FunctionDefinition, ToolDefinition
from api_agent.tasks.generation import operation_key
from api_agent.tasks.planning import (
    MAX_TESTS_PER_URL,
    MIN_TESTS_PER_URL,
    TaskPlanningService,
    fill_catalog_title,
)
from api_agent.tasks.schemas import Case, Catalog, StartTask, TaskRunMetadata
from api_agent.tasks.service import DEFAULT_AI_BADGE, TaskManager

DEFAULT_TESTS_PER_URL = 3
DEFAULT_COOLDOWN_MS = 500
MIN_COOLDOWN_MS = 0
MAX_COOLDOWN_MS = 60_000
MIN_OBSERVATION_MS = 1_000
MAX_OBSERVATION_MS = 30_000
DEFAULT_OBSERVATION_MS = 15_000
MIN_WALL_TIME_SECONDS = 30
MAX_WALL_TIME_SECONDS = 3_600
DEFAULT_WALL_TIME_SECONDS = 600
MAX_SUMMARY_CHARS = 4_000
MAX_COMPACT_RESULTS = 100

_CASE_ID_RE = re.compile(r"^r(\d+):c(\d+)$")

CaseLinkTone = Literal["info", "success", "warning", "danger", "neutral"]


class CaseLinkBlock(BaseModel):
    """Canonical clickable sub-case hyperlink block emitted by AI test suite tools."""

    type: Literal["case_link"] = "case_link"
    scope: Literal["tasks"] = "tasks"
    history_id: str
    case_id: str
    round: int = Field(ge=1)
    label: str
    status: str
    tone: CaseLinkTone


class TasksAiSuiteSpec(BaseModel):
    """Validated parameters for running a Smart Group Testing AI suite."""

    model_config = ConfigDict(extra="ignore")

    origin: str = Field(min_length=1, max_length=2000)
    prompt_or_doc: str = Field(default="", max_length=524_288)
    title: str | None = Field(default=None, max_length=160)
    rounds: int = Field(default=1, ge=1, le=20)
    cooldown_ms: int = Field(default=DEFAULT_COOLDOWN_MS, ge=MIN_COOLDOWN_MS, le=MAX_COOLDOWN_MS)
    allow_writes: bool = True
    observation_ms: int = Field(
        default=DEFAULT_OBSERVATION_MS,
        ge=MIN_OBSERVATION_MS,
        le=MAX_OBSERVATION_MS,
    )
    wall_time_seconds: int = Field(
        default=DEFAULT_WALL_TIME_SECONDS,
        ge=MIN_WALL_TIME_SECONDS,
        le=MAX_WALL_TIME_SECONDS,
    )
    instructions: str | None = Field(default=None, max_length=2000)
    cases: list[Case] | None = Field(default=None, max_length=200)

    @field_validator("origin")
    @classmethod
    def clean_origin(cls, value: str) -> str:
        """Trim surrounding whitespace and reject blank origins."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("origin_required")
        return cleaned

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value: Any) -> Any:
        """Normalize optional titles to a single bounded line."""
        return Catalog.compact_title(value)

    @field_validator("instructions", mode="before")
    @classmethod
    def clean_instructions(cls, value: Any) -> str | None:
        """Normalize empty or whitespace-only instructions to None."""
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        cleaned = value.strip()
        return cleaned[:2000] if cleaned else None


TASKS_TOOL_DEFINITIONS: list[ToolDefinition] = [
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="run_tasks_ai_suite",
            description=(
                "在智能群测页生成或接收多接口测试用例，按页面配置的 tests_per_url"
                "（不可由 AI 覆盖）启动单条后台多轮测试任务（带「AI 测试」标识与"
                "每轮冷却 cooldown_ms），流式返回子用例链接块并汇总结果。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": (
                            "目标服务基础地址（如 http://127.0.0.1:8080 或含基础路径）。"
                        ),
                    },
                    "prompt_or_doc": {
                        "type": "string",
                        "description": (
                            "用户测试需求、API 文档片段（OpenAPI/Swagger/Markdown）"
                            "或风格与边界要求。"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "可选任务标题，将显示在智能群测历史列表中。",
                    },
                    "rounds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 1,
                        "description": "循环测试轮次（1~20，默认 1）。",
                    },
                    "cooldown_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 60000,
                        "description": (
                            "每轮之间的冷却时间（毫秒，0~60000；省略时沿用页面设置或 500）。"
                        ),
                    },
                    "allow_writes": {
                        "type": "boolean",
                        "default": True,
                        "description": "是否允许执行 POST/PUT/PATCH/DELETE 与 WebSocket 发送消息。",
                    },
                    "observation_ms": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 30000,
                        "default": 15000,
                        "description": "SSE/WebSocket 单用例观察时长（毫秒，1000~30000）。",
                    },
                    "wall_time_seconds": {
                        "type": "integer",
                        "minimum": 30,
                        "maximum": 3600,
                        "default": 600,
                        "description": "整套任务总超时上限（秒，30~3600）。",
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "附加给用例生成模型的具体约束（如边界值、异常入参、鉴权场景等）。"
                        ),
                    },
                    "cases": {
                        "type": "array",
                        "description": (
                            "可选结构化用例列表（AI 直接编写时使用）；"
                            "仍会严格按页面 tests_per_url 上限约束每个接口的用例数。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "protocol": {
                                    "type": "string",
                                    "enum": ["http", "sse", "websocket"],
                                },
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
                                "path": {"type": "string"},
                                "headers": {"type": "object"},
                                "body": {},
                                "messages": {"type": "array", "items": {"type": "string"}},
                                "assertions": {"type": "array", "items": {"type": "object"}},
                                "extract": {"type": "array", "items": {"type": "object"}},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["origin"],
            },
        ),
    ),
    ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="read_task_sub_case",
            description=(
                "按 task_id 与 case_id（或 case_index + round）只读读取智能群测"
                "任务中某条子用例的完整请求配置、断言结果、提取变量及响应证据，"
                "用于详情复盘与问答分析。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "智能群测任务历史记录 ID（history_id）。",
                    },
                    "case_id": {
                        "type": "string",
                        "description": "子用例标识（例如 r1:c0、r2:c1）。",
                    },
                    "case_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "可选单轮用例序号（未传 case_id 时配合 round 定位）。",
                    },
                    "round": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 1,
                        "description": "可选轮次编号（未传 case_id 时配合 case_index 定位）。",
                    },
                },
                "required": ["task_id"],
            },
        ),
    ),
]


def _extract_setting(settings_context: dict[str, Any] | None, *keys: str) -> Any:
    """Read a setting from either a flat settings mapping or a nested ``settings`` dict."""
    if not isinstance(settings_context, dict):
        return None
    for key in keys:
        if key in settings_context and settings_context[key] is not None:
            return settings_context[key]
    nested = settings_context.get("settings")
    if isinstance(nested, dict):
        for key in keys:
            if key in nested and nested[key] is not None:
                return nested[key]
    return None


def _coerce_bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """Coerce an optional non-boolean integer-like value and clamp to [minimum, maximum]."""
    if value is None or isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def resolve_tests_per_url(settings_context: dict[str, Any] | None) -> int:
    """Extract ``tests_per_url`` strictly from page settings (default 3, clamped 1..10)."""
    raw = _extract_setting(settings_context, "tests_per_url", "testsPerUrl")
    return _coerce_bounded_int(
        raw,
        default=DEFAULT_TESTS_PER_URL,
        minimum=MIN_TESTS_PER_URL,
        maximum=MAX_TESTS_PER_URL,
    )


def resolve_cooldown_ms(
    args: dict[str, Any] | None,
    settings_context: dict[str, Any] | None,
) -> int:
    """Resolve ``cooldown_ms`` from tool args, falling back to page settings and then 500ms."""
    raw: Any = None
    if isinstance(args, dict):
        if args.get("cooldown_ms") is not None:
            raw = args.get("cooldown_ms")
        elif args.get("cooldownMs") is not None:
            raw = args.get("cooldownMs")
    if raw is None:
        raw = _extract_setting(settings_context, "cooldown_ms", "cooldownMs")
    return _coerce_bounded_int(
        raw,
        default=DEFAULT_COOLDOWN_MS,
        minimum=MIN_COOLDOWN_MS,
        maximum=MAX_COOLDOWN_MS,
    )


def _status_tone(status: str) -> CaseLinkTone:
    """Map task case verdict status onto canonical UI badge tone."""
    if status == "passed":
        return "success"
    if status in {"failed", "error"}:
        return "danger"
    if status in {"unknown", "skipped", "permission_required"}:
        return "warning"
    return "neutral"


def build_task_case_link(
    task_id: str,
    result: dict[str, Any],
    *,
    total_rounds: int = 1,
) -> dict[str, Any]:
    """Build one canonical ``CaseLinkBlock`` dictionary for a Smart Group Testing sub-case."""
    round_num = max(1, int(result.get("round") or 1))
    case_index = int(result.get("case_index", result.get("index", 0)))
    case_id = str(result.get("case_id") or f"r{round_num}:c{case_index}")
    name = str(result.get("name") or f"用例 #{case_index + 1}").strip()
    if total_rounds > 1:
        label = f"第 {round_num} 轮 · #{case_index + 1} {name}"
    else:
        label = f"#{case_index + 1} {name}"
    status = str(result.get("status") or "unknown")
    return CaseLinkBlock(
        history_id=task_id,
        case_id=case_id,
        round=round_num,
        label=label[:160],
        status=status,
        tone=_status_tone(status),
    ).model_dump()


async def _invoke_callback(callback: Any, payload: dict[str, Any]) -> None:
    """Invoke a sync or async callback if supplied."""
    if callback is None:
        return
    outcome = callback(payload)
    if inspect.isawaitable(outcome):
        await outcome


def _prepare_spec(
    args: dict[str, Any] | TasksAiSuiteSpec,
    settings_context: dict[str, Any] | None,
) -> TasksAiSuiteSpec:
    """Normalize tool args with page-settings fallbacks and strip AI tests_per_url overrides."""
    raw = args.model_dump() if isinstance(args, TasksAiSuiteSpec) else dict(args or {})
    # Never allow AI arguments to override the user's configured tests_per_url setting.
    raw.pop("tests_per_url", None)
    raw.pop("testsPerUrl", None)

    if not raw.get("origin"):
        fallback_origin = _extract_setting(settings_context, "origin")
        if not fallback_origin and isinstance(settings_context, dict):
            req = settings_context.get("request")
            if isinstance(req, dict):
                fallback_origin = req.get("origin")
        if isinstance(fallback_origin, str) and fallback_origin.strip():
            raw["origin"] = fallback_origin.strip()

    raw["cooldown_ms"] = resolve_cooldown_ms(raw, settings_context)

    if raw.get("observation_ms") is None:
        obs = _extract_setting(settings_context, "observation_ms", "observationMs")
        if obs is not None:
            raw["observation_ms"] = _coerce_bounded_int(
                obs,
                default=DEFAULT_OBSERVATION_MS,
                minimum=MIN_OBSERVATION_MS,
                maximum=MAX_OBSERVATION_MS,
            )

    if raw.get("wall_time_seconds") is None:
        wall = _extract_setting(settings_context, "wall_time_seconds", "wallTimeSeconds")
        if wall is not None:
            raw["wall_time_seconds"] = _coerce_bounded_int(
                wall,
                default=DEFAULT_WALL_TIME_SECONDS,
                minimum=MIN_WALL_TIME_SECONDS,
                maximum=MAX_WALL_TIME_SECONDS,
            )

    return TasksAiSuiteSpec.model_validate(raw)


async def _build_explicit_catalog(
    spec: TasksAiSuiteSpec,
    planning_agent: Any,
    tests_per_url: int,
) -> Catalog:
    """Compile AI-drafted structured cases into a bounded Catalog honoring ``tests_per_url``."""
    per_op_counts: dict[tuple[Any, ...], int] = {}
    selected: list[Case] = []
    trimmed = 0
    for case in spec.cases or []:
        key = operation_key(case)
        seen = per_op_counts.get(key, 0)
        if seen >= tests_per_url:
            trimmed += 1
            continue
        per_op_counts[key] = seen + 1
        selected.append(
            case.model_copy(
                update={
                    "source": "generated",
                    "kind": case.kind or "test",
                }
            )
        )
    warnings: list[str] = []
    if trimmed:
        warnings.append(
            f"已按页面配置 tests_per_url={tests_per_url} 裁剪超出配额的 {trimmed} 条用例。"
        )
    catalog = Catalog(
        format="custom",
        title=spec.title,
        server_url=Catalog.safe_server_hint(spec.origin),
        cases=selected,
        warnings=warnings,
    )
    if not catalog.title:
        await fill_catalog_title(planning_agent, catalog)
    return catalog


def _summarize_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Count completed sub-case verdicts by canonical status bucket."""
    counts = {
        "passed": 0,
        "failed": 0,
        "error": 0,
        "unknown": 0,
        "skipped": 0,
        "permission_required": 0,
    }
    for item in results:
        status = str(item.get("status") or "unknown")
        if status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
    return counts


def _compact_case_overview(
    result: dict[str, Any],
    snapshot_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a compact per-case summary entry safe for LLM tool return payloads."""
    case_index = int(result.get("case_index", result.get("index", 0)))
    case_input = snapshot_cases[case_index] if 0 <= case_index < len(snapshot_cases) else {}
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    response = evidence.get("response") if isinstance(evidence.get("response"), dict) else {}
    failed_assertions = [
        {
            "kind": item.get("kind"),
            "pointer": item.get("pointer"),
            "expected": item.get("expected"),
            "actual": item.get("actual"),
            "passed": item.get("passed"),
        }
        for item in (result.get("assertions") or [])
        if item.get("passed") is not True
    ][:6]
    return {
        "case_id": result.get("case_id") or f"r{result.get('round', 1)}:c{case_index}",
        "round": int(result.get("round") or 1),
        "case_index": case_index,
        "name": result.get("name"),
        "protocol": result.get("protocol"),
        "method": case_input.get("method"),
        "path": case_input.get("path"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "http_status": response.get("status"),
        "failed_assertions": failed_assertions,
    }


async def run_tasks_ai_suite(
    manager: TaskManager,
    planning_agent: Any,
    task_agent: Any,
    args: dict[str, Any],
    *,
    settings_context: dict[str, Any] | None = None,
    base_catalog: Catalog | None = None,
    on_run_bound: Any = None,
    on_case_done: Any = None,
) -> dict[str, Any]:
    """Generate/validate cases, run one annotated multi-round task, and stream CaseLinkBlocks."""
    tests_per_url = resolve_tests_per_url(settings_context)
    spec = _prepare_spec(args, settings_context)

    if spec.cases:
        catalog = await _build_explicit_catalog(spec, planning_agent, tests_per_url)
    else:
        planner = TaskPlanningService()
        catalog = await planner.build_ai_catalog(
            planning_agent,
            origin=spec.origin,
            prompt_or_doc=spec.prompt_or_doc,
            tests_per_url=tests_per_url,
            base_catalog=base_catalog,
            instructions=spec.instructions,
        )
        if spec.title:
            catalog = catalog.model_copy(update={"title": Catalog.compact_title(spec.title)})

    effective_task_agent = (
        task_agent if task_agent is not None else AgentService(manager.settings, None)
    )
    start_request = StartTask(
        origin=spec.origin,
        catalog=catalog,
        allow_writes=spec.allow_writes,
        observation_ms=spec.observation_ms,
        wall_time_seconds=spec.wall_time_seconds,
        rounds=spec.rounds,
        cooldown_ms=spec.cooldown_ms,
        source="ai",
        badge=DEFAULT_AI_BADGE,
    )
    metadata = TaskRunMetadata(
        source="ai",
        badge=DEFAULT_AI_BADGE,
        tests_per_url=tests_per_url,
    )
    started = manager.start(start_request, effective_task_agent, metadata=metadata)
    task_id = started["id"]
    total_rounds = int(started.get("total_rounds") or spec.rounds)

    await _invoke_callback(
        on_run_bound,
        {
            "scope": "tasks",
            "history_id": task_id,
            "task_id": task_id,
            "run_id": task_id,
            "title": started.get("title"),
            "source": started.get("source", "ai"),
            "badge": started.get("badge", DEFAULT_AI_BADGE),
            "origin": started["snapshot"]["origin"],
            "status": started.get("status", "queued"),
            "total_rounds": total_rounds,
            "cooldown_ms": int(started.get("cooldown_ms", spec.cooldown_ms)),
            "tests_per_url": started.get("tests_per_url", tests_per_url),
            "planned_cases": len(started["snapshot"]["cases"]),
        },
    )

    seen_case_ids: set[str] = set()
    case_links: list[dict[str, Any]] = []

    async def handle_progress(frame: dict[str, Any]) -> None:
        for item in frame.get("results") or []:
            link = build_task_case_link(task_id, item, total_rounds=total_rounds)
            if link["case_id"] in seen_case_ids:
                continue
            seen_case_ids.add(link["case_id"])
            case_links.append(link)
            await _invoke_callback(on_case_done, link)

    final_record = await manager.wait_for_completion(task_id, on_progress=handle_progress)
    final_results = list(final_record.get("results") or [])
    for item in final_results:
        link = build_task_case_link(task_id, item, total_rounds=total_rounds)
        if link["case_id"] in seen_case_ids:
            continue
        seen_case_ids.add(link["case_id"])
        case_links.append(link)
        await _invoke_callback(on_case_done, link)

    snapshot = final_record.get("snapshot") or {}
    snapshot_cases = list(snapshot.get("cases") or [])
    summary_text = final_record.get("summary")
    return {
        "ok": final_record.get("status") == "completed",
        "scope": "tasks",
        "history_id": task_id,
        "task_id": task_id,
        "title": final_record.get("title"),
        "origin": snapshot.get("origin"),
        "source": final_record.get("source", "ai"),
        "badge": final_record.get("badge", DEFAULT_AI_BADGE),
        "status": final_record.get("status"),
        "stage": final_record.get("stage"),
        "error_kind": final_record.get("error_kind"),
        "tests_per_url": final_record.get("tests_per_url", tests_per_url),
        "total_rounds": total_rounds,
        "cooldown_ms": int(final_record.get("cooldown_ms", spec.cooldown_ms)),
        "planned_cases_per_round": len(snapshot_cases),
        "executed_cases": len(final_results),
        "counts": _summarize_counts(final_results),
        "summary": summary_text[:MAX_SUMMARY_CHARS] if isinstance(summary_text, str) else None,
        "warnings": list(snapshot.get("warnings") or [])[:20],
        "results": [
            _compact_case_overview(item, snapshot_cases)
            for item in final_results[:MAX_COMPACT_RESULTS]
        ],
        "case_links": case_links,
    }


def _match_sub_case_result(
    results: list[dict[str, Any]],
    *,
    case_id: str | None,
    case_index: int | None,
    round_num: int | None,
) -> tuple[dict[str, Any] | None, str | None, int | None, int | None]:
    """Locate a persisted sub-case result by ``case_id``, numeric index, or ``(round, index)``."""
    target_id = case_id.strip() if isinstance(case_id, str) and case_id.strip() else None
    parsed_round = round_num
    parsed_index = case_index

    if target_id is not None:
        for item in results:
            if item.get("case_id") == target_id:
                r = int(item.get("round") or 1)
                c = int(item.get("case_index", item.get("index", 0)))
                return item, target_id, r, c
        match = _CASE_ID_RE.fullmatch(target_id)
        if match:
            parsed_round = int(match.group(1))
            parsed_index = int(match.group(2))
        elif target_id.isdigit():
            numeric = int(target_id)
            for item in results:
                if item.get("execution_index") == numeric or item.get("index") == numeric:
                    r = int(item.get("round") or 1)
                    c = int(item.get("case_index", item.get("index", 0)))
                    resolved_id = str(item.get("case_id") or f"r{r}:c{c}")
                    return item, resolved_id, r, c
            parsed_round = 1
            parsed_index = numeric

    if parsed_index is not None:
        effective_round = parsed_round if parsed_round is not None else 1
        canonical_id = f"r{effective_round}:c{parsed_index}"
        for item in results:
            item_round = int(item.get("round") or 1)
            item_case_index = int(item.get("case_index", item.get("index", -1)))
            if item.get("case_id") == canonical_id or (
                item_round == effective_round and item_case_index == parsed_index
            ):
                return item, canonical_id, effective_round, parsed_index
        return None, canonical_id, effective_round, parsed_index

    return None, target_id, parsed_round, parsed_index


def _safe_request_snapshot(
    case_snapshot: dict[str, Any] | None,
    origin: str,
) -> dict[str, Any]:
    """Return a redacted request snapshot dictionary for read-only sub-case replay."""
    if not isinstance(case_snapshot, dict):
        return {"origin": origin, "url": origin}
    copied = deepcopy(case_snapshot)
    if isinstance(copied.get("headers"), dict):
        copied["headers"] = redact_headers(copied["headers"])
    path = str(copied.get("path") or "")
    full_url = f"{origin.rstrip('/')}{path}" if origin and path else origin or path
    return {
        "origin": origin,
        "url": full_url,
        **copied,
    }


async def read_task_sub_case(
    manager: TaskManager,
    task_id: str,
    case_id: str | None = None,
    *,
    case_index: int | None = None,
    round: int | None = None,
) -> dict[str, Any]:
    """Read a persisted Smart Group Testing sub-case snapshot (request + assertions + evidence)."""
    try:
        record = manager.get_task(task_id)
    except (FileNotFoundError, ValueError):
        return {
            "ok": False,
            "found": False,
            "error": "task_not_found",
            "scope": "tasks",
            "task_id": task_id,
            "case_id": case_id,
        }

    snapshot = record.get("snapshot") or {}
    origin = str(snapshot.get("origin") or "")
    snapshot_cases = list(snapshot.get("cases") or [])
    results = list(record.get("results") or [])
    total_rounds = int(record.get("total_rounds") or snapshot.get("total_rounds") or 1)

    matched, resolved_id, resolved_round, resolved_index = _match_sub_case_result(
        results,
        case_id=case_id,
        case_index=case_index,
        round_num=round,
    )

    if matched is not None:
        idx = int(matched.get("case_index", matched.get("index", 0)))
        case_input = snapshot_cases[idx] if 0 <= idx < len(snapshot_cases) else None
        return {
            "ok": True,
            "found": True,
            "scope": "tasks",
            "history_id": record["id"],
            "task_id": record["id"],
            "task_title": record.get("title"),
            "task_status": record.get("status"),
            "source": record.get("source", snapshot.get("source", "manual")),
            "badge": record.get("badge", snapshot.get("badge")),
            "total_rounds": total_rounds,
            "case_id": resolved_id or f"r{resolved_round or 1}:c{idx}",
            "round": int(matched.get("round") or resolved_round or 1),
            "case_index": idx,
            "execution_index": matched.get("execution_index", matched.get("index")),
            "name": matched.get("name") or (case_input or {}).get("name"),
            "protocol": matched.get("protocol") or (case_input or {}).get("protocol"),
            "status": matched.get("status"),
            "reason": matched.get("reason"),
            "request": _safe_request_snapshot(case_input, origin),
            "assertions": deepcopy(matched.get("assertions") or []),
            "extracted": deepcopy(matched.get("extracted") or []),
            "evidence": deepcopy(matched.get("evidence")),
            "case_link": build_task_case_link(record["id"], matched, total_rounds=total_rounds),
        }

    # Support inspecting an unexecuted planned case while a run is in-flight or partially cancelled.
    if (
        resolved_round is not None
        and resolved_index is not None
        and 1 <= resolved_round <= total_rounds
        and 0 <= resolved_index < len(snapshot_cases)
    ):
        case_input = snapshot_cases[resolved_index]
        pending_stub = {
            "case_id": f"r{resolved_round}:c{resolved_index}",
            "round": resolved_round,
            "case_index": resolved_index,
            "name": case_input.get("name"),
            "protocol": case_input.get("protocol"),
            "status": "pending",
        }
        return {
            "ok": True,
            "found": True,
            "scope": "tasks",
            "history_id": record["id"],
            "task_id": record["id"],
            "task_title": record.get("title"),
            "task_status": record.get("status"),
            "source": record.get("source", snapshot.get("source", "manual")),
            "badge": record.get("badge", snapshot.get("badge")),
            "total_rounds": total_rounds,
            "case_id": pending_stub["case_id"],
            "round": resolved_round,
            "case_index": resolved_index,
            "execution_index": None,
            "name": case_input.get("name"),
            "protocol": case_input.get("protocol"),
            "status": "pending",
            "reason": "case_not_executed_yet",
            "request": _safe_request_snapshot(case_input, origin),
            "assertions": [],
            "extracted": [],
            "evidence": None,
            "case_link": build_task_case_link(
                record["id"], pending_stub, total_rounds=total_rounds
            ),
        }

    available_ids = [
        str(item.get("case_id") or f"r{item.get('round', 1)}:c{item.get('case_index', idx)}")
        for idx, item in enumerate(results[:50])
    ]
    return {
        "ok": False,
        "found": False,
        "error": "case_not_found",
        "scope": "tasks",
        "task_id": record["id"],
        "case_id": resolved_id or case_id,
        "available_case_ids": available_ids,
    }


__all__ = [
    "CaseLinkBlock",
    "DEFAULT_COOLDOWN_MS",
    "DEFAULT_TESTS_PER_URL",
    "TASKS_TOOL_DEFINITIONS",
    "TasksAiSuiteSpec",
    "build_task_case_link",
    "read_task_sub_case",
    "resolve_cooldown_ms",
    "resolve_tests_per_url",
    "run_tasks_ai_suite",
]
