"""Durable document-task lifecycle, sequential execution, cancellation and LLM summary."""

import asyncio
import hashlib
import inspect
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from api_agent.agent.service import AgentError, AgentService, CallBudget
from api_agent.agent.store import AgentStore
from api_agent.infrastructure.logging import REDACTED
from api_agent.infrastructure.privacy import TEMPLATE_ONLY_SECRET
from api_agent.infrastructure.targets import validate_target
from api_agent.tasks.documents import NativeCaseGenerator
from api_agent.tasks.evidence import TaskEvidence
from api_agent.tasks.executor import CaseExecutor
from api_agent.tasks.schemas import Case, TaskRunMetadata

DEFAULT_AI_BADGE = "AI 测试"
COOLDOWN_SLICE_SECONDS = 0.1
SUMMARY_RETRY_DELAY_SECONDS = 0.4
_TRANSIENT_SUMMARY_ERRORS = frozenset({"model_connection_failed", "model_response_error"})


class TaskActiveError(RuntimeError):
    """Raised when attempting to delete a task that is still queued or running."""


async def resolve_task_agent(
    session,
    settings,
    *,
    for_generation: bool = False,
    allow_unconfigured: bool = True,
) -> AgentService:
    """Resolve an AgentService from session/settings without coupling to FastAPI Request."""
    from api_agent.llm.service import LLMSettingService

    client = await LLMSettingService(session, settings).get_client(for_generation=for_generation)
    if not client.is_configured:
        if not allow_unconfigured:
            raise AgentError("model_not_configured")
        return AgentService(settings, None)
    return AgentService(settings, client)


def task_list_item(record: dict) -> dict:
    """Project bounded task metadata including origin, source, badge and round config."""
    item = {
        key: value for key, value in record.items() if key not in {"snapshot", "results", "summary"}
    }
    snapshot = record.get("snapshot")
    if isinstance(snapshot, dict):
        origin = snapshot.get("origin")
        if isinstance(origin, str):
            item["origin"] = origin
        if "source" not in item:
            item["source"] = snapshot.get("source", "manual")
        if "badge" not in item:
            item["badge"] = snapshot.get("badge")
        if "total_rounds" not in item:
            item["total_rounds"] = snapshot.get("total_rounds", 1)
        if "cooldown_ms" not in item:
            item["cooldown_ms"] = snapshot.get("cooldown_ms", 500)
        if "tests_per_url" not in item and "tests_per_url" in snapshot:
            item["tests_per_url"] = snapshot.get("tests_per_url")
    else:
        item.setdefault("source", "manual")
        item.setdefault("badge", None)
        item.setdefault("total_rounds", 1)
        item.setdefault("cooldown_ms", 500)
    return item


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
    snapshot = record.get("snapshot") or {}
    total_rounds = record.get("total_rounds") or snapshot.get("total_rounds") or 1
    cooldown_ms = record.get("cooldown_ms")
    if cooldown_ms is None:
        cooldown_ms = snapshot.get("cooldown_ms")
    source = record.get("source") or snapshot.get("source")
    badge = record.get("badge") or snapshot.get("badge")
    lines = [
        "# API 文档测试总结",
        "",
        f"任务：{record['id']}",
        f"状态：{record['status']}；阶段：{record['stage']}",
        f"计划 {len(record['snapshot']['cases'])} 条；已记录 {len(results)} 条",
    ]
    if source == "ai" or badge:
        source_label = f"{badge}（{source}）" if badge and source else (badge or source)
        lines.append(f"来源：{source_label}")
    if total_rounds > 1:
        cooldown_suffix = f"；每轮冷却 {cooldown_ms} ms" if cooldown_ms is not None else ""
        lines.append(f"轮次：共 {total_rounds} 轮{cooldown_suffix}")
    lines += [
        "",
        "## 用例结果",
        "",
    ]
    for result in results:
        round_prefix = (
            f"[第 {result['round']} 轮] " if total_rounds > 1 and result.get("round") else ""
        )
        lines.append(
            f"- {result['index'] + 1}. {round_prefix}{result['name']} — {result['status']}"
        )
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


