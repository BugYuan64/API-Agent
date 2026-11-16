"""结果分类与总挂钟超时回归；仅访问测试创建的回环目标，不调用外部服务。"""

import asyncio
import contextlib
import json
import ssl
import time
from datetime import UTC, datetime

import httpx
import pytest

from api_agent.execution.api_schemas import ExecutionHistoryResponse
from api_agent.execution.native import NativeHttpEngine
from api_agent.execution.report import normalize_report
from api_agent.execution.schemas import CaseResult, ExecutionPlan, RunResult
from api_agent.infrastructure.http import ResponseEncodingError, ResponseLimitError
from api_agent.infrastructure.orm import utc_now
from api_agent.streams.schemas import StreamHistoryResponse, StreamSessionResponse
from api_agent.streams.state import ActiveStream, serialize_stream


async def test_console_openapi_declares_classified_failure_envelopes(app):
    """API 客户端可从 OpenAPI 得知传输失败仍返回完整 ConsoleResult 信封。"""
    responses = app.openapi()["paths"]["/api/console/requests"]["post"]["responses"]
    for status in ("413", "502", "504"):
        schema = responses[status]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/ConsoleResult")


def make_plan(origin="http://127.0.0.1:9", count=1, **limits):
    """创建有 JSON 指针断言的受控计划，允许测试覆盖单请求与整轮限额。"""
    return ExecutionPlan.model_validate(
        {
            "origin": origin,
            "allowed_methods": ["GET"],
            "path_templates": ["/drip"],
            "requests": [
                {
                    "id": f"case-{index}",
                    "method": "GET",
                    "path": "/drip",
                    "assertions": [{"kind": "json_exists", "pointer": "/ok"}],
                }
                for index in range(count)
            ],
            "delay_ms": 0,
            **limits,
        }
    )


