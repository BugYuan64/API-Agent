import copy

import pytest

from api_agent.execution.report import capture_headers, decode_stream, normalize_report
from api_agent.execution.schemas import ExecutionPlan


@pytest.fixture
def plan():
    return ExecutionPlan.model_validate(
        {
            "origin": "http://127.0.0.1:8080",
            "allowed_methods": ["GET"],
            "path_templates": ["/health"],
            "requests": [
                {
                    "id": "health",
                    "method": "GET",
                    "path": "/health",
                    "assertions": [{"kind": "status", "expected": 200}],
                }
            ],
        }
    )


def buffer(text: str) -> dict:
    return {"type": "Buffer", "data": list(text.encode())}


def report(body="PRIVATE_RESPONSE"):
    return {
        "run": {
            "executions": [
                {
                    "item": {"name": "health"},
                    "response": {
                        "status": "OK",
                        "code": 200,
                        "responseTime": 3,
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "stream": buffer(body),
                    },
                    "request": {"header": [{"key": "token", "value": "PRIVATE_TOKEN"}]},
                    "assertions": [{"assertion": "a0_status"}],
                }
            ]
        }
    }


def test_normalize_pass_with_captured_response(plan):
    result = normalize_report(report(), plan, 0)
    assert result.status == "passed"
    assert result.observed_attempts == 1 and result.unknown_requests == 0
    assert result.executions[0].assertions[0].passed is True
    # 默认开启捕获：响应体与响应头都原样带出。
    payload = result.executions[0].response
    assert payload is not None
    assert payload.body == "PRIVATE_RESPONSE"
    assert payload.headers == {"Content-Type": ["application/json"]}
    assert payload.status_text == "OK"
    assert payload.omitted is False


def test_capture_can_be_disabled_per_run(plan):
    result = normalize_report(report(), plan, 0, capture=False)
    assert result.status == "passed"
    payload = result.executions[0].response
    assert payload is not None and payload.omitted is True
    assert payload.body is None and payload.headers == {}
    assert "PRIVATE_RESPONSE" not in result.model_dump_json()


def test_omitted_report_is_marked_not_silently_empty(plan):
    data = report()
    response = data["run"]["executions"][0]["response"]
    response.pop("header")
    response.pop("stream")
    payload = normalize_report(data, plan, 0).executions[0].response
    assert payload is not None
    assert payload.omitted is True
    assert payload.body is None and payload.headers == {}


def test_failed_assertion_carries_engine_message(plan):
    data = report()
    data["run"]["executions"][0]["assertions"][0]["error"] = {
        "message": "expected 200 to deeply equal 201"
    }
    result = normalize_report(data, plan, 1)
    assert result.status == "failed"
    assertion = result.executions[0].assertions[0]
    assert assertion.passed is False
    assert assertion.message == "expected 200 to deeply equal 201"


@pytest.mark.parametrize(
    "change",
    [
        "no_run",
        "empty",
        "missing_assertion",
        "wrong_name",
        "duplicate",
        "no_response",
        "unknown_assertion",
        "skipped",
    ],
)
def test_missing_or_unexpected_evidence_is_never_a_pass(plan, change):
    data = report()
    execution = data["run"]["executions"][0]
    if change == "no_run":
        data = {}
    elif change == "empty":
        data["run"]["executions"] = []
    elif change == "missing_assertion":
        execution["assertions"] = []
    elif change == "wrong_name":
        execution["item"]["name"] = "other"
    elif change == "duplicate":
        data["run"]["executions"].append(copy.deepcopy(execution))
    elif change == "no_response":
        execution.pop("response")
    elif change == "unknown_assertion":
        execution["assertions"][0]["assertion"] = "wrong"
    elif change == "skipped":
        execution["assertions"][0]["skipped"] = True
    assert normalize_report(data, plan, 0).status == "error"


def test_network_error_is_not_an_assertion_failure(plan):
    data = report()
    data["run"]["executions"][0]["requestError"] = {"message": "connect ECONNREFUSED"}
    result = normalize_report(data, plan, 1)
    assert result.status == "error"
    assert result.executions[0].error_kind == "request_error"
    assert result.executions[0].response.body == "connect ECONNREFUSED"


def test_nonzero_exit_with_all_passed_assertions_is_tool_error(plan):
    assert normalize_report(report(), plan, 1).status == "error"


def test_decode_stream_handles_buffer_base64_and_text():
    assert decode_stream(buffer("hello")) == ("hello", 5, False)
    assert decode_stream("aGVsbG8=")[0] == "hello"
    assert decode_stream("plain text")[0] == "plain text"
    assert decode_stream(None) == (None, 0, False)
    assert decode_stream({"type": "Buffer", "data": "bad"}) == (None, 0, True)


def test_decode_stream_truncates_oversized_body(monkeypatch):
    import api_agent.execution.report as report_module

    monkeypatch.setattr(report_module, "MAX_BODY_CHARS", 4)
    body, size, truncated = decode_stream(buffer("0123456789"))
    assert body == "0123"
    assert size == 10
    assert truncated is True


def test_capture_headers_keeps_all_values_and_skips_bad_entries():
    raw = [
        {"key": "Set-Cookie", "value": "a=1"},
        {"key": "Set-Cookie", "value": "b=2"},
        {"key": "", "value": "ignored"},
        {"value": "no-key"},
        "not-a-dict",
    ]
    assert capture_headers(raw) == {"Set-Cookie": ["a=1", "b=2"]}
    assert capture_headers(None) == {}
