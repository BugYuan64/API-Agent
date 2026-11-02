"""Persistent simulation runs, orchestration events and resource ledgers."""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin


class RunSnapshot(IdMixin, CreatedAtMixin, Base):
    """运行创建时冻结的输入快照，之后不再修改。

    作用：让一次运行可精确复现——即使项目配置后续被改，
    这次运行用的动作序列、资源上限和随机种子仍原样保留。
    实现：``actions``/``resource_limits`` 用 JSON 存（结构简单、不参与查询）；
    ``environment_version_id`` 等字段预留为后续阶段挂接环境版本、规则集等引用，
    现在允许为 NULL。``run_mode``/``generator_version`` 记录生成方式，便于结果溯源。
    """

    __tablename__ = "run_snapshots"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    run_mode: Mapped[str] = mapped_column(String(30), default="simulation")
    generator_version: Mapped[str] = mapped_column(String(80), default="simulation-v1")
    actions: Mapped[list[dict]] = mapped_column(JSON)
    resource_limits: Mapped[dict] = mapped_column(JSON)
    environment_version_id: Mapped[str | None] = mapped_column(String(36))
    specification_id: Mapped[str | None] = mapped_column(String(36))
    rule_set_id: Mapped[str | None] = mapped_column(String(36))
    chain_template_id: Mapped[str | None] = mapped_column(String(36))
    random_seed: Mapped[int] = mapped_column(Integer, default=0)


class Run(IdMixin, CreatedAtMixin, Base):
    """一次运行的生命周期记录。

    作用：集中持有状态机字段（status/stop_reason）、时间戳（started_at/ended_at/deadline）、
    进度（current_action/action_count）和运行期差错（workflow_error）。
    实现：``status`` 建索引以加速 worker 的“查 queued”轮询；
    模型模式分两列记录——``configured_llm_mode`` 是配置值，
    ``actual_llm_status`` 是实际使用情况，二者分离才能如实表达“降级”。
    ``snapshot`` 关系用 ``lazy="selectin"`` 预加载，避免异步会话里的隐式懒加载。
    """

    __tablename__ = "runs"

    snapshot_id: Mapped[str] = mapped_column(ForeignKey("run_snapshots.id", ondelete="RESTRICT"))
    run_mode: Mapped[str] = mapped_column(String(30), default="simulation")
    configured_llm_mode: Mapped[str] = mapped_column(String(20), default="disabled")
    actual_llm_status: Mapped[str] = mapped_column(String(20), default="not_applicable")
    degradation_reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(40))
    cancel_requested_at: Mapped[datetime | None]
    deadline: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    cleanup_status: Mapped[str] = mapped_column(String(20), default="not_required")
    workflow_error: Mapped[str | None] = mapped_column(String(500))
    current_action: Mapped[int] = mapped_column(Integer, default=0)
    action_count: Mapped[int] = mapped_column(Integer)

    snapshot: Mapped[RunSnapshot] = relationship(lazy="selectin")


class RunEvent(IdMixin, CreatedAtMixin, Base):
    """运行过程中的单条可审计事件。

    作用：把“谁在哪个节点做了什么、看到了什么、为什么这样决策、花了多少预算”
    逐条留痕，构成对外可展示的执行时间线。
    实现：``(run_id, sequence)`` 唯一约束保证同一运行的事件序号不重复；
    ``sequence`` 由仓储在插入时取 ``MAX+1`` 生成；
    ``input_summary``/``observation_summary``/``budget_summary`` 默认空 dict，
    让读取方无需处理 NULL。
    """

    __tablename__ = "run_events"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    node: Mapped[str] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    tool_name: Mapped[str | None] = mapped_column(String(80))
    input_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    observation_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    budget_summary: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (UniqueConstraint("run_id", "sequence"),)


class BudgetLedger(IdMixin, CreatedAtMixin, Base):
    """只追加的预算账本条目（预留 / 消耗 / 释放）。

    作用：用“事件记账”代替“计数字段”，既不丢历史又能随时重算出余额。
    实现：每行记录一次动作（``action``）及数量；``related_id`` 指向对应的
    ``dispatch_attempts.id``，用于把“预留”和后续“消耗/释放”配对结算。
    """

    __tablename__ = "budget_ledger"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    resource: Mapped[str] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    related_id: Mapped[str | None] = mapped_column(String(36))


class DispatchAttempt(IdMixin, CreatedAtMixin, Base):
    """一次动作派发的尝试记录。

    作用：区分“开始过并有了结论”与“根本没派发”以及“进程中断导致结果未知”，
    这三种情况在预算和重试语义上完全不同。
    实现：``(run_id, sequence)`` 唯一约束保证每个动作序号只对应一次尝试；
    ``error_kind`` 用来标注失败/跳过/中断的具体原因。
    """

    __tablename__ = "dispatch_attempts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="simulation_action")
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_kind: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]

    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
