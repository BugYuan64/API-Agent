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
from api_agent.tasks.evidence import TaskEvidence
from api_agent.tasks.executor import CaseExecutor
from api_agent.tasks.schemas import Case


def public_task(record):
    """Keep raw local attachments out of task API responses and live frames."""
    snapshot = record["snapshot"]
    document = snapshot.get("document")
    return {
        **record,
        "snapshot": {
            **{key: value for key, value in snapshot.items() if key != "document"},
            **({"document": {"filename": document["filename"]}} if document else {}),
        },
    }


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


async def summarize_results(agent, results, warnings, publish=None):
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
    if publish and len(chunks) <= 1:
        return await agent.stream_task_summary({"results": batch, "warnings": warnings}, publish)
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
            summary = await agent.report({"batch_summaries": parts[offset : offset + 5]}, budget)
            reduced.append(summary.model_dump())
        parts = reduced
    combined = {"batch_summaries": parts, "total_results": len(results)}
    if publish:
        return await agent.stream_task_summary(combined, publish)
    return await agent.report(combined, budget)


class LiveTask:
    """Keep only bounded current-case evidence and a wakeup signal alongside the worker."""

    def __init__(self, record):
        self.record = record
        self.events = []
        self.events_seen = 0
        self.live_summary = ""
        self.changed = asyncio.Event()

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def frame(self, sent_results):
        """Send only newly completed cases and the latest view of in-flight work."""
        record = self.record
        return {
            **{
                key: record.get(key)
                for key in ("status", "stage", "current_index", "active_stream", "error_kind")
            },
            "results": record["results"][sent_results:],
            "events": self.events,
            "events_seen": self.events_seen,
            "summary": record["summary"] or self.live_summary or None,
        }


