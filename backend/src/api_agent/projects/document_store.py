"""按项目保存不可变源码快照与可恢复文档任务，文件名只接受规范 UUID。"""

import json
import os
import tempfile
import uuid
from pathlib import Path

from api_agent.infrastructure.orm import new_id, utc_now


class DocumentStore:
    """管理项目工件的原子写入与读取，绝对源码路径不进入持久记录。"""

    def __init__(self, root: Path):
        """固定存储边界；按需创建项目、源码及文档目录。"""
        self.root = root.resolve()

    def path(self, project_id: str, kind: str, item_id: str) -> Path:
        """校验项目和记录 UUID，拒绝链接或路径穿越指向存储目录外。"""
        if kind not in {"sources", "documents"}:
            raise ValueError("invalid_artifact_kind")
        for value in (project_id, item_id):
            if str(uuid.UUID(value)) != value:
                raise ValueError("invalid_artifact_id")
        path = self.root / project_id / kind / f"{item_id}.json"
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise ValueError("invalid_artifact_path")
        return path

    def save(self, project_id: str, kind: str, record: dict):
        """完整写入临时文件并刷盘后原子替换，失败保留前一个可读版本。"""
        path = self.path(project_id, kind, record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, project_id: str, kind: str, item_id: str) -> dict:
        """读取指定项目工件，缺失或不合法 ID 交由路由映射为 404。"""
        with self.path(project_id, kind, item_id).open(encoding="utf-8") as handle:
            return json.load(handle)

    def list(self, project_id: str, kind: str) -> list[dict]:
        """稳定返回项目全部版本，校验每个文件的目录边界。"""
        directory = self.path(project_id, kind, new_id()).parent
        records = [self.read(project_id, kind, path.stem) for path in directory.glob("*.json")]
        return sorted(records, key=lambda row: (row["created_at"], row["id"]), reverse=True)

    def import_source(self, project_id: str, snapshot) -> dict:
        """冻结已脱敏源码及扫描清单，每次导入创建独立版本。"""
        record = snapshot.model_dump(mode="json")
        record.update(id=new_id(), project_id=project_id, created_at=utc_now().isoformat())
        self.save(project_id, "sources", record)
        return record

    def recover(self):
        """重启时把未结束文档任务标记中断，保留已生成条目及覆盖进度。"""
        for path in self.root.glob("*/documents/*.json"):
            project_id = path.parent.parent.name
            record = self.read(project_id, "documents", path.stem)
            if record["status"] in {"queued", "running"}:
                record.update(
                    status="interrupted",
                    error="服务重启导致生成中断，请重新生成。已完成结果可导出。",
                    updated_at=utc_now().isoformat(),
                )
                self.save(project_id, "documents", record)


def source_summary(record: dict) -> dict:
    """管理界面只接收扫描统计和路由证据，源码正文留在本地工件内。"""
    return {key: value for key, value in record.items() if key != "files"}
