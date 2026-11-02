"""Persisted execution evidence for plans run from the local console."""

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin


class ExecutionRecord(IdMixin, CreatedAtMixin, Base):
    """一次计划执行的持久化摘要。

    作用：让界面能回看“哪份计划、跑了哪些用例、哪条断言失败”，
    并在刷新或重启后继续可见。执行本身仍由 ``PostmanRunner`` 完成，
    这里只保存归一化后的结果，不保存 CLI 原始输出。
    实现：``executions`` 直接存 ``CaseResult`` 的 JSON——
    结构由 pydantic 保证，且必须能展开到断言级才能核对证据。
    """

    __tablename__ = "executions"

    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    label: Mapped[str] = mapped_column(String(80), default="")
    engine: Mapped[str] = mapped_column(String(40), default="postman-cli")
    status: Mapped[str] = mapped_column(String(20))
    error_kind: Mapped[str | None] = mapped_column(String(80))
    duration_ms: Mapped[float] = mapped_column(default=0)
    reserved_requests: Mapped[int] = mapped_column(Integer, default=0)
    observed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    unknown_requests: Mapped[int] = mapped_column(Integer, default=0)
    executions: Mapped[list[dict]] = mapped_column(JSON, default=list)
