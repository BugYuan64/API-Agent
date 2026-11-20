"""End-to-end document tasks using real local HTTP/SSE/WebSocket transports and scripted models."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from support.local_server import LocalServer, build_app

from api_agent.agent.service import AgentError, AgentService
from api_agent.llm.client import OpenAIClient
from api_agent.llm.schemas import ChatCompletionResponse
from api_agent.tasks.documents import NativeCaseGenerator, recognize
from api_agent.tasks.evidence import TaskEvidence
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
        content = response(request) if callable(response) else response
        content = content or {"overview": "已核查全部执行结果。", "recommendations": []}
        return ChatCompletionResponse(
            id="test", model="fixture", content=json.dumps(content), finish_reason="stop"
        )

    monkeypatch.setattr(OpenAIClient, "chat_completion", complete)

    async def stream_text(self, request):
        yield "## 结果概览\n"
        yield "已核查全部执行结果。"

    monkeypatch.setattr(OpenAIClient, "stream_text", stream_text)


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
    assert found.endpoints[0].path_template == "/users/{id}"
    cases = NativeCaseGenerator().generate(found)
    assert len(cases) == 3
    assert cases[0].name == "existing"
    assert cases[0].assertions[0].expected == 404
    assert cases[1].path == "/users/7"
    assert cases[2].protocol == "sse"


@pytest.mark.parametrize("sse_method", ["GET", "POST"])
async def test_mixed_task_executes_original_and_generated_cases_and_exports(
    client, app, target, monkeypatch, sse_method
):
    await enable_model(client, monkeypatch)
    input_catalog = catalog().model_dump()
    input_catalog["title"] = "本地接口文档"
    input_catalog["endpoints"][1]["method"] = sse_method
    if sse_method == "POST":
        input_catalog["endpoints"][1]["body"] = {"question": "hello"}
    started = await client.post(
        "/api/tasks",
        json={"catalog": input_catalog, "origin": target.base_url, "allow_writes": True},
    )
    assert started.status_code == 202, started.text
    task_id = started.json()["id"]
    result = await terminal(client, task_id)
    assert result["status"] == "completed", result
    assert result["snapshot"]["operations"] == [
        {"path": "/json", "example_index": 1, "case_indices": [0, 1]},
        {"path": "/sse", "example_index": 2, "case_indices": [2]},
        {"path": "/ws", "example_index": 3, "case_indices": [3]},
    ]
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
    history_item = (await client.get("/api/tasks")).json()["items"][0]
    assert history_item["id"] == task_id
    assert history_item["title"] == result["title"] == "本地接口文档"
    assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 204
    assert (await client.get(f"/api/tasks/{task_id}")).status_code == 404


async def test_rerun_reuses_complete_case_snapshot_and_creates_distinct_history(client, target):
    source = catalog().model_dump()
    source["title"] = "重测历史样例"
    source["endpoints"][1]["method"] = "POST"
    first = await client.post("/api/tasks", json={"origin": target.base_url, "catalog": source})
    assert first.status_code == 202
    original = await terminal(client, first.json()["id"])
    assert original["status"] == "completed"
    again = await client.post(
        f"/api/tasks/{original['id']}/rerun",
        json={"origin": target.base_url, "observation_ms": 2000, "allow_writes": False},
    )
    assert again.status_code == 202
    repeated = await terminal(client, again.json()["id"])
    assert original["id"] != repeated["id"]
    assert original["title"] == repeated["title"] == "重测历史样例"
    assert original["snapshot"] == repeated["snapshot"]
    assert repeated["snapshot"]["cases"] and "catalog" not in repeated["snapshot"]
    assert [case["status"] for case in repeated["results"]].count("permission_required") == 2
    history = (await client.get("/api/tasks")).json()["items"]
    assert {original["id"], repeated["id"]} <= {item["id"] for item in history}


async def test_rerun_applies_edited_cases_without_mutating_source_history(client, target):
    """Accept valid case edits, preserve transport identity, and leave source evidence intact."""
    source_catalog = {
        "endpoints": [
            {
                "name": "创建项目",
                "method": "POST",
                "path": "/echo",
                "body": {"project": "old"},
                "generated_cases": [
                    {
                        "name": "AI 创建项目",
                        "method": "POST",
                        "path": "/echo",
                        "body": {"project": "old"},
                    }
                ],
            }
        ]
    }
    created = await client.post(
        "/api/tasks",
        json={"origin": target.base_url, "allow_writes": True, "catalog": source_catalog},
    )
    original = await terminal(client, created.json()["id"])
    index = next(
        i for i, case in enumerate(original["snapshot"]["cases"]) if case["name"] == "AI 创建项目"
    )
    edited = {
        **original["snapshot"]["cases"][index],
        "name": "修改后的用例",
        "body": {"project": "new"},
    }
    repeated_response = await client.post(
        f"/api/tasks/{original['id']}/rerun",
        json={"origin": target.base_url, "allow_writes": True, "case_edits": {str(index): edited}},
    )
    assert repeated_response.status_code == 202, repeated_response.text
    repeated = await terminal(client, repeated_response.json()["id"])
    assert original["snapshot"]["cases"][index]["body"] == {"project": "old"}
    assert repeated["snapshot"]["cases"][index]["name"] == "修改后的用例"
    assert repeated["snapshot"]["cases"][index]["body"] == {"project": "new"}
    assert repeated["snapshot"].get("document") == original["snapshot"].get("document")


@pytest.mark.parametrize("edit", [{"method": "POST"}, {"protocol": "websocket"}])
async def test_rerun_rejects_edits_that_change_case_identity(client, target, edit):
    """The editor can adjust request data, but cannot change its owning transport."""
    source_catalog = {
        "endpoints": [
            {
                "method": "GET",
                "path": "/health",
                "generated_cases": [{"method": "GET", "path": "/health"}],
            }
        ]
    }
    created = await client.post(
        "/api/tasks", json={"origin": target.base_url, "catalog": source_catalog}
    )
    original = await terminal(client, created.json()["id"])
    case = original["snapshot"]["cases"][0]
    invalid = {**case, **edit}
    response = await client.post(
        f"/api/tasks/{original['id']}/rerun",
        json={"origin": target.base_url, "case_edits": {"0": invalid}},
    )
    assert response.status_code == 400


def test_operation_plan_keeps_boundary_cases_with_their_document_operation():
    found = Catalog.model_validate(
        {
            "cases": [{"path": "/standalone"}],
            "endpoints": [
                {
                    "path": "/users/7?view=full",
                    "path_template": "/users/{id}",
                    "cases": [{"path": "/users/8"}],
                    "generated_cases": [{"path": "/users/0"}],
                },
                {"path": "/users/me", "cases": [{"path": "/users/me?view=min"}]},
            ],
        }
    )
    cases, operations = NativeCaseGenerator().plan(found)
    assert [case.path for case in cases] == [
        "/standalone",
        "/users/8",
        "/users/me?view=min",
        "/users/0",
        "/users/7?view=full",
        "/users/me",
    ]
    assert [case.kind for case in cases] == [
        None,
        "document",
        "document",
        "test",
        "contract",
        "contract",
    ]
    assert operations == [
        {"path": "/users/{id}", "example_index": 4, "case_indices": [1, 3, 4]},
        {"path": "/users/me", "example_index": 5, "case_indices": [2, 5]},
    ]


async def test_nonstandard_document_uses_model_and_keeps_cases(client, monkeypatch):
    expected = catalog().model_dump()
    await enable_model(client, monkeypatch, expected)
    response = await client.post(
        "/api/tasks/recognize",
        json={"filename": "api.md", "text": "GET /json 返回 ok=true，测试用例应验证 /ok 为 true"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["endpoints"][0]["cases"][0]["name"] == "original"


async def test_import_generates_editable_cases_and_run_keeps_the_one_document(
    client, target, monkeypatch
):
    draft = {"groups": [{"cases": [{"name": "AI 示例", "path": "/echo", "body": {"q": "初稿"}}]}]}
    await enable_model(client, monkeypatch, draft)
    document = {
        "filename": "接口.json",
        "text": json.dumps(
            {"title": "回显示例", "endpoints": [{"method": "POST", "path": "/echo"}]}
        ),
    }
    prepared = await client.post("/api/tasks/recognize", json={**document, "tests_per_url": 1})
    assert prepared.status_code == 200, prepared.text
    assert (await client.get("/api/tasks")).json() == {"items": []}
    edited = prepared.json()
    edited["endpoints"][0]["generated_cases"][0]["body"] = {"q": "已编辑"}
    started = await client.post(
        "/api/tasks",
        json={
            "origin": target.base_url,
            "allow_writes": True,
            "catalog": edited,
            "document": document,
        },
    )
    assert started.status_code == 202, started.text
    record = await terminal(client, started.json()["id"])
    assert record["snapshot"]["document"] == {"filename": document["filename"]}
    assert record["snapshot"]["cases"][0]["body"] == {"q": "已编辑"}
    assert json.loads(json.loads(record["results"][0]["evidence"]["response"]["body"])["body"]) == {
        "q": "已编辑"
    }
    repeated = await client.post(
        f"/api/tasks/{record['id']}/rerun", json={"origin": target.base_url}
    )
    assert repeated.status_code == 202
    assert repeated.json()["id"] != record["id"]
    for task_id in (record["id"], repeated.json()["id"]):
        attachment = await client.get(f"/api/tasks/{task_id}/document")
        assert attachment.content == document["text"].encode()
        assert attachment.headers["content-disposition"].startswith("attachment;")


async def test_import_fills_only_missing_distinct_cases_across_rounds(client, monkeypatch):
    """Partial output and repeated requests must not reset the accepted cases or stop refill."""
    example_path = "/echo?a=1&b=2"
    example_copy = {
        "name": "同请求不同断言",
        "path": "/echo?b=2&a=1",
        "body": {"second": 2, "first": 1},
        "assertions": [{"kind": "status", "expected": 200}],
    }
    accepted = {"name": "不同请求", "path": example_path, "body": {"first": 3}}
    replacement = {"name": "补齐的请求", "path": example_path, "body": {"first": 4}}
    calls = []

    def draft(request):
        operations = json.loads(request.messages[-1].content)["operations"]
        calls.append([(item["path"], item["remaining_cases"]) for item in operations])
        if len(calls) == 1:
            return {
                "groups": [
                    {"cases": [example_copy, accepted]},
                    {
                        "cases": [
                            {"name": f"搜索{index}", "path": f"/search?q={index}"}
                            for index in (1, 2)
                        ]
                    },
                ]
            }
        assert accepted["body"] in [item["body"] for item in operations[0]["existing_inputs"]]
        if len(calls) == 2:
            return {"groups": [{"cases": [{**accepted, "name": "换名仍然重复"}]}]}
        return {"groups": [{"cases": [replacement]}]}

    await enable_model(client, monkeypatch, draft)
    document = {
        "title": "去重",
        "cases": [{"name": "独立文档用例", "method": "GET", "path": "/search?q=original"}],
        "endpoints": [
            {"method": "POST", "path": example_path, "body": {"first": 1, "second": 2}},
            {"method": "GET", "path": "/search?q=initial"},
        ],
    }
    response = await client.post(
        "/api/tasks/recognize",
        json={
            "filename": "contract.json",
            "text": json.dumps(document),
            "tests_per_url": 2,
        },
    )
    assert response.status_code == 200, response.text
    catalog = response.json()
    assert calls == [
        [(example_path, 2), ("/search?q=initial", 2)],
        [(example_path, 1)],
        [(example_path, 1)],
    ]
    assert [
        [case["name"] for case in item["generated_cases"]] for item in catalog["endpoints"]
    ] == [
        ["不同请求", "补齐的请求"],
        ["搜索1", "搜索2"],
    ]
    assert catalog["warnings"] == []
    assert catalog["cases"] == []
    assert [item["name"] for item in catalog["endpoints"][1]["cases"]] == ["独立文档用例"]
    cases, _ = NativeCaseGenerator().plan(Catalog.model_validate(catalog))
    assert sum(case.path == "/search?q=original" for case in cases) == 1
    assert sum(case.kind == "test" for case in cases) == 4
    assert (await client.get("/api/tasks")).json() == {"items": []}


async def test_import_stops_at_round_limit_without_padding_a_parameterless_operation(
    client, monkeypatch
):
    """Return a real shortfall for a contract with no variable inputs."""
    calls = []

    def draft(request):
        operations = json.loads(request.messages[-1].content)["operations"]
        calls.append(operations[0]["remaining_cases"])
        return {"groups": [{"cases": [{"name": "相同请求", "path": "/health"}]}]}

    await enable_model(client, monkeypatch, draft)
    response = await client.post(
        "/api/tasks/recognize",
        json={
            "filename": "contract.json",
            "text": json.dumps({"title": "健康检查", "endpoints": [{"path": "/health"}]}),
            "tests_per_url": 2,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "test_quota_unmet"
    assert calls == [2] * 6


async def test_refills_a_path_parameter_and_regenerates_only_selected_operations(
    client, monkeypatch
):
    """A duplicate-only model cannot leave a numeric route under quota."""
    calls = []

    def draft(request):
        operations = json.loads(request.messages[-1].content)["operations"]
        calls.extend(item["path"] for item in operations)
        return {
            "groups": [
                {"cases": [{"name": "duplicate", "path": item["path"]}]} for item in operations
            ]
        }

    await enable_model(client, monkeypatch, draft)
    previous = {
        "title": "Two routes",
        "endpoints": [
            {
                "path": "/keep",
                "generated_cases": [
                    {"path": "/keep?q=1", "name": "hand edited", "source": "generated"}
                ],
            }
        ],
    }
    text = json.dumps(
        {
            "title": "Two routes",
            "endpoints": [
                {"path": "/keep"},
                {"method": "DELETE", "path": "/items/1", "path_template": "/items/{id}"},
            ],
        }
    )
    response = await client.post(
        "/api/tasks/recognize",
        json={
            "text": text,
            "previous": previous,
            "tests_per_url": 3,
        },
    )
    assert response.status_code == 200, response.text
    catalog = response.json()
    assert catalog["endpoints"][0]["generated_cases"][0]["name"] == "hand edited"
    generated = catalog["endpoints"][1]["generated_cases"]
    assert {item["path"] for item in generated} == {"/items/2", "/items/3", "/items/4"}
    assert all(not item["assertions"] for item in generated)
    assert set(calls) == {"/items/1"}
    repeated = await client.post(
        "/api/tasks/regenerate",
        json={
            "catalog": catalog,
            "selected": [1],
            "tests_per_url": 2,
            "document": {"text": text},
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["endpoints"][0] == catalog["endpoints"][0]
    assert len(repeated.json()["endpoints"][1]["generated_cases"]) == 2


async def test_review_chat_streams_after_completion_and_uses_selected_model(
    client, app, monkeypatch
):
    await enable_model(client, monkeypatch)
    seen = []

    async def stream(self, payload):
        seen.append(payload.model)
        if payload.model:
            assert "测试证据" in payload.messages[1].content
        yield "第一段\n"
        yield "第二段"

    monkeypatch.setattr(OpenAIClient, "stream_text", stream)
    record = await client.post(
        "/api/tasks",
        json={
            "catalog": {"endpoints": [{"method": "DELETE", "path": "/items/1"}]},
            "origin": "http://127.0.0.1:9100",
        },
    )
    assert record.status_code == 202, record.text
    task_id = record.json()["id"]
    await terminal(client, task_id)
    response = await client.post(
        f"/api/tasks/{task_id}/chat",
        json={
            "model": "selected",
            "messages": [{"role": "user", "content": "发生了什么？"}],
        },
    )
    assert response.status_code == 200, response.text
    frames = [json.loads(line) for line in response.text.splitlines()]
    assert frames == [
        {"type": "text", "text": "第一段"},
        {"type": "done", "text": "第一段\n第二段"},
    ]
    assert seen[-1] == "selected"


async def test_live_case_and_summary_are_delivered_before_completion(client, app):
    allow_event = asyncio.Event()
    allow_summary = asyncio.Event()
    model_and_target = FastAPI()

    @model_and_target.get("/events")
    async def events():
        async def chunks():
            yield "data: first\n\n"
            await allow_event.wait()
            yield "data: last\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    @model_and_target.post("/v1/chat/completions")
    async def model(request: Request):
        payload = await request.json()
        assert payload["stream"] is True
        question = payload["messages"][-1]["content"]

        async def chunks():
            if question == "你好":
                assert payload["model"] == "fixture"
                yield 'data: {"choices":[{"delta":{"content":"你好！\\n"}}]}\n\n'
                yield "data: [DONE]\n\n"
                return
            yield 'data: {"choices":[{"delta":{"content":"## 概览\\n"}}]}\n\n'
            await allow_summary.wait()
            yield 'data: {"choices":[{"delta":{"content":"- 已完成"}}]}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    target = await LocalServer(model_and_target).start()
    management = await LocalServer(app).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": target.base_url + "/v1", "model": "fixture"},
        )
        question = {"messages": [{"role": "user", "content": "你好"}]}
        chat = await client.post("/api/tasks/chat", json=question)
        assert chat.status_code == 200, chat.text
        assert json.loads(chat.text.splitlines()[-1]) == {"type": "done", "text": "你好！"}
        started = await client.post(
            "/api/tasks",
            json={
                "origin": target.base_url,
                "observation_ms": 5000,
                "catalog": {"endpoints": [{"protocol": "sse", "path": "/events"}]},
            },
        )
        assert started.status_code == 202, started.text
        tasks = f"/api/tasks/{started.json()['id']}"
        async with httpx.AsyncClient(base_url=management.base_url, timeout=5) as browser:
            async with browser.stream("GET", tasks + "/live") as response:
                assert response.headers["content-type"].startswith("application/x-ndjson")
                async for line in response.aiter_lines():
                    frame = json.loads(line)
                    if frame.get("events_seen"):
                        assert frame["active_stream"] == "sse"
                        assert frame["results"] == [] and frame["events"][0]["data"] == "first"
                        allow_event.set()
                    if frame["stage"] == "summarizing" and frame["summary"]:
                        assert frame["summary"] == "## 概览"
                        chat = await client.post(
                            "/api/tasks/chat", json={**question, "task_id": started.json()["id"]}
                        )
                        assert chat.status_code == 200, chat.text
                        assert json.loads(chat.text.splitlines()[-1])["text"] == "你好！"
                        allow_summary.set()
                    if frame["status"] == "completed":
                        assert frame["summary"] == "## 概览\n- 已完成"
                        break
                else:
                    pytest.fail("Stream ended before the completed frame")
        assert allow_event.is_set() and allow_summary.is_set()
        saved = (await client.get(tasks)).json()
        assert saved["summary"] == "## 概览\n- 已完成"
        events = saved["results"][0]["evidence"]["response"]["sse"]["events"]
        assert [event["data"] for event in events] == ["first", "last"]
    finally:
        allow_event.set()
        allow_summary.set()
        await management.stop()
        await target.stop()


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
    progress = (await client.get(f"/api/tasks/{task_id}")).json()
    assert progress["active_stream"] == "websocket" and progress["results"] == []
    assert (await client.delete(f"/api/tasks/{task_id}")).status_code == 409
    stopped = await client.post(f"/api/tasks/{task_id}/cancel")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    assert "active_stream" not in stopped.json()
    assert not app.state.streams.active
    assert (await client.get(f"/api/tasks/{task_id}/report")).status_code == 200


async def test_task_detects_sse_response_for_an_ordinary_http_case(client, target, monkeypatch):
    await enable_model(client, monkeypatch)
    started = await client.post(
        "/api/tasks",
        json={
            "catalog": {
                "endpoints": [
                    {
                        "path": "/sse-forever",
                        "protocol": "http",
                        "assertions": [{"kind": "event_count", "expected": 1}],
                    }
                ]
            },
            "origin": target.base_url,
            "observation_ms": 2000,
        },
    )
    assert started.status_code == 202, started.text
    task_id = started.json()["id"]
    async with asyncio.timeout(5):
        while True:
            progress = (await client.get(f"/api/tasks/{task_id}")).json()
            if progress.get("active_stream") == "sse" or progress["stage"] == "finished":
                break
            await asyncio.sleep(0.01)
    assert progress["active_stream"] == "sse"
    assert progress["results"] == []
    result = await terminal(client, task_id)
    assert "active_stream" not in result
    assert result["results"][0]["status"] == "passed"


async def test_model_failure_does_not_discard_results(client, target, monkeypatch):
    await enable_model(client, monkeypatch)

    async def fail(self, data, publish):
        raise AgentError("model_timeout")

    monkeypatch.setattr(AgentService, "stream_task_summary", fail)
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


def test_task_evidence_redaction_and_compaction_keep_bounded_transport_evidence():
    value = json.dumps({"api_key": "fixture-private-password", "message": "ok"})
    assert "fixture-private-password" not in TaskEvidence.redact_body(value)
    assert TaskEvidence.redact_body("token sk-abcdefghijklmnop") != "token sk-abcdefghijklmnop"
    evidence = {"events": [{"data": "x" * 1001} for _ in range(41)]}
    compacted = TaskEvidence.compact(evidence)
    assert len(compacted["events"]) == 40
    assert compacted["events_truncated"] is True
    assert compacted["events"][0]["truncated"] is True


async def test_write_cases_skipped_by_default_and_scope_rejected(client, target, monkeypatch):
    await enable_model(client, monkeypatch)
    payload = {
        "catalog": {"endpoints": [{"path": "/echo", "method": "POST", "body": {"a": 1}}]},
        "origin": target.base_url,
    }
    response = await client.post("/api/tasks", json=payload)
    result = await terminal(client, response.json()["id"])
    assert result["results"][0]["status"] == "permission_required"
    assert result["results"][0]["reason"] == "write_permission_required"
    assert "evidence" not in result["results"][0]
    payload["origin"] = "http://example.com"
    assert (await client.post("/api/tasks", json=payload)).status_code == 400
    payload["origin"] = target.base_url
    payload["catalog"]["endpoints"][0]["path"] = "//example.com"
    assert (await client.post("/api/tasks", json=payload)).status_code == 422


async def test_task_appends_case_path_to_target_base(client, monkeypatch):
    await enable_model(client, monkeypatch)
    upstream = FastAPI()
    upstream.mount("/api/v2", build_app())
    server = await LocalServer(upstream).start()
    try:
        base_url = server.base_url + "/api/v2"
        started = await client.post(
            "/api/tasks",
            json={
                "origin": base_url + "/",
                "catalog": {
                    "endpoints": [
                        {"path": "/json", "assertions": [{"kind": "status", "expected": 200}]}
                    ]
                },
            },
        )
        assert started.status_code == 202, started.text
        record = await terminal(client, started.json()["id"])
        assert record["snapshot"]["origin"] == base_url
        assert record["results"][0]["status"] == "passed"
        assert record["results"][0]["evidence"]["request"]["url"] == base_url + "/json"
    finally:
        await server.stop()


async def test_task_history_recovers_id_masked_by_legacy_privacy_filter(client, app):
    import hashlib

    store = app.state.tasks.store
    run_id = "7b3c4aa4-e446-426f-9094-d13812345678"
    snapshot = {"cases": []}
    record = {
        "id": run_id,
        "status": "completed",
        "stage": "finished",
        "snapshot": snapshot,
        "snapshot_sha256": hashlib.sha256(
            json.dumps(snapshot, sort_keys=True).encode()
        ).hexdigest(),
        "results": [],
    }
    store.save(record)
    path = store.path(run_id)
    assert json.loads(path.read_text(encoding="utf-8"))["id"] == run_id
    path.write_text(
        json.dumps({**record, "id": "7b3c4aa4-e446-426f-9094-d[PHONE]"}), encoding="utf-8"
    )
    listed = await client.get("/api/tasks")
    assert listed.json()["items"][0]["id"] == run_id
    detail = await client.get(f"/api/tasks/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == run_id


async def test_standard_recognition_does_not_require_a_model(client):
    response = await client.post(
        "/api/tasks/recognize",
        json={"text": '{"openapi":"3.0.3","paths":{"/health":{"get":{"responses":{"200":{}}}}}}'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["endpoints"][0]["path"] == "/health"


def test_document_server_hint_preserves_base_path_and_resolves_defaults():
    found = recognize("""openapi: 3.0.3
