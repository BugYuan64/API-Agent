"""Background multi-round WebSocket AI suite runner with interruptible cooldown."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from api_agent.infrastructure.orm import as_utc, new_id, utc_now
from api_agent.infrastructure.targets import STREAM_SCHEMES, validate_target
from api_agent.streams.client import WebSocketTestClient
from api_agent.streams.models import StreamSessionRecord
from api_agent.streams.repository import StreamRepository
from api_agent.streams.schemas import StreamAiCaseSpec, StreamAiSuiteSpec, StreamStart
from api_agent.streams.service import StreamManager, _canonical_stream_url

logger = logging.getLogger("api_agent")

DEFAULT_AI_BADGE = "AI 测试"
COOLDOWN_SLICE_SECONDS = 0.05
MAX_CONCURRENT_SUITES = 4
MAX_COMPLETED_CACHE = 20


async def _invoke_callback(callback, payload: dict[str, Any]) -> None:
    """安全调用同步或异步进度回调，避免观察者异常中断后台套件。"""
    if callback is None:
        return
    try:
        outcome = callback(payload)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - 回调失败不得影响真实测试套件收尾
        logger.warning("Stream suite callback raised an exception", exc_info=True)


async def _await_task_done(task: asyncio.Task) -> None:
    """等待后台任务结束，且当等待方被取消时不连带取消后台任务。"""
    done = asyncio.Event()
    task.add_done_callback(lambda _completed: done.set())
    if task.done():
        done.set()
    await done.wait()


def _case_tone(status: str) -> str:
    """把子用例结果映射为前端徽章/链接语义色。"""
    if status in {"passed", "closed", "completed"}:
        return "success"
    if status in {"failed", "error"}:
        return "danger"
    if status in {"cancelled", "interrupted"}:
        return "warning"
    return "info"


def _evaluate_case(
    case: StreamAiCaseSpec, session: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[str, str, list[str]]:
    """基于真实握手、事件时间线与关闭码裁定单条 WebSocket 子用例结果。"""
    notes: list[str] = []
    error_kind = session.get("error_kind")
    opened_at = session.get("opened_at")
    close_code = session.get("close_code")
    stop_reason = session.get("stop_reason")

    if case.expect_handshake_reject:
        rejected = error_kind == "handshake_rejected" or (
            opened_at is None and session.get("status") in {"error", "closed"}
        )
        if rejected:
            return "passed", "success", ["握手按预期被拒绝"]
        return "failed", "danger", ["期望握手被拒绝，但实际建立或未按预期拒绝"]

    transport_ok = opened_at is not None and not error_kind
    if (
        transport_ok
        and close_code not in {None, 1000, 1001}
        and stop_reason not in {"deadline_exceeded", "limits_exceeded"}
        and case.expect_close_code != close_code
    ):
        transport_ok = False
        notes.append(f"异常关闭码 {close_code}")

    if not transport_ok:
        if error_kind:
            notes.append(f"连接错误：{error_kind}")
        return "error", "danger", notes

    failed = False
    inbound_events = [event for event in events if event.get("direction", "in") == "in"]
    inbound_data_events = [
        event for event in inbound_events if event.get("kind") in {"text", "binary"}
    ]
    if case.expect_contains is not None:
        combined = "\n".join(str(event.get("data") or "") for event in inbound_events)
        if case.expect_contains not in combined:
            failed = True
            notes.append(f"未收到包含 {case.expect_contains!r} 的消息")
    if case.expect_min_events is not None and len(inbound_data_events) < case.expect_min_events:
        failed = True
        notes.append(f"入站消息数 {len(inbound_data_events)} 少于期望下限 {case.expect_min_events}")
    if case.expect_close_code is not None and close_code != case.expect_close_code:
        failed = True
        notes.append(f"关闭码 {close_code} 与期望 {case.expect_close_code} 不一致")

    if failed:
        return "failed", "danger", notes
    return "passed", "success", notes


def _stored_case_link(parent_id: str, record: StreamSessionRecord) -> dict[str, Any]:
    """把已落库的子用例行转换为规范 CaseLinkBlock 字典，供断线重连或重启后准确还原断言结果。"""
    label = record.case_label or f"第 {record.round_index} 轮 · 用例 {record.case_index + 1}"
    settings = record.settings or {}
    stored_status = settings.get("case_status")
    if isinstance(stored_status, str) and stored_status in {
        "passed",
        "failed",
        "error",
        "cancelled",
        "interrupted",
    }:
        status = stored_status
    elif record.error_kind or record.status == "error":
        status = "error"
    elif record.status in {"cancelled", "interrupted"}:
        status = record.status
    else:
        status = "passed"
    link: dict[str, Any] = {
        "type": "case_link",
        "scope": "websocket",
        "history_id": parent_id,
        "case_id": record.id,
        "round": record.round_index,
        "case_index": record.case_index,
        "label": label,
        "status": status,
        "tone": _case_tone(status),
        "event_count": record.event_count,
        "sent_count": record.sent_count,
        "close_code": record.close_code,
        "stop_reason": record.stop_reason,
        "error_kind": record.error_kind,
    }
    stored_notes = settings.get("case_notes")
    if isinstance(stored_notes, list) and stored_notes:
        link["notes"] = [str(item) for item in stored_notes]
    return link


@dataclass
class LiveStreamSuite:
    """运行中多轮 WebSocket AI 套件的内存实时状态与重连回放视图。"""

    suite_id: str
    url: str
    title: str | None
    badge: str | None
    total_rounds: int
    total_cases: int
    cooldown_ms: int
    source: str = "ai"
    status: str = "running"
    stage: str = "executing"
    current_round: int = 1
    completed_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    error_cases: int = 0
    cooldown_remaining_ms: int | None = None
    current_case_index: int | None = None
    current_case_label: str | None = None
    summary: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    opened_at: datetime = field(default_factory=utc_now)
    finished: bool = False
    case_links: list[dict[str, Any]] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def notify(self) -> None:
        """唤醒全部观察者并重置下一次等待事件，避免竞态丢帧。"""
        self.changed.set()
        self.changed = asyncio.Event()

    def status_snapshot(self) -> dict[str, Any]:
        """导出套件当前进度摘要（供启动返回、活跃列表、取消与完成返回共用）。"""
        return {
            "id": self.suite_id,
            "status": self.status,
            "stage": self.stage,
            "source": self.source,
            "title": self.title,
            "badge": self.badge,
            "url": self.url,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "completed_cases": self.completed_cases,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "error_cases": self.error_cases,
            "cooldown_ms": self.cooldown_ms,
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "current_case_index": self.current_case_index,
            "current_case_label": self.current_case_label,
            "summary": self.summary,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "case_links": list(self.case_links),
        }

    def active_item(self) -> dict[str, Any]:
        """导出供 ``GET /api/streams/active`` 使用的跨页重连条目。"""
        return {
            "id": self.suite_id,
            "kind": "ai_suite",
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "badge": self.badge,
            "status": self.status,
            "stage": self.stage,
            "current_round": self.current_round,
            "total_rounds": self.total_rounds,
            "completed_cases": self.completed_cases,
            "total_cases": self.total_cases,
            "cooldown_ms": self.cooldown_ms,
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "opened_at": as_utc(self.opened_at),
        }


@dataclass
class ActiveStreamSuite:
    """持有后台多轮 WebSocket AI 套件任务与实时状态句柄。"""

    suite_id: str
    task: asyncio.Task
    live: LiveStreamSuite
    cancel_reason: str | None = None


class StreamSuiteRunner:
    """后台多轮 WebSocket AI 测试套件编排器。

    作用：将一次 AI 驱动的多轮 WebSocket 测试归档到单条顶层 ``stream_sessions`` 记录
    （``source="ai"``, ``badge="AI 测试"``），每条子用例独立连接、收发、落库为子行
    并在冷却前彻底关闭回收 socket；观察流断线不会取消后台任务，支持跨页切回重连。
    """

    def __init__(self, streams: StreamManager):
        """绑定应用级 StreamManager 及其底层 Database/StreamRepository。"""
        self.streams = streams
        self.database = streams.database
        self.repo: StreamRepository = getattr(streams, "repo", None) or streams.repository
        self.settings = streams.settings
        self.active: dict[str, ActiveStreamSuite] = {}
        self.live: dict[str, LiveStreamSuite] = {}
        self._completed: dict[str, dict[str, Any]] = {}

    def is_active(self, suite_id: str) -> bool:
        """判断指定套件当前是否仍在后台运行。"""
        return suite_id in self.active

    def active_runs(self) -> list[dict[str, Any]]:
        """列出当前所有仍在后台执行的 WebSocket AI 套件，供跨页重连使用。"""
        return [entry.live.active_item() for entry in list(self.active.values())]

    async def start_ai_suite(
        self,
        spec: StreamAiSuiteSpec,
        *,
        on_case_done=None,
    ) -> dict[str, Any]:
        """校验目标并启动一个后台多轮 WebSocket AI 套件，立即返回父会话摘要。"""
        if len(self.active) >= MAX_CONCURRENT_SUITES:
            raise ValueError("stream_suite_limit")

        allowed_origins = self.settings.target_allowed_origins
        parsed, origin = validate_target(spec.url, allowed_origins, STREAM_SCHEMES)
        parent_url = _canonical_stream_url(parsed)
        for case in spec.cases:
            if case.url:
                validate_target(case.url, allowed_origins, STREAM_SCHEMES)

        suite_id = new_id()
        total_cases = spec.rounds * len(spec.cases)
        title = (spec.title or f"AI WebSocket 测试 · {parent_url}")[:200]
        badge = spec.badge or DEFAULT_AI_BADGE

        record = await self.repo.create_parent_suite(
            session_id=suite_id,
            url=parent_url,
            origin=origin,
            headers=dict(spec.headers),
            subprotocols=list(spec.subprotocols),
            project_id=spec.project_id,
            source="ai",
            title=title,
            badge=badge,
            cooldown_ms=spec.cooldown_ms,
            total_rounds=spec.rounds,
            total_cases=total_cases,
            cases_per_round=len(spec.cases),
        )

        live = LiveStreamSuite(
            suite_id=suite_id,
            url=record.url,
            title=record.title,
            badge=record.badge,
            total_rounds=spec.rounds,
            total_cases=total_cases,
            cooldown_ms=spec.cooldown_ms,
            opened_at=record.opened_at or utc_now(),
        )
        self.live[suite_id] = live
        task = asyncio.create_task(
            self._run_suite(suite_id, spec, parent_url, live, on_case_done=on_case_done),
            name=f"stream-suite-{suite_id[:8]}",
        )
        self.active[suite_id] = ActiveStreamSuite(suite_id=suite_id, task=task, live=live)
        return live.status_snapshot()

    async def observe(self, suite_id: str) -> AsyncIterator[str]:
        """以可重连 NDJSON 流观察多轮套件进度；客户端断开绝不取消后台任务。"""
        live = self.live.get(suite_id)
        if live is None:
            async for line in self._replay_terminal_ndjson(suite_id):
                yield line
            return

        sent_cases = 0
        last_stage: str | None = None
        last_cooldown: int | None = None
        while True:
            changed = live.changed
            new_links = live.case_links[sent_cases:]
            sent_cases += len(new_links)
            snapshot = live.status_snapshot()
            yield (
                json.dumps(
                    {
                        "type": "progress",
                        "suite_id": suite_id,
                        **{k: v for k, v in snapshot.items() if k != "case_links"},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            for link in new_links:
                yield (
                    json.dumps(
                        {"type": "case_done", "suite_id": suite_id, "case_link": link},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            if live.stage == "cooling_down" and (
                last_stage != "cooling_down" or last_cooldown != live.cooldown_remaining_ms
            ):
                yield (
                    json.dumps(
                        {
                            "type": "cooling_down",
                            "suite_id": suite_id,
                            "current_round": live.current_round,
                            "total_rounds": live.total_rounds,
                            "cooldown_ms": live.cooldown_ms,
                            "cooldown_remaining_ms": live.cooldown_remaining_ms,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            last_stage = live.stage
            last_cooldown = live.cooldown_remaining_ms

            if live.finished:
                yield (
                    json.dumps(
                        {"type": "done", "suite_id": suite_id, **snapshot},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                return

            try:
                await asyncio.wait_for(changed.wait(), timeout=10)
            except TimeoutError:
                yield '{"type":"heartbeat"}\n'

    async def _replay_terminal_ndjson(self, suite_id: str) -> AsyncIterator[str]:
        """当观察者在套件结束后才连接或重连时，回放持久化快照与全部子用例链接。"""
        cached = self._completed.get(suite_id)
        if cached is not None:
            yield (
                json.dumps(
                    {
                        "type": "progress",
                        "suite_id": suite_id,
                        **{k: v for k, v in cached.items() if k != "case_links"},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            for link in cached.get("case_links", []):
                yield (
                    json.dumps(
                        {"type": "case_done", "suite_id": suite_id, "case_link": link},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            yield (
                json.dumps({"type": "done", "suite_id": suite_id, **cached}, ensure_ascii=False)
                + "\n"
            )
            return

        record = await self.repo.get_session(suite_id)
        if record is None:
            return
        sub_cases = await self.repo.list_sub_cases(suite_id)
        links = [_stored_case_link(suite_id, sub) for sub in sub_cases]
        snapshot = self._snapshot_from_record(record, links)
        yield (
            json.dumps(
                {
                    "type": "progress",
                    "suite_id": suite_id,
                    **{k: v for k, v in snapshot.items() if k != "case_links"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        for link in links:
            yield (
                json.dumps(
                    {"type": "case_done", "suite_id": suite_id, "case_link": link},
                    ensure_ascii=False,
                )
                + "\n"
            )
        yield (
            json.dumps({"type": "done", "suite_id": suite_id, **snapshot}, ensure_ascii=False)
            + "\n"
        )

    async def wait_for_completion(self, suite_id: str) -> dict[str, Any]:
        """等待指定套件运行至终态并返回包含总结与全部 case_links 的最终快照。"""
        entry = self.active.get(suite_id)
        if entry is not None:
            await _await_task_done(entry.task)
        if suite_id in self._completed:
            return dict(self._completed[suite_id])
        record = await self.repo.get_session(suite_id)
        if record is None:
            raise LookupError("stream_suite_not_found")
        sub_cases = await self.repo.list_sub_cases(suite_id)
        return self._snapshot_from_record(
            record, [_stored_case_link(suite_id, sub) for sub in sub_cases]
        )

    async def cancel(
        self, suite_id: str, *, reason: str = "client_stopped"
    ) -> dict[str, Any] | None:
        """显式取消正在运行的套件并等待当前子连接与收尾事务完成。"""
        entry = self.active.get(suite_id)
        if entry is not None:
            entry.cancel_reason = reason
            if not entry.task.done():
                entry.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(entry.task), timeout=5)
                except (TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:  # noqa: BLE001 - 任务内部已记录异常
                    pass
            # 若任务在首条指令前即被取消，补做终态持久化与表项清理。
            if entry.task.done() and suite_id in self.active:
                entry.live.status = "cancelled"
                entry.live.stage = "finished"
                entry.live.error_kind = "cancelled"
                entry.live.cooldown_remaining_ms = None
                entry.live.finished = True
                await self.repo.update_parent_suite(
                    suite_id,
                    status="cancelled",
                    stop_reason=reason,
                    error_kind="cancelled",
                    closed_at=utc_now(),
                )
                self._cache_completed(suite_id, entry.live.status_snapshot())
                entry.live.notify()
                self.active.pop(suite_id, None)
                self.live.pop(suite_id, None)
            if suite_id in self._completed:
                return dict(self._completed[suite_id])
            return entry.live.status_snapshot()

        if suite_id in self._completed:
            return dict(self._completed[suite_id])
        record = await self.repo.get_session(suite_id)
        if record is None:
            return None
        sub_cases = await self.repo.list_sub_cases(suite_id)
        return self._snapshot_from_record(
            record, [_stored_case_link(suite_id, sub) for sub in sub_cases]
        )

    async def recover(self) -> int:
        """进程重启后把遗留未结束的套件行标记为 interrupted，不自动重连。"""
        return await self.repo.recover()

    async def shutdown(self) -> None:
        """应用关闭时取消并等待所有后台 AI 套件彻底释放连接与数据库事务。"""
        await asyncio.gather(
            *(
                self.cancel(suite_id, reason="service_shutdown")
                for suite_id in list(self.active.keys())
            ),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(entry.task for entry in list(self.active.values())),
            return_exceptions=True,
        )

    async def _cooldown(self, live: LiveStreamSuite, cooldown_ms: int) -> None:
        """在轮次/用例间隙按短切片可中断冷却，确保取消指令毫秒级响应。"""
        remaining = max(0, int(cooldown_ms))
        if remaining <= 0:
            return
        live.stage = "cooling_down"
        live.current_case_index = None
        live.current_case_label = None
        live.cooldown_remaining_ms = remaining
        live.notify()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (remaining / 1000.0)
        while True:
            left_s = deadline - loop.time()
            if left_s <= 0:
                break
            await asyncio.sleep(min(COOLDOWN_SLICE_SECONDS, left_s))
            left_ms = max(0, int(round((deadline - loop.time()) * 1000)))
            if left_ms != live.cooldown_remaining_ms:
                live.cooldown_remaining_ms = left_ms
                live.notify()
        live.stage = "executing"
        live.cooldown_remaining_ms = None
        live.notify()

    async def _run_suite(
        self,
        suite_id: str,
        spec: StreamAiSuiteSpec,
        parent_url: str,
        live: LiveStreamSuite,
        *,
        on_case_done=None,
    ) -> None:
        """按轮次依次执行每条子用例，独立回收连接、按需冷却并持久化汇总结论。"""
        client = WebSocketTestClient(self.streams, self.settings.target_allowed_origins)
        total_events = 0
        total_bytes = 0
        total_sent = 0
        final_status = "completed"
        stop_reason: str | None = None
        error_kind: str | None = None
        error_message: str | None = None
        total_executions = spec.rounds * len(spec.cases)
        executed = 0

        try:
            for round_idx in range(1, spec.rounds + 1):
                live.current_round = round_idx
                live.stage = "executing"
                live.cooldown_remaining_ms = None
                live.notify()

                for case_idx, case in enumerate(spec.cases):
                    label = f"第 {round_idx} 轮 · {case.label}" if spec.rounds > 1 else case.label
                    live.current_case_index = case_idx
                    live.current_case_label = label
                    live.notify()

                    merged_headers = {**spec.headers, **case.headers}
                    merged_subprotocols = list(case.subprotocols or spec.subprotocols)
                    payload = StreamStart(
                        kind="websocket",
                        url=case.url or parent_url,
                        headers=merged_headers,
                        subprotocols=merged_subprotocols,
                        max_events=case.max_events,
                        max_duration_ms=case.max_duration_ms,
                        connect_timeout_ms=case.connect_timeout_ms,
                        ping_interval_ms=case.ping_interval_ms,
                        send=list(case.send),
                        send_interval_ms=case.send_interval_ms,
                        project_id=spec.project_id,
                    )
                    result = await client.execute(
                        payload,
                        persist_history=True,
                        source="ai",
                        parent_session_id=suite_id,
                        round_index=round_idx,
                        case_index=case_idx,
                        case_label=label,
                    )
                    child_session = result["session"]
                    child_events = result["events"]
                    case_status, tone, notes = _evaluate_case(case, child_session, child_events)
                    await self.repo.update_sub_case_verdict(
                        child_session["id"],
                        case_status=case_status,
                        notes=notes,
                    )

                    total_events += int(child_session.get("event_count") or 0)
                    total_bytes += int(child_session.get("byte_count") or 0)
                    total_sent += int(child_session.get("sent_count") or 0)
                    if case_status == "passed":
                        live.passed_cases += 1
                    elif case_status == "failed":
                        live.failed_cases += 1
                    else:
                        live.error_cases += 1

                    case_link: dict[str, Any] = {
                        "type": "case_link",
                        "scope": "websocket",
                        "history_id": suite_id,
                        "case_id": child_session["id"],
                        "round": round_idx,
                        "case_index": case_idx,
                        "label": label,
                        "status": case_status,
                        "tone": tone,
                        "event_count": int(child_session.get("event_count") or 0),
                        "sent_count": int(child_session.get("sent_count") or 0),
                        "close_code": child_session.get("close_code"),
                        "stop_reason": child_session.get("stop_reason"),
                        "error_kind": child_session.get("error_kind"),
                    }
                    if notes:
                        case_link["notes"] = notes

                    live.case_links.append(case_link)
                    live.completed_cases += 1
                    executed += 1
                    live.notify()

                    await _invoke_callback(on_case_done, case_link)

                    if executed < total_executions and spec.cooldown_ms > 0:
                        await self._cooldown(live, spec.cooldown_ms)

            live.current_case_index = None
            live.current_case_label = None
            live.stage = "finished"
            live.status = "completed"
            live.summary = self._build_summary(live, total_events, cancelled=False)
            final_status = "completed"
        except asyncio.CancelledError:
            entry = self.active.get(suite_id)
            stop_reason = (entry.cancel_reason if entry else None) or "client_stopped"
            error_kind = "cancelled"
            final_status = "cancelled"
            live.status = "cancelled"
            live.stage = "finished"
            live.error_kind = error_kind
            live.summary = self._build_summary(live, total_events, cancelled=True)
        except Exception as error:  # noqa: BLE001 - 套件异常必须落为终态证据
            logger.warning("WebSocket AI suite %s failed", suite_id, exc_info=True)
            final_status = "error"
            stop_reason = "stream_failed"
            error_kind = "stream_failed"
            error_message = str(error)[:300] or "WebSocket AI suite execution failed"
            live.status = "error"
            live.stage = "finished"
            live.error_kind = error_kind
            live.error_message = error_message
            live.summary = self._build_summary(live, total_events, cancelled=False, errored=True)
        finally:
            live.current_case_index = None
            live.current_case_label = None
            live.cooldown_remaining_ms = None
            try:
                updated = await self.repo.update_parent_suite(
                    suite_id,
                    status=final_status,
                    summary=live.summary,
                    stop_reason=stop_reason,
                    error_kind=error_kind,
                    error_message=error_message,
                    event_count=total_events,
                    byte_count=total_bytes,
                    sent_count=total_sent,
                    total_rounds=spec.rounds,
                    closed_at=utc_now(),
                )
                if updated is not None and updated.summary is not None:
                    live.summary = updated.summary
            except Exception:  # noqa: BLE001
                logger.warning("Failed to persist final parent suite %s", suite_id, exc_info=True)
            live.finished = True
            self._cache_completed(suite_id, live.status_snapshot())
            live.notify()
            self.active.pop(suite_id, None)
            self.live.pop(suite_id, None)

    @staticmethod
    def _build_summary(
        live: LiveStreamSuite,
        total_events: int,
        *,
        cancelled: bool = False,
        errored: bool = False,
    ) -> str:
        """生成简洁、确定性的多轮 WebSocket 测试套件汇总文本。"""
        prefix = "测试已取消：" if cancelled else ("测试异常中止：" if errored else "")
        return (
            f"{prefix}共计划 {live.total_rounds} 轮 {live.total_cases} 条 WebSocket 用例，"
            f"已执行 {live.completed_cases} 条"
            f"（通过 {live.passed_cases}，失败 {live.failed_cases}，"
            f"异常 {live.error_cases}），累计采集事件 {total_events} 条。"
        )

    @staticmethod
    def _snapshot_from_record(
        record: StreamSessionRecord, case_links: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """从持久化父行与子用例链接重建套件状态快照。"""
        settings = record.settings or {}
        total_cases = int(settings.get("total_cases") or len(case_links))
        passed = sum(1 for item in case_links if item.get("status") == "passed")
        failed = sum(1 for item in case_links if item.get("status") == "failed")
        errors = sum(1 for item in case_links if item.get("status") == "error")
        terminal = record.status in {"closed", "completed", "error", "cancelled", "interrupted"}
        return {
            "id": record.id,
            "status": record.status,
            "stage": "finished" if terminal else "executing",
            "source": record.source,
            "title": record.title,
            "badge": record.badge,
            "url": record.url,
            "current_round": record.total_rounds,
            "total_rounds": record.total_rounds,
            "completed_cases": len(case_links),
            "total_cases": total_cases,
            "passed_cases": passed,
            "failed_cases": failed,
            "error_cases": errors,
            "cooldown_ms": record.cooldown_ms,
            "cooldown_remaining_ms": None,
            "current_case_index": None,
            "current_case_label": None,
            "summary": record.summary,
            "error_kind": record.error_kind,
            "error_message": record.error_message,
            "case_links": case_links,
        }

    def _cache_completed(self, suite_id: str, snapshot: dict[str, Any]) -> None:
        """缓存近期完成的套件快照，超限时按 FIFO 淘汰避免内存增长。"""
        self._completed[suite_id] = snapshot
        while len(self._completed) > MAX_COMPLETED_CACHE:
            oldest = next(iter(self._completed))
            self._completed.pop(oldest, None)


__all__ = [
    "ActiveStreamSuite",
    "DEFAULT_AI_BADGE",
    "LiveStreamSuite",
    "StreamAiCaseSpec",
    "StreamAiSuiteSpec",
    "StreamSuiteRunner",
]
