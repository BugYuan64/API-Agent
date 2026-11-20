"""暴露模型配置、状态、预览探测、模型列表及非流式对话接口。

依赖 FastAPI、配置服务和客户端；配置响应隐藏密钥，对话异常映射
为 HTTP 错误，探测失败在结果体中返回，不负责执行模型工具。
"""

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
    LLMModelUpdate,
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
    """返回当前有效配置的脱敏展示，未保存时回退环境默认，不创建数据库记录。"""
    return await LLMSettingService(session, request.app.state.settings).get_response()


@router.put("/settings/llm", response_model=LLMConfigResponse)
@router.put("/llm/config", response_model=LLMConfigResponse)
async def update_llm_settings(
    payload: LLMConfigUpdate, session: Session, request: Request
) -> LLMConfigResponse:
    """保存显式配置覆盖并返回脱敏结果；同源密钥复用规则由配置服务执行。"""
    return await LLMSettingService(session, request.app.state.settings).update(payload)


@router.patch("/settings/llm/model", response_model=LLMConfigResponse)
async def select_llm_model(
    payload: LLMModelUpdate, session: Session, request: Request
) -> LLMConfigResponse:
    """Share the selected model with Settings without replacing addresses, keys or limits."""
    return await LLMSettingService(session, request.app.state.settings).select_model(payload.model)


@router.post("/settings/llm/ping", response_model=LLMPingResponse)
@router.post("/llm/ping", response_model=LLMPingResponse)
async def ping_llm(
    session: Session, request: Request, payload: LLMPingRequest | None = None
) -> LLMPingResponse:
    """使用预览或有效配置发起真实模型探测，返回成功标志、耗时与说明。

    未传字段沿用有效配置，隐式密钥仅同源复用；缺地址返回 400，
    其他探测异常转为 ok=False 的结果，不保存预览配置。
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
    """通过预览或有效配置获取模型列表，返回去重模型和数量，不保存配置。

    缺地址返回 400，其他查询异常转为 ok=False 的空列表结果；不逐个试调模型。
    """
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
    """根据启用开关和有效地址返回本地就绪状态，不发起网络健康检查。"""
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
    """向有效配置发起单次对话并返回规范化响应，工具调用仅作为数据返回。

    未配置返回 400，鉴权失败返回 401，超时返回 504，连接或响应错误返回 502。
    """
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
