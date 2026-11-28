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


async def enable_model(client, monkeypatch, response=None, *, generation_model=None):
    await client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "base_url": "http://127.0.0.1:9101/v1",
            "model": "fixture",
            "generation_model": generation_model,
        },
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
    assert history_item["origin"] == target.base_url
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

    await enable_model(client, monkeypatch, draft, generation_model="generator-fixture")
    original_complete = OpenAIClient.chat_completion

    async def complete(self, request):
        assert self.model == "generator-fixture"
        return await original_complete(self, request)

    monkeypatch.setattr(OpenAIClient, "chat_completion", complete)
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
    await enable_model(client, monkeypatch, generation_model="generator-fixture")
    seen = []

    async def stream(self, payload):
        assert self.model == "fixture"
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


async def test_write_cases_skipped_by_default_and_scope_rejected(client, app, target, monkeypatch):
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
    app.state.settings.target_allowed_origins = [target.base_url]
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


async def test_chat_history_title_uses_model_summary_and_falls_back_to_question(
    client, monkeypatch
):
    await enable_model(client, monkeypatch, {"title": "排查 SSE 响应分块"})
    response = await client.post(
        "/api/tasks/chat/title", json={"question": "为什么这个 SSE 请求分块返回？"}
    )
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


async def test_recognize_skips_parameterless_openapi_operation_without_quota_error(
    client, monkeypatch
):
    """OpenAPI 无可变入参的只读接口（如 GET /health）跳过重复 test 生成且保留文档用例。"""
    seen_paths = []

    def draft(request):
        operations = json.loads(request.messages[-1].content)["operations"]
        seen_paths.extend(item["path"] for item in operations)
        return {
            "groups": [
                {
                    "cases": [
                        {
                            "name": "有效提问",
                            "path": "/ask",
                            "body": {"question": "CRM 有哪些模块？"},
                        },
                        {
                            "name": "空问题边界",
                            "path": "/ask",
                            "body": {"question": ""},
                        },
                    ]
                }
            ]
        }

    await enable_model(client, monkeypatch, draft)
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "CRM RAG", "version": "1.0.0"},
        "paths": {
            "/health": {
                "get": {
                    "summary": "健康检查",
                    "responses": {"200": {"description": "ok"}},
                    "x-test-cases": [
                        {
                            "name": "健康检查-不支持方法",
                            "method": "POST",
                            "path": "/health",
                            "assertions": [{"kind": "status", "expected": 405}],
                        }
                    ],
                }
            },
            "/ask": {
                "post": {
                    "summary": "非流式问答",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["question"],
                                    "properties": {"question": {"type": "string"}},
                                },
                                "example": {"question": "你好"},
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }
    response = await client.post(
        "/api/tasks/recognize",
        json={
            "filename": "CRM-RAG.openapi.json",
            "text": json.dumps(spec),
            "tests_per_url": 2,
        },
    )
    assert response.status_code == 200, response.text
    catalog = response.json()
    assert seen_paths == ["/ask"]
    health = next(item for item in catalog["endpoints"] if item["path"] == "/health")
    ask = next(item for item in catalog["endpoints"] if item["path"] == "/ask")
    assert health["generated_cases"] == []
    assert [case["name"] for case in health["cases"]] == ["健康检查-不支持方法"]
    assert len(ask["generated_cases"]) == 2
    assert any("GET /health 无可变入参" in warning for warning in catalog["warnings"])


def test_openapi_websocket_101_response_recognized_without_status_warning():
    """OpenAPI WebSocket 端点声明 101 响应时自动生成 status=101 断言且不报未声明成功状态。"""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "CRM WS", "version": "1.0.0"},
        "paths": {
            "/api/ws/chat": {
                "get": {
                    "summary": "实时双向对话",
                    "x-protocol": "websocket",
                    "x-messages": ['{"type":"ping"}'],
                    "responses": {
                        "101": {"description": "Switching Protocols"},
                        "401": {"description": "Unauthorized"},
                    },
                }
            }
        },
    }
    found = recognize(json.dumps(spec))
    assert found.warnings == []
    assert len(found.endpoints) == 1
    endpoint = found.endpoints[0]
    assert endpoint.protocol == "websocket"
    assert endpoint.messages == ['{"type":"ping"}']
    assert [item.model_dump() for item in endpoint.assertions] == [
        {"kind": "status", "expected": 101, "pointer": ""}
    ]


