"""AI 助手小Y 向量知识库的配置、状态、构建与检索数据结构。"""

from typing import Literal

from pydantic import Field

from api_agent.infrastructure.api import ApiInput, ApiModel

KnowledgeIndexStatus = Literal["unconfigured", "unbuilt", "stale", "ready"]
RetrievalMode = Literal["vector", "lexical_fallback"]


class EmbeddingConfigResponse(ApiModel):
    """脱敏后的向量模型配置响应（不返回明文密钥）。"""

    enabled: bool = True
    use_llm_endpoint: bool = True
    base_url: str | None = None
    effective_base_url: str | None = None
    model: str = ""
    dimensions: int | None = None
    top_k: int = 4
    timeout_seconds: float = 30.0
    has_api_key: bool = False
    has_effective_api_key: bool = False


class EmbeddingConfigUpdate(ApiInput):
    """前端保存向量模型配置时的输入体。"""

    enabled: bool = True
    use_llm_endpoint: bool = True
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(default="", max_length=120)
    dimensions: int | None = Field(default=None, ge=16, le=8192)
    top_k: int = Field(default=4, ge=1, le=10)
    timeout_seconds: float = Field(default=30.0, ge=3.0, le=180.0)


class KnowledgeDocumentSummary(ApiModel):
    """知识库中的单篇主题文档概览。"""

    doc_id: str
    category: str
    title: str
    sections: list[str]
    chunk_count: int


class KnowledgeChunkPreview(ApiModel):
    """单条知识分块预览（用于前端检查已准备好的语料）。"""

    chunk_id: str
    doc_id: str
    category: str
    title: str
    section: str
    keywords: list[str]
    content: str
    has_vector: bool = False


class KnowledgeStatusResponse(ApiModel):
    """向量知识库准备度与索引状态总览。"""

    status: KnowledgeIndexStatus
    configured: bool
    ready: bool
    vector_store: str = "chroma"
    collection_name: str = "api_agent_knowledge"
    retrieval_mode: RetrievalMode
    corpus_hash: str
    chunk_count: int
    vector_count: int
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    built_at: str | None = None
    last_error: str | None = None
    config: EmbeddingConfigResponse
    documents: list[KnowledgeDocumentSummary] = Field(default_factory=list)


class KnowledgeBuildResponse(ApiModel):
    """触发向量构建后的汇总结果。"""

    ok: bool
    status: KnowledgeIndexStatus
    vector_store: str = "chroma"
    collection_name: str = "api_agent_knowledge"
    chunk_count: int
    vector_count: int
    embedding_model: str
    embedding_dimensions: int
    corpus_hash: str
    built_at: str
    duration_ms: float
    message: str


class KnowledgeSearchRequest(ApiInput):
    """知识库检索调试输入。"""

    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=10)


class KnowledgeSearchHit(ApiModel):
    """单条知识库命中结果。"""

    chunk_id: str
    doc_id: str
    category: str
    title: str
    section: str
    score: float
    snippet: str
    content: str


class KnowledgeSearchResponse(ApiModel):
    """知识库检索响应，明确标注当前使用向量检索还是词法回退。"""

    query: str
    mode: RetrievalMode
    hits: list[KnowledgeSearchHit] = Field(default_factory=list)
