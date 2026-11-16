"""保存和恢复 Agent 的本地 JSON 运行产物，并按需渲染报告。

依赖隐私清洗和报告渲染；以 UUID 限定文件名，校验冻结快照哈希后原子替换，
不持有运行任务，也不重放中断前的请求。
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

from api_agent.agent.report import render_report
from api_agent.infrastructure.privacy import Context


class AgentStore:
    """管理单个产物目录中的运行 JSON，校验 ID、快照完整性与恢复记账。"""

    def __init__(self, root: Path):
        """解析并保留产物目录的绝对边界；实际目录在首次保存时创建。"""
        self.root = root.resolve()

    def path(self, run_id: str) -> Path:
        """将规范 UUID 转为本目录中的 JSON 路径；无效 ID 或越界抛 ValueError。"""
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("invalid_agent_run_id")
        path = (self.root / f"{run_id}.json").resolve()
        if path.parent != self.root:
            raise ValueError("invalid_agent_run_id")
        return path

    def save(self, record: dict):
        """校验冻结快照哈希，清洗追加字段后刷盘并原子替换运行 JSON。

        快照已被修改时抛 ValueError；编码及文件系统失败直接上抛，旧目标文件
        在替换成功前保持可读。
        """
        path = self.path(record["id"])
        encoded = json.dumps(record["snapshot"], sort_keys=True, ensure_ascii=True).encode()
        if hashlib.sha256(encoded).hexdigest() != record["snapshot_sha256"]:
            raise ValueError("agent_snapshot_modified")
        safe = Context().clean({k: v for k, v in record.items() if k != "snapshot"})
        safe["snapshot"] = record["snapshot"]
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(safe, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def read(self, run_id: str) -> dict:
        """按规范运行 ID 读取 JSON 字典；ID、文件不存在和解码错误直接上抛。"""
        with self.path(run_id).open(encoding="utf-8") as handle:
            return json.load(handle)

    def report(self, run_id: str) -> str:
        """读取指定运行并返回 Markdown 报告；沿用读取与渲染过程的错误。"""
        return render_report(self.read(run_id))

    def delete(self, run_id: str) -> bool:
        """删除一次编排运行的 JSON 证据。

        作用：让历史列表和其它两类记录一样可清理。
        实现：先经 ``path`` 校验规范 UUID 并确认落在本目录内（防目录穿越），
        文件不存在返回 False 交由调用方决定 404，删除本身失败向上抛。
        运行中的记录不应到这里——调用方须先确认它已到终态。
        """
        path = self.path(run_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def recover(self):
        """把目录内 queued/running 记录标记为中断，并保守结算在途预算。

        未完成模型调用改为 unknown，HTTP 预留计入未知请求；不重放请求，
        损坏记录或保存失败直接上抛供启动流程处理。
        """
        for path in self.root.glob("*.json"):
            record = self.read(path.stem)
            if record["status"] not in {"queued", "running"}:
                continue
            record.update(status="interrupted", stop_reason="interrupted", report_status="partial")
            for kind in ("target_http", "model_call"):
                budget = record["budget"][kind]
                if kind == "target_http":
                    budget["unknown_requests"] += budget["reserved"]
                budget["consumed"] += budget["reserved"]
                budget["reserved"] = 0
            for call in record["model_calls"]:
                if call["status"] == "dispatching":
                    call["status"] = "unknown"
            self.save(record)