async def test_sse_executor_aligns_timeout_with_observation_window(app, monkeypatch):
    """SSE 执行超时不短于 observation_ms + 5s（上限 60s），避免慢首包在观察期内提前超时。"""
    from api_agent.tasks.executor import CaseExecutor
    from api_agent.tasks.schemas import Case, TaskRunOptions

    seen_timeouts = []

    class FakeResult:
        def model_dump(self, mode="json"):
            return {
                "ok": True,
                "response": {
                    "status": 200,
                    "headers": {"content-type": "text/event-stream"},
                    "body": "data: ok\n\n",
                    "sse": {
                        "events": [{"event": "message", "data": "ok"}],
                        "stop_reason": "eof",
                    },
                },
            }

    async def fake_execute(self, payload, **_kwargs):
        seen_timeouts.append(payload.timeout_ms)
        return FakeResult()

    executor = CaseExecutor(app.state.settings, app.state.streams)
    monkeypatch.setattr(executor.http, "execute", fake_execute.__get__(executor.http))
    case = Case(
        name="SSE流式", protocol="sse", method="GET", path="/api/chat/ask/stream?question=hi"
    )

    assert TaskRunOptions(origin="http://127.0.0.1:8088").timeout_ms == 30_000
    await executor.execute(
        case,
        TaskRunOptions(origin="http://127.0.0.1:8088", timeout_ms=10_000, observation_ms=25_000),
    )
    await executor.execute(
        case,
        TaskRunOptions(origin="http://127.0.0.1:8088", timeout_ms=10_000, observation_ms=30_000),
    )
    await executor.execute(
        case,
        TaskRunOptions(origin="http://127.0.0.1:8088", timeout_ms=50_000, observation_ms=10_000),
    )
    assert seen_timeouts == [30_000, 35_000, 50_000]


def test_redact_body_preserves_compact_json_wire_format():
    """脱敏 JSON 帧时保留原始紧凑格式，避免 WebSocket/SSE 的 message_contains 断言误判。"""
    from api_agent.tasks.evidence import TaskEvidence

    raw = '{"type":"welcome","room":"lobby"}'
    assert TaskEvidence.redact_body(raw) == raw
    with_secret = '{"type":"welcome","email":"a@example.com","token":"secret-token-value"}'
    redacted = TaskEvidence.redact_body(with_secret)
    assert '"type":"welcome"' in redacted
    assert "secret-token-value" not in redacted
    assert "a@example.com" not in redacted
    nested = '{"type":"chat","format":"json","content":"{\\"customKey\\":\\"ok\\"}"}'
    assert TaskEvidence.redact_body(nested) == nested


def test_matches_path_ignores_query_suffix_in_llm_path_template():
    """模型识别非标准文档误在 path_template 携带查询串时，仍能正确匹配生成的查询变体。"""
    from api_agent.tasks.generation import DraftInput, bind_case, matches_path, operation_key
    from api_agent.tasks.schemas import Endpoint

    ep = Endpoint(
        name="统计接口",
        method="GET",
        path="/stats?scope=day",
        path_template="/stats?scope={scope}",
    )
    assert operation_key(ep) == ("http", "GET", "/stats")
    assert matches_path(ep, "/stats?scope=week") is True
    case = bind_case(ep, DraftInput(name="按周统计", path="/stats?scope=week"))
    assert case.path == "/stats?scope=week"


