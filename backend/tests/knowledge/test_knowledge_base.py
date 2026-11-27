"""AI 助手小Y 向量知识库：语料准备、Embedding 配置、构建向量索引与混合检索回归。"""

import hashlib

from fastapi import FastAPI, Request
from support.local_server import LocalServer


def _deterministic_vector(text: str, dims: int = 8) -> list[float]:
    """根据文本关键词生成确定性伪向量，便于验证余弦相似度排序。"""
    buckets = [0.0] * dims
    keywords = [
        ("代理", 0),
        ("proxy", 0),
        ("websocket", 1),
        ("长连接", 1),
        ("有状态", 2),
        ("extract", 2),
        ("大模型", 3),
        ("anthropic", 3),
        ("配色", 4),
        ("logo", 4),
        ("智能群测", 5),
        ("mcp", 6),
        ("小y", 7),
    ]
    lower = text.lower()
    for token, idx in keywords:
        if token in lower:
            buckets[idx] += 3.0
    digest = hashlib.sha256(lower.encode("utf-8")).digest()
    for i in range(dims):
        buckets[i] += (digest[i] / 255.0) * 0.25
    return buckets


async def test_knowledge_base_prepares_corpus_lexical_fallback_and_vector_build(client, app):
    """未配置向量模型时可查看语料并走词法回退；配置后写入 Chroma 并切换向量语义检索。"""
    initial = await client.get("/api/knowledge/status")
    assert initial.status_code == 200, initial.text
    status = initial.json()
    assert status["status"] == "unconfigured"
    assert status["vector_store"] == "chroma"
    assert status["collection_name"] == "api_agent_knowledge"
    assert status["ready"] is False
    assert status["retrieval_mode"] == "lexical_fallback"
    assert status["chunk_count"] >= 18
    assert status["vector_count"] == 0
    assert len(status["documents"]) >= 10

    # 未配置向量模型直接构建应返回明确 400 引导错误。
    blocked_build = await client.post("/api/knowledge/build")
    assert blocked_build.status_code == 400
    assert blocked_build.json()["error"]["code"] == "embedding_not_configured"

    # 未构建向量前仍可通过词法回退检索项目功能介绍与使用方法。
    lexical = await client.post(
        "/api/knowledge/search",
        json={"query": "如何开启 TRUST_SYSTEM_PROXY 系统代理？"},
    )
    assert lexical.status_code == 200, lexical.text
    lex_body = lexical.json()
    assert lex_body["mode"] == "lexical_fallback"
    assert lex_body["hits"]
    assert any("TRUST_SYSTEM_PROXY" in hit["content"] for hit in lex_body["hits"])

    # 启动本地 mock Embedding 服务，验证配置保存、密钥脱敏、向量构建与向量检索切换。
    embed_calls: list[dict] = []
    mock_embed = FastAPI()

    @mock_embed.post("/v1/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        embed_calls.append(
            {
                "auth": request.headers.get("authorization"),
                "model": body.get("model"),
                "count": len(body.get("input", [])),
            }
        )
        items = body["input"]
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": idx, "embedding": _deterministic_vector(text)}
                for idx, text in enumerate(items)
            ],
        }

    server = await LocalServer(mock_embed).start()
    try:
        saved = await client.put(
            "/api/knowledge/config",
            json={
                "enabled": True,
                "use_llm_endpoint": False,
                "base_url": f"{server.base_url}/v1",
                "api_key": "sk-embed-secret-12345",
                "model": "BAAI/bge-m3",
                "top_k": 3,
                "timeout_seconds": 15,
            },
        )
        assert saved.status_code == 200, saved.text
        saved_body = saved.json()
        assert saved_body["status"] == "unbuilt"
        assert saved_body["configured"] is True
        assert saved_body["config"]["has_api_key"] is True
        assert "sk-embed-secret-12345" not in saved.text

        built = await client.post("/api/knowledge/build")
        assert built.status_code == 200, built.text
        built_body = built.json()
        assert built_body["ok"] is True
        assert built_body["status"] == "ready"
        assert built_body["vector_store"] == "chroma"
        assert built_body["vector_count"] == status["chunk_count"]
        assert built_body["embedding_dimensions"] == 8
        assert embed_calls and embed_calls[0]["auth"] == "Bearer sk-embed-secret-12345"
        assert embed_calls[0]["model"] == "BAAI/bge-m3"
        chroma_sqlite = app.state.settings.artifact_dir / "knowledge" / "chroma" / "chroma.sqlite3"
        assert chroma_sqlite.is_file()

        after = (await client.get("/api/knowledge/status")).json()
        assert after["status"] == "ready"
        assert after["ready"] is True
        assert after["retrieval_mode"] == "vector"
        assert after["vector_count"] == status["chunk_count"]

        chunks = (await client.get("/api/knowledge/chunks")).json()
        assert len(chunks) == status["chunk_count"]
        assert all(item["has_vector"] is True for item in chunks)

        vector_search = await client.post(
            "/api/knowledge/search",
            json={"query": "有状态多步骤依赖链 extract 怎么用？"},
        )
        assert vector_search.status_code == 200, vector_search.text
        vec_body = vector_search.json()
        assert vec_body["mode"] == "vector"
        assert vec_body["hits"]
        assert vec_body["hits"][0]["doc_id"] == "stateful-chains"
    finally:
        await server.stop()
