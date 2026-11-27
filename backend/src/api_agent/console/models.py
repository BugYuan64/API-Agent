"""Console history with scrubbed replay inputs and bounded response previews."""

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, false
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
    request_body: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(default=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[float] = mapped_column(default=0)
    error_kind: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(String(300))
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    response_preview: Mapped[str | None] = mapped_column(Text)
    response_bytes: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    badge: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="completed", server_default="completed"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    cooldown_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default="500"
    )
    http_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    response_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    redirects_json: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    sse_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)


class ConsoleSubCaseRecord(IdMixin, CreatedAtMixin, Base):
    """AI 多轮接口测试中单个子用例的执行快照。

    作用：挂在单条 ``console_requests`` 父历史记录之下，按 ``(round_index, case_index)``
    保留每个子用例的脱敏请求与响应证据，供点击子用例链接时只读回放。
    """

    __tablename__ = "console_request_cases"

    parent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "console_requests.id",
            name="fk_console_request_cases_parent_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    round_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    case_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    request_headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    body_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none", server_default="none"
    )
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_headers: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    response_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    http_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    response_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    redirects_json: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    sse_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    assertion_summary: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
