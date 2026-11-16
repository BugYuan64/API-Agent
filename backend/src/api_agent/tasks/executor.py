"""Sequential case execution and assertions over shared HTTP/SSE and WebSocket clients."""

import json

from api_agent.console.service import ConsoleRequest, HttpTestClient
from api_agent.infrastructure.json_pointer import resolve
from api_agent.streams.client import WebSocketTestClient
from api_agent.streams.schemas import StreamStart


def evaluate(check, evidence):
    """Evaluate against real evidence; unavailable values remain unknown rather than passed."""
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
            passed = isinstance(check.expected, str) and check.expected in actual
        elif check.kind == "event_count":
            passed = type(check.expected) is int and actual >= check.expected
        else:
            passed = type(actual) is type(check.expected) and actual == check.expected
    truncated = response.get("body_truncated", False) or (response.get("sse") or {}).get(
        "stop_reason"
    ) in {"byte_limit", "event_limit", "observation_complete"}
    truncated = truncated or any(event.get("truncated") for event in evidence.get("events", []))
    if truncated and passed is False and check.kind != "status":
        passed = None
    return {**check.model_dump(), "actual": actual, "passed": passed}


class CaseExecutor:
    """Dispatch cases through the same classes used by interactive testing."""

    def __init__(self, settings, streams):
        """Construct lightweight adapters over application-owned settings and sessions."""
        self.http = HttpTestClient(settings)
        self.websocket = WebSocketTestClient(streams, settings.target_allowed_origins)

    async def execute(self, case, request):
        """Return transport evidence and assertion verdicts without model involvement."""
        url = request.origin.rstrip("/") + case.path
        if case.protocol == "websocket":
            evidence = await self.websocket.execute(
                StreamStart(
                    url=url,
                    headers=case.headers,
                    send=case.messages,
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
                event["data"] = self._redact_body(event.get("data"))
        else:
            headers = dict(case.headers)
            if case.protocol == "sse":
                headers.setdefault("Accept", "text/event-stream")
            evidence = (
                await self.http.execute(
                    ConsoleRequest(
                        method=case.method,
                        url=url,
                        headers=headers,
                        body_mode="json" if case.body is not None else "none",
                        body=json.dumps(case.body) if case.body is not None else None,
                        timeout_ms=request.timeout_ms,
                        sse_duration_ms=request.observation_ms,
                        sse_max_events=request.max_events,
                    )
                )
            ).model_dump(mode="json")
            transport_ok = evidence["ok"]
            events = (evidence.get("response") or {}).get("sse", {}).get("events", [])
            for event in events:
                event["data"] = self._redact_body(event.get("data"))
            if events:
                evidence["response"]["body"] = None
        assertions = [evaluate(check, evidence) for check in case.assertions]
        status = (
            "error"
            if not transport_ok
            else "failed"
            if any(check["passed"] is False for check in assertions)
            else "unknown"
            if not assertions or any(check["passed"] is None for check in assertions)
            else "passed"
        )
        return {
            "status": status,
            "assertions": assertions,
            "evidence": self._compact_evidence(evidence),
        }

    @staticmethod
    def _compact_evidence(evidence):
        """Bound persisted evidence while preserving actual response and event totals."""
        response = evidence.get("response")
        if response and isinstance(response.get("body"), str) and len(response["body"]) > 16000:
            response["body"] = response["body"][:16000]
            response["body_truncated"] = True
        events = (response or {}).get("sse", {}).get("events")
        if events is None:
            events = evidence.get("events")
        if not isinstance(events, list):
            return evidence
        for event in events:
            data = event.get("data")
            if isinstance(data, str) and len(data) > 1000:
                event["data"] = data[:1000]
                event["truncated"] = True
        if len(events) > 40:
            if response and response.get("sse"):
                response["sse"].update(
                    total_events=len(events), events_truncated=True, events=events[:40]
                )
            else:
                evidence.update(total_events=len(events), events_truncated=True, events=events[:40])
        return evidence

    @staticmethod
    def _redact_body(value):
        """Redact labelled credentials in SSE JSON payloads before persistence or model context."""
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            from api_agent.infrastructure.privacy import Context

            return Context().text(value)
        if not isinstance(parsed, (dict, list)):
            from api_agent.infrastructure.privacy import Context

            return Context().text(value)
        from api_agent.infrastructure.privacy import Context

        context = Context()
        context.learn(parsed)
        return json.dumps(context.clean(parsed), ensure_ascii=False)