def test_draft_input_headers_merge_safely_and_preserve_extract_rules():
    """DraftInput 支持可选 headers 与 extract，合并时不覆盖端点的敏感鉴权头。"""
    from pydantic import ValidationError

    from api_agent.tasks.generation import DraftInput, bind_case
    from api_agent.tasks.schemas import Endpoint, ExtractRule

    endpoint = Endpoint(
        name="受保护查询",
        method="GET",
        path="/items/1",
        path_template="/items/{id}",
        headers={"Authorization": "Bearer real-secret", "X-Tenant": "base"},
    )
    draft = DraftInput(
        name="边界查询",
        path="/items/2",
        headers={
            "authorization": "Bearer spoofed",
            "x-tenant": "override",
            "X-Test-Mode": "boundary",
        },
        extract=[ExtractRule(name="item_id", source="json", pointer="/id")],
    )
    bound = bind_case(endpoint, draft)
    assert bound.headers["Authorization"] == "Bearer real-secret"
    assert "authorization" not in bound.headers
    assert bound.headers["x-tenant"] == "override"
    assert "X-Tenant" not in bound.headers
    assert bound.headers["X-Test-Mode"] == "boundary"
    assert len(bound.extract) == 1
    assert bound.extract[0].name == "item_id"

    with pytest.raises(ValidationError):
        DraftInput(name="非法保留头", path="/items/3", headers={"Host": "evil.example"})


