"""对完整源码快照分批调用模型生成接口文档，保留进度、遗漏与部分成果。"""

import asyncio
import re
from collections import Counter
from copy import deepcopy

from api_agent.agent.service import AgentError, CallBudget
from api_agent.infrastructure.api import ApiError
from api_agent.infrastructure.orm import new_id, utc_now
from api_agent.projects.document_schema import DocumentBatch, DocumentEndpoint
from api_agent.projects.document_store import DocumentStore
from api_agent.projects.source import sanitize_source

BATCH_CHARS = 18_000
CONTEXT_CHARS = 24_000
PENDING = "AI 尚未补全此接口，请核对源码声明。"


def prepare_source_context(data: dict, context) -> dict:
    """源码专用脱敏保留类型与变量引用，模型密钥仍从每个文本片段中移除。"""
    safe = context.clean(
        {
            key: value
            for key, value in data.items()
            if key not in {"source_files", "related_sources"}
        }
    )
    for field in ("source_files", "related_sources"):
        safe[field] = []
        for item in data[field]:
            cleaned = dict(item)
            for key in ("content", "preceding_context", "following_context"):
                if key in cleaned:
                    cleaned[key] = sanitize_source(
                        cleaned[key],
                        secrets=context.redactor.secrets,
                        path=item["path"],
                    )
            safe[field].append(cleaned)
    return safe


def source_batches(files: list[dict], size: int = BATCH_CHARS) -> list[list[dict]]:
    """按行分批覆盖每个源码字符，片段附前后文处理跨边界注解，大文件不截断。"""
    batches = []
    current = []
    used = 0
    for source in files:
        content = source["content"]
        offset = 0
        line = 1
        # 空文件也需要纳入扫描计数与模型确认。
        while offset < len(content) or (offset == 0 and not content):
            end = min(offset + size, len(content))
            if end < len(content):
                newline = content.rfind("\n", offset, end)
                if newline >= offset:
                    end = newline + 1
            chunk = content[offset:end]
            if current and used + len(chunk) > size:
                batches.append(current)
                current, used = [], 0
            current.append(
                {
                    "path": source["path"],
                    "start_line": line,
                    "content": chunk,
                    "preceding_context": content[max(0, offset - 1200) : offset],
                    "following_context": content[end : end + 1200],
                }
            )
            used += len(chunk)
            offset += len(chunk)
            line += chunk.count("\n")
            if not content:
                break
    if current:
        batches.append(current)
    return batches


def related_sources(batch: list[dict], files: list[dict]) -> tuple[list[dict], bool]:
    """补充批次引用的类型或模块定义；超出上下文预算时明确报告关联信息不全。"""
    names = set(re.findall(r"\b[A-Za-z_]\w*\b", "\n".join(x["content"] for x in batch)))
    included = {item["path"] for item in batch}
    context = []
    remaining = CONTEXT_CHARS
    omitted = False
    for source in files:
        if source["path"] in included:
            continue
        stem = source["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        definitions = set(
            re.findall(r"\b(?:class|interface|record|struct|type)\s+(\w+)", source["content"])
        )
        if stem not in names and not definitions & names:
            continue
        if len(source["content"]) > remaining:
            omitted = True
            continue
        context.append({"path": source["path"], "content": source["content"]})
        remaining -= len(source["content"])
    return context, omitted


