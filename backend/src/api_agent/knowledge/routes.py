"""AI 助手小Y 向量知识库管理与检索 HTTP 接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import ApiError
from api_agent.infrastructure.database import session_for
from api_agent.knowledge.schemas import (
    EmbeddingConfigUpdate,
    KnowledgeBuildResponse,
    KnowledgeChunkPreview,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeStatusResponse,
)
from api_agent.knowledge.service import KnowledgeError
from api_agent.llm.service import LLMSettingService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
Session = Annotated[AsyncSession, Depends(session_for)]


async def _llm_fallback(session: AsyncSession, request: Request) -> dict[str, str | None]:
    record = await LLMSettingService(session, request.app.state.settings).get_effective_record()
    return {"base_url": record.base_url, "api_key": record.api_key}


@router.get("/status", response_model=KnowledgeStatusResponse)
async def get_knowledge_status(session: Session, request: Request) -> KnowledgeStatusResponse:
    """查看小Y 向量知识库语料准备度、向量模型配置与索引构建状态。"""
    fallback = await _llm_fallback(session, request)
    return request.app.state.knowledge.get_status(fallback)


@router.put("/config", response_model=KnowledgeStatusResponse)
async def update_knowledge_config(
    payload: EmbeddingConfigUpdate, session: Session, request: Request
) -> KnowledgeStatusResponse:
    """保存向量模型（Embedding）配置，密钥脱敏存储不回显明文。"""
    fallback = await _llm_fallback(session, request)
    return request.app.state.knowledge.update_config(payload, fallback)


@router.post("/build", response_model=KnowledgeBuildResponse)
async def build_knowledge_index(session: Session, request: Request) -> KnowledgeBuildResponse:
    """使用已配置的向量模型为全量项目功能介绍与使用指南生成向量索引。"""
    fallback = await _llm_fallback(session, request)
    await session.rollback()
    try:
        return await request.app.state.knowledge.build_index(fallback)
    except KnowledgeError as error:
        raise ApiError(error.status_code, error.code, error.message) from None


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    payload: KnowledgeSearchRequest, session: Session, request: Request
) -> KnowledgeSearchResponse:
    """按问题检索项目知识库（已构建向量时走向量检索，未构建时自动词法回退）。"""
    fallback = await _llm_fallback(session, request)
    await session.rollback()
    return await request.app.state.knowledge.search(
        payload.query, top_k=payload.top_k, llm_fallback=fallback
    )


@router.get("/chunks", response_model=list[KnowledgeChunkPreview])
async def list_knowledge_chunks(request: Request) -> list[KnowledgeChunkPreview]:
    """列出全部已准备的项目功能知识分块及向量化状态。"""
    return request.app.state.knowledge.list_chunks()