async def test_task_planning_service_recognize_regenerate_and_build_ai_catalog(app):
    """TaskPlanningService 支持 recognize_and_draft、regenerate_selected 与 build_ai_catalog。"""
    from api_agent.tasks.generation import DraftBatch
    from api_agent.tasks.planning import (
        GeneratedTitle,
        TaskPlanningService,
        fill_catalog_title,
    )
    from api_agent.tasks.schemas import DocumentInput, RecognizeDocument, RegenerateTests

    seen_calls: list[tuple[str, dict]] = []

    class ScriptedClient:
        model = "planner-fixture"
        api_key = None

        async def chat_completion(self, request):
            payload = json.loads(request.messages[-1].content)
            system = request.messages[0].content
            if "task_title" in system or "不加 task: 前缀" in system:
                seen_calls.append(("task_title", payload))
                content = {"title": "AI 规划服务"}
            elif "operations" in payload:
                seen_calls.append(("task_cases", payload))
                groups = []
                for op in payload["operations"]:
                    base_path = op["path"].split("?", 1)[0]
                    remaining = op["remaining_cases"]
                    cases = [
                        {
                            "name": f"边界用例-{idx}",
                            "path": f"{base_path}?case={idx}&seed={len(seen_calls)}",
                        }
                        for idx in range(1, remaining + 1)
                    ]
                    groups.append({"cases": cases})
                content = {"groups": groups}
            else:
                seen_calls.append(("recognize", payload))
                content = {
                    "title": "非标准接口文档",
                    "server_url": "http://hallucinated.invalid",
                    "endpoints": [
                        {
                            "name": "查询用户",
                            "method": "GET",
                            "path": "/users/1",
                            "path_template": "/users/{id}",
                        }
                    ],
                }
            return ChatCompletionResponse(
                id="planner-call",
                model=self.model,
                content=json.dumps(content, ensure_ascii=False),
                finish_reason="stop",
            )

    agent = AgentService(app.state.settings, ScriptedClient())
    planner = TaskPlanningService()

    # 1. fill_catalog_title 无需 Request/Session，无模型时平滑回退
    titleless = Catalog.model_validate({"endpoints": [{"name": "兜底标题", "path": "/ping"}]})
    await fill_catalog_title(None, titleless)
    assert titleless.title == "兜底标题"

    titleless_ai = Catalog.model_validate({"endpoints": [{"name": "默认名", "path": "/ping"}]})
    await fill_catalog_title(agent, titleless_ai)
    assert titleless_ai.title == "AI 规划服务"

    # 2. recognize_and_draft：非标准文本走 LLM、过滤幻觉 server_url，并透传截断后的 instructions
    long_instructions = "重点覆盖空值与极值边界。" + ("A" * 2500)
    recognized = await planner.recognize_and_draft(
        agent,
        RecognizeDocument(filename="notes.md", text="GET /users/{id} 查询用户", tests_per_url=2),
        instructions=long_instructions,
    )
    assert recognized.server_url is None
    assert len(recognized.endpoints) == 1
    assert len(recognized.endpoints[0].generated_cases) == 2
    last_case_payload = [payload for stage, payload in seen_calls if stage == "task_cases"][-1]
    assert last_case_payload["instructions"] == long_instructions[:2000]
    assert len(last_case_payload["instructions"]) == 2000

    # 3. regenerate_selected：仅重生成选中接口并透传 instructions
    two_ep_doc = json.dumps(
        {
            "title": "双接口",
            "endpoints": [
                {"name": "保留接口", "method": "GET", "path": "/keep?v=0"},
                {"name": "重测接口", "method": "GET", "path": "/items?page=1"},
            ],
        }
    )
    base = await planner.recognize_and_draft(
        agent,
        RecognizeDocument(filename="two.json", text=two_ep_doc, tests_per_url=1),
    )
    regenerated = await planner.regenerate_selected(
        agent,
        RegenerateTests(
            catalog=base,
            document=DocumentInput(filename="two.json", text=two_ep_doc),
            tests_per_url=2,
            selected=[1],
        ),
        instructions="针对分页接口构造边界参数",
    )
    assert regenerated.endpoints[0] == base.endpoints[0]
    assert len(regenerated.endpoints[1].generated_cases) == 2
    regen_payload = [payload for stage, payload in seen_calls if stage == "task_cases"][-1]
    assert regen_payload["instructions"] == "针对分页接口构造边界参数"

    # 4. build_ai_catalog：基于 base_catalog 精炼，严格遵守 tests_per_url（并夹紧 1..10）
    refined = await planner.build_ai_catalog(
        agent,
        origin="http://127.0.0.1:8088",
        prompt_or_doc="强化异常查询参数覆盖",
        tests_per_url=3,
        base_catalog=base,
        instructions="优先构造负向查询边界",
    )
    assert refined.server_url == "http://127.0.0.1:8088"
    assert all(len(ep.generated_cases) == 3 for ep in refined.endpoints)
    refine_payload = [payload for stage, payload in seen_calls if stage == "task_cases"][-1]
    assert "优先构造负向查询边界" in refine_payload["instructions"]
    assert "强化异常查询参数覆盖" in refine_payload["instructions"]

    # 5. build_ai_catalog：无 base_catalog 时直接从自然语言构建并回填安全 origin 提示
    built = await planner.build_ai_catalog(
        agent,
        origin="http://127.0.0.1:9000",
        prompt_or_doc="请测试用户查询接口 GET /users/{id}",
        tests_per_url=0,  # clamped to 1
        instructions="极简冒烟测试",
    )
    assert built.server_url == "http://127.0.0.1:9000"
    assert len(built.endpoints[0].generated_cases) == 1
    assert isinstance(GeneratedTitle(title="ok"), GeneratedTitle)
    assert isinstance(DraftBatch(groups=[]), DraftBatch)


