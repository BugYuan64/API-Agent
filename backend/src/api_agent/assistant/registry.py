"""Strict page-scoped AI assistant tool registry and dispatcher."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from api_agent.agent.service import AgentService
from api_agent.assistant.models import VALID_PAGE_SCOPES, PageScope
from api_agent.console.runner import ConsoleRunManager
from api_agent.console.tools import (
    CONSOLE_TOOL_DEFINITIONS,
    read_console_sub_case,
    run_console_ai_suite,
)
from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.targets import TargetError
from api_agent.llm.schemas import ToolDefinition
from api_agent.llm.testing_manager import ModelTestingConfigError, ModelTestManager
from api_agent.llm.tools import (
    MODEL_TESTING_TOOL_DEFINITIONS,
    discover_model_testing_models,
    read_model_testing_sub_case,
    run_model_testing_ai_suite,
)
from api_agent.streams.runner import StreamSuiteRunner
from api_agent.streams.tools import (
    WEBSOCKET_TOOL_DEFINITIONS,
    read_websocket_sub_case,
    run_websocket_ai_suite,
)
from api_agent.tasks.chat import READ_CURRENT_TEST_TOOL, build_current_test_tool_payload
from api_agent.tasks.schemas import Catalog
from api_agent.tasks.service import TaskManager
from api_agent.tasks.tools import (
    TASKS_TOOL_DEFINITIONS,
    read_task_sub_case,
    run_tasks_ai_suite,
)

logger = logging.getLogger("api_agent")

SUITE_STARTING_TOOLS: frozenset[str] = frozenset(
    {
        "run_console_ai_suite",
        "run_websocket_ai_suite",
        "run_tasks_ai_suite",
        "run_model_testing_ai_suite",
    }
)

SCOPE_TOOL_WHITELIST: dict[PageScope, frozenset[str]] = {
    "console": frozenset(
        {
            "read_current_test_result",
            "run_console_ai_suite",
            "read_console_sub_case",
        }
    ),
    "websocket": frozenset(
        {
            "read_current_test_result",
            "run_websocket_ai_suite",
            "read_websocket_sub_case",
        }
    ),
    "tasks": frozenset(
        {
            "read_current_test_result",
            "run_tasks_ai_suite",
            "read_task_sub_case",
        }
    ),
    "model_testing": frozenset(
        {
            "read_current_test_result",
            "discover_model_testing_models",
            "run_model_testing_ai_suite",
            "read_model_testing_sub_case",
        }
    ),
}

_SCOPE_TOOL_DEFINITIONS: dict[PageScope, tuple[ToolDefinition, ...]] = {
    "console": (READ_CURRENT_TEST_TOOL, *CONSOLE_TOOL_DEFINITIONS),
    "websocket": (READ_CURRENT_TEST_TOOL, *WEBSOCKET_TOOL_DEFINITIONS),
    "tasks": (READ_CURRENT_TEST_TOOL, *TASKS_TOOL_DEFINITIONS),
    "model_testing": (READ_CURRENT_TEST_TOOL, *MODEL_TESTING_TOOL_DEFINITIONS),
}


@dataclass(frozen=True)
class RegisteredToolSpec:
    """单个受控工具的元数据声明（含是否启动新测试任务标记）。"""

    name: str
    definition: ToolDefinition
    starts_new_run: bool = False


@dataclass
class ToolExecutionContext:
    """单次助手后台运行期间传给工具分发器的执行上下文（不落库）。"""

    scope: PageScope
    run_id: str
    agent: AgentService
    generation_agent: AgentService | None = None
    current_test: dict[str, Any] | None = None
    history_id: str | None = None
    task_id: str | None = None
    settings_context: dict[str, Any] | None = None
    base_catalog: dict[str, Any] | None = None
    ephemeral_api_key: str | None = None
    on_run_bound: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    on_case_done: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
    session_factory: Callable[[], Any] | None = None
    active_child_run_id: str | None = None
    active_child_scope: PageScope | None = None

    def clear_ephemeral_secrets(self) -> None:
        """立刻清空上下文中暂存的内存态临时凭证。"""
        self.ephemeral_api_key = None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _invoke_hook(
    callback: Callable[[dict[str, Any]], Awaitable[None] | None] | None,
    payload: dict[str, Any],
) -> None:
    if callback is None:
        return
    outcome = callback(payload)
    if inspect.isawaitable(outcome):
        await outcome


def _scrub_error_message(message: str, ctx: ToolExecutionContext) -> str:
    cleaned = ctx.agent.context.text(message) if message else ""
    if ctx.ephemeral_api_key and len(ctx.ephemeral_api_key) >= 4:
        cleaned = cleaned.replace(ctx.ephemeral_api_key, "[REDACTED]")
    return cleaned[:400]


def _merge_model_testing_settings(
    args: dict[str, Any],
    settings_context: dict[str, Any] | None,
    *,
    include_cooldown: bool,
) -> dict[str, Any]:
    """合并模型测试页上下文默认值（仅补缺，不覆盖工具显式参数，绝不写入密钥）。"""
    merged = dict(args)
    if not isinstance(settings_context, dict):
        return merged

    if not merged.get("base_url"):
        fallback_url = settings_context.get("base_url") or settings_context.get("baseUrl")
        if isinstance(fallback_url, str) and fallback_url.strip():
            merged["base_url"] = fallback_url.strip()

    if not merged.get("protocol"):
        fallback_proto = settings_context.get("protocol")
        if isinstance(fallback_proto, str) and fallback_proto.strip():
            merged["protocol"] = fallback_proto.strip()

    if "use_saved_key" not in merged:
        raw_saved = settings_context.get("use_saved_key")
        if raw_saved is None:
            raw_saved = settings_context.get("useSavedKey")
        if isinstance(raw_saved, bool):
            merged["use_saved_key"] = raw_saved

    if include_cooldown and merged.get("cooldown_ms") is None:
        raw_cd = settings_context.get("cooldown_ms")
        if raw_cd is None:
            raw_cd = settings_context.get("cooldownMs")
        if raw_cd is not None:
            merged["cooldown_ms"] = raw_cd

    return merged


class AssistantToolRegistry:
    """强制页面作用域白名单隔离的 AI 助手工具注册表与调度器。"""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        console_runs: ConsoleRunManager,
        stream_suites: StreamSuiteRunner,
        tasks: TaskManager,
        model_tests: ModelTestManager,
    ) -> None:
        self.database = database
        self.settings = settings
        self.console_runs = console_runs
        self.stream_suites = stream_suites
        self.tasks = tasks
        self.model_tests = model_tests

        self._specs_by_scope: dict[PageScope, list[RegisteredToolSpec]] = {}
        for scope, definitions in _SCOPE_TOOL_DEFINITIONS.items():
            allowed = SCOPE_TOOL_WHITELIST[scope]
            specs: list[RegisteredToolSpec] = []
            for definition in definitions:
                name = definition.function.name
                if name not in allowed:
                    continue
                specs.append(
                    RegisteredToolSpec(
                        name=name,
                        definition=definition,
                        starts_new_run=name in SUITE_STARTING_TOOLS,
                    )
                )
            self._specs_by_scope[scope] = specs

    def tools_for_scope(self, scope: PageScope) -> list[ToolDefinition]:
        """仅返回目标页面作用域白名单内的工具定义列表。"""
        if scope not in VALID_PAGE_SCOPES:
            raise ValueError(f"Unsupported assistant scope: {scope!r}")
        return [spec.definition for spec in self._specs_by_scope[scope]]

    def is_allowed(self, scope: PageScope, tool_name: str) -> bool:
        """校验 ``tool_name`` 是否属于 ``scope`` 的显式白名单。"""
        allowed = SCOPE_TOOL_WHITELIST.get(scope)
        return allowed is not None and tool_name in allowed

    def starts_new_run(self, tool_name: str) -> bool:
        """判断该工具是否会启动新的域测试运行并触发历史绑定/分叉。"""
        return tool_name in SUITE_STARTING_TOOLS

    async def cancel_child_run(self, scope: PageScope, child_run_id: str) -> None:
        """将助手取消信号安全传播到当前正在运行的域子测试任务。"""
        clean_id = str(child_run_id or "").strip()
        if not clean_id:
            return
        try:
            if scope == "console":
                await self.console_runs.cancel(clean_id)
            elif scope == "websocket":
                await self.stream_suites.cancel(clean_id)
            elif scope == "tasks":
                await self.tasks.cancel(clean_id)
            elif scope == "model_testing":
                await self.model_tests.cancel(clean_id)
        except Exception:  # noqa: BLE001 - 子任务可能已终态或已被清理
            logger.debug(
                "Child run cancellation ignored for scope=%s child_run_id=%s",
                scope,
                clean_id,
                exc_info=True,
            )

    def _resolve_task_record(self, ctx: ToolExecutionContext) -> dict[str, Any] | None:
        target_task_id = ctx.task_id or (ctx.history_id if ctx.scope == "tasks" else None)
        if not target_task_id:
            return None
        try:
            return self.tasks.get_task(target_task_id)
        except (FileNotFoundError, ValueError):
            return None

    async def dispatch(
        self,
        scope: PageScope,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        """执行二次白名单鉴权并将受控工具路由至对应领域处理器。"""
        # 二次防线：无论上游模型请求调用什么工具，先校验页面作用域白名单。
        if not self.is_allowed(scope, tool_name):
            return {
                "error": "tool_not_allowed_for_scope",
                "scope": scope,
                "tool": tool_name,
            }

        safe_args = dict(arguments) if isinstance(arguments, dict) else {}

        async def _handle_run_bound(bound_payload: dict[str, Any]) -> None:
            child_id = (
                bound_payload.get("run_id")
                or bound_payload.get("history_id")
                or bound_payload.get("task_id")
                or bound_payload.get("id")
            )
            if isinstance(child_id, str) and child_id.strip():
                ctx.active_child_run_id = child_id.strip()
                ctx.active_child_scope = scope
            await _invoke_hook(ctx.on_run_bound, bound_payload)

        try:
            if tool_name == "read_current_test_result":
                include_body = bool(safe_args.get("include_body", True))
                task_record = self._resolve_task_record(ctx)
                return build_current_test_tool_payload(
                    ctx.current_test,
                    task_record,
                    ctx.agent.context,
                    include_body=include_body,
                )

            if tool_name == "run_console_ai_suite":
                return await run_console_ai_suite(
                    self.console_runs,
                    safe_args,
                    on_case_done=ctx.on_case_done,
                    on_run_bound=_handle_run_bound,
                    settings_context=ctx.settings_context,
                )

            if tool_name == "read_console_sub_case":
                record_id = str(
                    safe_args.get("record_id")
                    or safe_args.get("history_id")
                    or ctx.history_id
                    or ""
                ).strip()
                case_id = str(safe_args.get("case_id") or "").strip()
                return await read_console_sub_case(self.console_runs.repo, record_id, case_id)

            if tool_name == "run_websocket_ai_suite":
                return await run_websocket_ai_suite(
                    self.stream_suites,
                    safe_args,
                    settings_context=ctx.settings_context,
                    on_case_done=ctx.on_case_done,
                    on_run_bound=_handle_run_bound,
                )

            if tool_name == "read_websocket_sub_case":
                parent_id = str(
                    safe_args.get("parent_id")
                    or safe_args.get("history_id")
                    or ctx.history_id
                    or ""
                ).strip()
                case_id = str(safe_args.get("case_id") or "").strip()
                return await read_websocket_sub_case(self.stream_suites.repo, parent_id, case_id)

            if tool_name == "run_tasks_ai_suite":
                validated_catalog = (
                    Catalog.model_validate(ctx.base_catalog)
                    if ctx.base_catalog is not None
                    else None
                )
                planning_agent = ctx.generation_agent or ctx.agent
                return await run_tasks_ai_suite(
                    self.tasks,
                    planning_agent,
                    ctx.agent,
                    safe_args,
                    settings_context=ctx.settings_context,
                    base_catalog=validated_catalog,
                    on_run_bound=_handle_run_bound,
                    on_case_done=ctx.on_case_done,
                )

            if tool_name == "read_task_sub_case":
                task_id = str(
                    safe_args.get("task_id")
                    or safe_args.get("history_id")
                    or ctx.task_id
                    or ctx.history_id
                    or ""
                ).strip()
                raw_case_id = safe_args.get("case_id")
                case_id = str(raw_case_id).strip() if raw_case_id is not None else None
                return await read_task_sub_case(
                    self.tasks,
                    task_id,
                    case_id=case_id or None,
                    case_index=_coerce_optional_int(safe_args.get("case_index")),
                    round=_coerce_optional_int(safe_args.get("round")),
                )

            if tool_name == "discover_model_testing_models":
                return await self._dispatch_model_testing_discover(safe_args, ctx)

            if tool_name == "run_model_testing_ai_suite":
                return await self._dispatch_model_testing_suite(safe_args, ctx, _handle_run_bound)

            if tool_name == "read_model_testing_sub_case":
                run_id = str(
                    safe_args.get("run_id") or safe_args.get("history_id") or ctx.history_id or ""
                ).strip()
                case_id = str(safe_args.get("case_id") or "").strip()
                return await read_model_testing_sub_case(self.model_tests.repo, run_id, case_id)

            return {
                "error": "tool_not_allowed_for_scope",
                "scope": scope,
                "tool": tool_name,
            }
        except ModelTestingConfigError as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": _scrub_error_message(exc.message, ctx),
            }
        except TargetError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "message": "目标地址未通过本地安全策略校验",
            }
        except (ValidationError, ValueError, TypeError) as exc:
            return {
                "ok": False,
                "error": "invalid_tool_arguments",
                "message": _scrub_error_message(str(exc), ctx),
            }

    async def _dispatch_model_testing_discover(
        self,
        safe_args: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        tool_args = _merge_model_testing_settings(
            safe_args, ctx.settings_context, include_cooldown=False
        )
        injected = False
        if not tool_args.get("api_key") and ctx.ephemeral_api_key:
            tool_args["api_key"] = ctx.ephemeral_api_key
            injected = True
        session_cm = (
            ctx.session_factory() if ctx.session_factory is not None else self.database.session()
        )
        try:
            async with session_cm as session:
                return await discover_model_testing_models(session, self.settings, tool_args)
        finally:
            if injected:
                tool_args.pop("api_key", None)

    async def _dispatch_model_testing_suite(
        self,
        safe_args: dict[str, Any],
        ctx: ToolExecutionContext,
        on_run_bound: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        tool_args = _merge_model_testing_settings(
            safe_args, ctx.settings_context, include_cooldown=True
        )
        injected = False
        if not tool_args.get("api_key") and ctx.ephemeral_api_key:
            tool_args["api_key"] = ctx.ephemeral_api_key
            injected = True
        session_cm = (
            ctx.session_factory() if ctx.session_factory is not None else self.database.session()
        )
        try:
            async with session_cm as session:

                async def _bound_and_release(bound_payload: dict[str, Any]) -> None:
                    await session.rollback()
                    await on_run_bound(bound_payload)

                return await run_model_testing_ai_suite(
                    self.model_tests,
                    session,
                    tool_args,
                    on_case_done=ctx.on_case_done,
                    on_run_bound=_bound_and_release,
                )
        finally:
            if injected:
                tool_args.pop("api_key", None)


__all__ = [
    "AssistantToolRegistry",
    "RegisteredToolSpec",
    "SCOPE_TOOL_WHITELIST",
    "SUITE_STARTING_TOOLS",
    "ToolExecutionContext",
]