@pytest.fixture
async def wire_target():
    """提供慢正文、分段重定向和真实 HTTP 504，结束时回收所有连接任务。"""
    tasks = set()

    async def handle(reader, writer):
        """每次读到完整请求头后发送指定响应，断开时仅关闭本连接。"""
        task = asyncio.current_task()
        tasks.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            path = header.split(b" ")[1]
            if path.startswith(b"/redirect"):
                await asyncio.sleep(0.06)
                writer.write(
                    b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n"
                    b"Location: /redirect-next\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            elif path == b"/status-504":
                writer.write(b"HTTP/1.1 504 Gateway Timeout\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            else:
                body = b'{"ok":true}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                )
                await writer.drain()
                for byte in body:
                    await asyncio.sleep(0.04)
                    writer.write(bytes([byte]))
                    await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield origin
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize(
    ("status", "error", "code", "number"),
    [
        ("passed", None, "PASSED", 200),
        ("failed", None, "ASSERTION_FAILED", 422),
        ("timed_out", "request_timeout", "REQUEST_TIMEOUT", 504),
        ("timed_out", "execution_timeout", "EXECUTION_TIMEOUT", 504),
        ("cancelled", "cancelled", "CANCELLED", None),
        ("unknown", None, "UNKNOWN_RESULT", None),
        ("error", "request_failed", "CONNECTION_FAILED", 502),
        ("error", "tls_error", "TLS_ERROR", 502),
        ("error", "response_limit_exceeded", "RESPONSE_LIMIT_EXCEEDED", 413),
        ("error", "response_decode_error", "RESPONSE_ENCODING_ERROR", 502),
        ("error", "invalid_report", "INVALID_REPORT", 502),
        ("error", "cli_unavailable", "ENGINE_UNAVAILABLE", 503),
        ("error", "origin_not_allowed", "POLICY_REJECTED", 400),
        ("error", "request_budget_exhausted", "BUDGET_EXHAUSTED", 429),
        ("error", "unexpected", "EXECUTION_ERROR", 500),
    ],
)
def test_case_result_exposes_safe_outcome_without_faking_http(status, error, code, number):
    """旧字段可直接派生稳定结果分类，HTTP 字段保持未知，不从结果状态反推。"""
    payload = CaseResult(id="case", status=status, error_kind=error).model_dump()
    assert payload["result_code"] == code
    assert payload["result_status"] == number
    assert payload["http_status"] is None


async def test_native_request_deadline_covers_dripping_body(wire_target):
    """每片数据都早于 HTTPX 读超时仍须触发单请求总截止，断言保持未评估。"""
    result = await NativeHttpEngine().run(
        make_plan(wire_target, request_timeout_ms=100, timeout_ms=2000), {}
    )
    assert result.status == "timed_out"
    assert result.error_kind == "request_timeout"
    case = result.executions[0]
    assert case.status == "timed_out" and case.http_status is None
    assert case.result_code == "REQUEST_TIMEOUT" and case.result_status == 504
    assert case.assertions[0].passed is None
    assert case.assertions[0].pointer == "/ok" and case.assertions[0].index == 0
    assert result.duration_ms < 1200


@pytest.mark.parametrize("kind", ["timeout", "connect", "tls"])
async def test_native_transport_failure_never_fails_assertions(kind):
    """没有完整响应时断言没有真假，异常原文不得进入结果或消息。"""

    def handle(request):
        """模拟只能在传输层产生的异常，原文含敏感标记用于检测泄漏。"""
        if kind == "timeout":
            raise httpx.ReadTimeout("PRIVATE_TRANSPORT_DETAIL", request=request)
        error = httpx.ConnectError("PRIVATE_TRANSPORT_DETAIL", request=request)
        if kind == "tls":
            raise error from ssl.SSLError("PRIVATE_CERTIFICATE_DETAIL")
        raise error

    plan = make_plan()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        case = await NativeHttpEngine()._run_case(client, plan, plan.requests[0], {})
    assert case.assertions[0].passed is None
    assert case.assertions[0].pointer == "/ok"
    assert (
        case.result_code
        == {"timeout": "REQUEST_TIMEOUT", "connect": "CONNECTION_FAILED", "tls": "TLS_ERROR"}[kind]
    )
    assert "PRIVATE" not in case.model_dump_json()


@pytest.mark.parametrize("path", ["/drip", "/redirect"])
async def test_console_deadline_includes_body_and_all_redirects(client, wire_target, path):
    """整条操作共享截止；保存失败历史后返回结构化 504，不产生目标伪状态。"""
    started = time.monotonic()
    response = await client.post(
        "/api/console/requests",
        json={"url": wire_target + path, "timeout_ms": 100, "follow_redirects": True},
    )
    assert response.status_code == 504
    payload = response.json()
    assert payload["result_code"] == "REQUEST_TIMEOUT" and payload["result_status"] == 504
    assert payload["response"] is None and not payload["ok"]
    history = (await client.get("/api/console/history")).json()["items"][0]
    assert history["result_code"] == "REQUEST_TIMEOUT" and history["status_code"] is None
    assert time.monotonic() - started < 1.5


async def test_console_real_target_504_remains_a_response(client, wire_target):
    """目标真正返回 504 时外层仍为 200，收到响应与本机超时是两种结果。"""
    response = await client.post("/api/console/requests", json={"url": wire_target + "/status-504"})
    assert response.status_code == 200
    assert response.json()["response"]["status"] == 504
    assert response.json()["result_code"] == "HTTP_RESPONSE"


@pytest.mark.parametrize(
    ("error", "code", "number"),
    [
        (httpx.ConnectError("PRIVATE_DETAIL"), "CONNECTION_FAILED", 502),
        (ResponseLimitError("response_limit_exceeded"), "RESPONSE_LIMIT_EXCEEDED", 413),
        (ResponseEncodingError("response_decode_error"), "RESPONSE_ENCODING_ERROR", 502),
    ],
)
async def test_console_routes_preserve_error_body_and_history(
    client, monkeypatch, error, code, number
):
    """传输错误使用合适 HTTP 状态，同时保留完整业务结果和可查历史。"""

    async def fail(*args, **kwargs):
        """在真实发送边界注入可控传输错误，测试路由和存储仍实际执行。"""
        raise error

    monkeypatch.setattr("api_agent.console.service.bounded_send", fail)
    response = await client.post("/api/console/requests", json={"url": "http://127.0.0.1:9/test"})
    assert response.status_code == number
    assert response.json()["result_code"] == code
    assert "PRIVATE" not in response.text
    item = (await client.get("/api/console/history")).json()["items"][0]
    assert item["result_code"] == code and item["status_code"] is None


@pytest.mark.parametrize("error_code", ["ETIMEDOUT", "ESOCKETTIMEDOUT"])
def test_postman_request_timeout_is_known_and_assertions_unevaluated(error_code):
    """Postman 明确报告超时应保留已知类别，不归为报告不完整或伪 HTTP 504。"""
    data = {
        "run": {
            "executions": [
                {
                    "item": {"name": "case-0"},
                    "requestError": {"code": error_code, "message": "PRIVATE_TRANSPORT_DETAIL"},
                }
            ]
        }
    }
    result = normalize_report(data, make_plan(), 1)
    assert result.status == "timed_out" and result.error_kind == "request_timeout"
    assert result.result_code == "REQUEST_TIMEOUT"
    case = result.executions[0]
    assert case.status == "timed_out" and case.http_status is None
    assert case.assertions[0].passed is None and case.assertions[0].pointer == "/ok"
    assert "PRIVATE" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("status", "reason", "error", "code", "number"),
    [
        ("connecting", None, None, "RUNNING", None),
        ("open", None, None, "RUNNING", None),
        ("closed", "target_closed", None, "COMPLETED", 200),
        ("closed", "limits_exceeded", None, "LIMIT_REACHED", None),
        ("error", "handshake_rejected", "handshake_rejected", "HANDSHAKE_REJECTED", 502),
        ("error", "recovered_after_restart", None, "INTERRUPTED", None),
        ("closed", "deadline_exceeded", None, "EXECUTION_TIMEOUT", 504),
        ("cancelled", "client_stopped", None, "CANCELLED", None),
        ("closed", "client_stopped", None, "CANCELLED", None),
        ("error", "connect_timeout", "connect_timeout", "REQUEST_TIMEOUT", 504),
        ("error", "connect_failed", "connect_failed", "CONNECTION_FAILED", 502),
    ],
)
def test_stream_live_and_old_history_derive_same_outcome(status, reason, error, code, number):
    """活动序列化与无新增数据库列的历史模型输出相同结果，运行中不报告通过。"""
    stream = ActiveStream(id="stream", kind="sse", url="http://127.0.0.1:9")
    stream.status, stream.stop_reason, stream.error_kind = status, reason, error
    raw = serialize_stream(stream)
    current = StreamSessionResponse.model_validate(raw).model_dump()
    history = StreamHistoryResponse.model_validate(
        {
            **raw,
            "request_headers": {},
            "settings": {},
            "created_at": datetime(2026, 11, 9, tzinfo=UTC),
        }
    ).model_dump()
    for payload in (raw, current, history):
        assert payload["result_code"] == code and payload["result_status"] == number


def test_run_result_derived_fields_survive_serialized_roundtrip():
    """新派生字段能往返现有运行快照读取链，且不改变目标 HTTP 504 的通过判定。"""
    original = RunResult(
        status="passed",
        reserved_requests=1,
        executions=[CaseResult(id="case", status="passed", http_status=504)],
    )
    result = RunResult.model_validate_json(original.model_dump_json())
    assert result.result_status == 200 and result.result_code == "PASSED"
    assert json.loads(result.model_dump_json())["executions"][0]["http_status"] == 504


@pytest.mark.parametrize(
    ("status", "reason", "code", "number"),
    [
        ("completed", "model_budget_exhausted", "BUDGET_EXHAUSTED", 429),
        ("completed", "model_connection_failed", "MODEL_ERROR", 502),
        ("completed", "model_response_error", "MODEL_ERROR", 502),
        ("completed", "model_timeout", "MODEL_TIMEOUT", 504),
        ("completed", "model_authentication_failed", "MODEL_AUTH_FAILED", 502),
        ("completed", "no_hypotheses", "COMPLETED", 200),
        ("interrupted", "interrupted", "INTERRUPTED", None),
    ],
)
async def test_agent_rest_outcome_does_not_mutate_saved_record(
    client, app, status, reason, code, number
):
    """Agent 查询派生实际停止原因，不让 completed 表面状态覆盖失败或修改磁盘记录。"""
    record = {
        "id": "run",
        "status": status,
        "stop_reason": reason,
        "snapshot": {"contract": "fixed"},
    }
    original = json.dumps(record, sort_keys=True)
    app.state.agents.store.read = lambda run_id: record
    response = await client.get("/api/agent/runs/run")
    assert response.status_code == 200
    assert response.json()["result_code"] == code and response.json()["result_status"] == number
    assert json.dumps(record, sort_keys=True) == original


@pytest.mark.parametrize("reason", ["invalid_model_output", "no_hypotheses"])
async def test_agent_completed_with_degraded_report_is_not_success(client, app, reason):
    """报告模型失败可能保留原停止原因；退化事实不能被 completed 状态覆盖。"""
    record = {
        "id": "run",
        "status": "completed",
        "stop_reason": reason,
        "degradation_reasons": ["invalid_model_output"],
        "actual_llm_status": "degraded",
    }
    app.state.agents.store.read = lambda run_id: record
    response = await client.get("/api/agent/runs/run")
    assert response.json()["result_code"] == "MODEL_ERROR"
    assert "result_code" not in record


def test_old_execution_history_derives_nested_case_outcomes():
    """没有新字段的历史执行及嵌套用例同样输出结果分类，不需迁移 JSON 或数据表。"""
    history = ExecutionHistoryResponse.model_validate(
        {
            "id": "old",
            "project_id": None,
            "label": "",
            "engine": "native",
            "status": "error",
            "error_kind": "request_timeout",
            "duration_ms": 1,
            "reserved_requests": 1,
            "observed_attempts": 1,
            "unknown_requests": 0,
            "executions": [{"id": "case", "status": "error", "error_kind": "request_timeout"}],
            "created_at": utc_now(),
        }
    ).model_dump()
    assert history["result_code"] == "REQUEST_TIMEOUT"
    assert history["executions"][0]["result_status"] == 504


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["timed_out", "failed"], "timed_out"),
        (["timed_out", "error"], "error"),
        (["passed", "failed"], "failed"),
    ],
)
async def test_native_aggregate_priority_preserves_most_severe_result(
    monkeypatch, statuses, expected
):
    """已知错误优先于请求超时，其次才是断言失败，不能按结果出现顺序误选错误码。"""

    async def result_for(client, plan, case, secrets):
        """只替换外部执行边界，聚合逻辑和 RunResult 仍由真实引擎执行。"""
        status = statuses[int(case.id.split("-")[1])]
        error = (
            "request_timeout"
            if status == "timed_out"
            else "request_failed"
            if status == "error"
            else None
        )
        return CaseResult(id=case.id, status=status, error_kind=error)

    engine = NativeHttpEngine()
    monkeypatch.setattr(engine, "_run_case", result_for)
    result = await engine.run(make_plan(count=2), {})
    assert result.status == expected
    assert (
        result.result_code
        == {
            "error": "CONNECTION_FAILED",
            "timed_out": "REQUEST_TIMEOUT",
            "failed": "ASSERTION_FAILED",
        }[expected]
    )


