"""Data-only model outputs and independently bounded agent inputs."""

import json
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from api_agent.execution.schemas import (
    IDENTIFIER,
    Assertion,
    ExecutionPlan,
    Input,
    Method,
    RequestCase,
    RunResult,
    local_origin,
)
from api_agent.projects.schemas import AuthReference


class Scope(Input):
    origin: str
    allowed_methods: list[Method] = Field(min_length=1, max_length=7)
    path_templates: list[str] = Field(min_length=1, max_length=50)
    auth_ref: AuthReference | None = None
    request_timeout_ms: int = Field(default=3000, ge=50, le=10_000)
    timeout_ms: int = Field(default=30_000, ge=100, le=120_000)
    delay_ms: int = Field(default=50, ge=0, le=1000)

    _origin = field_validator("origin")(local_origin)

    def plan(self, requests: list[RequestCase]) -> ExecutionPlan:
        return ExecutionPlan(**self.model_dump(), requests=requests, max_requests=len(requests))


class ContractInput(Input):
    contract: str | dict[str, JsonValue]

    @field_validator("contract")
    @classmethod
    def bounded_contract(cls, value):
        if not value or len(json.dumps(value, ensure_ascii=False).encode()) > 131_072:
            raise ValueError("contract must be nonempty and at most 128 KiB")
        return value


class GenerateRequest(ContractInput):
    scope: Scope
    max_cases: int = Field(default=8, ge=1, le=50)


class CaseIntent(Input):
    case_id: str = Field(min_length=1, max_length=64)
    category: Literal["positive", "boundary", "negative", "auth"]
    basis: Literal["contract", "inferred"]
    rationale: str = Field(min_length=1, max_length=1000)
    source_quote: str | None = Field(default=None, max_length=1000)


class GeneratedCases(Input):
    requests: list[RequestCase] = Field(min_length=1, max_length=50)
    intents: list[CaseIntent] = Field(min_length=1, max_length=50)
    notes: list[str] = Field(default_factory=list, max_length=30)


class Generation(Input):
    plan: ExecutionPlan
    intents: list[CaseIntent]
    notes: list[str]


class Evidence(Input):
    """一条确定性证据：断言观测事实，由代码从计划与执行结果生成。

    作用：让模型和报告看到“期望什么、实际什么”，而不只是“通过没有”。
    实测教训：缺少 actual 时，模型无法区分“值不符”与“类型不符”
    （例如期望数字 400、目标返回字符串 "400"），会把推断错误当业务缺陷。
    ``actual`` 已在执行层脱敏并截断；``actual_present=False`` 表示没取到值，
    不能当作“值等于 null”。
    """

    id: str
    case_id: str
    status: str
    http_status: int | None = None
    error_kind: str | None = None
    assertion_index: int | None = None
    kind: str | None = None
    expected: JsonValue = None
    passed: bool | None = None
    message: str | None = None
    actual: JsonValue = None
    actual_present: bool | None = None
    actual_type: str | None = None
    actual_truncated: bool | None = None


class Hypothesis(Input):
    id: str = Field(pattern=IDENTIFIER)
    cause: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=30)
    next_check: str = Field(min_length=1, max_length=1000)


class Analysis(Input):
    summary: str = Field(min_length=1, max_length=4000)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=20)
    coverage_gaps: list[str] = Field(default_factory=list, max_length=30)


class Diagnosis(Input):
    evidence: list[Evidence]
    analysis: Analysis


class DiagnoseRequest(ContractInput):
    plan: ExecutionPlan
    result: RunResult
    round_index: int = Field(default=0, ge=0, le=100)


class DiagnoseExecutionRequest(ContractInput):
    """按已存执行记录诊断时的输入：只有契约，计划与结果由服务端取。

    实现：``Input`` 的 ``extra="forbid"`` 会让调用方传 ``plan`` 直接 422，
    从协议层堵住“用编辑器内容顶替历史计划”的路径。
    """


class Mutation(Input):
    parent_id: str
    hypothesis_id: str
    location: Literal["body", "query", "path"]
    pointer: str = Field(default="", max_length=250)
    action: Literal["set", "remove"] = "set"
    value: JsonValue = None
    assertions: list[Assertion] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)


class Proposal(Input):
    continue_testing: bool
    reason: str = Field(min_length=1, max_length=2000)
    mutations: list[Mutation] = Field(default_factory=list, max_length=50)


class AdaptRequest(ContractInput):
    plan: ExecutionPlan
    diagnosis: Diagnosis
    previous_requests: list[RequestCase] = Field(default_factory=list, max_length=500)
    remaining_requests: int = Field(ge=0, le=50)
    allow_mutating_followups: bool = False
    round_index: int = Field(default=1, ge=1, le=100)


class Adaptation(Input):
    continue_testing: bool
    reason: str
    plan: ExecutionPlan | None = None
    mutations: list[Mutation] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


class Limits(Input):
    target_requests: int = Field(default=16, ge=1, le=100)
    model_calls: int = Field(default=8, ge=1, le=30)
    max_rounds: int = Field(default=3, ge=1, le=10)
    wall_time_seconds: float = Field(default=120.0, ge=0.1, le=600.0)


class AgentRunRequest(GenerateRequest):
    limits: Limits = Field(default_factory=Limits)
    engine: str = Field(default="native", min_length=1, max_length=40)
    allow_mutating_followups: bool = False


class ReportCommentary(Input):
    overview: str = Field(min_length=1, max_length=4000)
    recommendations: list[str] = Field(default_factory=list, max_length=20)
