"""Native execution engine tests: assertion parity with the CLI engine."""

import pytest
from support.local_server import LocalServer

from api_agent.execution.native import NativeHttpEngine, evaluate_pointer
from api_agent.execution.schemas import ExecutionPlan


@pytest.fixture
async def target():
    """真实回环目标：原生引擎走真实 TCP，才能验证超时与响应时间断言。"""
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


def plan_for(base_url: str, assertions: list[dict], path: str = "/json") -> ExecutionPlan:
    return ExecutionPlan.model_validate(
        {
            "origin": base_url,
            "allowed_methods": ["GET"],
            "path_templates": [path],
            "requests": [{"id": "case-1", "method": "GET", "path": path, "assertions": assertions}],
        }
    )


def test_pointer_semantics_match_postman():
    """JSON Pointer 必须与 Postman atPointer 行为一致（数组下标严格十进制）。"""
    doc = {"a": [10, 20], "b": {"c": None}, "": "empty"}
    assert evaluate_pointer(doc, "") == (True, doc)
    assert evaluate_pointer(doc, "/a/1") == (True, 20)
    assert evaluate_pointer(doc, "/a/01") == (False, None)
    assert evaluate_pointer(doc, "/a/2") == (False, None)
    assert evaluate_pointer(doc, "/a/length") == (False, None)
    assert evaluate_pointer(doc, "/b/c") == (True, None)
    assert evaluate_pointer(doc, "/b/missing") == (False, None)
    assert evaluate_pointer({"x": {"y": 1}}, "/x") == (True, {"y": 1})


async def test_native_engine_passes_all_five_assertion_kinds(target):
    """五种断言全部通过时，整体应为 passed 且观察到完整预算。"""
    plan = plan_for(
        target.base_url,
        [
            {"kind": "status", "expected": 200},
            {"kind": "content_type", "expected": "application/json"},
            {"kind": "response_time", "expected": 5000},
            {"kind": "json_exists", "pointer": "/nested/name"},
            {"kind": "json_equals", "pointer": "/ok", "expected": True},
        ],
    )
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "passed", result
    assert result.engine == "native"
    assert result.observed_attempts == 1
    assert result.unknown_requests == 0
    assert all(a.passed is True for a in result.executions[0].assertions)
    assert result.executions[0].response is not None
    assert result.executions[0].response.body_bytes > 0


async def test_native_engine_failure_keeps_diff_evidence(target):
    """断言失败应记为 failed（不是 error），并给出可读差异。"""
    plan = plan_for(
        target.base_url,
        [
            {"kind": "status", "expected": 201},
            {"kind": "json_equals", "pointer": "/ok", "expected": False},
        ],
    )
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "failed"
    assertions = result.executions[0].assertions
    assert assertions[0].passed is False
    assert "received 200" in (assertions[0].message or "")
    assert assertions[1].passed is False


async def test_native_engine_marks_pointer_absence_as_failure(target):
    """指针不存在时既不能通过，也不能抛异常。"""
    plan = plan_for(
        target.base_url,
        [{"kind": "json_equals", "pointer": "/does-not-exist", "expected": 1}],
    )
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "failed"
    assert result.executions[0].assertions[0].passed is False


async def test_native_engine_connection_failure_is_error():
    """连不上目标应归类为 error，而不是断言失败。"""
    plan = plan_for("http://127.0.0.1:1", [{"kind": "status", "expected": 200}], path="/json")
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "error"
    assert result.executions[0].error_kind == "request_failed"
    assert result.unknown_requests == 0


async def test_native_engine_omit_payload_hides_response(target):
    """省略模式不能带出响应正文。"""
    plan = plan_for(target.base_url, [{"kind": "status", "expected": 200}])
    result = await NativeHttpEngine(omit_response_payload=True).run(plan, {})
    capture = result.executions[0].response
    assert capture is not None
    assert capture.omitted is True
    assert capture.body is None
    assert "fixture" not in result.model_dump_json()


async def test_native_engine_status_is_always_available():
    status = await NativeHttpEngine().status()
    assert status["available"] is True
    assert status["engine"] == "native"


def test_pointer_preserves_empty_segments_and_rejects_non_ascii_indices():
    """指针解析必须与 RFC 6901 / Postman 一致，且不得因怪异下标抛异常。

    回归来源：``lstrip("/")`` 会把 ``//a`` 的空段吃掉，使 native 与 Postman
    对同一份计划给出相反判定；``str.isdigit()`` 对 ``'٢'``、``'²'`` 为真，
    ``int()`` 要么得到非法索引、要么直接抛错把断言求成 500。
    """
    document = {"": {"a": 1}, "a": 2, "items": [10, 20]}
    # 空段必须保留：``//a`` 指向 {"": {"a": 1}} 而不是 {"a": 2}
    assert evaluate_pointer(document, "//a") == (True, 1)
    assert evaluate_pointer(document, "/items/¹") == (False, None)
    assert evaluate_pointer(document, "/items/²") == (False, None)
    assert evaluate_pointer(document, "/items/1") == (True, 20)
    assert evaluate_pointer(document, "/items/01") == (False, None)