@pytest.mark.parametrize("second_status", ["error", "unknown", "failed"])
def test_postman_timeout_aggregation_respects_error_and_unknown_evidence(second_status):
    """请求超时可与其他结果并存，报告缺项保守未知，连接错误优先，断言失败次之。"""
    entries = [{"item": {"name": "case-0"}, "requestError": {"code": "ETIMEDOUT"}}]
    if second_status == "error":
        entries.append({"item": {"name": "case-1"}, "requestError": {"code": "ECONNREFUSED"}})
    elif second_status == "failed":
        entries.append(
            {
                "item": {"name": "case-1"},
                "response": {"code": 200},
                "assertions": [{"assertion": "a0_json_exists", "error": {"message": "missing"}}],
            }
        )
    result = normalize_report({"run": {"executions": entries}}, make_plan(count=2), 1)
    assert result.status == ("timed_out" if second_status == "failed" else "error")
    assert (
        result.result_code
        == {"error": "CONNECTION_FAILED", "unknown": "INVALID_REPORT", "failed": "REQUEST_TIMEOUT"}[
            second_status
        ]
    )


@pytest.mark.parametrize(
    ("error", "code", "number"),
    [
        (httpx.ReadTimeout("PRIVATE_TRANSPORT_DETAIL"), "REQUEST_TIMEOUT", 504),
        (httpx.ConnectError("PRIVATE_TRANSPORT_DETAIL"), "CONNECTION_FAILED", 502),
        (ssl.SSLError("PRIVATE_CERTIFICATE_DETAIL"), "TLS_ERROR", 502),
    ],
)
async def test_stream_transport_failures_have_fixed_safe_outcomes(
    client, app, monkeypatch, error, code, number
):
    """SSE 未取得响应的传输异常应分类并安全保存，不能回显异常原文。"""

    async def fail(*args, **kwargs):
        """替换网络读取边界，保留真实后台任务、落库和接口读取链。"""
        raise error

    monkeypatch.setattr(app.state.streams, "_run_sse", fail)
    response = await client.post(
        "/api/streams", json={"kind": "sse", "url": "http://127.0.0.1:9/events"}
    )
    stream_id = response.json()["id"]
    stream = app.state.streams.active.get(stream_id) or app.state.streams.history[stream_id]
    if stream.task is not None:
        await stream.task
    page = (await client.get(f"/api/streams/{stream_id}")).json()
    assert page["session"]["result_code"] == code
    assert page["session"]["result_status"] == number
    assert page["session"]["http_status"] is None
    assert "PRIVATE" not in json.dumps(page)
