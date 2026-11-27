"""ChromaDB 本地持久化向量存储与配置原子落盘。"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

COLLECTION_NAME = "api_agent_knowledge"
VECTOR_STORE_NAME = "chroma"


class KnowledgeStore:
    """将向量模型配置与 Chroma 持久化向量库落盘到 ``artifact_dir/knowledge``。"""

    def __init__(self, root: Path):
        self.root = root
        self.config_path = root / "embedding_config.json"
        self.meta_path = root / "index_meta.json"
        self.legacy_index_path = root / "vector_index.json"
        self.chroma_dir = root / "chroma"
        self._chroma_client: Any = None

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=self.root, prefix=f".{path.stem}-", suffix=".tmp")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _get_chroma_client(self, *, create_if_missing: bool = False):
        if self._chroma_client is not None:
            return self._chroma_client
        if not create_if_missing and not (self.chroma_dir / "chroma.sqlite3").is_file():
            return None
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self._chroma_client = chromadb.PersistentClient(
            path=str(self.chroma_dir),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        return self._chroma_client

    def close(self) -> None:
        """释放 Chroma 本地 SQLite 句柄，确保应用退出与测试目录清理干净无锁。"""
        client = self._chroma_client
        self._chroma_client = None
        if client is None:
            return
        system = getattr(client, "_system", None)
        if system is not None and hasattr(system, "stop"):
            try:
                system.stop()
            except Exception:
                pass

    def read_config(self) -> dict[str, Any]:
        """读取本地向量模型配置，缺失时返回安全默认值。"""
        saved = self._read_json(self.config_path) or {}
        return {
            "enabled": bool(saved.get("enabled", True)),
            "use_llm_endpoint": bool(saved.get("use_llm_endpoint", True)),
            "base_url": saved.get("base_url") if isinstance(saved.get("base_url"), str) else None,
            "api_key": saved.get("api_key") if isinstance(saved.get("api_key"), str) else None,
            "model": saved.get("model") if isinstance(saved.get("model"), str) else "",
            "dimensions": saved.get("dimensions") if type(saved.get("dimensions")) is int else None,
            "top_k": saved.get("top_k") if type(saved.get("top_k")) is int else 4,
            "timeout_seconds": (
                float(saved["timeout_seconds"])
                if isinstance(saved.get("timeout_seconds"), (int, float))
                and not isinstance(saved.get("timeout_seconds"), bool)
                else 30.0
            ),
            "last_error": (
                saved.get("last_error") if isinstance(saved.get("last_error"), str) else None
            ),
        }

    def save_config(self, config: dict[str, Any]) -> None:
        """原子保存向量模型配置。"""
        self._write_json(self.config_path, config)

    def read_index_meta(self) -> dict[str, Any] | None:
        """读取 Chroma 索引元信息并与集合实际记录数核对。"""
        meta = self._read_json(self.meta_path)
        if not meta:
            return None
        client = self._get_chroma_client(create_if_missing=False)
        if client is None:
            return None
        try:
            collection = client.get_collection(name=COLLECTION_NAME, embedding_function=None)
            actual_count = int(collection.count())
        except Exception:
            return None
        return {
            **meta,
            "vector_store": VECTOR_STORE_NAME,
            "collection_name": COLLECTION_NAME,
            "vector_count": actual_count,
        }

    def indexed_ids(self) -> set[str]:
        """返回当前 Chroma 集合中已存储向量的全部 chunk_id。"""
        client = self._get_chroma_client(create_if_missing=False)
        if client is None:
            return set()
        try:
            collection = client.get_collection(name=COLLECTION_NAME, embedding_function=None)
            result = collection.get(include=[])
            ids = result.get("ids") if isinstance(result, dict) else []
            return {str(item) for item in (ids or [])}
        except Exception:
            return set()

    def replace_vectors(self, entries: list[dict[str, Any]], meta: dict[str, Any]) -> None:
        """重建 Chroma 集合并写入全量向量与元数据，支持无缝切换不同维度的向量模型。"""
        client = self._get_chroma_client(create_if_missing=True)
        assert client is not None
        try:
            client.delete_collection(name=COLLECTION_NAME)
        except Exception:
            pass

        collection = client.create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )
        if entries:
            collection.upsert(
                ids=[str(item["chunk_id"]) for item in entries],
                embeddings=[item["vector"] for item in entries],
                documents=[str(item["content"]) for item in entries],
                metadatas=[
                    {
                        "doc_id": str(item["doc_id"]),
                        "category": str(item["category"]),
                        "title": str(item["title"]),
                        "section": str(item["section"]),
                    }
                    for item in entries
                ],
            )
        self._write_json(
            self.meta_path,
            {
                **meta,
                "schema_version": "2",
                "vector_store": VECTOR_STORE_NAME,
                "collection_name": COLLECTION_NAME,
                "vector_count": len(entries),
            },
        )
        # 迁移至 Chroma 后清理旧版 JSON 向量快照文件，避免双份状态歧义。
        self.legacy_index_path.unlink(missing_ok=True)

    def query_vectors(self, query_vector: list[float], n_results: int) -> list[dict[str, Any]]:
        """在 Chroma 余弦空间中执行向量近邻检索，返回 ``chunk_id`` 与余弦相似度。"""
        client = self._get_chroma_client(create_if_missing=False)
        if client is None:
            return []
        try:
            collection = client.get_collection(name=COLLECTION_NAME, embedding_function=None)
            total = int(collection.count())
            if total <= 0:
                return []
            limit = max(1, min(n_results, total))
            raw = collection.query(
                query_embeddings=[query_vector],
                n_results=limit,
                include=["distances"],
            )
        except Exception:
            return []

        ids = (raw.get("ids") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]
        hits: list[dict[str, Any]] = []
        for chunk_id, distance in zip(ids, distances, strict=False):
            dist_val = float(distance)
            similarity = max(0.0, min(1.0, 1.0 - dist_val))
            hits.append(
                {
                    "chunk_id": str(chunk_id),
                    "distance": dist_val,
                    "similarity": similarity,
                }
            )
        return hits
