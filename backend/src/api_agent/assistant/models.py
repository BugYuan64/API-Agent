"""ORM models for page-scoped AI assistant threads and messages."""

from datetime import datetime
from typing import Literal

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin, utc_now

PageScope = Literal["console", "websocket", "tasks", "model_testing"]
VALID_PAGE_SCOPES: frozenset[str] = frozenset({"console", "websocket", "tasks", "model_testing"})


class AssistantThreadRecord(IdMixin, CreatedAtMixin, Base):
    """绑定到某个工作台页面及历史记录的 AI 助手会话线程。

    作用：按 ``(scope, history_id)`` 唯一标识一条历史记录专属的助手对话，
    使接口测试、长连接测试、智能群测与模型测试的助手记录互不串话，并在刷新后保留。
    """

    __tablename__ = "assistant_threads"

    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("scope", "history_id", name="uq_assistant_thread_scope_history"),
    )


class AssistantMessageRecord(IdMixin, CreatedAtMixin, Base):
    """AI 助手线程中的单条持久化消息及其结构化块（如子用例链接、工具调用摘要）。"""

    __tablename__ = "assistant_messages"

    thread_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "assistant_threads.id",
            name="fk_assistant_messages_thread_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    blocks_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
