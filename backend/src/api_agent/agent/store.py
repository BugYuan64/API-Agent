"""Atomic local artifacts. IDs are server generated, never caller paths."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from api_agent.agent.report import render_report
from api_agent.infrastructure.privacy import Context


class AgentStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, run_id: str) -> Path:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError("invalid_agent_run_id")
        path = (self.root / f"{run_id}.json").resolve()
        if path.parent != self.root:
            raise ValueError("invalid_agent_run_id")
        return path

    def save(self, record: dict):
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
        with self.path(run_id).open(encoding="utf-8") as handle:
            return json.load(handle)

    def report(self, run_id: str) -> str:
        return render_report(self.read(run_id))

    def recover(self):
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
