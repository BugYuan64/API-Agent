"""定义动态大模型配置的 SQLAlchemy 持久化映射。

依赖 ORM 基类及时间辅助函数；只描述字段，配置选择、密钥复用和
脱敏响应由服务层负责，数据库模型本身不提供密钥加密。
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin, utc_now


class LLMSettingRecord(IdMixin, CreatedAtMixin, Base):
    """保存控制台显式配置的服务地址、密钥、默认模型、时限及启用状态。

    服务按单条覆盖配置使用，模型本身未声明数据库单例约束；没有记录
    时仍可由服务回退到环境配置，读取此记录不等于已验证模型可用。
    """

    __tablename__ = "llm_settings"

    provider: Mapped[str] = mapped_column(String(40), default="openai")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="gpt-4o")
    generation_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ModelTestRunRecord(IdMixin, CreatedAtMixin, Base):
    """持久化一次手动或 AI 驱动的模型能力探测任务历史。

    安全约束：绝不落库明文 ``api_key``，仅记录 ``used_saved_key`` 与
    ``has_custom_api_key`` 布尔标记及脱敏后的结果快照。
    """

    __tablename__ = "model_test_runs"

    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    badge: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", server_default="queued"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    protocol: Mapped[str] = mapped_column(
        String(20), nullable=False, default="auto", server_default="auto"
    )
    resolved_protocol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    used_saved_key: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    has_custom_api_key: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    timeout_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=20.0, server_default="20.0"
    )
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    target_models: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cooldown_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default="500"
    )
    total_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    completed_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    discover_meta: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    models_snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rounds_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sub_cases_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    summary_json: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
