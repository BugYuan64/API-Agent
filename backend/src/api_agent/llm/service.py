"""Service for managing effective LLM configuration and explicit SQLite overrides."""

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.orm import utc_now
from api_agent.llm.client import OpenAIClient
from api_agent.llm.models import LLMSettingRecord
from api_agent.llm.schemas import LLMConfigResponse, LLMConfigUpdate


def same_origin(first: str | None, second: str | None) -> bool:
    """Reuse credentials only across valid HTTP(S) origins with equal effective ports."""

    def origin(value):
        if not value or any(char.isspace() or ord(char) < 32 for char in value):
            return None
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
            port = parsed.port
            if port == 0:
                return None
            return (
                parsed.scheme,
                parsed.hostname.lower(),
                port or (443 if parsed.scheme == "https" else 80),
            )
        except ValueError:
            return None

    first_origin = origin(first)
    return first_origin is not None and first_origin == origin(second)


class LLMSettingService:
    """大模型动态配置业务层：管理存储在 SQLite 中的模型 Base URL、密钥与启用状态。"""

    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        self.session = session
        self.settings = settings

    async def get_record(self) -> LLMSettingRecord | None:
        """读取当前的配置记录，不存在时返回 None。"""
        return (await self.session.scalars(select(LLMSettingRecord).limit(1))).first()

    async def get_effective_record(self) -> LLMSettingRecord:
        """Read the explicit override, otherwise expose environment defaults without writing."""
        record = await self.get_record()
        if record is None:
            settings = self.settings
            record = LLMSettingRecord(
                provider="openai",
                base_url=settings.llm_base_url if settings else None,
                api_key=(
                    settings.llm_api_key.get_secret_value()
                    if settings and settings.llm_api_key
                    else None
                ),
                model=settings.llm_model if settings else "gpt-4o",
                timeout_seconds=settings.llm_timeout_seconds if settings else 60.0,
                enabled=settings.llm_mode != "disabled" if settings else False,
                updated_at=utc_now(),
            )
        return record

    async def get_or_create(self) -> LLMSettingRecord:
        """Create an override only for an explicit save, preserving effective defaults."""
        record = await self.get_record()
        if record is None:
            record = await self.get_effective_record()
            self.session.add(record)
            await self.session.commit()
            await self.session.refresh(record)
        return record

    async def get_response(self) -> LLMConfigResponse:
        """返回供前端展示的脱敏配置（不包含明文密钥）。"""
        record = await self.get_effective_record()
        has_key = bool(record.api_key and record.api_key.strip())
        return LLMConfigResponse(
            enabled=record.enabled,
            provider=record.provider,
            base_url=record.base_url,
            model=record.model,
            timeout_seconds=record.timeout_seconds,
            has_api_key=has_key,
        )

    async def update(self, data: LLMConfigUpdate) -> LLMConfigResponse:
        """根据前端提交更新配置，持久化落库。

        规则：
        - ``api_key`` 为 None 时仅同 origin 保留原密钥；
        - 空字符串清空密钥，非空字符串更新密钥；
        - ``base_url`` 统一去除尾部斜杠。
        """
        record = await self.get_or_create()
        record.enabled = data.enabled
        record.provider = data.provider
        if data.base_url is not None:
            target = data.base_url.strip().rstrip("/") if data.base_url.strip() else None
            if data.api_key is None and not same_origin(record.base_url, target):
                record.api_key = None
            record.base_url = target
        record.model = data.model.strip()
        record.timeout_seconds = data.timeout_seconds

        if data.api_key is not None:
            record.api_key = data.api_key.strip() or None

        record.updated_at = utc_now()
        await self.session.commit()
        await self.session.refresh(record)

        has_key = bool(record.api_key and record.api_key.strip())
        return LLMConfigResponse(
            enabled=record.enabled,
            provider=record.provider,
            base_url=record.base_url,
            model=record.model,
            timeout_seconds=record.timeout_seconds,
            has_api_key=has_key,
        )

    async def get_client(self) -> OpenAIClient:
        """Construct a client from the explicit override or environment, without writing."""
        record = await self.get_effective_record()
        return OpenAIClient(
            base_url=record.base_url,
            api_key=record.api_key,
            model=record.model,
            timeout_seconds=record.timeout_seconds,
            enabled=record.enabled,
        )

    async def get_preview_client(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> OpenAIClient:
        """Preview a configuration, never carrying implicit credentials across origins."""
        record = await self.get_effective_record()
        target = base_url.strip().rstrip("/") if base_url and base_url.strip() else record.base_url
        key = (
            api_key.strip() or None
            if api_key is not None
            else (record.api_key if same_origin(record.base_url, target) else None)
        )
        return OpenAIClient(
            base_url=target,
            api_key=key,
            model=model.strip() if model and model.strip() else record.model,
            timeout_seconds=min(15.0, record.timeout_seconds),
            enabled=True,
        )
