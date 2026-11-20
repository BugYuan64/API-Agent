"""Persisted console requests and long-lived stream probes."""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin


class StreamSessionRecord(IdMixin, CreatedAtMixin, Base):
    """一次 WebSocket 调试会话。

    作用：把“连了什么、协商出什么、收了多少条、为什么结束”持久化，
    让界面在会话结束后仍能展示完整证据链。
    实现：``status`` 表示会话生命周期（connecting/open/closed/error/cancelled），
    ``stop_reason`` 记录结束原因（target_closed/limits_exceeded/connect_failed…），
    两者分离才能如实表达“是目标主动关闭，还是本服务主动断开”。
    ``event_count``/``byte_count`` 在每次批量落库时更新，避免前端逐条计数。
    """

    __tablename__ = "stream_sessions"

    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(12))
    url: Mapped[str] = mapped_column(String(2000))
    request_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="connecting", index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(40))
    error_kind: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(String(300))
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(200))
    subprotocol: Mapped[str | None] = mapped_column(String(100))
    close_code: Mapped[int | None] = mapped_column(Integer)
    close_reason: Mapped[str | None] = mapped_column(String(300))
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    byte_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    truncated_count: Mapped[int] = mapped_column(Integer, default=0)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    scheduled_messages: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    opened_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]


class StreamEventRecord(IdMixin, CreatedAtMixin, Base):
    """长连接上收发的单条事件。

    作用：构成可回看的时间线——每条 WebSocket 帧占一行，
    带序号、方向、类型与数据预览。
    实现：``(session_id, sequence)`` 唯一约束保证序号不重复；
    ``data`` 只存前 64KiB，超出部分丢弃并置 ``truncated``，
    真实长度记在 ``data_bytes``，避免一条巨型事件拖垮界面与数据库。
    """

    __tablename__ = "stream_events"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("stream_sessions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(10), default="in")
    kind: Mapped[str] = mapped_column(String(20))
    event_name: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[str | None] = mapped_column(Text)
    data_bytes: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(default=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("session_id", "sequence"),)