async def _run_summary_with_retry(fn, *, max_attempts: int = 2):
    """对上游瞬时断流或空响应做有界单次重试，减少大任务总结因短暂抖动降级为 partial。"""
    for attempt in range(max_attempts):
        try:
            return await fn()
        except AgentError as error:
            if str(error) in _TRANSIENT_SUMMARY_ERRORS and attempt + 1 < max_attempts:
                if SUMMARY_RETRY_DELAY_SECONDS > 0:
                    await asyncio.sleep(SUMMARY_RETRY_DELAY_SECONDS)
                continue
            raise


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
        return await _run_summary_with_retry(
            lambda: agent.stream_task_summary({"results": batch, "warnings": warnings}, publish)
        )
    budget = CallBudget(limit=2 * (2 * len(chunks) + 4))
    parts = []
    for chunk in chunks:
        commentary = await _run_summary_with_retry(
            lambda c=chunk: agent.report({"results": c, "warnings": warnings}, budget)
        )
        parts.append(commentary.model_dump())
    if len(parts) == 1:
        return commentary
    while len(parts) > 5:
        reduced = []
        for offset in range(0, len(parts), 5):
            slice_parts = parts[offset : offset + 5]
            summary = await _run_summary_with_retry(
                lambda s=slice_parts: agent.report({"batch_summaries": s}, budget)
            )
            reduced.append(summary.model_dump())
        parts = reduced
    combined = {"batch_summaries": parts, "total_results": len(results)}
    if publish:
        return await _run_summary_with_retry(lambda: agent.stream_task_summary(combined, publish))
    return await _run_summary_with_retry(lambda: agent.report(combined, budget))


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
                for key in (
                    "status",
                    "stage",
                    "current_index",
                    "current_round",
                    "total_rounds",
                    "cooldown_remaining_ms",
                    "active_stream",
                    "error_kind",
                )
            },
            "results": record["results"][sent_results:],
            "events": self.events,
            "events_seen": self.events_seen,
            "summary": record["summary"] or self.live_summary or None,
        }


async def _invoke_progress(callback, frame: dict) -> None:
    """Invoke a sync or async progress callback cleanly."""
    outcome = callback(frame)
    if inspect.isawaitable(outcome):
        await outcome


async def _await_task_done(task: asyncio.Task) -> None:
    """Await background task completion without cancelling it if the waiter is cancelled."""
    done = asyncio.Event()
    task.add_done_callback(lambda _completed: done.set())
    if task.done():
        done.set()
    await done.wait()


