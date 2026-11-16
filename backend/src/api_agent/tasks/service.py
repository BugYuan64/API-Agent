"""Durable document-task lifecycle, sequential execution, cancellation and LLM summary."""

import asyncio
import hashlib
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from api_agent.agent.service import AgentError, CallBudget
from api_agent.agent.store import AgentStore
from api_agent.infrastructure.targets import validate_target
from api_agent.tasks.documents import NativeCaseGenerator
from api_agent.tasks.executor import CaseExecutor


def report(record):
    """Render factual totals and model commentary as an exportable Markdown document."""
    results = record["results"]
    lines = [
        "# API 文档测试总结",
        "",
        f"任务：{record['id']}",
        f"状态：{record['status']}；阶段：{record['stage']}",
        f"计划 {len(record['snapshot']['cases'])} 条；已记录 {len(results)} 条",
        "",
        "## 用例结果",
        "",
    ]
    for result in results:
        lines.append(f"- {result['index'] + 1}. {result['name']} — {result['status']}")
        if result.get("reason"):
            lines.append(f"  - {result['reason']}")
        for assertion in result.get("assertions", []):
            lines.append(
                f"  - {assertion['kind']}：{assertion['passed']}；"
                f"期望 {assertion['expected']!r}；实际 {assertion['actual']!r}"
            )
    lines += [
        "",
        "## AI 总结",
        "",
        record.get("summary") or "未生成 AI 总结，已有确定性结果已保留。",
    ]
    lines += [
        "",
        "## 限制",
        "",
        "生成器为内置契约/示例引擎；无断言为 unknown。模型解释需结合证据核实。",
    ]
    lines += [f"- {warning}" for warning in record["snapshot"].get("warnings", [])]
    if record.get("error_kind"):
        lines.append(f"- {record['error_kind']}")
    return "\n".join(lines) + "\n"


async def summarize_results(agent, results, warnings):
    """Summarize all results in bounded chunks, then combine every chunk's commentary."""
    chunks = []
    batch = []
    for result in results:
        compact = deepcopy({key: value for key, value in result.items() if key != "evidence"})
        for assertion in compact.get("assertions", []):
            for key in ("expected", "actual"):
                encoded = json.dumps(assertion[key], ensure_ascii=False)
                if len(encoded.encode()) > 2000:
                    assertion[key] = {"preview": encoded[:500], "truncated": True}
        if batch and len(json.dumps([*batch, compact], ensure_ascii=False).encode()) > 48000:
            chunks.append(batch)
            batch = []
        batch.append(compact)
    if batch:
        chunks.append(batch)
    budget = CallBudget(limit=2 * (2 * len(chunks) + 4))
    parts = []
    for chunk in chunks:
        commentary = await agent.report({"results": chunk, "warnings": warnings}, budget)
        parts.append(commentary.model_dump())
    if len(parts) == 1:
        return commentary
    while len(parts) > 5:
        reduced = []
        for offset in range(0, len(parts), 5):
            summary = await agent.report({"batch_summaries": parts[offset:offset + 5]}, budget)
            reduced.append(summary.model_dump())
        parts = reduced
    return await agent.report({"batch_summaries": parts, "total_results": len(results)}, budget)