info: {title: 测试接口}
servers:
  - url: /relative
  - url: 'http://127.0.0.1:{port}/api/v2'
    variables: {port: {default: '9100'}}
paths:
  /health:
    get:
      responses: {'200': {}}
""")
    assert found.title == "测试接口"
    assert found.server_url == "http://127.0.0.1:9100/api/v2"
    assert found.endpoints[0].path == "/health"


def test_exported_websocket_operations_are_kept_in_the_case_snapshot():
    found = recognize("""openapi: 3.1.0
info: {title: 双协议 API}
paths: {/health: {get: {responses: {'200': {}}}}}
x-websocket-operations:
  - path: /channel/{id}
    summary: 订阅频道
    x-messages: [subscribe]
""")
    cases, operations = NativeCaseGenerator().plan(found)
    assert [(case.protocol, case.path) for case in cases] == [
        ("http", "/health"),
        ("websocket", "/channel/1"),
    ]
    assert cases[1].messages == ["subscribe"]
    assert operations[1]["path"] == "/channel/{id}"


async def test_titleless_standard_document_uses_model_title(client, monkeypatch):
    await enable_model(client, monkeypatch, {"title": "健康检查接口"})
    response = await client.post(
        "/api/tasks/recognize",
        json={"text": "openapi: 3.0.3\npaths: {/health: {get: {responses: {'200': {}}}}}"},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "健康检查接口"


async def test_chat_history_title_uses_model_summary_and_falls_back_to_question(client, monkeypatch):
    await enable_model(client, monkeypatch, {"title": "排查 SSE 响应分块"})
    response = await client.post("/api/tasks/chat/title", json={"question": "为什么这个 SSE 请求分块返回？"})
    assert response.status_code == 200
    assert response.json() == {"title": "排查 SSE 响应分块"}

    async def unavailable(*_args, **_kwargs):
        from api_agent.llm.errors import LLMError

        raise LLMError("fixture_unavailable")

    monkeypatch.setattr(OpenAIClient, "chat_completion", unavailable)
    fallback = await client.post("/api/tasks/chat/title", json={"question": "首轮对话标题回退"})
    assert fallback.status_code == 200
    assert fallback.json() == {"title": "首轮对话标题回退"}


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
