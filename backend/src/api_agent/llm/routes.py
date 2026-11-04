"""Endpoints for dynamic LLM settings management and tool-enabled completions."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import ApiError
from api_agent.infrastructure.database import session_for
from api_agent.llm.client import (
    LLMAuthenticationError,
    LLMConfigError,
    LLMConnectionError,
    LLMResponseError,
    LLMTimeoutError,
)
from api_agent.llm.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    LLMConfigResponse,
    LLMConfigUpdate,
    LLMModelListRequest,
    LLMModelListResponse,
    LLMPingRequest,
    LLMPingResponse,
    LLMStatusResponse,
)
from api_agent.llm.service import LLMSettingService

router = APIRouter(prefix="/api", tags=["llm"])
Session = Annotated[AsyncSession, Depends(session_for)]


@router.get("/settings/llm", response_model=LLMConfigResponse)
@router.get("/llm/config", response_model=LLMConfigResponse)
async def get_llm_settings(session: Session, request: Request) -> LLMConfigResponse:
    """获取通过前端控制台配置的大模型设置（脱敏展示）。"""
    return await LLMSettingService(session, request.app.state.settings).get_response()


@router.put("/settings/llm", response_model=LLMConfigResponse)
@router.put("/llm/config", response_model=LLMConfigResponse)
async def update_llm_settings(
    payload: LLMConfigUpdate, session: Session, request: Request
) -> LLMConfigResponse:
    """更新大模型配置并持久化至 SQLite 数据库（由前端控制台调用，代替 .env 注入）。"""
    return await LLMSettingService(session, request.app.state.settings).update(payload)


@router.post("/settings/llm/ping", response_model=LLMPingResponse)
@router.post("/llm/ping", response_model=LLMPingResponse)
async def ping_llm(
    session: Session, request: Request, payload: LLMPingRequest | None = None
) -> LLMPingResponse:
    """测试大模型接口连通性。

    实现：
    - 若传入待验证的 base_url、api_key、model，则直接基于传入值测连通性（用于保存前验证）；
    - 未传字段使用有效配置；密钥仅同源复用，显式空字符串表示无认证。
    """
    service = LLMSettingService(session, request.app.state.settings)
    client = await service.get_preview_client(
        base_url=payload.base_url if payload else None,
        api_key=payload.api_key if payload else None,
        model=payload.model if payload else None,
    )
    if not client.base_url:
        raise ApiError(400, "llm_not_configured", "请先填写大模型服务接口 Base URL")

    started = time.monotonic()
    try:
        res = await client.ping()
        return LLMPingResponse(
            ok=True,
            latency_ms=res["latency_ms"],
            message=f"连通成功，模型响应正常（'{res.get('model')}'）",
        )
    except Exception as error:
        elapsed = round((time.monotonic() - started) * 1000, 2)
        return LLMPingResponse(
            ok=False,
            latency_ms=elapsed,
            message=str(error) or type(error).__name__,
        )


@router.post("/settings/llm/models", response_model=LLMModelListResponse)
@router.post("/llm/models", response_model=LLMModelListResponse)
async def list_llm_models(
    session: Session, request: Request, payload: LLMModelListRequest | None = None
) -> LLMModelListResponse:
    """获取指定 Base URL（或已保存配置）下的可用模型清单（通过 GET /models）。"""
    service = LLMSettingService(session, request.app.state.settings)
    client = await service.get_preview_client(
        base_url=payload.base_url if payload else None,
        api_key=payload.api_key if payload else None,
    )
    if not client.base_url:
        raise ApiError(400, "llm_not_configured", "请先填写大模型服务接口 Base URL")

    try:
        models = await client.list_models()
        return LLMModelListResponse(
            ok=True,
            models=models,
            count=len(models),
            message=f"成功获取到 {len(models)} 个可用模型" if models else "服务未返回任何模型项",
        )
    except Exception as error:
        return LLMModelListResponse(
            ok=False,
            models=[],
            count=0,
            message=str(error) or type(error).__name__,
        )


@router.get("/llm/status", response_model=LLMStatusResponse)
async def get_llm_status(session: Session, request: Request) -> LLMStatusResponse:
    """查询模型当前运行模式与就绪状态。"""
    config = await LLMSettingService(session, request.app.state.settings).get_response()
    mode = "openai" if (config.enabled and config.base_url) else "disabled"
    return LLMStatusResponse(
        configured=bool(config.enabled and config.base_url),
        mode=mode,
        base_url=config.base_url,
        model=config.model,
        has_api_key=config.has_api_key,
    )


@router.post("/llm/chat", response_model=ChatCompletionResponse)
async def chat_completion(
    payload: ChatCompletionRequest, session: Session, request: Request
) -> ChatCompletionResponse:
    """向配置的 OpenAI 兼容接口发起单次请求（支持工具/函数调用）。"""
    client = await LLMSettingService(session, request.app.state.settings).get_client()
    if not client.is_configured:
        raise ApiError(
            400,
            "llm_not_configured",
            "大模型服务尚未开启或未配置 Base URL。请前往「系统设置」配置并启用。",
        )
    try:
        return await client.chat_completion(payload)
    except LLMConfigError as error:
        raise ApiError(400, "llm_not_configured", str(error)) from None
    except LLMAuthenticationError as error:
        raise ApiError(401, "llm_auth_failed", str(error)) from None
    except LLMTimeoutError as error:
        raise ApiError(504, "llm_timeout", str(error)) from None
    except LLMConnectionError as error:
        raise ApiError(502, "llm_connection_failed", str(error)) from None
    except LLMResponseError as error:
        raise ApiError(502, "llm_response_error", str(error)) from None