async def test_task_manager_multi_round_cooldown_ai_badge_and_crud(app, target):
    """TaskManager 支持多轮冷却、AI 徽章持久化、wait_for_completion 进度回调与任务管理方法。"""
    from api_agent.tasks.schemas import StartTask, TaskRunMetadata
    from api_agent.tasks.service import report

    manager = app.state.tasks
    agent = AgentService(app.state.settings, None)
    raw_origin = target.base_url + "/"
    start_req = StartTask(
        origin=raw_origin,
        allow_writes=True,
        rounds=2,
        cooldown_ms=150,
        reset_vars_per_round=True,
        catalog=Catalog.model_validate(
            {
                "title": "多轮 AI 群测",
                "endpoints": [
                    {
                        "name": "获取状态",
                        "method": "GET",
                        "path": "/json",
                        "assertions": [{"kind": "status", "expected": 200}],
                    },
                    {
                        "name": "回显提交",
                        "method": "POST",
                        "path": "/echo",
                        "body": {"hello": "world"},
                        "assertions": [{"kind": "status", "expected": 200}],
                    },
                ],
            }
        ),
    )

    started = manager.start(
        start_req,
        agent,
        metadata=TaskRunMetadata(source="ai", badge="AI 测试", tests_per_url=2),
    )
    # _start 不得原地修改入参 request.origin
    assert start_req.origin == raw_origin
    task_id = started["id"]

    frames: list[dict] = []

    async def on_progress(frame: dict) -> None:
        frames.append(frame)

    final = await manager.wait_for_completion(task_id, on_progress=on_progress)
    assert final["status"] == "completed"
    assert final["stage"] == "finished"
    assert final["source"] == "ai"
    assert final["badge"] == "AI 测试"
    assert final["total_rounds"] == 2
    assert final["current_round"] == 2
    assert final["cooldown_ms"] == 150
    assert final["cooldown_remaining_ms"] is None
    assert final["tests_per_url"] == 2

    # snapshot 也持久化了 AI 来源、徽章与多轮配置
    assert final["snapshot"]["source"] == "ai"
    assert final["snapshot"]["badge"] == "AI 测试"
    assert final["snapshot"]["total_rounds"] == 2
    assert final["snapshot"]["cooldown_ms"] == 150
    assert final["snapshot"]["tests_per_url"] == 2

    # 观测到 cooling_down 阶段帧
    assert any(
        frame.get("stage") == "cooling_down" and (frame.get("cooldown_remaining_ms") or 0) > 0
        for frame in frames
    ), frames

    # 2 条用例 × 2 轮 = 4 条执行记录，具备唯一 case_id / round / case_index / execution_index
    results = final["results"]
    assert len(results) == 4
    assert [item["case_id"] for item in results] == ["r1:c0", "r1:c1", "r2:c0", "r2:c1"]
    assert [item["round"] for item in results] == [1, 1, 2, 2]
    assert [item["case_index"] for item in results] == [0, 1, 0, 1]
    assert [item["execution_index"] for item in results] == [0, 1, 2, 3]
    assert [item["index"] for item in results] == [0, 1, 2, 3]
    assert all(item["status"] == "passed" for item in results)

    # 报告包含 AI 来源与轮次摘要且保留原标题
    markdown = report(final)
    assert markdown.startswith("# API 文档测试总结")
    assert "AI 测试" in markdown
    assert "共 2 轮" in markdown
    assert "[第 1 轮]" in markdown and "[第 2 轮]" in markdown

    # TaskManager 封装的 list_tasks / get_task / delete_task
    listed = manager.list_tasks(limit=10)
    entry = next(item for item in listed if item["id"] == task_id)
    assert entry["source"] == "ai"
    assert entry["badge"] == "AI 测试"
    assert entry["origin"] == target.base_url
    assert entry["total_rounds"] == 2
    assert entry["cooldown_ms"] == 150

    assert manager.get_task(task_id)["id"] == task_id
    with pytest.raises(ValueError):
        manager.get_task("not-a-valid-uuid")

    manager.delete_task(task_id)
    with pytest.raises(FileNotFoundError):
        manager.get_task(task_id)


