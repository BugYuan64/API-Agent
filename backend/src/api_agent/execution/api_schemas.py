"""HTTP contracts for running a plan and reviewing earlier executions."""

from pydantic import Field

from api_agent.execution.schemas import CaseResult
from api_agent.infrastructure.api import ApiInput, ApiModel, UtcDatetime
from api_agent.infrastructure.outcomes import OutcomeFields


class ExecutionRunRequest(ApiInput):
    """把一份受控执行计划交给执行引擎运行。

    实现：``plan`` 保持原始对象形态，结构与范围校验全部交给
    ``ExecutionService``——在这里重复声明字段会让两处契约慢慢分叉。
    ``engine`` 选择执行驱动（``native`` 原生 Python / ``postman`` Postman CLI），
    留空时由服务端使用默认驱动，未知名称在解析阶段即被拒。
    """

    plan: dict
    label: str = Field(default="", max_length=80)
    project_id: str | None = None
    engine: str | None = Field(default=None, max_length=32)


class ExecutionHistoryResponse(ApiModel, OutcomeFields):
    """一次执行的摘要与逐用例结果。

    实现：``executions`` 原样保留每条用例的状态、HTTP 状态、耗时与断言明细，
    使界面能一直展开到“第几条断言、什么类型、是否通过”。
    """

    id: str
    project_id: str | None
    label: str
    engine: str
    status: str
    error_kind: str | None
    duration_ms: float
    reserved_requests: int
    observed_attempts: int
    unknown_requests: int
    executions: list[CaseResult]
    # 执行时冻结的脱敏原计划与指纹；旧记录为 None，
    # 界面据此禁用「按本次执行诊断」并提示需要重新执行。
    plan: dict | None = None
    plan_sha256: str | None = None
    created_at: UtcDatetime