class TaskManager:
    """Own bounded background tasks and persist evidence before every next case."""

    def __init__(self, root, settings, streams):
        """Reuse atomic artifact storage with separate live state per running task."""
        self.store = AgentStore(root)
        self.settings = settings
        self.executor = CaseExecutor(settings, streams)
        self.active = {}
        self.live = {}

    def recover(self):
        """Mark interrupted tasks without replaying requests after a process restart."""
        for path in self.store.root.glob("*.json"):
            record = self.store.read(path.stem)
            if record["status"] in {"queued", "running"}:
                record.pop("active_stream", None)
                self._mark_incomplete(record)
                record.update(
                    status="interrupted", stage="finished", error_kind="service_restarted"
                )
                self.store.save(record)

    def start(self, request, agent):
        """Compile the reviewed catalog and attach its optional source document locally."""
        cases, operations = NativeCaseGenerator().plan(request.catalog)
        return self._start(
            request,
            agent,
            cases,
            operations,
            request.catalog.warnings,
            request.catalog.title,
            request.document.model_dump() if request.document else None,
        )

    def rerun(self, request, agent, source):
        """Apply edits to a fresh case list without changing saved order, groups or attachment."""
        snapshot = source["snapshot"]
        cases = [Case.model_validate(case) for case in snapshot["cases"]]
        for index, edited in request.case_edits.items():
            if not 0 <= index < len(cases):
                raise ValueError("unknown_case_index")
            original = cases[index]
            if (edited.method, edited.protocol, edited.source) != (
                original.method,
                original.protocol,
                original.source,
            ):
                raise ValueError("case_identity_changed")
            cases[index] = edited
        return self._start(
            request,
            agent,
            cases,
            snapshot.get("operations", []),
            snapshot.get("warnings", []),
            source.get("title"),
            snapshot.get("document"),
        )

    def _start(self, request, agent, cases, operations, warnings, title, document):
        """Validate every case and freeze the safe plan before scheduling any target traffic."""
        if len(self.active) >= 4:
            raise ValueError("task_capacity_exceeded")
        parsed, origin = validate_target(request.origin, self.settings.target_allowed_origins)
        if "?" in request.origin or "#" in request.origin or "\\" in request.origin:
            raise ValueError("invalid_target_base_url")
        request.origin = origin + parsed.path.rstrip("/")
        if not cases or len(cases) > 200:
            raise ValueError("case_limit_exceeded")
        for case in cases:
            validate_target(request.origin + case.path, self.settings.target_allowed_origins)
            if case.body is not None and case.method in {"GET", "HEAD", "OPTIONS"}:
                raise ValueError("body_not_allowed_for_method")
        raw_snapshot = {
            "origin": request.origin,
            "cases": [case.model_dump() for case in cases],
            "operations": operations,
            "warnings": warnings,
            "engine": "native-contract-v1",
        }
        agent.context.learn(raw_snapshot)
        snapshot = agent.context.clean(raw_snapshot)
        if document is not None:
            snapshot["document"] = document
        record = {
            "id": str(uuid.uuid4()),
            "title": agent.context.clean(title) if title else None,
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
        live = self.live[record["id"]] = LiveTask(record)
        task = asyncio.create_task(self._run(record, request, cases, agent, live))
        self.active[record["id"]] = task

        def remove_worker(completed):
            self.active.pop(record["id"], None)
            self.live.pop(record["id"], None)
            live.notify()

        task.add_done_callback(remove_worker)
        return record

    async def observe(self, record):
        """Follow a run over NDJSON. Reopening starts with its current bounded state."""
        live = self.live.get(record["id"]) or LiveTask(record)
        sent = 0
        while True:
            changed = live.changed
            frame = live.frame(sent)
            sent += len(frame["results"])
            terminal = frame["status"] not in {"queued", "running"}
            yield json.dumps({"type": "progress", **frame}, ensure_ascii=False) + "\n"
            if terminal:
                return
            try:
                await asyncio.wait_for(changed.wait(), timeout=10)
            except TimeoutError:
                yield '{"type":"heartbeat"}\n'
            if record["id"] not in self.live and live.record["status"] in {"queued", "running"}:
                live = LiveTask(self.store.read(record["id"]))

    async def _run(self, record, request, cases, agent, live):
        """Execute in order, retain skipped/failed cases, then summarize all recorded verdicts."""
        record.update(status="running", stage="executing")

        async def observe_sse(event):
            """Mark confirmed SSE responses and forward complete, redacted event previews."""
            if event is None:
                record["active_stream"] = "sse"
                self.store.save(record)
            else:
                live.events_seen += 1
                if len(live.events) < 40:
                    # Replace the list on append so a frame remains coherent across yields.
                    clean = agent.context.clean(
                        {"event": event["event"], "data": TaskEvidence.redact_body(event["data"])}
                    )
                    live.events = [*live.events, {**clean, "data": clean["data"][:1000]}]
            live.notify()

        async def publish_summary(text):
            """Expose model commentary while keeping the summary snapshot consistent on reload."""
            live.live_summary = text
            live.notify()

        try:
            async with asyncio.timeout(request.wall_time_seconds):
                for index, case in enumerate(cases):
                    record["current_index"] = index
                    live.events = []
                    live.events_seen = 0
                    self.store.save(record)
                    live.notify()
                    if not request.allow_writes and (
                        case.method not in {"GET", "HEAD", "OPTIONS"} or case.messages
                    ):
                        result = {
                            "status": "permission_required",
                            "reason": "write_permission_required",
                            "assertions": [],
                        }
                    else:
                        try:
                            if case.protocol == "websocket":
                                record["active_stream"] = case.protocol
                                self.store.save(record)
                                live.notify()
                            result = await self.executor.execute(
                                case,
                                request,
                                on_sse=observe_sse,
                            )
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
                    record.pop("active_stream", None)
                    live.events = []
                    live.events_seen = 0
                    self.store.save(record)
                    live.notify()
                if agent.client is not None:
                    record["stage"] = "summarizing"
                    self.store.save(record)
                    live.notify()
                    record["summary"] = await summarize_results(
                        agent, record["results"], record["snapshot"]["warnings"], publish_summary
                    )
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
            record.pop("active_stream", None)
            live.events = []
            live.events_seen = 0
            if not record["summary"] and live.live_summary:
                record["summary"] = live.live_summary
            self._mark_incomplete(record)
            self.store.save(record)
            live.notify()

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