async def test_task_manager_cancel_during_cooldown_and_thread_cleanup(client, app, target):
    """冷却期可立即取消且保留已完成轮次；运行中拒绝删除；删除终态任务同步清理助手线程。"""
    import time

    from api_agent.tasks.schemas import StartTask
    from api_agent.tasks.service import TaskActiveError

    manager = app.state.tasks
    agent = AgentService(app.state.settings, None)
    start_req = StartTask(
        origin=target.base_url,
        rounds=2,
        cooldown_ms=5000,
        source="ai",
        catalog=Catalog.model_validate(
            {
                "title": "冷却取消验证",
                "endpoints": [
                    {
                        "name": "单步查询",
                        "method": "GET",
                        "path": "/json",
                        "assertions": [{"kind": "status", "expected": 200}],
                    }
                ],
            }
        ),
    )
    started = manager.start(start_req, agent)
    task_id = started["id"]
    assert started["source"] == "ai"
    assert started["badge"] == "AI 测试"

    entered_cooldown = asyncio.Event()

    def watch(frame: dict) -> None:
        if frame.get("stage") == "cooling_down":
            entered_cooldown.set()

    watcher = asyncio.create_task(manager.wait_for_completion(task_id, on_progress=watch))
    try:
        async with asyncio.timeout(5):
            await entered_cooldown.wait()

        # 运行/冷却期间删除必须抛出 TaskActiveError
        with pytest.raises(TaskActiveError):
            manager.delete_task(task_id)

        t0 = time.monotonic()
        cancelled = await manager.cancel(task_id)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.5
        assert cancelled["status"] == "cancelled"
        assert cancelled["stage"] == "finished"
        assert cancelled["error_kind"] == "cancelled"
        assert cancelled["cooldown_remaining_ms"] is None
        # 第 1 轮已完成且冷却期间不应伪造第 2 轮 unknown 用例
        assert len(cancelled["results"]) == 1
        assert cancelled["results"][0]["case_id"] == "r1:c0"
        assert cancelled["results"][0]["status"] == "passed"

        waited = await asyncio.wait_for(watcher, timeout=3)
        assert waited["status"] == "cancelled"
    finally:
        if not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    # 路由层删除终态任务时若存在 assistant_threads 应同步清理对应线程
    deleted_threads: list[tuple[str, str]] = []

    class FakeThreads:
        async def delete_thread(self, scope: str, history_id: str) -> bool:
            deleted_threads.append((scope, history_id))
            return True

    app.state.assistant_threads = FakeThreads()
    try:
        resp = await client.delete(f"/api/tasks/{task_id}")
        assert resp.status_code == 204
        assert deleted_threads == [("tasks", task_id)]
    finally:
        delattr(app.state, "assistant_threads")


async def test_project_document_generate_openapi_for_directory_decoupled(app, tmp_path):
    """generate_openapi_for_directory 脱离 Request 和弹窗独立生成并保存 OpenAPI 文档。"""
    from api_agent.projects.document_schema import DocumentEndpoint
    from api_agent.tasks.project_document import generate_openapi_for_directory

    project = tmp_path / "demo_proj"
    project.mkdir()
    (project / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/ping')\ndef ping(): return {}\n",
        encoding="utf-8",
    )

    class FakeDocuments:
        async def analyze(self, name, snapshot, agent):
            assert name == "demo_proj"
            assert snapshot["scanned_files"] >= 1
            return {
                "status": "completed",
                "project_name": name,
                "processed_files": 1,
                "total_files": 1,
                "processed_batches": 1,
                "total_batches": 1,
                "scan_complete": True,
                "endpoints": [
                    DocumentEndpoint(
                        method="GET",
                        path="/ping",
                        summary="心跳",
                        description="检测服务可用性",
                        source_files=["main.py"],
                        responses=[{"status": "200", "description": "OK"}],
                    ).model_dump()
                ],
                "warnings": [],
                "issues": [],
            }

    result = await generate_openapi_for_directory(
        project, app.state.settings, FakeDocuments(), AgentService(app.state.settings, None)
    )
    assert result["filename"].startswith("openapi") and result["filename"].endswith(".json")
    assert result["partial"] is False
    assert "/ping" in json.loads(result["text"])["paths"]
    assert (project / "API" / result["filename"]).is_file()


