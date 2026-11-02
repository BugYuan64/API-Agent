"""Persisted console request history with redacted headers and bounded previews."""

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin


class ConsoleRequestRecord(IdMixin, CreatedAtMixin, Base):
    """一次手动请求的历史记录。

    作用：让界面能回看“刚才发了什么、目标回了什么”，并在刷新后继续可见。
    实现：请求头与响应头在落库前经 ``redact_headers`` 脱敏，
    响应体只留前 8KiB 预览并用 ``body_bytes`` 记录真实长度——
    历史记录是审计与复现用的线索，不应成为凭据或大数据量的第二份副本。
    """

    __tablename__ = "console_requests"

    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    method: Mapped[str] = mapped_column(String(10))
    url: Mapped[str] = mapped_column(String(2000))
    request_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    body_mode: Mapped[str] = mapped_column(String(10), default="none")
    body_preview: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(default=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[float] = mapped_column(default=0)
    error_kind: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(String(300))
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    response_preview: Mapped[str | None] = mapped_column(Text)
    response_bytes: Mapped[int] = mapped_column(Integer, default=0)
