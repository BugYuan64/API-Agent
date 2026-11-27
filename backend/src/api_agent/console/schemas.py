"""Response and specification contracts for console HTTP requests, runs, and AI suites."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api_agent.infrastructure.api import ApiModel, UtcDatetime
from api_agent.infrastructure.http import MAX_BODY_CHARS
from api_agent.infrastructure.outcomes import OutcomeFields
from api_agent.infrastructure.targets import validate_headers

HttpMethod = Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
BodyMode = Literal["none", "json", "text", "form"]


class ConsoleSubCaseResponse(ApiModel, OutcomeFields):
    """AI 多轮接口测试中单个子用例的脱敏只读回放快照。"""

    id: str
    parent_id: str
    round_index: int = 1
    case_index: int = 0
    label: str = ""
    method: str
    url: str
    request_headers: dict[str, Any] = Field(default_factory=dict)
    body_mode: str = "none"
    request_body: str | None = None
    ok: bool = False
    status_code: int | None = None
    elapsed_ms: int | float | None = None
    error_kind: str | None = None
    error_message: str | None = None
    response_headers: dict[str, Any] = Field(default_factory=dict)
    response_preview: str | None = None
    response_bytes: int = 0
    http_version: str | None = None
    response_reason: str | None = None
    redirects: list[dict[str, Any]] = Field(default_factory=list)
    sse: dict[str, Any] | None = None
    assertion_summary: dict[str, Any] | None = None
    created_at: UtcDatetime


class ConsoleHistoryResponse(ApiModel, OutcomeFields):
    """一条控制台请求的历史摘要。

    实现：头已脱敏、正文只留预览，``response_bytes`` 保留真实长度，
    让界面能说清“原文多大、这里只留了多少”。列表响应不展开 ``request_body``。
    """

    id: str
    project_id: str | None = None
    method: str
    url: str
    request_headers: dict[str, Any] = Field(default_factory=dict)
    body_mode: str = "none"
    body_preview: str | None = None
    ok: bool = False
    status_code: int | None = None
    elapsed_ms: float = 0
    error_kind: str | None = None
    error_message: str | None = None
    response_headers: dict[str, Any] = Field(default_factory=dict)
    response_preview: str | None = None
    response_bytes: int = 0
    source: str = "manual"
    title: str | None = None
    badge: str | None = None
    status: str = "completed"
    summary: str | None = None
    total_rounds: int = 1
    cooldown_ms: int = 500
    sub_case_count: int = 0
    http_version: str | None = None
    response_reason: str | None = None
    created_at: UtcDatetime


class ConsoleHistoryDetail(ConsoleHistoryResponse):
    """Include the replay body, redirects, SSE snapshot, and ordered sub-cases when opened."""

    request_body: str | None = None
    redirects: list[dict[str, Any]] = Field(default_factory=list)
    sse: dict[str, Any] | None = None
    sub_cases: list[ConsoleSubCaseResponse] = Field(default_factory=list)


class ConsoleRunSummary(ApiModel, OutcomeFields):
    """后台控制台单次请求或 AI 多轮套件的运行摘要（用于启动响应与跨页重连）。"""

    id: str
    source: str = "manual"
    title: str | None = None
    badge: str | None = None
    status: str = "running"
    stage: str = "executing"
    method: str = "GET"
    url: str = ""
    ok: bool = False
    status_code: int | None = None
    elapsed_ms: float = 0
    error_kind: str | None = None
    error_message: str | None = None
    total_rounds: int = 1
    current_round: int = 0
    total_cases: int = 1
    completed_cases: int = 0
    cooldown_ms: int = 500
    cooldown_remaining_ms: int | None = None
    summary: str | None = None
    created_at: UtcDatetime | None = None


class ConsoleActiveRunsResponse(ApiModel):
    """当前仍在后台执行的控制台运行列表。"""

    items: list[ConsoleRunSummary] = Field(default_factory=list)


class ConsoleAiCaseSpec(BaseModel):
    """AI 接口测试套件中的单条测试用例规格。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    label: str = Field(default="", max_length=200)
    method: HttpMethod | None = None
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    body_mode: BodyMode | None = None
    body: str | None = Field(default=None, max_length=MAX_BODY_CHARS)
    timeout_ms: int | None = Field(default=None, ge=100, le=60_000)
    follow_redirects: bool | None = None
    sse_max_events: int | None = Field(default=None, ge=1, le=1000)
    sse_duration_ms: int | None = Field(default=None, ge=100, le=30_000)
    expected_status: int | list[int] | None = None
    expect_contains: str | None = Field(default=None, max_length=500)
    round_index: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="before")
    @classmethod
    def normalize_case_input(cls, data: Any) -> Any:
        """容错规范化 LLM 工具参数（如 name->label、小写方法、结构化 JSON body）。"""
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if not payload.get("label") and payload.get("name"):
            payload["label"] = str(payload["name"])[:200]
        if isinstance(payload.get("method"), str):
            payload["method"] = payload["method"].strip().upper()
        raw_body = payload.get("body")
        if raw_body is not None and not isinstance(raw_body, str):
            payload["body"] = json.dumps(raw_body, ensure_ascii=False)
            if not payload.get("body_mode"):
                payload["body_mode"] = "json"
        if isinstance(payload.get("headers"), dict):
            payload["headers"] = {str(k): str(v) for k, v in payload["headers"].items()}
        return payload

    @field_validator("headers")
    @classmethod
    def check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        validate_headers(value)
        return value


class ConsoleAiSuiteSpec(BaseModel):
    """AI 多轮接口测试套件规格。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=200)
    badge: str | None = Field(default="AI 测试", max_length=40)
    url: str | None = Field(default=None, max_length=2000)
    method: HttpMethod = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    rounds: int = Field(default=1, ge=1, le=20)
    cooldown_ms: int = Field(default=500, ge=0, le=60_000)
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    follow_redirects: bool = False
    sse_max_events: int = Field(default=50, ge=1, le=1000)
    sse_duration_ms: int = Field(default=3000, ge=100, le=30_000)
    cases: list[ConsoleAiCaseSpec] = Field(min_length=1, max_length=50)
    project_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_suite_input(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        if isinstance(payload.get("method"), str):
            payload["method"] = payload["method"].strip().upper()
        if isinstance(payload.get("headers"), dict):
            payload["headers"] = {str(k): str(v) for k, v in payload["headers"].items()}
        return payload

    @field_validator("headers")
    @classmethod
    def check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        validate_headers(value)
        return value
