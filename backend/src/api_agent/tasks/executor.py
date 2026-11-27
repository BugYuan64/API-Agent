"""Sequential case execution, stateful variable extraction/injection, and assertions."""

import json
from typing import Any
from urllib.parse import quote

from api_agent.console.service import ConsoleRequest, HttpTestClient
from api_agent.console.sse import SseProgress
from api_agent.execution.variables import (
    PLACEHOLDER_RE,
    extract_from_response,
    is_safe_header_value,
    is_safe_resolved_path,
    resolve_json_value,
    resolve_string,
    scalar_to_text,
)
from api_agent.infrastructure.json_pointer import resolve
from api_agent.infrastructure.targets import TargetError, validate_target
from api_agent.streams.client import WebSocketTestClient
from api_agent.streams.schemas import StreamStart
from api_agent.tasks.evidence import TaskEvidence


def _resolve_task_path(raw_path: str, variables: dict[str, Any]) -> tuple[str, list[str], bool]:
    """解析任务用例的相对路径与查询串中的 `{{var}}`，并校验路径段无穿越注入。

    返回 `(resolved_path, missing_vars, path_safe)`。
    """
    if "{{" not in raw_path:
        return raw_path, [], True
    if "?" in raw_path:
        raw_route, raw_query = raw_path.split("?", 1)
    else:
        raw_route, raw_query = raw_path, None

    missing: list[str] = []
    route_had_vars = "{{" in raw_route
    resolved_route, route_miss = resolve_string(raw_route, variables, preserve_scalar=False)
    missing.extend(route_miss)

    resolved_query = raw_query
    if raw_query is not None and "{{" in raw_query:

        def _replace_query_var(match):
            name = match.group(1)
            if name not in variables:
                if name not in missing:
                    missing.append(name)
                return match.group(0)
            return quote(scalar_to_text(variables[name]), safe="")

        resolved_query = PLACEHOLDER_RE.sub(_replace_query_var, raw_query)

    if missing:
        return raw_path, missing, True

    path_safe = True
    if route_had_vars and not is_safe_resolved_path(str(resolved_route)):
        path_safe = False
    final_path = (
        f"{resolved_route}?{resolved_query}" if resolved_query is not None else str(resolved_route)
    )
    return final_path, [], path_safe


def evaluate(check, evidence, *, expected_override=None, has_override=False):
    """Evaluate against real evidence; unavailable values remain unknown rather than passed."""
    expected = expected_override if has_override else check.expected
    actual = None
    present = True
    response = evidence.get("response") or {}
    session = evidence.get("session") or {}
    if check.kind == "status":
        actual = response.get("status", session.get("http_status"))
        if session.get("opened_at") and session.get("kind") == "websocket":
            actual = 101
        present = actual is not None
    elif check.kind == "contains":
        actual = response.get("body")
        present = actual is not None
    elif check.kind == "json_equals":
        try:
            present, actual = resolve(json.loads(response.get("body") or ""), check.pointer)
        except (ValueError, TypeError):
            present = False
    elif check.kind == "event_count":
        events = (response.get("sse") or {}).get("events")
        if events is None and session:
            events = [
                event
                for event in evidence["events"]
                if event["direction"] == "in" and event["kind"] in {"text", "binary"}
            ]
        present = events is not None
        actual = len(events) if present else None
    elif check.kind == "message_contains":
        events = (response.get("sse") or {}).get("events", evidence.get("events"))
        present = events is not None
        actual = "\n".join(
            event.get("data") or ""
            for event in events or []
            if event.get("direction", "in") == "in"
        )
    passed = None
    if present:
        if check.kind in {"contains", "message_contains"}:
            passed = isinstance(expected, str) and expected in actual
        elif check.kind == "event_count":
            passed = type(expected) is int and actual >= expected
        else:
            passed = type(actual) is type(expected) and actual == expected
    truncated = response.get("body_truncated", False) or (response.get("sse") or {}).get(
        "stop_reason"
    ) in {"byte_limit", "event_limit", "observation_complete"}
    truncated = truncated or any(event.get("truncated") for event in evidence.get("events", []))
    if truncated and passed is False and check.kind != "status":
        passed = None
    return {**check.model_dump(), "expected": expected, "actual": actual, "passed": passed}