async def test_chained_reruns_preserve_test_document_and_ai_task_without_document_reruns_cleanly(
    client, target
):
    """Ensure documents survive chained reruns with edits and docless tasks rerun cleanly."""
    doc = {
        "filename": "service-spec.json",
        "text": json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "服务规范", "version": "1.0.0"},
                "paths": {"/json": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }
        ),
    }
    created = await client.post(
        "/api/tasks",
        json={
            "origin": target.base_url,
            "catalog": catalog().model_dump(),
            "document": doc,
        },
    )
    assert created.status_code == 202, created.text
    first = await terminal(client, created.json()["id"])
    assert first["snapshot"]["document"] == {"filename": "service-spec.json"}

    rerun_one = await client.post(
        f"/api/tasks/{first['id']}/rerun",
        json={"origin": target.base_url},
    )
    assert rerun_one.status_code == 202, rerun_one.text
    second = await terminal(client, rerun_one.json()["id"])
    assert second["snapshot"]["document"] == {"filename": "service-spec.json"}

    edited_case = {**second["snapshot"]["cases"][0], "name": "二轮编辑用例"}
    rerun_two = await client.post(
        f"/api/tasks/{second['id']}/rerun",
        json={"origin": target.base_url, "case_edits": {"0": edited_case}},
    )
    assert rerun_two.status_code == 202, rerun_two.text
    third = await terminal(client, rerun_two.json()["id"])
    assert third["snapshot"]["document"] == {"filename": "service-spec.json"}

    for task_id in (first["id"], second["id"], third["id"]):
        attachment = await client.get(f"/api/tasks/{task_id}/document")
        assert attachment.status_code == 200
        assert attachment.content == doc["text"].encode()

    # AI-triggered task without an uploaded document works and reruns cleanly.
    no_doc_created = await client.post(
        "/api/tasks",
        json={
            "origin": target.base_url,
            "catalog": catalog().model_dump(),
            "source": "ai",
            "badge": "AI 测试",
        },
    )
    assert no_doc_created.status_code == 202, no_doc_created.text
    no_doc_first = await terminal(client, no_doc_created.json()["id"])
    assert "document" not in no_doc_first["snapshot"]
    assert (await client.get(f"/api/tasks/{no_doc_first['id']}/document")).status_code == 404

    no_doc_rerun = await client.post(
        f"/api/tasks/{no_doc_first['id']}/rerun",
        json={"origin": target.base_url},
    )
    assert no_doc_rerun.status_code == 202, no_doc_rerun.text
    no_doc_second = await terminal(client, no_doc_rerun.json()["id"])
    assert "document" not in no_doc_second["snapshot"]


async def test_summarize_results_retries_once_on_transient_stream_error(monkeypatch):
    """大任务总结遇到上游单次瞬时断流或空响应时自动重试一次并成功产出 Markdown。"""
    import api_agent.tasks.service as task_service
    from api_agent.agent.service import AgentError

    monkeypatch.setattr(task_service, "SUMMARY_RETRY_DELAY_SECONDS", 0)
    calls = {"stream": 0}

    class FlakySummaryAgent:
        async def stream_task_summary(self, data, publish):
            calls["stream"] += 1
            if calls["stream"] == 1:
                raise AgentError("model_connection_failed")
            await publish("## 结果概览\n全部通过")
            return "## 结果概览\n全部通过"

    published: list[str] = []

    async def publish(text: str):
        published.append(text)

    summary = await task_service.summarize_results(
        FlakySummaryAgent(),
        [{"name": "ok", "status": "passed", "assertions": []}],
        [],
        publish=publish,
    )
    assert calls["stream"] == 2
    assert "全部通过" in summary
    assert published == ["## 结果概览\n全部通过"]
