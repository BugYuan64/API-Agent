"""Background lifecycle manager for single console requests and multi-round AI suites."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from api_agent.console.repository import ConsoleRepository
from api_agent.console.schemas import ConsoleAiCaseSpec, ConsoleAiSuiteSpec
from api_agent.console.service import ConsoleRequest, ConsoleResult, HttpTestClient, build_url
from api_agent.console.sse import SseEvent
from api_agent.infrastructure.api import error_response
from api_agent.infrastructure.config import Settings
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.history import PREVIEW_LIMIT
from api_agent.infrastructure.orm import new_id, utc_now
from api_agent.infrastructure.outcomes import outcome_for
from api_agent.infrastructure.targets import TargetError, validate_target

logger = logging.getLogger("api_agent")

DEFAULT_AI_BADGE = "AI 测试"
COOLDOWN_SLICE_SECONDS = 0.1
MAX_ACTIVE_RUNS = 16


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _encode_frame(frame: dict[str, Any]) -> bytes:
    return (json.dumps(frame, ensure_ascii=False, default=_json_default) + "\n").encode("utf-8")


async def _await_task_done(task: asyncio.Task) -> None:
    """Wait for a background task to finish without cancelling it if the waiter cancels."""
    done = asyncio.Event()
    task.add_done_callback(lambda _completed: done.set())
    if task.done():
        done.set()
    await done.wait()


async def _invoke_callback(callback, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        outcome = callback(payload)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - 外部回调异常不得中断测试落库与收尾
        logger.warning("Console run callback raised an error", exc_info=True)


def build_case_link(run_id: str, sub_case: dict[str, Any]) -> dict[str, Any]:
    """构造面向前端助手消息区的规范化子用例可点击回放链接块（CaseLinkBlock）。"""
    ok = bool(sub_case.get("ok"))
    status_code = sub_case.get("status_code")
    assertion_summary = sub_case.get("assertion_summary")
    assertion_passed = (
        assertion_summary.get("passed") if isinstance(assertion_summary, dict) else None
    )

    if not ok:
        status_label = str(sub_case.get("error_kind") or "error")
        tone = "danger"
    elif assertion_passed is False:
        status_label = "failed"
        tone = "danger"
    elif assertion_passed is True:
        status_label = "passed"
        tone = "success"
    elif isinstance(status_code, int) and 200 <= status_code < 400:
        status_label = f"HTTP {status_code}"
        tone = "success"
    elif isinstance(status_code, int) and 400 <= status_code < 500:
        status_label = f"HTTP {status_code}"
        tone = "warning"
    elif isinstance(status_code, int) and status_code >= 500:
        status_label = f"HTTP {status_code}"
        tone = "danger"
    else:
        status_label = "completed"
        tone = "neutral"

    return {
        "type": "case_link",
        "scope": "console",
        "history_id": run_id,
        "case_id": sub_case["id"],
        "round": int(sub_case.get("round_index", 1)),
        "case_index": int(sub_case.get("case_index", 0)),
        "label": sub_case.get("label") or f"{sub_case.get('method', 'GET')} {sub_case.get('url')}",
        "method": sub_case.get("method", "GET"),
        "status": status_label,
        "status_code": status_code,
        "ok": ok,
        "elapsed_ms": sub_case.get("elapsed_ms"),
        "tone": tone,
    }


def _evaluate_case_assertions(
    case: _ResolvedSuiteCase,
    result: ConsoleResult,
) -> dict[str, Any] | None:
    """对 AI 套件用例的可选预期状态码/响应子串进行轻量确定性校验。"""
    if case.expected_status is None and case.expect_contains is None:
        return None

    checks: list[dict[str, Any]] = []
    actual_status = result.response.status if result.response else None
    actual_body = (result.response.body or "") if result.response else ""

    if case.expected_status is not None:
        passed = result.ok and actual_status in case.expected_status
        checks.append(
            {
                "kind": "status",
                "expected": (
                    case.expected_status[0]
                    if len(case.expected_status) == 1
                    else case.expected_status
                ),
                "actual": actual_status,
                "passed": passed,
            }
        )
    if case.expect_contains is not None:
        passed = result.ok and (case.expect_contains in actual_body)
        checks.append(
            {
                "kind": "body_contains",
                "expected": case.expect_contains,
                "actual": result.ok and (case.expect_contains in actual_body),
                "passed": passed,
            }
        )

    return {
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


@dataclass
class _ResolvedSuiteCase:
    case_index: int
    label: str
    request: ConsoleRequest
    canonical_url: str
    expected_status: list[int] | None
    expect_contains: str | None
    explicit_round: int | None


class LiveConsoleRun:
    """保存单条后台控制台运行的实时状态、可重放帧列表与唤醒事件。"""

    def __init__(
        self,
        *,
        run_id: str,
        kind: Literal["single", "ai_suite"],
        source: str,
        title: str | None,
        badge: str | None,
        method: str,
        url: str,
        total_rounds: int = 1,
        total_cases: int = 1,
        cooldown_ms: int = 500,
        created_at: datetime | None = None,
    ):
        self.run_id = run_id
        self.kind = kind
        self.source = source
        self.title = title
        self.badge = badge
        self.method = method
        self.url = url
        self.status: str = "running"
        self.stage: str = "executing"
        self.ok: bool = False
        self.status_code: int | None = None
        self.elapsed_ms: float = 0.0
        self.error_kind: str | None = None
        self.error_message: str | None = None
        self.total_rounds = total_rounds
        self.current_round: int = 0
        self.total_cases = total_cases
        self.completed_cases: int = 0
        self.cooldown_ms = cooldown_ms
        self.cooldown_remaining_ms: int | None = None
        self.summary: str | None = None
        self.created_at = created_at or utc_now()
        self.frames: list[dict[str, Any]] = []
        self.case_links: list[dict[str, Any]] = []
        self.finished: bool = False
        self.changed = asyncio.Event()

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def append_frame(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        self.notify()

    def summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.run_id,
            "source": self.source,
            "title": self.title,
            "badge": self.badge,
            "status": self.status,
            "stage": self.stage,
            "method": self.method,
            "url": self.url,
            "ok": self.ok,
            "status_code": self.status_code,
            "elapsed_ms": self.elapsed_ms,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "total_rounds": self.total_rounds,
            "current_round": self.current_round,
            "total_cases": self.total_cases,
            "completed_cases": self.completed_cases,
            "cooldown_ms": self.cooldown_ms,
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "summary": self.summary,
            "created_at": self.created_at,
        }


@dataclass
class ActiveConsoleRun:
    """后台运行中的控制台任务句柄。"""

    id: str
    kind: Literal["single", "ai_suite"]
    task: asyncio.Task
    live: LiveConsoleRun
    defer_persist: bool = False


class ConsoleRunManager:
    """管理控制台单次请求与 AI 多轮接口测试套件的后台生命周期、重连流与资源回收。"""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.repo = ConsoleRepository(database)
        self.http = HttpTestClient(settings)
        self.active: dict[str, ActiveConsoleRun] = {}
        self.live: dict[str, LiveConsoleRun] = {}
        self.max_active_runs = MAX_ACTIVE_RUNS

    async def start_single(
        self,
        payload: ConsoleRequest,
        *,
        source: str = "manual",
        title: str | None = None,
        badge: str | None = None,
        project_id: str | None = None,
        defer_persist: bool = False,
    ) -> dict[str, Any]:
        """启动后台单次 HTTP/SSE 请求，立即返回运行摘要，断开观察流不会取消后台任务。"""
        run_info, _live = await self.start_single_with_live(
            payload,
            source=source,
            title=title,
            badge=badge,
            project_id=project_id,
            defer_persist=defer_persist,
        )
        return run_info

    async def start_single_with_live(
        self,
        payload: ConsoleRequest,
        *,
        source: str = "manual",
        title: str | None = None,
        badge: str | None = None,
        project_id: str | None = None,
        defer_persist: bool = False,
    ) -> tuple[dict[str, Any], LiveConsoleRun]:
        """启动后台单次请求并同时返回内存 LiveConsoleRun 句柄（供即时流直接订阅）。"""
        if len(self.active) >= self.max_active_runs:
            raise ValueError("console_run_limit")
        parsed, _origin = validate_target(payload.url, self.settings.target_allowed_origins)
        canonical_url = build_url(parsed)
        run_id = new_id()

        if defer_persist:
            created_at = utc_now()
        else:
            created = await self.repo.create_run(
                record_id=run_id,
                method=payload.method,
                url=canonical_url,
                body_mode=payload.body_mode,
                request_headers=payload.headers,
                request_body=payload.body,
                source=source,
                title=title,
                badge=badge,
                status="running",
                total_rounds=1,
                cooldown_ms=500,
                project_id=project_id,
            )
            created_at = created["created_at"]

        live = LiveConsoleRun(
            run_id=run_id,
            kind="single",
            source=source,
            title=title,
            badge=badge,
            method=payload.method,
            url=canonical_url,
            total_rounds=1,
            total_cases=1,
            cooldown_ms=500,
            created_at=created_at,
        )
        self.live[run_id] = live
        task = asyncio.create_task(
            self._run_single(
                run_id,
                payload,
                live,
                defer_persist=defer_persist,
                source=source,
                title=title,
                badge=badge,
                project_id=project_id,
            ),
            name=f"console-run-{run_id[:8]}",
        )
        self.active[run_id] = ActiveConsoleRun(
            id=run_id,
            kind="single",
            task=task,
            live=live,
            defer_persist=defer_persist,
        )
        return live.summary_dict(), live

    async def start_ai_suite(
        self,
        spec: ConsoleAiSuiteSpec,
        *,
        on_case_done=None,
    ) -> dict[str, Any]:
        """启动 AI 多轮接口测试套件：落库单条带标注的父记录，循环执行子用例并支持可中断冷却。"""
        if len(self.active) >= self.max_active_runs:
            raise ValueError("console_run_limit")

        resolved_cases = self._resolve_suite_cases(spec)
        has_explicit_rounds = spec.rounds == 1 and any(
            item.explicit_round is not None for item in resolved_cases
        )
        if has_explicit_rounds:
            total_rounds = max(item.explicit_round or 1 for item in resolved_cases)
            schedule = [
                (item.explicit_round or 1, idx, item) for idx, item in enumerate(resolved_cases)
            ]
        else:
            total_rounds = spec.rounds
            schedule = [
                (round_num, idx, item)
                for round_num in range(1, total_rounds + 1)
                for idx, item in enumerate(resolved_cases)
            ]

        first = resolved_cases[0]
        title = (spec.title or "").strip() or f"AI 接口测试（{len(schedule)} 条）"
        badge = spec.badge if spec.badge is not None else DEFAULT_AI_BADGE
        run_id = new_id()
        created = await self.repo.create_run(
            record_id=run_id,
            method=first.request.method,
            url=first.canonical_url,
            body_mode=first.request.body_mode,
            request_headers=first.request.headers,
            request_body=first.request.body,
            source="ai",
            title=title,
            badge=badge,
            status="running",
            total_rounds=total_rounds,
            cooldown_ms=spec.cooldown_ms,
            project_id=spec.project_id,
        )
        live = LiveConsoleRun(
            run_id=run_id,
            kind="ai_suite",
            source="ai",
            title=title,
            badge=badge,
            method=first.request.method,
            url=first.canonical_url,
            total_rounds=total_rounds,
            total_cases=len(schedule),
            cooldown_ms=spec.cooldown_ms,
            created_at=created["created_at"],
        )
        self.live[run_id] = live
        task = asyncio.create_task(
            self._run_ai_suite(run_id, spec, schedule, live, on_case_done=on_case_done),
            name=f"console-ai-suite-{run_id[:8]}",
        )
        self.active[run_id] = ActiveConsoleRun(
            id=run_id,
            kind="ai_suite",
            task=task,
            live=live,
            defer_persist=False,
        )
        return live.summary_dict()

    def _resolve_suite_cases(self, spec: ConsoleAiSuiteSpec) -> list[_ResolvedSuiteCase]:
        """在启动任何网络请求前校验并解析套件中每一条用例的 URL、头、正文与方法。"""
        base_url = spec.url
        resolved: list[_ResolvedSuiteCase] = []
        for idx, item in enumerate(spec.cases):
            resolved.append(self._resolve_one_case(spec, base_url, idx, item))
        return resolved

    def _resolve_one_case(
        self,
        spec: ConsoleAiSuiteSpec,
        base_url: str | None,
        idx: int,
        item: ConsoleAiCaseSpec,
    ) -> _ResolvedSuiteCase:
        raw_url = item.url or base_url
        if not raw_url:
            raise ValueError(f"Case #{idx + 1} is missing a target url")
        if raw_url.startswith("/") and base_url:
            raw_url = urljoin(base_url, raw_url)

        method = item.method or spec.method
        merged_headers = {**spec.headers, **item.headers}
        body = item.body
        body_mode = item.body_mode
        if body_mode is None:
            if body is not None and body.strip() and method not in {"GET", "HEAD", "OPTIONS"}:
                try:
                    json.loads(body)
                    body_mode = "json"
                except ValueError:
                    body_mode = "text"
            else:
                body_mode = "none"
        if method in {"GET", "HEAD", "OPTIONS"}:
            body_mode = "none"
            body = None

        request = ConsoleRequest(
            method=method,
            url=raw_url,
            headers=merged_headers,
            body_mode=body_mode,
            body=body if body_mode != "none" else None,
            timeout_ms=item.timeout_ms if item.timeout_ms is not None else spec.timeout_ms,
            follow_redirects=(
                item.follow_redirects
                if item.follow_redirects is not None
                else spec.follow_redirects
            ),
            sse_max_events=(
                item.sse_max_events if item.sse_max_events is not None else spec.sse_max_events
            ),
            sse_duration_ms=(
                item.sse_duration_ms if item.sse_duration_ms is not None else spec.sse_duration_ms
            ),
        )
        parsed, _origin = validate_target(request.url, self.settings.target_allowed_origins)
        canonical_url = build_url(parsed)
        path_hint = urlsplit(canonical_url).path or "/"
        label = item.label.strip() or f"{request.method} {path_hint}"

        expected_status: list[int] | None = None
        if isinstance(item.expected_status, int):
            expected_status = [item.expected_status]
        elif isinstance(item.expected_status, list) and item.expected_status:
            expected_status = [int(code) for code in item.expected_status]

        return _ResolvedSuiteCase(
            case_index=idx,
            label=label[:200],
            request=request,
            canonical_url=canonical_url,
            expected_status=expected_status,
            expect_contains=item.expect_contains,
            explicit_round=item.round_index,
        )

    async def _run_single(
        self,
        run_id: str,
        payload: ConsoleRequest,
        live: LiveConsoleRun,
        *,
        defer_persist: bool,
        source: str,
        title: str | None,
        badge: str | None,
        project_id: str | None,
    ) -> None:
        started = time.monotonic()

        async def publish_sse(event: SseEvent | None) -> None:
            if event is None:
                live.append_frame({"type": "sse_start"})
            else:
                live.append_frame({"type": "sse_event", "event": event})

        try:
            result = await self.http.execute(payload, on_sse=publish_sse)
            result.history_id = run_id
            response = result.response
            if defer_persist:
                await self.repo.save_single_result(
                    payload,
                    result,
                    source=source,
                    title=title,
                    badge=badge,
                    status="completed",
                    record_id=run_id,
                    project_id=project_id,
                )
            else:
                redirects_json = (
                    [hop.model_dump(mode="json") for hop in response.redirects]
                    if response and response.redirects
                    else None
                )
                await self.repo.finalize_run(
                    run_id,
                    status="completed",
                    ok=result.ok,
                    status_code=response.status if response else None,
                    elapsed_ms=result.elapsed_ms,
                    error_kind=result.error_kind,
                    error_message=result.error_message,
                    response_headers=response.headers if response else {},
                    response_preview=(response.body or "")[:PREVIEW_LIMIT] if response else None,
                    response_bytes=response.body_bytes if response else 0,
                    http_version=response.http_version if response else None,
                    response_reason=response.reason if response else None,
                    redirects_json=redirects_json,
                    sse_json=response.sse if response else None,
                    full_body=payload.body,
                    full_response=response.body if response else None,
                    url=result.request["url"],
                    request_headers=payload.headers,
                )

            live.status = "completed"
            live.stage = "finished"
            live.ok = result.ok
            live.status_code = response.status if response else None
            live.elapsed_ms = result.elapsed_ms
            live.error_kind = result.error_kind
            live.error_message = result.error_message
            live.completed_cases = 1
            live.finished = True
            live.append_frame({"type": "result", "result": result.model_dump(mode="json")})
        except asyncio.CancelledError:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            cancel_msg = outcome_for("cancelled").message
            await self._persist_terminal_failure(
                run_id,
                payload,
                defer_persist=defer_persist,
                source=source,
                title=title,
                badge=badge,
                project_id=project_id,
                status="cancelled",
                error_kind="cancelled",
                error_message=cancel_msg,
                elapsed_ms=elapsed,
                canonical_url=live.url,
            )
            live.status = "cancelled"
            live.stage = "finished"
            live.ok = False
            live.elapsed_ms = elapsed
            live.error_kind = "cancelled"
            live.error_message = cancel_msg
            live.finished = True
            live.append_frame(
                {
                    "type": "done",
                    "run_id": run_id,
                    "status": "cancelled",
                    "ok": False,
                    "error_kind": "cancelled",
                }
            )
        except TargetError as error:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            code = str(error)
            message = "Target rejected by local target policy"
            if not defer_persist:
                await self._persist_terminal_failure(
                    run_id,
                    payload,
                    defer_persist=False,
                    source=source,
                    title=title,
                    badge=badge,
                    project_id=project_id,
                    status="error",
                    error_kind=code,
                    error_message=message,
                    elapsed_ms=elapsed,
                    canonical_url=live.url,
                )
            live.status = "error"
            live.stage = "finished"
            live.ok = False
            live.elapsed_ms = elapsed
            live.error_kind = code
            live.error_message = message
            live.finished = True
            live.append_frame({"type": "error", "status": 400, "code": code, "message": message})
        except Exception:
            logger.error("Live console request could not finish", exc_info=True)
            elapsed = round((time.monotonic() - started) * 1000, 2)
            if not defer_persist:
                await self._persist_terminal_failure(
                    run_id,
                    payload,
                    defer_persist=False,
                    source=source,
                    title=title,
                    badge=badge,
                    project_id=project_id,
                    status="error",
                    error_kind="internal_error",
                    error_message="Internal server error",
                    elapsed_ms=elapsed,
                    canonical_url=live.url,
                )
            live.status = "error"
            live.stage = "finished"
            live.ok = False
            live.elapsed_ms = elapsed
            live.error_kind = "internal_error"
            live.error_message = "Internal server error"
            live.finished = True
            live.append_frame(
                {
                    "type": "error",
                    "status": 500,
                    "code": "internal_error",
                    "message": "Internal server error",
                }
            )
        finally:
            live.finished = True
            self.active.pop(run_id, None)
            self.live.pop(run_id, None)
            live.notify()

    async def _persist_terminal_failure(
        self,
        run_id: str,
        payload: ConsoleRequest,
        *,
        defer_persist: bool,
        source: str,
        title: str | None,
        badge: str | None,
        project_id: str | None,
        status: str,
        error_kind: str,
        error_message: str,
        elapsed_ms: float,
        canonical_url: str,
    ) -> None:
        if defer_persist:
            await self.repo.create_run(
                record_id=run_id,
                method=payload.method,
                url=canonical_url,
                body_mode=payload.body_mode,
                request_headers=payload.headers,
                request_body=payload.body,
                source=source,
                title=title,
                badge=badge,
                status=status,
                total_rounds=1,
                cooldown_ms=500,
                project_id=project_id,
            )
        await self.repo.finalize_run(
            run_id,
            status=status,
            ok=False,
            elapsed_ms=elapsed_ms,
            error_kind=error_kind,
            error_message=error_message,
            full_body=payload.body,
            request_headers=payload.headers,
        )

    async def _cooldown(self, run_id: str, live: LiveConsoleRun, cooldown_ms: int) -> None:
        """以 100ms 可中断分片执行轮次/用例间冷却，保证取消立即生效。"""
        live.stage = "cooling_down"
        remaining = int(cooldown_ms)
        live.cooldown_remaining_ms = remaining
        live.append_frame(
            {
                "type": "cooling_down",
                "run_id": run_id,
                "round": live.current_round,
                "cooldown_ms": remaining,
                "cooldown_remaining_ms": remaining,
            }
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (remaining / 1000.0)
        while True:
            left_s = deadline - loop.time()
            if left_s <= 0:
                break
            await asyncio.sleep(min(COOLDOWN_SLICE_SECONDS, left_s))
            live.cooldown_remaining_ms = max(0, int(round((deadline - loop.time()) * 1000)))
        live.stage = "executing"
        live.cooldown_remaining_ms = None
        live.notify()

    async def _run_ai_suite(
        self,
        run_id: str,
        spec: ConsoleAiSuiteSpec,
        schedule: list[tuple[int, int, _ResolvedSuiteCase]],
        live: LiveConsoleRun,
        *,
        on_case_done=None,
    ) -> None:
        started = time.monotonic()
        live.append_frame({"type": "progress", **live.summary_dict()})

        ok_count = 0
        failed_count = 0
        error_count = 0
        case_links: list[dict[str, Any]] = []
        last_result: ConsoleResult | None = None

        try:
            for step_idx, (round_num, case_idx, item) in enumerate(schedule):
                live.current_round = round_num
                live.stage = "executing"
                live.cooldown_remaining_ms = None

                result = await self.http.execute(item.request)
                last_result = result
                assertion_summary = _evaluate_case_assertions(item, result)

                if not result.ok:
                    error_count += 1
                elif assertion_summary is not None and not assertion_summary["passed"]:
                    failed_count += 1
                elif (
                    assertion_summary is None
                    and result.response is not None
                    and result.response.status >= 400
                ):
                    failed_count += 1
                else:
                    ok_count += 1

                response = result.response
                redirects_json = (
                    [hop.model_dump(mode="json") for hop in response.redirects]
                    if response and response.redirects
                    else None
                )
                sub_case = await self.repo.append_sub_case(
                    run_id,
                    {
                        "round_index": round_num,
                        "case_index": case_idx,
                        "label": item.label,
                        "method": item.request.method,
                        "url": result.request["url"],
                        "request_headers": item.request.headers,
                        "body_mode": item.request.body_mode,
                        "request_body": item.request.body,
                        "ok": result.ok,
                        "status_code": response.status if response else None,
                        "elapsed_ms": int(round(result.elapsed_ms)),
                        "error_kind": result.error_kind,
                        "error_message": result.error_message,
                        "response_headers": response.headers if response else None,
                        "response_preview": (
                            (response.body or "")[:PREVIEW_LIMIT] if response else None
                        ),
                        "full_response": response.body if response else None,
                        "response_bytes": response.body_bytes if response else 0,
                        "http_version": response.http_version if response else None,
                        "response_reason": response.reason if response else None,
                        "redirects_json": redirects_json,
                        "sse_json": response.sse if response else None,
                        "assertion_summary": assertion_summary,
                    },
                )
                case_link = build_case_link(run_id, sub_case)
                case_links.append(case_link)
                live.case_links.append(case_link)
                live.completed_cases = len(case_links)
                live.elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                live.append_frame(
                    {
                        "type": "case_done",
                        "run_id": run_id,
                        "round": round_num,
                        "case_index": case_idx,
                        "completed_cases": live.completed_cases,
                        "total_cases": live.total_cases,
                        "case": sub_case,
                        "case_link": case_link,
                    }
                )
                await _invoke_callback(on_case_done, case_link)

                if step_idx < len(schedule) - 1 and spec.cooldown_ms > 0:
                    await self._cooldown(run_id, live, spec.cooldown_ms)

            total_elapsed = round((time.monotonic() - started) * 1000, 2)
            all_passed = error_count == 0 and failed_count == 0 and len(case_links) > 0
            summary = (
                f"共执行 {len(schedule)} 条用例"
                f"（{live.total_rounds} 轮，冷却 {spec.cooldown_ms} ms）："
                f"通过 {ok_count} 条，状态/断言异常 {failed_count} 条，传输失败 {error_count} 条。"
            )
            last_resp = last_result.response if last_result else None
            last_redirects = (
                [hop.model_dump(mode="json") for hop in last_resp.redirects]
                if last_resp and last_resp.redirects
                else None
            )
            final_record = await self.repo.finalize_run(
                run_id,
                status="completed",
                ok=all_passed,
                status_code=last_resp.status if last_resp else None,
                elapsed_ms=total_elapsed,
                error_kind=last_result.error_kind if last_result and not last_result.ok else None,
                error_message=(
                    last_result.error_message if last_result and not last_result.ok else None
                ),
                response_headers=last_resp.headers if last_resp else {},
                response_preview=(last_resp.body or "")[:PREVIEW_LIMIT] if last_resp else None,
                response_bytes=last_resp.body_bytes if last_resp else 0,
                http_version=last_resp.http_version if last_resp else None,
                response_reason=last_resp.reason if last_resp else None,
                redirects_json=last_redirects,
                sse_json=last_resp.sse if last_resp else None,
                summary=summary,
                full_response=last_resp.body if last_resp else None,
            )
            live.status = "completed"
            live.stage = "finished"
            live.ok = all_passed
            live.status_code = final_record["status_code"]
            live.elapsed_ms = total_elapsed
            live.summary = final_record["summary"]
            live.finished = True
            live.append_frame(
                {
                    "type": "done",
                    "run_id": run_id,
                    "status": "completed",
                    "ok": all_passed,
                    "summary": final_record["summary"],
                    "case_links": list(case_links),
                    "record": final_record,
                }
            )
        except asyncio.CancelledError:
            total_elapsed = round((time.monotonic() - started) * 1000, 2)
            summary = (
                f"测试已取消：已完成 {len(case_links)}/{len(schedule)} 条用例"
                f"（通过 {ok_count}，异常 {failed_count}，传输失败 {error_count}）。"
            )
            final_record = await self.repo.finalize_run(
                run_id,
                status="cancelled",
                ok=False,
                elapsed_ms=total_elapsed,
                error_kind="cancelled",
                error_message=outcome_for("cancelled").message,
                summary=summary,
            )
            live.status = "cancelled"
            live.stage = "finished"
            live.ok = False
            live.elapsed_ms = total_elapsed
            live.error_kind = "cancelled"
            live.error_message = outcome_for("cancelled").message
            live.summary = final_record["summary"]
            live.cooldown_remaining_ms = None
            live.finished = True
            live.append_frame(
                {
                    "type": "done",
                    "run_id": run_id,
                    "status": "cancelled",
                    "ok": False,
                    "error_kind": "cancelled",
                    "summary": final_record["summary"],
                    "case_links": list(case_links),
                    "record": final_record,
                }
            )
        except Exception:
            logger.error("Console AI suite failed", exc_info=True)
            total_elapsed = round((time.monotonic() - started) * 1000, 2)
            summary = f"测试因内部异常中止：已完成 {len(case_links)}/{len(schedule)} 条用例。"
            final_record = await self.repo.finalize_run(
                run_id,
                status="error",
                ok=False,
                elapsed_ms=total_elapsed,
                error_kind="console_suite_failed",
                error_message="Internal server error",
                summary=summary,
            )
            live.status = "error"
            live.stage = "finished"
            live.ok = False
            live.elapsed_ms = total_elapsed
            live.error_kind = "console_suite_failed"
            live.error_message = "Internal server error"
            live.summary = final_record["summary"]
            live.cooldown_remaining_ms = None
            live.finished = True
            live.append_frame(
                {
                    "type": "done",
                    "run_id": run_id,
                    "status": "error",
                    "ok": False,
                    "error_kind": "console_suite_failed",
                    "summary": final_record["summary"],
                    "case_links": list(case_links),
                    "record": final_record,
                }
            )
        finally:
            live.cooldown_remaining_ms = None
            live.finished = True
            self.active.pop(run_id, None)
            self.live.pop(run_id, None)
            live.notify()

    async def observe(
        self,
        run_id: str,
        *,
        live: LiveConsoleRun | None = None,
        heartbeat_seconds: float = 10.0,
        request_for_error=None,
    ):
        """以可重连 NDJSON 流观察运行进度；客户端断开仅关闭本生成器，绝不取消后台任务。"""
        target_live = live or self.live.get(run_id)
        if target_live is not None:
            cursor = 0
            while True:
                changed = target_live.changed
                batch = target_live.frames[cursor:]
                cursor += len(batch)
                for frame in batch:
                    if (
                        frame.get("type") == "error"
                        and request_for_error is not None
                        and "error" not in frame
                    ):
                        envelope = error_response(
                            request_for_error,
                            int(frame.get("status", 500)),
                            str(frame.get("code", "internal_error")),
                            str(frame.get("message", "Internal server error")),
                        )
                        yield _encode_frame(
                            {
                                "type": "error",
                                "status": int(frame.get("status", 500)),
                                **json.loads(envelope.body),
                            }
                        )
                    else:
                        yield _encode_frame(frame)
                if target_live.finished and cursor >= len(target_live.frames):
                    return
                try:
                    await asyncio.wait_for(changed.wait(), timeout=heartbeat_seconds)
                except TimeoutError:
                    yield b'{"type":"heartbeat"}\n'
            return

        detail = await self.repo.get_detail(run_id)
        if detail is None:
            return
        if detail.get("source") == "ai" or detail.get("sub_cases"):
            yield _encode_frame(
                {
                    "type": "done",
                    "run_id": run_id,
                    "status": detail["status"],
                    "ok": detail["ok"],
                    "summary": detail.get("summary"),
                    "case_links": [
                        build_case_link(run_id, sc) for sc in detail.get("sub_cases", [])
                    ],
                    "record": detail,
                }
            )
            return
        yield _encode_frame(
            {
                "type": "done",
                "run_id": run_id,
                "status": detail["status"],
                "ok": detail["ok"],
                "record": detail,
            }
        )

    async def wait_for_completion(self, run_id: str) -> dict[str, Any]:
        """等待指定后台运行进入终态并返回持久化详情；等待方被取消不会取消后台任务。"""
        active = self.active.get(run_id)
        if active is not None:
            await _await_task_done(active.task)
        detail = await self.repo.get_detail(run_id)
        if detail is None:
            raise KeyError(f"Console request {run_id} was not found")
        return detail

    async def cancel(self, run_id: str) -> dict[str, Any] | None:
        """显式取消指定后台运行并等待收尾落库，返回终态详情（不存在时返回 None）。"""
        active = self.active.get(run_id)
        if active is not None:
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)
            # 若任务在首条指令前即被取消，未能进入 _run_* 的 except/finally，补做收尾。
            if run_id in self.active:
                detail = await self.repo.get_detail(run_id)
                if detail is not None and detail["status"] in {"running", "queued"}:
                    await self.repo.finalize_run(
                        run_id,
                        status="cancelled",
                        ok=False,
                        error_kind="cancelled",
                        error_message=outcome_for("cancelled").message,
                    )
                active.live.status = "cancelled"
                active.live.stage = "finished"
                active.live.error_kind = "cancelled"
                active.live.finished = True
                self.active.pop(run_id, None)
                self.live.pop(run_id, None)
                active.live.notify()
        return await self.repo.get_detail(run_id)

    def active_runs(self) -> list[dict[str, Any]]:
        """返回当前所有仍在后台执行的控制台运行摘要列表。"""
        return [run.live.summary_dict() for run in self.active.values()]

    async def recover(self) -> int:
        """应用启动时恢复因上次进程中断残留的运行中记录。"""
        return await self.repo.recover()

    async def shutdown(self) -> None:
        """应用关闭前取消并等待全部后台控制台运行安全回收。"""
        await asyncio.gather(
            *(self.cancel(run_id) for run_id in list(self.active)),
            return_exceptions=True,
        )
