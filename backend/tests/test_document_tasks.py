"""End-to-end document tasks using real local HTTP/SSE/WebSocket transports and scripted models."""

import asyncio
import json

import pytest
from support.local_server import LocalServer

from api_agent.agent.service import AgentError, AgentService
from api_agent.llm.client import OpenAIClient
from api_agent.llm.schemas import ChatCompletionResponse
from api_agent.tasks.documents import NativeCaseGenerator, recognize
from api_agent.tasks.executor import evaluate
from api_agent.tasks.schemas import Catalog, Check


@pytest.fixture
async def target():
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


def catalog():
    """Cover original cases, HTTP JSON, SSE events and WebSocket scheduled messages."""
    return Catalog.model_validate(
        {
            "endpoints": [
                {
                    "name": "json",
                    "path": "/json",
                    "assertions": [{"kind": "status", "expected": 200}],
                    "cases": [
                        {
                            "name": "original",
                            "path": "/json",
                            "assertions": [
                                {"kind": "json_equals", "pointer": "/ok", "expected": True}
                            ],
                        }
                    ],
                },
                {
                    "name": "sse",
                    "path": "/sse",
                    "protocol": "sse",
                    "assertions": [
                        {"kind": "event_count", "expected": 5},
                        {"kind": "message_contains", "expected": "line one\nline two"},
                    ],
                },
                {
                    "name": "socket",
                    "path": "/ws",
                    "protocol": "websocket",
                    "messages": ["ping"],
                    "assertions": [{"kind": "message_contains", "expected": "echo:ping"}],
                },
            ]
        }
    )


async def enable_model(client, monkeypatch, response=None):
    await client.put(
        "/api/settings/llm",
        json={"enabled": True, "base_url": "http://127.0.0.1:9101/v1", "model": "fixture"},
    )

    async def complete(self, request):
        content = response or {"overview": "已核查全部执行结果。", "recommendations": []}
        return ChatCompletionResponse(
            id="test", model="fixture", content=json.dumps(content), finish_reason="stop"
        )

    monkeypatch.setattr(OpenAIClient, "chat_completion", complete)


async def terminal(client, task_id):
    async with asyncio.timeout(15):
        while True:
            result = (await client.get(f"/api/tasks/{task_id}")).json()
            if result["status"] not in {"queued", "running"}:
                return result
            await asyncio.sleep(0.02)


def test_openapi_yaml_all_operations_local_refs_and_original_cases():
    document = """openapi: 3.0.3
paths:
  /users/{id}:
    parameters:
      - name: id
        in: path
        schema: {type: integer, example: 7}
    get:
      responses:
        200: {description: OK}
      x-test-cases:
        - name: existing
          assertions: [{kind: status, expected: 404}]
  /events:
    get:
      responses:
        '200':
          content: {text/event-stream: {schema: {type: string}}}
"""
    found = recognize(document)
    assert len(found.endpoints) == 2
    cases = NativeCaseGenerator().generate(found)
    assert len(cases) == 3
    assert cases[0].name == "existing"
    assert cases[0].assertions[0].expected == 404
    assert cases[1].path == "/users/7"
    assert cases[2].protocol == "sse"


async def test_mixed_task_executes_original_and_generated_cases_and_exports(
    client, app, target, monkeypatch
):
    await enable_model(client, monkeypatch)
    started = await client.post(
        "/api/tasks",
        json={"catalog": catalog().model_dump(), "origin": target.base_url, "allow_writes": True},
    )
    assert started.status_code == 202, started.text
    task_id = started.json()["id"]
    result = await terminal(client, task_id)
    assert result["status"] == "completed", result
    assert [entry["name"] for entry in result["results"]] == ["original", "json", "sse", "socket"]
    assert [
        (entry["name"], entry["status"], [item["passed"] for item in entry["assertions"]])
        for entry in result["results"]
    ] == [
        ("original", "passed", [True]),
        ("json", "passed", [True]),
        ("sse", "passed", [True, True]),
        ("socket", "passed", [True]),
    ]
    sse_result = next(entry for entry in result["results"] if entry["protocol"] == "sse")
    assert sse_result["evidence"]["response"]["sse"]["events"][-1]["data"] == "line one\nline two"
    exported = await client.get(f"/api/tasks/{task_id}/report")
    assert "text/markdown" in exported.headers["content-type"]
    assert "attachment" in exported.headers["content-disposition"]
    assert "original" in exported.text and "已核查全部执行结果" in exported.text
    assert (await client.get("/api/tasks")).json()["items"][0]["id"] == task_id
    assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 204
    assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404