def merge_endpoints(existing: list[dict], additions: list[dict]) -> list[dict]:
    """按方法与完整路径聚合批次，合并证据及参数，避免后批覆盖已知字段。"""
    merged = {(item["method"], item["path"]): deepcopy(item) for item in existing}
    for item in additions:
        key = (item["method"], item["path"])
        substantive = any(
            item.get(key)
            for key in (
                "summary",
                "description",
                "parameters",
                "responses",
                "request_body",
                "request_schema",
                "authentication",
            )
        )
        if not substantive and PENDING not in item["uncertainties"]:
            item = {**item, "uncertainties": [*item["uncertainties"], PENDING]}
        if key not in merged:
            merged[key] = deepcopy(item)
            continue
        target = merged[key]
        for field in ("source_files", "uncertainties"):
            target[field] = list(dict.fromkeys(target[field] + item[field]))
        if substantive:
            target["uncertainties"] = [x for x in target["uncertainties"] if x != PENDING]
        for field, identifiers in (
            ("parameters", ("location", "name")),
            ("responses", ("status",)),
        ):
            values = {tuple(x[k] for k in identifiers): x for x in target[field]}
            for addition in item[field]:
                identity = tuple(addition[k] for k in identifiers)
                if identity not in values:
                    values[identity] = deepcopy(addition)
                    continue
                original = values[identity]
                for name, value in addition.items():
                    if value is None or value == "" or value == {}:
                        continue
                    if name not in original or original[name] in (None, "", {}):
                        original[name] = deepcopy(value)
                    elif original[name] != value:
                        target["uncertainties"].append(
                            f"跨文件分析对 {'/'.join(identity)} 的 {name} 存在不同解释，"
                            "请核对源码。"
                        )
            target[field] = list(values.values())
        for field in (
            "summary",
            "description",
            "request_body",
            "request_schema",
            "request_content_type",
            "authentication",
        ):
            if item.get(field):
                if not target.get(field) or PENDING in target.get("description", ""):
                    target[field] = deepcopy(item[field])
                elif target[field] != item[field] and field == "description":
                    target[field] += "\n\n" + item[field]
                elif target[field] != item[field]:
                    target["uncertainties"].append(
                        f"跨文件分析对 {field} 存在不同解释，请核对源码。"
                    )
        target["uncertainties"] = list(dict.fromkeys(target["uncertainties"]))
    return sorted(merged.values(), key=lambda item: (item["path"], item["method"]))