class TaskManager:
    """Own bounded background tasks and persist evidence before every next case."""

    def __init__(self, root, settings, streams):
        """Reuse atomic artifact storage; keep raw documents and credentials out of records."""
        self.store = AgentStore(root)
        self.settings = settings
        self.executor = CaseExecutor(settings, streams)
        self.active = {}

    def recover(self):
        """Mark interrupted tasks without replaying requests after a process restart."""
        for path in self.store.root.glob("*.json"):
            record = self.store.read(path.stem)
            if record["status"] in {"queued", "running"}:
                self._mark_incomplete(record)
                record.update(
                    status="interrupted", stage="finished", error_kind="service_restarted"
                )
                self.store.save(record)

    def start(self, request, agent):
        """Validate every case and freeze the safe plan before scheduling any target traffic."""
        if len(self.active) >= 4:
            raise ValueError("task_capacity_exceeded")
        parsed, origin = validate_target(request.origin, self.settings.target_allowed_origins)
        if parsed.path not in {"", "/"} or parsed.query:
            raise ValueError("origin_must_not_include_path")
        request.origin = origin
        cases = NativeCaseGenerator().generate(request.catalog)
        if not cases or len(cases) > 200:
            raise ValueError("case_limit_exceeded")
        for case in cases:
            validate_target(origin + case.path, self.settings.target_allowed_origins)
            if case.body is not None and case.method in {"GET", "HEAD", "OPTIONS"}:
                raise ValueError("body_not_allowed_for_method")
        agent.context.learn(request.model_dump())
        snapshot = agent.context.clean(
            {
                "origin": origin,
                "cases": [case.model_dump() for case in cases],
                "warnings": request.catalog.warnings,
                "engine": "native-contract-v1",
            }
        )
        record = {
            "id": str(uuid.uuid4()),
            "status": "queued",
            "stage": "generated",
            "created_at": datetime.now(UTC).isoformat(),
            "snapshot": snapshot,
            "snapshot_sha256": hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest(),
            "results": [],
            "summary": None,
            "error_kind": None,
        }
        self.store.save(record)
        task = asyncio.create_task(self._run(record, request, cases, agent))
        self.active[record["id"]] = task
        task.add_done_callback(lambda completed: self.active.pop(record["id"], None))
        return record

    async def _run(self, record, request, cases, agent):
        """Execute in order, retain skipped/failed cases, then summarize all recorded verdicts."""
        record.update(status="running", stage="executing")
        try:
            async with asyncio.timeout(request.wall_time_seconds):
                for index, case in enumerate(cases):
                    record["current_index"] = index
                    self.store.save(record)
                    if not request.allow_writes and (
                        case.method not in {"GET", "HEAD", "OPTIONS"} or case.messages
                    ):
                        result = {
                            "status": "skipped",
                            "reason": "write_permission_required",
                            "assertions": [],
                        }
                    else:
                        try:
                            result = await self.executor.execute(case, request)
                        except (ValueError, OSError):
                            result = {
                                "status": "error",
                                "reason": "case_execution_rejected",
                                "assertions": [],
                            }
                    result.update(
                        index=index, name=case.name, source=case.source, protocol=case.protocol
                    )
                    agent.context.learn(result)
                    response = (result.get("evidence") or {}).get("response")
                    if response and response.get("body"):
                        response["body"] = agent.context.body(response["body"])
                    record["results"].append(agent.context.clean(result))
                    self.store.save(record)
                record["stage"] = "summarizing"
                self.store.save(record)
                summary = await summarize_results(
                    agent, record["results"], request.catalog.warnings
                )
                record["summary"] = summary.overview + "\n" + "\n".join(summary.recommendations)
                record.update(status="completed", stage="finished")
        except asyncio.CancelledError:
            record.update(status="cancelled", stage="finished", error_kind="cancelled")
        except TimeoutError:
            record.update(status="timed_out", stage="finished", error_kind="task_deadline_exceeded")
        except AgentError as error:
            record.update(status="partial", stage="finished", error_kind=str(error))
        except Exception:
            record.update(status="partial", stage="finished", error_kind="task_or_summary_failed")
        finally:
            self._mark_incomplete(record)
            self.store.save(record)

    @staticmethod
    def _mark_incomplete(record):
        """Retain an explicit unknown verdict for a dispatched case interrupted before evidence."""
        index = record.get("current_index")
        if index is None or any(result["index"] == index for result in record["results"]):
            return
        case = record["snapshot"]["cases"][index]
        record["results"].append(
            {
                "index": index,
                "name": case["name"],
                "source": case["source"],
                "protocol": case["protocol"],
                "status": "unknown",
                "reason": "interrupted_without_complete_evidence",
                "assertions": [],
            }
        )

    async def cancel(self, task_id):
        """Cancel only the requested owned task and wait for transport cleanup."""
        task = self.active.get(task_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            record = self.store.read(task_id)
            if record["status"] == "queued":
                record.update(status="cancelled", stage="finished")
                self.store.save(record)
        return self.store.read(task_id)

    async def shutdown(self):
        """Stop task workers before the shared stream manager and database close."""
        await asyncio.gather(*(self.cancel(task_id) for task_id in list(self.active)))