class TaskManager:
    """Own bounded background tasks and persist evidence before every next case."""

    def __init__(self, root, settings, streams):
        """Reuse atomic artifact storage with separate live state per running task."""
        self.store = AgentStore(root)
        self.settings = settings
        self.executor = CaseExecutor(settings, streams)
        self.active = {}
        self.live = {}
        # 仅在当前进程内存缓存脱敏前的静态请求头（最多 32 条任务，绝不落盘，退出即清空），
        # 使含静态 token 的智能群测历史可直接 rerun 而不发出字面量 [REDACTED]。
        self._ephemeral_headers: dict[str, dict[int, dict[str, str]]] = {}

    def recover(self):
        """Mark interrupted tasks without replaying requests after a process restart."""
        for path in self.store.root.glob("*.json"):
            record = self.store.read(path.stem)
            if record["status"] in {"queued", "running"}:
                record.pop("active_stream", None)
                record["cooldown_remaining_ms"] = None
                self._mark_incomplete(record)
                record.update(
                    status="interrupted", stage="finished", error_kind="service_restarted"
                )
                self.store.save(record)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        """Return bounded persisted task summaries ordered by latest modification time."""
        if not self.store.root.exists():
            return []
        bounded = max(1, min(200, int(limit)))
        paths = sorted(
            self.store.root.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:bounded]
        return [task_list_item(self.store.read(path.stem)) for path in paths]

    def get_task(self, task_id: str) -> dict:
        """Read a persisted task record; raise FileNotFoundError or ValueError when absent."""
        return self.store.read(task_id)

    def delete_task(self, task_id: str) -> None:
        """Delete only terminal artifacts; raise TaskActiveError when queued or running."""
        record = self.store.read(task_id)
        if task_id in self.active or record.get("status") in {"queued", "running"}:
            raise TaskActiveError("task_active")
        self.store.delete(task_id)

    async def wait_for_completion(self, task_id: str, on_progress=None) -> dict:
        """Await a task's terminal record without polling loops or cancelling the worker."""
        record = self.get_task(task_id)
        if on_progress is None:
            task = self.active.get(task_id)
            if task is None:
                return record
            await _await_task_done(task)
            return self.get_task(task_id)

        sent = 0
        live = self.live.get(task_id)
        if live is None:
            await _invoke_progress(on_progress, LiveTask(record).frame(0))
            return record

        while True:
            changed = live.changed
            frame = live.frame(sent)
            sent += len(frame["results"])
            await _invoke_progress(on_progress, frame)
            if frame["status"] not in {"queued", "running"}:
                task = self.active.get(task_id)
                if task is not None:
                    await _await_task_done(task)
                return self.get_task(task_id)
            await changed.wait()
            if task_id not in self.live and live.record["status"] in {"queued", "running"}:
                live = LiveTask(self.get_task(task_id))

    def start(self, request, agent, metadata: TaskRunMetadata | None = None):
        """Compile the reviewed catalog and attach its optional source document locally."""
        cases, operations = NativeCaseGenerator().plan(request.catalog)
        merged_vars = {**request.catalog.variables, **request.variables}
        return self._start(
            request,
            agent,
            cases,
            operations,
            request.catalog.warnings,
            request.catalog.title,
            request.document.model_dump() if request.document else None,
            merged_vars,
            metadata=metadata,
        )

    def rerun(self, request, agent, source, metadata: TaskRunMetadata | None = None):
        """Apply edits to a fresh case list without changing saved order, groups or attachment."""
        snapshot = source["snapshot"]
        cases = [Case.model_validate(case) for case in snapshot["cases"]]
        source_id = source.get("id")
        cached_headers = self._ephemeral_headers.get(source_id, {}) if source_id else {}
        runtime_headers = getattr(request, "runtime_headers", None) or {}
        runtime_lookup = {k.lower(): v for k, v in runtime_headers.items()}
        for index, case in enumerate(cases):
            if index in request.case_edits:
                continue
            case_cache = cached_headers.get(index, {})
            if not case.headers and not runtime_lookup:
                continue
            updated_headers = dict(case.headers)
            changed = False
            for h_key, h_val in list(updated_headers.items()):
                low = h_key.lower()
                if low in runtime_lookup:
                    updated_headers[h_key] = runtime_lookup[low]
                    changed = True
                elif (h_val == REDACTED or REDACTED in h_val) and h_key in case_cache:
                    updated_headers[h_key] = case_cache[h_key]
                    changed = True
            if changed:
                cases[index] = case.model_copy(update={"headers": updated_headers})
        if runtime_headers:
            agent.context.learn({"headers": runtime_headers})
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
        merged_vars = {**(snapshot.get("variables") or {}), **request.variables}
        effective_metadata = metadata
        if effective_metadata is None:
            effective_metadata = TaskRunMetadata(
                source=getattr(request, "source", "manual") or "manual",
                badge=getattr(request, "badge", None),
                tests_per_url=snapshot.get("tests_per_url"),
            )
        return self._start(
            request,
            agent,
            cases,
            snapshot.get("operations", []),
            snapshot.get("warnings", []),
            source.get("title"),
            deepcopy(snapshot.get("document")),
            merged_vars,
            metadata=effective_metadata,
        )

    def _start(
        self,
        request,
        agent,
        cases,
        operations,
        warnings,
        title,
        document,
        variables=None,
        metadata: TaskRunMetadata | None = None,
    ):
        """Validate every case and freeze the safe plan before scheduling any target traffic."""
        if len(self.active) >= 4:
            raise ValueError("task_capacity_exceeded")
        parsed, origin = validate_target(request.origin, self.settings.target_allowed_origins)
        if "?" in request.origin or "#" in request.origin or "\\" in request.origin:
            raise ValueError("invalid_target_base_url")
        resolved_origin = origin + parsed.path.rstrip("/")
        effective_request = request.model_copy(update={"origin": resolved_origin})
        if not cases or len(cases) > 200:
            raise ValueError("case_limit_exceeded")
        for case in cases:
            validate_target(resolved_origin + case.path, self.settings.target_allowed_origins)
            if case.body is not None and case.method in {"GET", "HEAD", "OPTIONS"}:
                raise ValueError("body_not_allowed_for_method")

        effective_meta = metadata or TaskRunMetadata(
            source=getattr(effective_request, "source", "manual") or "manual",
            badge=getattr(effective_request, "badge", None),
            tests_per_url=None,
        )
        resolved_source = effective_meta.source
        resolved_badge = (
            effective_meta.badge
            if effective_meta.badge is not None
            else (DEFAULT_AI_BADGE if resolved_source == "ai" else None)
        )
        total_rounds = getattr(effective_request, "rounds", 1)
        cooldown_ms = getattr(effective_request, "cooldown_ms", 500)
        resolved_tests_per_url = effective_meta.tests_per_url

        raw_snapshot = {
            "origin": resolved_origin,
            "cases": [case.model_dump() for case in cases],
            "operations": operations,
            "warnings": warnings,
            "engine": "native-contract-v1",
            "source": resolved_source,
            "badge": resolved_badge,
            "total_rounds": total_rounds,
            "cooldown_ms": cooldown_ms,
            "tests_per_url": resolved_tests_per_url,
        }
        if variables:
            raw_snapshot["variables"] = dict(variables)
        agent.context.learn(raw_snapshot)
        snapshot = agent.context.clean(raw_snapshot)
        if document is not None:
            snapshot["document"] = document
        record = {
            "id": str(uuid.uuid4()),
            "title": agent.context.clean(title) if title else None,
            "source": resolved_source,
            "badge": resolved_badge,
            "total_rounds": total_rounds,
            "current_round": 0,
            "cooldown_ms": cooldown_ms,
            "cooldown_remaining_ms": None,
            "tests_per_url": resolved_tests_per_url,
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
        ephemeral_for_task: dict[int, dict[str, str]] = {}
        snap_cases = snapshot.get("cases") or []
        for idx, raw_case in enumerate(cases):
            snap_headers = snap_cases[idx].get("headers") if idx < len(snap_cases) else {}
            restored_map: dict[str, str] = {}
            for h_key, raw_val in raw_case.headers.items():
                snap_val = (snap_headers or {}).get(h_key)
                if (
                    isinstance(snap_val, str)
                    and (snap_val == REDACTED or REDACTED in snap_val)
                    and raw_val != REDACTED
                    and not TEMPLATE_ONLY_SECRET.fullmatch(raw_val)
                ):
                    restored_map[h_key] = raw_val
            if restored_map:
                ephemeral_for_task[idx] = restored_map
        if ephemeral_for_task:
            self._ephemeral_headers[record["id"]] = ephemeral_for_task
            while len(self._ephemeral_headers) > 32:
                self._ephemeral_headers.pop(next(iter(self._ephemeral_headers)), None)
        live = self.live[record["id"]] = LiveTask(record)
        task = asyncio.create_task(self._run(record, effective_request, cases, agent, live))
        self.active[record["id"]] = task

        def remove_worker(completed):
            if completed.cancelled() and record["status"] == "queued":
                record.update(
                    status="cancelled",
                    stage="finished",
                    error_kind="cancelled",
                    cooldown_remaining_ms=None,
                )
                self.store.save(record)
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

    async def _cooldown(self, record: dict, live: LiveTask, cooldown_ms: int) -> None:
        """Pause between rounds in short interruptible slices for prompt cancellation."""
        record["stage"] = "cooling_down"
        record["current_index"] = None
        remaining = int(cooldown_ms)
        record["cooldown_remaining_ms"] = remaining
        self.store.save(record)
        live.notify()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (remaining / 1000.0)
        while True:
            left_s = deadline - loop.time()
            if left_s <= 0:
                break
            await asyncio.sleep(min(COOLDOWN_SLICE_SECONDS, left_s))
            left_ms = max(0, int(round((deadline - loop.time()) * 1000)))
            if left_ms != record.get("cooldown_remaining_ms"):
                record["cooldown_remaining_ms"] = left_ms
                live.notify()
        record["stage"] = "executing"
        record["cooldown_remaining_ms"] = None
        self.store.save(record)
        live.notify()

    async def _run(self, record, request, cases, agent, live):
        """Execute in order across rounds, retain skipped/failed cases, then summarize verdicts."""
        total_rounds = getattr(request, "rounds", 1)
        cooldown_ms = getattr(request, "cooldown_ms", 500)
        reset_vars_per_round = getattr(request, "reset_vars_per_round", True)
        record.update(
            status="running",
            stage="executing",
            total_rounds=total_rounds,
            cooldown_remaining_ms=None,
        )

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

        initial_vars = dict(record["snapshot"].get("variables") or {})
        runtime_vars = dict(initial_vars)
        try:
            async with asyncio.timeout(request.wall_time_seconds):
                for round_num in range(1, total_rounds + 1):
                    record["current_round"] = round_num
                    record["stage"] = "executing"
                    record["cooldown_remaining_ms"] = None
                    if reset_vars_per_round:
                        runtime_vars = dict(initial_vars)
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
                                    variables=runtime_vars,
                                )
                            except (ValueError, OSError):
                                result = {
                                    "status": "error",
                                    "reason": "case_execution_rejected",
                                    "assertions": [],
                                }
                        extracted_secrets = result.pop("_extracted_secrets", None)
                        if extracted_secrets:
                            merged = set(agent.context.redactor.secrets) | set(extracted_secrets)
                            agent.context.redactor.secrets = tuple(
                                sorted(merged, key=len, reverse=True)
                            )
                        execution_index = len(record["results"])
                        result_index = index if total_rounds == 1 else execution_index
                        result.update(
                            index=result_index,
                            case_id=f"r{round_num}:c{index}",
                            round=round_num,
                            case_index=index,
                            execution_index=execution_index,
                            name=case.name,
                            source=case.source,
                            protocol=case.protocol,
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
                    if round_num < total_rounds and cooldown_ms > 0:
                        await self._cooldown(record, live, cooldown_ms)
                record["current_index"] = None
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
            record["cooldown_remaining_ms"] = None
            live.events = []
            live.events_seen = 0
            if not record["summary"] and live.live_summary:
                record["summary"] = live.live_summary
            self._mark_incomplete(record)
            self.store.save(record)
            self.active.pop(record["id"], None)
            self.live.pop(record["id"], None)
            live.notify()

    @staticmethod
    def _mark_incomplete(record):
        """Retain an explicit unknown verdict for a dispatched case interrupted before evidence."""
        index = record.get("current_index")
        if index is None:
            return
        total_rounds = record.get("total_rounds") or 1
        round_num = record.get("current_round") or 1
        target_case_id = f"r{round_num}:c{index}"
        if any(
            result.get("case_id") == target_case_id
            or (
                "case_id" not in result
                and result.get("round", 1) == round_num
                and result.get("case_index", result.get("index")) == index
            )
            for result in record["results"]
        ):
            return
        cases = record["snapshot"]["cases"]
        if not 0 <= index < len(cases):
            return
        case = cases[index]
        execution_index = len(record["results"])
        result_index = index if total_rounds == 1 else execution_index
        record["results"].append(
            {
                "index": result_index,
                "case_id": target_case_id,
                "round": round_num,
                "case_index": index,
                "execution_index": execution_index,
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
                record.update(
                    status="cancelled",
                    stage="finished",
                    error_kind="cancelled",
                    cooldown_remaining_ms=None,
                )
                self.store.save(record)
        return self.store.read(task_id)

    async def shutdown(self):
        """Stop task workers before the shared stream manager and database close."""
        await asyncio.gather(*(self.cancel(task_id) for task_id in list(self.active)))
        self._ephemeral_headers.clear()