class CaseExecutor:
    """Dispatch cases through the same classes used by interactive testing."""

    def __init__(self, settings, streams):
        """Construct lightweight adapters over application-owned settings and sessions."""
        self.settings = settings
        self.http = HttpTestClient(settings)
        self.websocket = WebSocketTestClient(streams, settings.target_allowed_origins)

    async def execute(
        self,
        case,
        request,
        *,
        on_sse: SseProgress | None = None,
        variables: dict[str, Any] | None = None,
    ):
        """Return transport evidence, extracted variables and assertion verdicts."""
        runtime_vars = variables if variables is not None else {}

        # 1) 解析路径、请求头、请求体与 WebSocket 消息中的受控变量占位符
        resolved_path, missing_vars, path_safe = _resolve_task_path(case.path, runtime_vars)
        resolved_headers: dict[str, str] = {}
        for h_key, h_val in case.headers.items():
            r_val, h_miss = resolve_string(h_val, runtime_vars, preserve_scalar=False)
            resolved_headers[h_key] = str(r_val)
            for name in h_miss:
                if name not in missing_vars:
                    missing_vars.append(name)

        resolved_body = case.body
        if case.body is not None:
            resolved_body, b_miss = resolve_json_value(case.body, runtime_vars)
            for name in b_miss:
                if name not in missing_vars:
                    missing_vars.append(name)

        resolved_messages: list[str] = []
        for msg in case.messages:
            r_msg, m_miss = resolve_string(msg, runtime_vars, preserve_scalar=False)
            resolved_messages.append(str(r_msg))
            for name in m_miss:
                if name not in missing_vars:
                    missing_vars.append(name)

        if missing_vars:
            return {
                "status": "skipped",
                "reason": "missing_variable:" + ",".join(missing_vars),
                "assertions": [],
            }

        if not path_safe or any(not is_safe_header_value(val) for val in resolved_headers.values()):
            return {
                "status": "error",
                "reason": "unsafe_variable_value",
                "assertions": [],
            }

        url = request.origin.rstrip("/") + resolved_path
        try:
            validate_target(url, self.settings.target_allowed_origins)
        except TargetError:
            return {
                "status": "error",
                "reason": "unsafe_variable_value",
                "assertions": [],
            }

        # 2) 执行目标请求
        raw_response_body: str | None = None
        if case.protocol == "websocket":
            evidence = await self.websocket.execute(
                StreamStart(
                    url=url,
                    headers=resolved_headers,
                    send=resolved_messages,
                    max_events=request.max_events,
                    max_duration_ms=request.observation_ms,
                    connect_timeout_ms=request.timeout_ms,
                )
            )
            transport_ok = (
                evidence["session"]["opened_at"] is not None
                and not evidence["session"]["error_kind"]
            )
            if evidence["session"]["close_code"] not in {None, 1000, 1001} and evidence["session"][
                "stop_reason"
            ] not in {"deadline_exceeded", "limits_exceeded"}:
                transport_ok = False
            for event in evidence["events"]:
                event["data"] = TaskEvidence.redact_body(event.get("data"))
        else:
            headers = dict(resolved_headers)
            effective_timeout_ms = request.timeout_ms
            if case.protocol == "sse":
                headers.setdefault("Accept", "text/event-stream")
                effective_timeout_ms = min(
                    60_000, max(request.timeout_ms, request.observation_ms + 5000)
                )
            evidence = (
                await self.http.execute(
                    ConsoleRequest(
                        method=case.method,
                        url=url,
                        headers=headers,
                        body_mode="json" if resolved_body is not None else "none",
                        body=json.dumps(resolved_body) if resolved_body is not None else None,
                        timeout_ms=effective_timeout_ms,
                        sse_duration_ms=request.observation_ms,
                        sse_max_events=request.max_events,
                    ),
                    on_sse=on_sse,
                )
            ).model_dump(mode="json")
            transport_ok = evidence["ok"]
            raw_response_body = (evidence.get("response") or {}).get("body")
            events = ((evidence.get("response") or {}).get("sse") or {}).get("events", [])
            for event in events:
                event["data"] = TaskEvidence.redact_body(event.get("data"))
            if events:
                evidence["response"]["body"] = None

        # 3) 在正文脱敏前执行响应变量提取
        extracted_records: list[dict[str, Any]] = []
        extracted_secrets: set[str] = set()
        extraction_failed = False
        extract_rules = getattr(case, "extract", None) or []
        if extract_rules and transport_ok and case.protocol != "websocket":
            resp_obj = evidence.get("response") or {}
            parsed_json = None
            has_json = False
            if isinstance(raw_response_body, str) and raw_response_body:
                try:
                    parsed_json = json.loads(raw_response_body)
                    has_json = True
                except ValueError:
                    pass
            new_vars, extracted_records, extracted_secrets, extraction_failed = (
                extract_from_response(
                    extract_rules,
                    status_code=resp_obj.get("status"),
                    headers=resp_obj.get("headers") or {},
                    has_parsed_json=has_json,
                    parsed_json=parsed_json,
                )
            )
            if variables is not None:
                variables.update(new_vars)

        # 4) 评估断言（支持期望值引用已绑定变量）
        assertions = []
        for check in case.assertions:
            resolved_expected, exp_miss = resolve_json_value(check.expected, runtime_vars)
            if exp_miss:
                assertions.append(
                    {
                        **check.model_dump(),
                        "actual": None,
                        "passed": False,
                    }
                )
            else:
                assertions.append(
                    evaluate(
                        check,
                        evidence,
                        expected_override=resolved_expected,
                        has_override=True,
                    )
                )

        if not transport_ok:
            status = "error"
            reason = None
        elif any(check["passed"] is False for check in assertions):
            status = "failed"
            reason = None
        elif extraction_failed:
            status = "failed"
            reason = "variable_extraction_failed"
        elif not assertions or any(check["passed"] is None for check in assertions):
            status = "unknown"
            reason = None
        else:
            status = "passed"
            reason = None

        output: dict[str, Any] = {
            "status": status,
            "assertions": assertions,
            "evidence": TaskEvidence.compact(evidence),
        }
        if reason:
            output["reason"] = reason
        if extracted_records:
            output["extracted"] = extracted_records
        if extracted_secrets:
            output["_extracted_secrets"] = extracted_secrets
        return output
