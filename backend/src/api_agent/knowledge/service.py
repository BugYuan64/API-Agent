"""AI 助手小Y 向量知识库服务：Embedding 调用、Chroma 向量构建与混合检索。"""

import asyncio
import math
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from api_agent.infrastructure.config import Settings
from api_agent.knowledge.corpus import (
    KnowledgeChunk,
    build_corpus_chunks,
    corpus_hash,
    list_document_summaries,
)
from api_agent.knowledge.schemas import (
    EmbeddingConfigResponse,
    EmbeddingConfigUpdate,
    KnowledgeBuildResponse,
    KnowledgeChunkPreview,
    KnowledgeDocumentSummary,
    KnowledgeIndexStatus,
    KnowledgeSearchHit,
    KnowledgeSearchResponse,
    KnowledgeStatusResponse,
    RetrievalMode,
)
from api_agent.knowledge.store import COLLECTION_NAME, VECTOR_STORE_NAME, KnowledgeStore
from api_agent.llm.service import same_origin

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_./:-]{2,}", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 阿里 DashScope 等供应商单批 /embeddings 限制 <= 10 条，取 8 兼顾吞吐与跨厂商兼容。
_EMBED_BATCH_SIZE = 8


class KnowledgeError(ValueError):
    """知识库配置或构建错误，承载稳定错误码与用户可读说明。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _normalize_endpoint(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    cleaned = url.strip().rstrip("/")
    for suffix in ("/embeddings", "/chat/completions"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip("/")
    return cleaned or None


def _l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 1e-12:
        return values
    return [v / norm for v in values]


def _tokenize_mixed(text: str) -> set[str]:
    """中英文混合轻量分词：英文/数字词 + 中文双字与单字，用于词法回退与混合加权。"""
    lowered = text.lower()
    tokens: set[str] = set(_ASCII_TOKEN_RE.findall(lowered))
    for run in _CJK_RUN_RE.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            for idx in range(len(run) - 1):
                tokens.add(run[idx : idx + 2])
            if len(run) <= 4:
                tokens.add(run)
    return tokens


def _lexical_score(query_tokens: set[str], query_lower: str, chunk: KnowledgeChunk) -> float:
    if not query_tokens:
        return 0.0
    title_text = f"{chunk.category} {chunk.title} {chunk.section}".lower()
    keyword_text = " ".join(chunk.keywords).lower()
    body_text = chunk.content.lower()

    title_tokens = _tokenize_mixed(title_text)
    keyword_tokens = _tokenize_mixed(keyword_text)
    body_tokens = _tokenize_mixed(body_text)

    title_overlap = len(query_tokens & title_tokens)
    keyword_overlap = len(query_tokens & keyword_tokens)
    body_overlap = len(query_tokens & body_tokens)

    exact_keyword_bonus = sum(1.5 for kw in chunk.keywords if kw.lower() in query_lower)
    raw = title_overlap * 2.2 + keyword_overlap * 2.5 + body_overlap * 1.0 + exact_keyword_bonus
    denom = max(len(query_tokens) * 2.0, 1.0)
    return round(min(raw / denom, 1.0), 4)


class KnowledgeBaseService:
    """管理项目文档语料、向量模型配置、Chroma 向量索引构建与检索。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = KnowledgeStore(settings.artifact_dir / "knowledge")
        self._chunks = build_corpus_chunks()
        self._chunk_map = {item.chunk_id: item for item in self._chunks}
        self._corpus_hash = corpus_hash(self._chunks)

    def close(self) -> None:
        """释放底层 Chroma 存储句柄。"""
        self.store.close()

    @property
    def chunks(self) -> list[KnowledgeChunk]:
        return self._chunks

    @property
    def current_corpus_hash(self) -> str:
        return self._corpus_hash

    def _effective_connection(
        self, config: dict[str, Any], llm_fallback: dict[str, Any] | None = None
    ) -> tuple[str | None, str | None, str]:
        """计算实际生效的 (base_url, api_key, model)。"""
        fallback_url = _normalize_endpoint(llm_fallback.get("base_url") if llm_fallback else None)
        fallback_key = (
            llm_fallback.get("api_key").strip()
            if llm_fallback
            and isinstance(llm_fallback.get("api_key"), str)
            and llm_fallback.get("api_key").strip()
            else None
        )
        custom_url = _normalize_endpoint(config.get("base_url"))
        custom_key = (
            config["api_key"].strip()
            if isinstance(config.get("api_key"), str) and config["api_key"].strip()
            else None
        )
        model = (config.get("model") or "").strip()

        if config.get("use_llm_endpoint", True):
            effective_url = fallback_url or custom_url
            effective_key = custom_key or fallback_key
        else:
            effective_url = custom_url
            effective_key = custom_key
        return effective_url, effective_key, model

    def _config_response(
        self, config: dict[str, Any], llm_fallback: dict[str, Any] | None = None
    ) -> EmbeddingConfigResponse:
        effective_url, effective_key, model = self._effective_connection(config, llm_fallback)
        return EmbeddingConfigResponse(
            enabled=bool(config.get("enabled", True)),
            use_llm_endpoint=bool(config.get("use_llm_endpoint", True)),
            base_url=_normalize_endpoint(config.get("base_url")),
            effective_base_url=effective_url,
            model=model,
            dimensions=config.get("dimensions"),
            top_k=int(config.get("top_k", 4)),
            timeout_seconds=float(config.get("timeout_seconds", 30.0)),
            has_api_key=bool(config.get("api_key") and str(config["api_key"]).strip()),
            has_effective_api_key=bool(effective_key),
        )

    def _evaluate_index(
        self,
        config: dict[str, Any],
        index_meta: dict[str, Any] | None,
        llm_fallback: dict[str, Any] | None = None,
    ) -> tuple[KnowledgeIndexStatus, RetrievalMode, int, int | None, str | None]:
        effective_url, _effective_key, model = self._effective_connection(config, llm_fallback)
        configured = bool(config.get("enabled", True) and effective_url and model)

        vector_count = int(index_meta.get("vector_count", 0)) if index_meta else 0
        dims = index_meta.get("embedding_dimensions") if index_meta else None
        built_at = index_meta.get("built_at") if index_meta else None

        if not configured:
            return "unconfigured", "lexical_fallback", vector_count, dims, built_at
        if vector_count == 0 or not index_meta:
            return "unbuilt", "lexical_fallback", 0, None, None
        same_corpus = index_meta.get("corpus_hash") == self._corpus_hash
        same_model = (index_meta.get("embedding_model") or "") == model
        same_count = vector_count == len(self._chunks)
        if same_corpus and same_model and same_count:
            return "ready", "vector", vector_count, dims, built_at
        return "stale", "lexical_fallback", vector_count, dims, built_at

    def get_status(self, llm_fallback: dict[str, Any] | None = None) -> KnowledgeStatusResponse:
        """返回 Chroma 向量知识库的准备度、语料统计、索引状态与检索模式。"""
        config = self.store.read_config()
        index_meta = self.store.read_index_meta()
        cfg_resp = self._config_response(config, llm_fallback)
        status, retrieval_mode, vector_count, dims, built_at = self._evaluate_index(
            config, index_meta, llm_fallback
        )
        configured = status != "unconfigured"
        return KnowledgeStatusResponse(
            status=status,
            configured=configured,
            ready=status == "ready",
            vector_store=VECTOR_STORE_NAME,
            collection_name=COLLECTION_NAME,
            retrieval_mode=retrieval_mode,
            corpus_hash=self._corpus_hash,
            chunk_count=len(self._chunks),
            vector_count=vector_count,
            embedding_model=(
                (index_meta.get("embedding_model") if index_meta else None)
                or cfg_resp.model
                or None
            ),
            embedding_dimensions=dims,
            built_at=built_at,
            last_error=config.get("last_error"),
            config=cfg_resp,
            documents=[
                KnowledgeDocumentSummary.model_validate(item)
                for item in list_document_summaries(self._chunks)
            ],
        )

    def list_chunks(self) -> list[KnowledgeChunkPreview]:
        """返回所有已准备好的知识分块及其在 Chroma 中的向量化标记。"""
        indexed = self.store.indexed_ids()
        return [
            KnowledgeChunkPreview(
                chunk_id=item.chunk_id,
                doc_id=item.doc_id,
                category=item.category,
                title=item.title,
                section=item.section,
                keywords=list(item.keywords),
                content=item.content,
                has_vector=item.chunk_id in indexed,
            )
            for item in self._chunks
        ]

    def update_config(
        self, payload: EmbeddingConfigUpdate, llm_fallback: dict[str, Any] | None = None
    ) -> KnowledgeStatusResponse:
        """保存向量模型配置（遵循同源密钥复用保护，绝不回显明文密钥）。"""
        current = self.store.read_config()
        next_url = _normalize_endpoint(payload.base_url)

        if payload.api_key is not None:
            next_key = payload.api_key.strip() or None
        else:
            if payload.use_llm_endpoint or same_origin(current.get("base_url"), next_url):
                next_key = current.get("api_key")
            else:
                next_key = None

        updated = {
            "enabled": payload.enabled,
            "use_llm_endpoint": payload.use_llm_endpoint,
            "base_url": next_url,
            "api_key": next_key,
            "model": payload.model.strip(),
            "dimensions": payload.dimensions,
            "top_k": payload.top_k,
            "timeout_seconds": payload.timeout_seconds,
            "last_error": None,
        }
        self.store.save_config(updated)
        return self.get_status(llm_fallback)

    async def embed_texts(
        self,
        texts: list[str],
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float = 30.0,
        dimensions: int | None = None,
    ) -> list[list[float]]:
        """调用 OpenAI 兼容 /embeddings 接口并返回按输入顺序归一化后的向量列表。"""
        headers = {"Content-Type": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"

        url = f"{base_url.rstrip('/')}/embeddings"
        vectors: list[list[float]] = []

        async with httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=self.settings.trust_system_proxy,
        ) as client:
            for start in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[start : start + _EMBED_BATCH_SIZE]
                body: dict[str, Any] = {"model": model, "input": batch}
                if dimensions is not None:
                    body["dimensions"] = dimensions
                try:
                    response = await client.post(url, headers=headers, json=body)
                except httpx.TimeoutException:
                    raise KnowledgeError(
                        "embedding_timeout",
                        f"向量接口请求超时（>{timeout_seconds:g}s），请检查服务可达性",
                        status_code=504,
                    ) from None
                except (httpx.HTTPError, OSError):
                    raise KnowledgeError(
                        "embedding_connection_failed",
                        "无法连接到向量模型接口，请检查 Base URL 与网络设置",
                        status_code=502,
                    ) from None

                if response.status_code in {401, 403}:
                    raise KnowledgeError(
                        "embedding_auth_failed",
                        f"向量模型鉴权失败（HTTP {response.status_code}），请检查 API Key",
                        status_code=401,
                    )
                if response.status_code != 200:
                    raise KnowledgeError(
                        "embedding_response_error",
                        f"向量模型接口返回 HTTP {response.status_code}，"
                        "请确认模型名与端点支持 /embeddings",
                        status_code=502,
                    )
                try:
                    raw = response.json()
                except (ValueError, RecursionError):
                    raise KnowledgeError(
                        "embedding_invalid_response",
                        "向量模型接口返回内容不是合法 JSON",
                        status_code=502,
                    ) from None

                items = raw.get("data") if isinstance(raw, dict) else None
                if not isinstance(items, list) or len(items) != len(batch):
                    raise KnowledgeError(
                        "embedding_invalid_response",
                        "向量接口返回的 data 数量与请求分块数量不一致",
                        status_code=502,
                    )
                ordered = sorted(
                    items,
                    key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0,
                )
                for item in ordered:
                    vec = item.get("embedding") if isinstance(item, dict) else None
                    if (
                        not isinstance(vec, list)
                        or not vec
                        or not all(
                            isinstance(x, (int, float)) and not isinstance(x, bool) for x in vec
                        )
                    ):
                        raise KnowledgeError(
                            "embedding_invalid_response",
                            "向量接口返回了无效的 embedding 数值数组",
                            status_code=502,
                        )
                    vectors.append(_l2_normalize([float(x) for x in vec]))
        return vectors

    async def build_index(
        self, llm_fallback: dict[str, Any] | None = None
    ) -> KnowledgeBuildResponse:
        """为全部项目功能知识分块生成向量并持久化写入 Chroma 向量库。"""
        config = self.store.read_config()
        effective_url, effective_key, model = self._effective_connection(config, llm_fallback)
        if not config.get("enabled", True):
            raise KnowledgeError(
                "embedding_disabled",
                "向量知识库当前处于停用状态，请先启用后再构建",
                status_code=400,
            )
        if not effective_url or not model:
            raise KnowledgeError(
                "embedding_not_configured",
                "尚未配置向量模型（Embedding Model）或接口地址，请先保存配置后再构建向量知识库",
                status_code=400,
            )

        started = time.monotonic()
        texts = [
            f"{chunk.category} | {chunk.title} - {chunk.section}\n"
            f"关键词：{', '.join(chunk.keywords)}\n{chunk.content}"
            for chunk in self._chunks
        ]
        try:
            vectors = await self.embed_texts(
                texts,
                base_url=effective_url,
                api_key=effective_key,
                model=model,
                timeout_seconds=float(config.get("timeout_seconds", 30.0)),
                dimensions=config.get("dimensions"),
            )
        except KnowledgeError as error:
            config["last_error"] = error.message
            self.store.save_config(config)
            raise

        dims = len(vectors[0]) if vectors else 0
        if dims <= 0 or any(len(vec) != dims for vec in vectors):
            raise KnowledgeError(
                "embedding_dimension_mismatch",
                "向量接口返回的维度不一致，构建已取消",
                status_code=502,
            )

        built_at = datetime.now(UTC).isoformat()
        entries = [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "category": chunk.category,
                "title": chunk.title,
                "section": chunk.section,
                "keywords": list(chunk.keywords),
                "content": chunk.content,
                "vector": vector,
            }
            for chunk, vector in zip(self._chunks, vectors, strict=True)
        ]
        meta = {
            "corpus_hash": self._corpus_hash,
            "embedding_model": model,
            "embedding_dimensions": dims,
            "built_at": built_at,
        }
        await asyncio.to_thread(self.store.replace_vectors, entries, meta)
        config["last_error"] = None
        self.store.save_config(config)

        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        return KnowledgeBuildResponse(
            ok=True,
            status="ready",
            vector_store=VECTOR_STORE_NAME,
            collection_name=COLLECTION_NAME,
            chunk_count=len(self._chunks),
            vector_count=len(entries),
            embedding_model=model,
            embedding_dimensions=dims,
            corpus_hash=self._corpus_hash,
            built_at=built_at,
            duration_ms=elapsed_ms,
            message=f"已使用 {model} 将 {len(entries)} 条知识块写入 Chroma 向量库（维度 {dims}）",
        )

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        llm_fallback: dict[str, Any] | None = None,
    ) -> KnowledgeSearchResponse:
        """检索与用户问题最相关的项目功能知识片段（优先 Chroma 向量检索，未构建时词法回退）。"""
        cleaned = query.strip()
        if not cleaned:
            return KnowledgeSearchResponse(query="", mode="lexical_fallback", hits=[])

        config = self.store.read_config()
        limit = max(1, min(top_k or int(config.get("top_k", 4)), 10))
        index_meta = await asyncio.to_thread(self.store.read_index_meta)
        status, _mode, _count, _dims, _built_at = self._evaluate_index(
            config, index_meta, llm_fallback
        )
        query_lower = cleaned.lower()
        query_tokens = _tokenize_mixed(cleaned)

        mode: RetrievalMode = "lexical_fallback"
        scored: list[tuple[float, KnowledgeChunk]] = []

        if status == "ready" and index_meta is not None:
            effective_url, effective_key, model = self._effective_connection(config, llm_fallback)
            if effective_url and model:
                try:
                    query_vecs = await self.embed_texts(
                        [cleaned],
                        base_url=effective_url,
                        api_key=effective_key,
                        model=model,
                        timeout_seconds=min(float(config.get("timeout_seconds", 30.0)), 15.0),
                        dimensions=config.get("dimensions"),
                    )
                    q_vec = query_vecs[0]
                    candidate_count = min(len(self._chunks), max(limit * 3, 10))
                    chroma_hits = await asyncio.to_thread(
                        self.store.query_vectors, q_vec, candidate_count
                    )
                    for item in chroma_hits:
                        chunk = self._chunk_map.get(item["chunk_id"])
                        if chunk is None:
                            continue
                        cosine = item["similarity"]
                        lex = _lexical_score(query_tokens, query_lower, chunk)
                        hybrid = round(cosine * 0.85 + lex * 0.15, 4)
                        scored.append((hybrid, chunk))
                    if scored:
                        mode = "vector"
                except KnowledgeError:
                    scored = []
                    mode = "lexical_fallback"

        if not scored:
            for chunk in self._chunks:
                score = _lexical_score(query_tokens, query_lower, chunk)
                if score > 0:
                    scored.append((score, chunk))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits = [
            KnowledgeSearchHit(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                category=chunk.category,
                title=chunk.title,
                section=chunk.section,
                score=score,
                snippet=chunk.content[:180] + ("…" if len(chunk.content) > 180 else ""),
                content=chunk.content,
            )
            for score, chunk in scored[:limit]
            if score >= (0.18 if mode == "vector" else 0.12)
        ]
        return KnowledgeSearchResponse(query=cleaned, mode=mode, hits=hits)

    @staticmethod
    def format_prompt_context(response: KnowledgeSearchResponse) -> str:
        """将命中的知识库片段格式化为受控提示词参考段。"""
        if not response.hits:
            return ""
        mode_label = "Chroma 向量语义检索" if response.mode == "vector" else "本地知识库词法检索"
        blocks = [
            f"[{idx}] {hit.category} · {hit.title} / {hit.section}\n{hit.content}"
            for idx, hit in enumerate(response.hits, start=1)
        ]
        return (
            f"以下是 API-Agent 项目功能知识库参考资料（{mode_label}，仅作功能与使用说明参考）：\n\n"
            + "\n\n".join(blocks)
        )