class DocumentManager:
    """单进程后台生成管理器，模型调用脱离 HTTP 请求并且随应用关闭释放。"""

    def __init__(self, store: DocumentStore):
        """保存工件仓储及在途任务，最多允许两个项目并行生成。"""
        self.store = store
        self.active: dict[str, asyncio.Task] = {}

    def start(self, project, source: dict, agent) -> dict:
        """冻结扫描版本后受理任务；同项目在途请求拒绝以防重复消耗模型额度。"""
        if len(self.active) >= 2:
            raise ApiError(429, "document_capacity", "已有两个文档任务正在生成，请稍后重试")
        for record in self.store.list(project.id, "documents"):
            if record["id"] in self.active:
                raise ApiError(409, "document_active", "此项目已有文档生成任务，请等待或取消")
        if not source["files"]:
            raise ApiError(400, "source_empty", "项目中没有可分析的源码文件")
        batches = source_batches(source["files"])
        endpoints = [
            DocumentEndpoint(
                method=item["method"],
                path=item["path"],
                source_files=[item["file"]],
                uncertainties=[PENDING, *item.get("warnings", [])],
            ).model_dump(mode="json")
            for item in source["endpoints"]
        ]
        now = utc_now().isoformat()
        record = {
            "schema_version": "1",
            "id": new_id(),
            "project_id": project.id,
            "project_name": project.name,
            "source_id": source["id"],
            "source_sha256": source["sha256"],
            "created_at": now,
            "updated_at": now,
            "status": "queued",
            "processed_files": 0,
            "total_files": len(source["files"]),
            "processed_batches": 0,
            "total_batches": len(batches),
            "model_calls": 0,
            "model": getattr(agent.client, "model", "unknown"),
            "scan_complete": source["complete"],
            "issues": source["issues"],
            "warnings": list(source["warnings"]),
            "error": None,
            "endpoints": merge_endpoints([], endpoints),
        }
        self.store.save(project.id, "documents", record)
        task = asyncio.create_task(self._run(record, source, batches, agent))
        self.active[record["id"]] = task
        task.add_done_callback(lambda finished: self.active.pop(record["id"], None))
        return deepcopy(record)

    async def _run(self, record: dict, source: dict, batches: list, agent):
        """逐批校验文件确认和接口来源，失败/取消保存部分文档，成功核对静态清单。"""
        remaining = Counter(item["path"] for batch in batches for item in batch)

        async def save_progress():
            """模型预算变动在派发前持久化，失败重启仍能识别未完成批次。"""
            record["model_calls"] = budget.consumed + budget.reserved
            self._save(record)

        budget = CallBudget(limit=len(batches) * 2, changed=save_progress)
        try:
            record["status"] = "running"
            self._save(record)
            for batch in batches:
                current_paths = {item["path"] for item in batch}
                context, omitted = related_sources(batch, source["files"])
                allowed_paths = current_paths | {item["path"] for item in context}
                known = [x for x in source["endpoints"] if x["file"] in current_paths]

                def validate(output, current_paths=current_paths, allowed_paths=allowed_paths):
                    """必须逐个确认本批文件，接口只能引用已提供的源码。"""
                    if set(output.reviewed_files) != current_paths:
                        raise ValueError("incomplete_file_review")
                    for endpoint in output.endpoints:
                        if not set(endpoint.source_files) <= allowed_paths:
                            raise ValueError("unknown_source_file")
                        if not set(endpoint.source_files) & current_paths:
                            raise ValueError("endpoint_outside_batch")

                output = await agent.structured(
                    "document_source",
                    {"source_files": batch, "related_sources": context, "known_routes": known},
                    DocumentBatch,
                    budget,
                    validate=validate,
                    prepare_context=prepare_source_context,
                )
                additions = [item.model_dump(mode="json") for item in output.endpoints]
                if omitted:
                    for item in additions:
                        item["uncertainties"].append(
                            "部分跨文件类型定义未完整提供给此批分析，字段需核对。"
                        )
                record["endpoints"] = merge_endpoints(record["endpoints"], additions)
                record["warnings"].extend(output.warnings)
                if omitted:
                    record["warnings"].append(
                        "部分跨文件关联定义超出单次上下文容量，相关文件已独立分析，嵌套字段需核对。"
                    )
                for item in batch:
                    remaining[item["path"]] -= 1
                record["processed_files"] = sum(count == 0 for count in remaining.values())
                record["processed_batches"] += 1
                record["warnings"] = list(dict.fromkeys(record["warnings"]))
                self._save(record)
            pending = sum(PENDING in item["uncertainties"] for item in record["endpoints"])
            if pending:
                record["warnings"].append(f"静态扫描中有 {pending} 个接口尚未得到 AI 补全。")
            if not record["endpoints"]:
                record["warnings"].append("未识别到接口，请确认目录和框架，并核对动态注册逻辑。")
            record["status"] = (
                "completed"
                if source["complete"] and not pending and record["endpoints"]
                else "partial"
            )
        except asyncio.CancelledError:
            record.update(status="cancelled", error="生成已取消，已完成结果可导出。")
        except AgentError as error:
            record.update(status="failed", error=f"模型生成未完成（{error}），已完成结果可导出。")
        except Exception:
            record.update(status="failed", error="文档生成或保存失败，已完成结果可导出。")
        finally:
            self._save(record)

    def _save(self, record: dict):
        """刷新更新时间后原子保存；文件系统失败向上游传播。"""
        record["updated_at"] = utc_now().isoformat()
        self.store.save(record["project_id"], "documents", record)

    async def cancel(self, project_id: str, document_id: str) -> dict:
        """取消在途调用并等待工件收尾，重复取消返回当前终态。"""
        record = self.store.read(project_id, "documents", document_id)
        task = self.active.get(document_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            record = self.store.read(project_id, "documents", document_id)
            if record["status"] in {"queued", "running"}:
                record.update(status="cancelled", error="生成已取消，已完成结果可导出。")
                self._save(record)
        return record

    async def shutdown(self):
        """停止所有后台任务并等待取消写入完成，避免应用销毁后继续请求模型。"""
        for task in list(self.active.values()):
            task.cancel()
        await asyncio.gather(*list(self.active.values()), return_exceptions=True)