async def test_nonstandard_document_uses_model_and_keeps_cases(client, monkeypatch):
    expected = catalog().model_dump()
    await enable_model(client, monkeypatch, expected)
    response = await client.post(
        "/api/tasks/recognize",
        json={"filename": "api.md", "text": "GET /json 返回 ok=true，测试用例应验证 /ok 为 true"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["endpoints"][0]["cases"][0]["name"] == "original"


async def test_cancel_task_closes_socket_and_preserves_history(client, app, target, monkeypatch):
    await enable_model(client, monkeypatch)
    started = await client.post(
        "/api/tasks",
        json={
            "catalog": {"endpoints": [{"path": "/ws", "protocol": "websocket"}]},
            "origin": target.base_url,
            "observation_ms": 30000,
        },
    )
    task_id = started.json()["id"]
    async with asyncio.timeout(5):
        while not app.state.streams.active:
            await asyncio.sleep(0.01)
    assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 409
    stopped = await client.post(f"/api/tasks/{task_id}/cancel")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert not app.state.streams.active
    assert (await client.get(f"/api/tasks/{task_id}/report")).status_code == 200


async def test_model_failure_does_not_discard_results(client, target, monkeypatch):
    await enable_model(client, monkeypatch)

    async def fail(self, data, budget):
        raise AgentError("model_timeout")

    monkeypatch.setattr(AgentService, "report", fail)
    response = await client.post(
        "/api/tasks",
        json={
            "catalog": {
                "endpoints": [
                    {"path": "/json", "assertions": [{"kind": "status", "expected": 200}]}
                ]
            },
            "origin": target.base_url,
        },
    )
    result = await terminal(client, response.json()["id"])
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "passed"


def test_missing_assertion_evidence_is_unknown_and_types_remain_strict():
    assert evaluate(Check(kind="status", expected=200), {})["passed"] is None
    assert (
        evaluate(
            Check(kind="json_equals", pointer="/ok", expected=1),
            {"response": {"body": '{"ok": true}'}},
        )["passed"]
        is False
    )


async def test_write_cases_skipped_by_default_and_scope_rejected(client, target, monkeypatch):
    await enable_model(client, monkeypatch)
    payload = {
        "catalog": {"endpoints": [{"path": "/echo", "method": "POST", "body": {"a": 1}}]},
        "origin": target.base_url,
    }
    response = await client.post("/api/tasks", json=payload)
    result = await terminal(client, response.json()["id"])
    assert result["results"][0]["status"] == "skipped"
    payload["origin"] = "http://example.com"
    assert (await client.post("/api/tasks", json=payload)).status_code == 400
    payload["origin"] = target.base_url
    payload["catalog"]["endpoints"][0]["path"] = "//example.com"
    assert (await client.post("/api/tasks", json=payload)).status_code == 422


async def test_standard_recognition_does_not_require_a_model(client):
    response = await client.post(
        "/api/tasks/recognize",
        json={"text": '{"openapi":"3.0.3","paths":{"/health":{"get":{"responses":{"200":{}}}}}}'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["endpoints"][0]["path"] == "/health"


def test_openapi_generator_creates_real_required_query_case():
    found = recognize("""openapi: 3.0.3
paths:
  /users:
    get:
      parameters:
        - name: page
          in: query
          required: true
          schema: {type: integer, minimum: 1, maximum: 10}
      responses: {'200': {description: ok}}
""")
    cases = NativeCaseGenerator().generate(found)
    assert [case.name for case in cases[:2]] == ["缺少必填查询参数", "page 边界值"]
    assert cases[0].path == "/users"
    assert "page=0" in cases[1].path
    assert cases[0].source == cases[1].source == "generated"


async def test_task_results_and_report_redact_body_credentials(client, target, monkeypatch):
    await enable_model(client, monkeypatch)
    response = await client.post(
        "/api/tasks",
        json={
            "origin": target.base_url,
            "allow_writes": True,
            "catalog": {
                "endpoints": [
                    {
                        "method": "POST",
                        "path": "/echo",
                        "body": {"password": "fixture-private-password"},
                    }
                ]
            },
        },
    )
    result = await terminal(client, response.json()["id"])
    assert "fixture-private-password" not in json.dumps(result)
    assert (
        "fixture-private-password"
        not in (await client.get(f"/api/tasks/{result['id']}/report")).text
    )


async def test_interrupted_inflight_case_has_explicit_unknown_verdict(
    client, app, target, monkeypatch
):
    await enable_model(client, monkeypatch)
    response = await client.post(
        "/api/tasks",
        json={"origin": target.base_url, "catalog": {"endpoints": [{"path": "/slow"}]}},
    )
    task_id = response.json()["id"]
    await asyncio.sleep(0.1)
    stopped = (await client.post(f"/api/tasks/{task_id}/cancel")).json()
    assert stopped["results"][0]["status"] == "unknown"
    assert stopped["results"][0]["reason"] == "interrupted_without_complete_evidence"
    assert task_id not in app.state.tasks.active


async def test_summary_batches_include_every_result_without_mutating_evidence():
    from api_agent.agent.schemas import ReportCommentary
    from api_agent.tasks.service import summarize_results

    results = [
        {
            "index": index,
            "name": str(index),
            "assertions": [{"expected": "x" * 3000, "actual": "y" * 3000}],
        }
        for index in range(100)
    ]
    calls = []

    class Agent:
        async def report(self, data, budget):
            calls.append(data)
            return ReportCommentary(overview="batch", recommendations=[])

    await summarize_results(Agent(), results, [])
    assert len(calls) > 2
    assert sum(len(call.get("results", [])) for call in calls) == 100
    assert calls[-1]["total_results"] == 100
    assert results[0]["assertions"][0]["actual"] == "y" * 3000
