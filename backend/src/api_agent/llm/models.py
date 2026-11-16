"""定义动态大模型配置的 SQLAlchemy 持久化映射。

依赖 ORM 基类及时间辅助函数；只描述字段，配置选择、密钥复用和
脱敏响应由服务层负责，数据库模型本身不提供密钥加密。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String
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
    timeout_seconds: Mapped[float] = mapped_column(Float, default=60.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
