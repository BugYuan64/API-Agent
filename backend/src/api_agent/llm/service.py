"""选择有效模型配置，保存数据库显式覆盖并构建普通或预览客户端。

依赖 SQLAlchemy、环境 Settings 和 HTTP 客户端；读取可回退环境且不落库，
隐式密钥仅同源复用，不负责模型请求或数据库密钥加密。
"""

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.orm import utc_now
from api_agent.llm.client import OpenAIClient
from api_agent.llm.models import LLMSettingRecord
from api_agent.llm.schemas import LLMConfigResponse, LLMConfigUpdate


def same_origin(first: str | None, second: str | None) -> bool:
    """比较两个合法 HTTP(S) 地址的协议、主机和有效端口，返回密钥可否隐式复用。

    拒绝空值、空白、控制字符、用户信息和非法端口；路径不同不影响同源。
    """

    def origin(value):
        """把合法地址规范为协议、主机和有效端口元组；无法安全解释时返回 None。"""
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
    """管理数据库显式配置与环境回退，并在地址变化时约束隐式密钥复用。"""

    def __init__(self, session: AsyncSession, settings: Settings | None = None):
        """绑定数据库会话及可选环境设置；不读取、创建或提交配置记录。"""
        self.session = session
        self.settings = settings

    async def get_record(self) -> LLMSettingRecord | None:
        """读取至多一条显式配置；不存在返回 None，数据库错误向外传播。"""
        return (await self.session.scalars(select(LLMSettingRecord).limit(1))).first()

    async def get_effective_record(self) -> LLMSettingRecord:
        """返回显式覆盖或按环境默认构造的未持久化配置，不产生数据库写入。"""
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
        """读取显式配置，缺失时将有效默认值新增提交并刷新后返回。

        用于保存流程；提交及刷新失败向外传播，不用于普通只读查询。
        """
        record = await self.get_record()
        if record is None:
            record = await self.get_effective_record()
            self.session.add(record)
            await self.session.commit()
            await self.session.refresh(record)
        return record

    async def get_response(self) -> LLMConfigResponse:
        """把有效配置转换为脱敏响应，仅返回密钥是否存在，不暴露密钥文本。"""
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

    async def select_model(self, model: str) -> LLMConfigResponse:
        """Persist only the model name so the assistant cannot overwrite other settings."""
        record = await self.get_or_create()
        record.model = model
        await self.session.commit()
        return await self.get_response()

    async def update(self, data: LLMConfigUpdate) -> LLMConfigResponse:
        """根据输入更新并提交显式配置，返回脱敏展示；数据库错误向外传播。

        api_key=None 仅在地址同源时保留原密钥，空字符串清空；地址字段
        未传则保留，传入时去首尾空白和末尾斜杠，其他字段按输入覆盖。
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
        """根据显式配置或环境回退构建客户端，不写数据库，也不探测端点。"""
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
        """合并预览字段与有效配置，返回启用且超时最多 15 秒的临时客户端。

        未显式传密钥时只对同源地址沿用旧密钥，空字符串表示无鉴权；
        不保存预览、不发起网络请求，数据库读取错误仍向外传播。
        """
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
