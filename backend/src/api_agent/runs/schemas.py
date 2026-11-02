"""Simulation-run management schemas; no target or model credentials."""

from typing import Literal

from pydantic import Field, model_validator

from api_agent.infrastructure.api import ApiInput, ApiModel, UtcDatetime


class SimulationAction(ApiInput):
    """单个模拟动作：纯本地、无副作用的“假请求”。

    实现：``duration_ms`` 上限 10 秒，避免单个动作长时间占住 worker；
    ``outcome`` 只能取 completed/failed，让失败路径可被确定性地演练。
    """

    name: str = Field(min_length=1, max_length=80)
    duration_ms: int = Field(default=0, ge=0, le=10_000)
    outcome: Literal["completed", "failed"] = "completed"


class ResourceLimits(ApiInput):
    """一次运行允许消耗的资源上限。

    实现：``max_actions`` 限制动作次数（1..1000），
    ``work_timeout_ms`` 限制整次运行的挂钟时长（0.1s..15min），
    两者都在运行创建时冻结进快照，事后不可修改。
    """

    max_actions: int = Field(default=100, ge=1, le=1000)
    work_timeout_ms: int = Field(default=60_000, ge=100, le=900_000)


class RunCreate(ApiInput):
    """创建运行的请求体。

    实现：``actions`` 最多 1000 条（字段级上限），随后由模型级校验
    再与 ``resource_limits.max_actions`` 对齐——两个上限都要满足。
    """

    project_id: str
    actions: list[SimulationAction] = Field(default_factory=list, max_length=1000)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    random_seed: int = 0

    @model_validator(mode="after")
    def action_count_within_budget(self):
        """校验动作数量不超过本次运行声明的预算。

        实现：在字段级校验之后做跨字段比较，
        防止“字段里写了 10 条动作，但 max_actions 只给了 3”这种自相矛盾的请求
        被接受后在 worker 里才以“预算耗尽”收场。
        """
        if len(self.actions) > self.resource_limits.max_actions:
            raise ValueError("action count exceeds resource_limits.max_actions")
        return self


class RunSnapshotResponse(ApiModel):
    id: str
    project_id: str
    run_mode: str
    generator_version: str
    actions: list[dict]
    resource_limits: dict
    environment_version_id: str | None
    specification_id: str | None
    rule_set_id: str | None
    chain_template_id: str | None
    random_seed: int
    created_at: UtcDatetime


class RunResponse(ApiModel):
    id: str
    snapshot_id: str
    run_mode: str
    configured_llm_mode: str
    actual_llm_status: str
    degradation_reason: str | None
    status: str
    stop_reason: str | None
    cancel_requested_at: UtcDatetime | None
    deadline: UtcDatetime | None
    started_at: UtcDatetime | None
    ended_at: UtcDatetime | None
    created_at: UtcDatetime
    cleanup_status: str
    workflow_error: str | None
    current_action: int
    action_count: int


class RunEventResponse(ApiModel):
    id: str
    run_id: str
    sequence: int
    node: str
    event_type: str
    status: str
    tool_name: str | None
    input_summary: dict
    observation_summary: dict
    decision_reason: str | None
    evidence_ids: list[str]
    budget_summary: dict
    created_at: UtcDatetime


class BudgetEntryResponse(ApiModel):
    id: str
    run_id: str
    resource: str
    action: str
    amount: int
    status: str
    related_id: str | None
    created_at: UtcDatetime


class DispatchAttemptResponse(ApiModel):
    id: str
    run_id: str
    kind: str
    sequence: int
    status: str
    error_kind: str | None
    started_at: UtcDatetime | None
    ended_at: UtcDatetime | None
    created_at: UtcDatetime
