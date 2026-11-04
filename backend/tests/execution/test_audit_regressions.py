"""Regression contracts for audit R01/R05/R12, using bounded fake transports."""

import gzip

import httpx
import pytest

from api_agent.console.service import ConsoleRequest, send
from api_agent.execution.native import NativeHttpEngine
from api_agent.execution.schemas import ExecutionPlan
from api_agent.infrastructure.config import load_settings


def plan_for(expected, pointer="/value"):
    return ExecutionPlan.model_validate(
        {
            "origin": "http://127.0.0.1:9100",
            "allowed_methods": ["GET"],
            "path_templates": ["/json"],
            "requests": [
                {
                    "id": "case",
                    "method": "GET",
                    "path": "/json",
                    "assertions": [
                        {"kind": "json_equals", "pointer": pointer, "expected": expected}
                    ],
                }
            ],
        }
    )


@pytest.mark.parametrize(
    ("actual", "expected", "passed"),
    [
        (True, 1, False),
        (False, 0, False),
        ([True], [1], False),
        ({"nested": [False]}, {"nested": [0]}, False),
        (1, 1.0, True),
        ({"nested": [1]}, {"nested": [1.0]}, True),
    ],
)
async def test_json_comparison_preserves_json_types(actual, expected, passed):
    plan = plan_for(expected)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"value": actual}))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await NativeHttpEngine()._run_case(client, plan, plan.requests[0], {})
    assert result.assertions[0].passed is passed


async def test_business_failure_retains_structured_actual_and_pointer():
    plan = plan_for("400", "/code")
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"code": "500"}))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await NativeHttpEngine()._run_case(client, plan, plan.requests[0], {})
    assertion = result.assertions[0].model_dump()
    assert assertion["pointer"] == "/code"
    assert assertion["actual"] == "500"
    assert assertion["actual_present"] is True
    assert assertion["actual_type"] == "string"


async def test_sensitive_actual_and_omit_mode_cannot_expose_response_value():
    secret = "synthetic-audit-response-token"
    plan = plan_for("expected", "/token")
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"token": secret}))
    async with httpx.AsyncClient(transport=transport) as client:
        for omitted in (False, True):
            result = await NativeHttpEngine(omit_response_payload=omitted)._run_case(
                client, plan, plan.requests[0], {}
            )
            observation = result.assertions[0].model_dump()
            assert observation.get("actual") != secret
            if omitted:
                assert observation.get("actual") is None


class CountingBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("omit", [False, True])
async def test_native_stops_stream_before_accumulating_oversize_body(omit):
    body = CountingBody([b"a" * 32] * 20)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    plan = plan_for(True)
    engine = NativeHttpEngine(omit_response_payload=omit, max_response_bytes=64)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await engine._run_case(client, plan, plan.requests[0], {})
    assert result.error_kind == "response_limit_exceeded"
    assert body.reads < 20
    assert body.closed is True


async def test_compressed_response_cannot_expand_past_decoded_budget():
    body = CountingBody([gzip.compress(b"x" * 8192)])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=body)
    )
    plan = plan_for(True)
    engine = NativeHttpEngine(max_response_bytes=128)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await engine._run_case(client, plan, plan.requests[0], {})
    assert result.error_kind == "response_limit_exceeded"
    assert body.closed is True


async def test_console_uses_same_hard_response_boundary(tmp_path, monkeypatch):
    body = CountingBody([b"x" * 32] * 20)
    original = httpx.AsyncClient

    def client(**kwargs):
        return original(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    settings = load_settings(env_file=None, project_root=tmp_path, response_max_bytes=64)
    result = await send(settings, ConsoleRequest(url="http://127.0.0.1:9100/json"))
    assert result.ok is False
    assert result.error_kind == "response_limit_exceeded"
    assert body.reads < 20
    assert body.closed is True
