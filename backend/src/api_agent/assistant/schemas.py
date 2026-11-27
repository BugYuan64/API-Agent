"""Pydantic schemas for page-scoped AI assistant runs and persisted threads."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from api_agent.assistant.models import PageScope
from api_agent.infrastructure.api import ApiInput, ApiModel
from api_agent.tasks.chat import MAX_CURRENT_TEST_BYTES, ReviewMessage

MAX_ASSISTANT_MESSAGES = 20
MAX_ASSISTANT_SETTINGS_BYTES = 65_536


class AssistantRunStartRequest(ApiInput):
    """启动页面隔离后台 AI 助手任务的请求体。

    ``ephemeral_api_key`` 仅用于内存态 ``model_testing`` 工具执行，绝不记日志、不落库。
    """

    scope: PageScope
    history_id: str | None = Field(default=None, max_length=100)
    task_id: str | None = Field(default=None, max_length=100)
    messages: list[ReviewMessage] = Field(min_length=1, max_length=MAX_ASSISTANT_MESSAGES)
    current_test: dict[str, Any] | None = Field(default=None)
    settings: dict[str, Any] | None = Field(default=None)
    model: str | None = Field(default=None, min_length=1, max_length=120)
    base_catalog: dict[str, Any] | None = Field(default=None)
    ephemeral_api_key: str | None = Field(default=None, max_length=512)

    @field_validator("history_id", "task_id", "ephemeral_api_key", mode="before")
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("current_test")
    @classmethod
    def _bounded_current_test(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """限制前端上传的当前测试页快照大小，避免异常大包耗尽内存。"""
        if value is None:
            return None
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_CURRENT_TEST_BYTES:
            raise ValueError("current_test payload exceeds 64 KiB limit")
        return value

    @field_validator("settings")
    @classmethod
    def _bounded_settings(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_ASSISTANT_SETTINGS_BYTES:
            raise ValueError("settings payload exceeds 64 KiB limit")
        return value


class AssistantRunStartResponse(ApiModel):
    """受理后台 AI 助手运行后立即返回的句柄。"""

    run_id: str
    scope: PageScope
    history_id: str | None = None
    status: str


class AssistantActiveRunItem(ApiModel):
    """当前仍在后台执行的助手任务摘要项，供切页重连恢复流式展示。"""

    run_id: str
    scope: PageScope
    history_id: str | None = None
    previous_history_id: str | None = None
    status: str
    phase: str | None = None
    tool: str | None = None
    started_at: str | None = None
    child_run_id: str | None = None


class AssistantActiveRunsResponse(ApiModel):
    """当前活跃助手后台任务列表。"""

    items: list[AssistantActiveRunItem] = Field(default_factory=list)


class AssistantRunCancelResponse(ApiModel):
    """取消后台助手任务及其子测试任务后的状态响应。"""

    run_id: str
    scope: PageScope
    history_id: str | None = None
    status: str
    cancelled: bool
    child_run_id: str | None = None


class AssistantMessageItem(ApiModel):
    """线程中单条持久化消息及结构化渲染块（如子用例回放按钮）。"""

    id: str | None = None
    role: str
    content: str = ""
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    run_id: str | None = None
    created_at: str | None = None


class AssistantThreadResponse(ApiModel):
    """按 ``(scope, history_id)`` 绑定的助手对话线程快照及当前在跑 run_id。"""

    id: str | None = None
    scope: PageScope
    history_id: str
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    active_run_id: str | None = None
    messages: list[AssistantMessageItem] = Field(default_factory=list)


class AssistantThreadMigrateMessage(ApiInput):
    """迁移旧版客户端消息时的单条消息输入（容忍旧版多余展示字段）。"""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=64_000)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    run_id: str | None = Field(default=None, max_length=64)


class AssistantThreadMigrateRequest(ApiInput):
    """将旧版浏览器缓存对话幂等迁移至空线程的请求体。"""

    title: str | None = Field(default=None, max_length=200)
    messages: list[AssistantThreadMigrateMessage] = Field(default_factory=list, max_length=200)


__all__ = [
    "AssistantActiveRunItem",
    "AssistantActiveRunsResponse",
    "AssistantMessageItem",
    "AssistantRunCancelResponse",
    "AssistantRunStartRequest",
    "AssistantRunStartResponse",
    "AssistantThreadMigrateMessage",
    "AssistantThreadMigrateRequest",
    "AssistantThreadResponse",
]
