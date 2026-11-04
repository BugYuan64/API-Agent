"""Persistent LLM settings configured dynamically via the frontend console."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin, utc_now


class LLMSettingRecord(IdMixin, CreatedAtMixin, Base):
    """持久化的大模型接口配置（通过前端控制台配置，不通过 .env 注入）。

    实现：单机全库单条配置记录，由前端控制台通过 API 读取和修改。
    包含 Base URL、API Key、默认模型名称、超时时间以及启用开关。
    """

    __tablename__ = "llm_settings"

    provider: Mapped[str] = mapped_column(String(40), default="openai")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="gpt-4o")
    timeout_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
