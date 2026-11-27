"""Comprehensive tests for Stage 2 Page-Scoped AI Assistant Engine, Routes & Lifespan."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from support.local_server import LocalServer

from api_agent.agent.service import AgentService
from api_agent.assistant.registry import (
    SCOPE_TOOL_WHITELIST,
    SUITE_STARTING_TOOLS,
    ToolExecutionContext,
)
from api_agent.assistant.runner import AssistantRunManager
from api_agent.console.runner import ConsoleRunManager
from api_agent.infrastructure.database import Database
from api_agent.llm.testing_manager import ModelTestManager
from api_agent.main import create_app
from api_agent.streams.runner import StreamSuiteRunner


def _sse_tool_call_chunks(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
) -> list[str]:
    encoded_args = json.dumps(arguments, ensure_ascii=False)
    split_at = max(1, len(encoded_args) // 2)
    chunk_1 = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": encoded_args[:split_at],
                            },
                        }
                    ],
                },
            }
        ]
    }
    chunk_2 = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": encoded_args[split_at:]},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    return [
        f"data: {json.dumps(chunk_1, ensure_ascii=False)}\n\n",
        f"data: {json.dumps(chunk_2, ensure_ascii=False)}\n\n",
        "data: [DONE]\n\n",
    ]


def _sse_text_chunks(*lines: str) -> list[str]:
    chunks: list[str] = []
    for idx, text in enumerate(lines):
        is_last = idx == len(lines) - 1
        payload = {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    **({"finish_reason": "stop"} if is_last else {}),
                }
            ]
        }
        chunks.append(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")
    chunks.append("data: [DONE]\n\n")
    return chunks


async def _collect_ndjson(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    response = await client.get(url)
    assert response.status_code == 200, response.text
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


async def test_strict_page_scope_tool_isolation_and_cross_scope_refusal(app, monkeypatch):
    """每个页面作用域只暴露自身白名单工具，跨页工具调用在分发层被直接拒绝且不触发任何域管理器。"""
    registry = app.state.assistant_runs.registry

    expected_by_scope = {
        "console": {
            "read_current_test_result",
            "run_console_ai_suite",
            "read_console_sub_case",
        },
        "websocket": {
            "read_current_test_result",
            "run_websocket_ai_suite",
            "read_websocket_sub_case",
        },
        "tasks": {
            "read_current_test_result",
            "run_tasks_ai_suite",
            "read_task_sub_case",
        },
        "model_testing": {
            "read_current_test_result",
            "discover_model_testing_models",
            "run_model_testing_ai_suite",
            "read_model_testing_sub_case",
        },
    }

    for scope, expected_names in expected_by_scope.items():
        exposed_names = {tool.function.name for tool in registry.tools_for_scope(scope)}
        assert exposed_names == expected_names
        assert SCOPE_TOOL_WHITELIST[scope] == expected_names

    assert SUITE_STARTING_TOOLS == {
        "run_console_ai_suite",
        "run_websocket_ai_suite",
        "run_tasks_ai_suite",
        "run_model_testing_ai_suite",
    }
    for tool_name in SUITE_STARTING_TOOLS:
        assert registry.starts_new_run(tool_name) is True
    assert registry.starts_new_run("read_current_test_result") is False
    assert registry.starts_new_run("discover_model_testing_models") is False

    # 如果任何域运行器被跨页调用，立刻断言失败。
    invoked: list[str] = []

    async def _forbidden(*_args, **_kwargs):
        invoked.append("called")
        raise AssertionError("Cross-scope domain manager must never be called")

    monkeypatch.setattr(app.state.console_runs, "start_ai_suite", _forbidden)
    monkeypatch.setattr(app.state.stream_suites, "start_ai_suite", _forbidden)
    monkeypatch.setattr(app.state.tasks, "start", _forbidden)
    monkeypatch.setattr(app.state.model_tests, "start_ai_multi_round_run", _forbidden)

    all_tools = set().union(*expected_by_scope.values()) | {"nonexistent_dangerous_tool"}
    agent = AgentService(app.state.settings, None)

    for scope, allowed in expected_by_scope.items():
        ctx = ToolExecutionContext(scope=scope, run_id="iso-test", agent=agent)
        for tool_name in sorted(all_tools - allowed):
            assert registry.is_allowed(scope, tool_name) is False
            result = await registry.dispatch(scope, tool_name, {"url": "http://127.0.0.1:9"}, ctx)
            assert result == {
                "error": "tool_not_allowed_for_scope",
                "scope": scope,
                "tool": tool_name,
            }

    assert invoked == []


async def test_multi_hop_console_suite_streams_case_links_and_persists_blocks_json(
    app, client, settings
):
    """多跳后台助手执行接口测试套件：实时推送 case_link 并将结构化块持久化到 blocks_json。"""
    target_app = FastAPI()

    @target_app.get("/health")
    async def target_health():
        return {"ok": True, "service": "demo"}

    @target_app.get("/items")
    async def target_items():
        return {"items": [1, 2, 3]}

    target_server = await LocalServer(target_app).start()

    seen_llm_requests: list[dict[str, Any]] = []
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        seen_llm_requests.append(body)

        # Hop 1: 确认只暴露了 console 域工具，并调用 run_console_ai_suite。
        if len(seen_llm_requests) == 1:
            tool_names = {item["function"]["name"] for item in body.get("tools") or []}
            assert tool_names == {
                "read_current_test_result",
                "run_console_ai_suite",
                "read_console_sub_case",
            }
            chunks = _sse_tool_call_chunks(
                "call_console_1",
                "run_console_ai_suite",
                {
                    "title": "AI 健康与列表检查",
                    "url": target_server.base_url,
                    "rounds": 1,
                    "cooldown_ms": 0,
                    "cases": [
                        {"label": "健康检查", "method": "GET", "url": "/health"},
                        {"label": "列表查询", "method": "GET", "url": "/items"},
                    ],
                },
            )

            async def gen_1():
                for chunk in chunks:
                    yield chunk

            return StreamingResponse(gen_1(), media_type="text/event-stream")

        # Hop 2: 校验工具返回体并输出中文总结。
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["name"] == "run_console_ai_suite"
        tool_payload = json.loads(tool_msg["content"])
        assert tool_payload["ok"] is True
        assert len(tool_payload["case_links"]) == 2

        chunks = _sse_text_chunks(
            "### 测试汇总\n",
            "共执行 2 条接口用例，全部返回 200 通过。",
        )

        async def gen_2():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen_2(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-gpt"},
        )
        start_resp = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "console",
                "messages": [{"role": "user", "content": "帮我测试 health 和 items 接口"}],
            },
        )
        assert start_resp.status_code == 202, start_resp.text
        started = start_resp.json()
        run_id = started["run_id"]
        assert started["scope"] == "console"
        assert started["status"] == "running"

        frames = await _collect_ndjson(client, f"/api/assistant/runs/{run_id}/live")
        frame_types = [f["type"] for f in frames]
        assert "status" in frame_types
        assert "run_bound" in frame_types
        assert "case_link" in frame_types
        assert frames[-1]["type"] == "done"

        testing_status = next(f for f in frames if f.get("phase") == "testing")
        assert testing_status["tool"] == "run_console_ai_suite"

        bound_frame = next(f for f in frames if f["type"] == "run_bound")
        new_history_id = bound_frame["history_id"]
        assert bound_frame["scope"] == "console"
        assert bound_frame["badge"] == "AI 测试"
        assert bound_frame["forked"] is False
        assert bound_frame["previous_history_id"] is None

        case_link_frames = [f["block"] for f in frames if f["type"] == "case_link"]
        assert len(case_link_frames) == 2
        assert {blk["label"] for blk in case_link_frames} == {"健康检查", "列表查询"}
        assert all(blk["history_id"] == new_history_id for blk in case_link_frames)

        done_frame = frames[-1]
        assert done_frame["history_id"] == new_history_id
        assert len(done_frame["blocks"]) == 2
        assert "全部返回 200 通过" in done_frame["text"]

        # 通过 REST 线程接口校验持久化消息与 blocks。
        thread_resp = await client.get(f"/api/assistant/threads/console/{new_history_id}")
        assert thread_resp.status_code == 200, thread_resp.text
        thread_data = thread_resp.json()
        assert thread_data["history_id"] == new_history_id
        assert thread_data["active_run_id"] is None
        assert [m["role"] for m in thread_data["messages"]] == ["user", "assistant"]
        assert thread_data["messages"][0]["content"] == "帮我测试 health 和 items 接口"
        assert len(thread_data["messages"][1]["blocks"]) == 2

        # 直接核验 SQLite assistant_messages.blocks_json 落库内容。
        conn = sqlite3.connect(settings.database_path)
        try:
            rows = conn.execute(
                "SELECT role, blocks_json, run_id FROM assistant_messages ORDER BY created_at, id"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "user"
        assert rows[1][0] == "assistant"
        stored_blocks = json.loads(rows[1][1])
        assert len(stored_blocks) == 2
        assert {blk["label"] for blk in stored_blocks} == {"健康检查", "列表查询"}
        assert rows[1][2] == run_id
    finally:
        await llm_server.stop()
        await target_server.stop()


async def test_client_disconnect_does_not_cancel_run_and_reconnect_replays_done(app):
    """客户端中途断开 /live 观察流不会取消后台助手任务，重连后可完整回放全部帧及 done。"""
    llm_started = asyncio.Event()
    release_llm = asyncio.Event()
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        await request.json()
        llm_started.set()
        await release_llm.wait()
        chunks = _sse_text_chunks("后台运行已完成，", "断线重连依然可见。")

        async def gen():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    # 使用真实 TCP 服务器承载被测 FastAPI 应用，确保 /live 流式传输不会被 ASGITransport 整包缓冲。
    api_server = await LocalServer(app).start()
    try:
        async with httpx.AsyncClient(base_url=api_server.base_url, timeout=10.0) as live_client:
            await live_client.put(
                "/api/settings/llm",
                json={
                    "enabled": True,
                    "base_url": f"{llm_server.base_url}/v1",
                    "model": "mock-slow",
                },
            )
            start_resp = await live_client.post(
                "/api/assistant/runs",
                json={
                    "scope": "console",
                    "history_id": "hist-reconnect-1",
                    "messages": [{"role": "user", "content": "解释一下 HTTP 429"}],
                },
            )
            assert start_resp.status_code == 202
            run_id = start_resp.json()["run_id"]

            await asyncio.wait_for(llm_started.wait(), timeout=5)

            # 第一次连接：读取首帧 status 后立刻主动关闭连接模拟浏览器切页/断流。
            first_frames: list[dict[str, Any]] = []
            async with live_client.stream(
                "GET", f"/api/assistant/runs/{run_id}/live"
            ) as stream_resp:
                assert stream_resp.status_code == 200
                async for line in stream_resp.aiter_lines():
                    if line.strip():
                        first_frames.append(json.loads(line))
                        break

            assert len(first_frames) == 1
            assert first_frames[0]["type"] == "status"

            # 断开后任务必须仍在 active 列表中继续运行。
            active_resp = await live_client.get(
                "/api/assistant/runs/active", params={"scope": "console"}
            )
            assert active_resp.status_code == 200
            active_ids = [item["run_id"] for item in active_resp.json()["items"]]
            assert run_id in active_ids

            # 线程接口也能查到当前绑定该历史记录的 active_run_id。
            thread_during = await live_client.get("/api/assistant/threads/console/hist-reconnect-1")
            assert thread_during.status_code == 200
            assert thread_during.json()["active_run_id"] == run_id

            # 放行后台任务直至终态。
            release_llm.set()
            entry = app.state.assistant_runs.active.get(run_id)
            if entry is not None:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=5)

            # 终态后重连 /live 仍可从头无损回放全部缓冲帧直至 done。
            replay_frames = await _collect_ndjson(live_client, f"/api/assistant/runs/{run_id}/live")
            assert replay_frames[0]["type"] == "status"
            assert replay_frames[-1]["type"] == "done"
            assert "断线重连依然可见" in replay_frames[-1]["text"]
            assert replay_frames[-1]["history_id"] == "hist-reconnect-1"

            # 线程中也已完整落库 user + assistant 消息。
            thread_after = await live_client.get("/api/assistant/threads/console/hist-reconnect-1")
            assert thread_after.status_code == 200
            msgs = thread_after.json()["messages"]
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert "断线重连依然可见" in msgs[1]["content"]
    finally:
        release_llm.set()
        await api_server.stop()
        await llm_server.stop()


async def test_atomic_fork_leaves_old_thread_untouched_and_persists_under_new_history(app, client):
    """原子分叉：在旧 history_id='old-1' 发起 AI 测试套件时，old-1 线程完全不受污染。"""
    # 预先在 old-1 写入既有对话记录。
    await app.state.assistant_threads.append_message("console", "old-1", "user", "旧问题")
    await app.state.assistant_threads.append_message("console", "old-1", "assistant", "旧回答")

    target_app = FastAPI()

    @target_app.get("/ping")
    async def target_ping():
        return {"pong": True}

    target_server = await LocalServer(target_app).start()

    call_count = 0
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        nonlocal call_count
        call_count += 1
        await request.json()
        if call_count == 1:
            chunks = _sse_tool_call_chunks(
                "call_fork_1",
                "run_console_ai_suite",
                {
                    "title": "分叉后的新测试",
                    "url": f"{target_server.base_url}/ping",
                    "rounds": 1,
                    "cooldown_ms": 0,
                    "cases": [{"label": "Ping 探测", "method": "GET", "url": "/ping"}],
                },
            )

            async def gen_1():
                for chunk in chunks:
                    yield chunk

            return StreamingResponse(gen_1(), media_type="text/event-stream")

        chunks = _sse_text_chunks("新测试分叉完成，Ping 返回 200。")

        async def gen_2():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen_2(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-fork"},
        )
        start_resp = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "console",
                "history_id": "old-1",
                "messages": [
                    {"role": "user", "content": "旧问题"},
                    {"role": "assistant", "content": "旧回答"},
                    {"role": "user", "content": "帮我重新执行一次 ping 测试"},
                ],
            },
        )
        assert start_resp.status_code == 202
        run_id = start_resp.json()["run_id"]

        frames = await _collect_ndjson(client, f"/api/assistant/runs/{run_id}/live")
        bound = next(f for f in frames if f["type"] == "run_bound")
        new_history_id = bound["history_id"]
        assert new_history_id != "old-1"
        assert bound["forked"] is True
        assert bound["previous_history_id"] == "old-1"
        assert bound["badge"] == "AI 测试"
        assert frames[-1]["type"] == "done"
        assert frames[-1]["history_id"] == new_history_id

        # 关键断言：旧线程 old-1 必须丝毫未动！
        old_thread = (await client.get("/api/assistant/threads/console/old-1")).json()
        assert [(m["role"], m["content"]) for m in old_thread["messages"]] == [
            ("user", "旧问题"),
            ("assistant", "旧回答"),
        ]

        # 触发提示、子用例链接块与助手总结必须完整保存在 new_history_id 下。
        new_thread = (await client.get(f"/api/assistant/threads/console/{new_history_id}")).json()
        assert [(m["role"], m["content"]) for m in new_thread["messages"]] == [
            ("user", "帮我重新执行一次 ping 测试"),
            ("assistant", "新测试分叉完成，Ping 返回 200。"),
        ]
        assert len(new_thread["messages"][1]["blocks"]) == 1
        assert new_thread["messages"][1]["blocks"][0]["history_id"] == new_history_id
        assert new_thread["messages"][1]["blocks"][0]["label"] == "Ping 探测"
    finally:
        await llm_server.stop()
        await target_server.stop()


async def test_thread_crud_migrate_and_cancel_propagates_to_child_run(app, client):
    """验证线程 GET/PUT/DELETE 契约、400 作用域校验及 /cancel 对运行中子测试任务的取消传播。"""
    # 1. 空线程读取返回空消息列表；非法 scope 返回 400。
    empty_resp = await client.get("/api/assistant/threads/websocket/ws-empty-1")
    assert empty_resp.status_code == 200
    assert empty_resp.json()["messages"] == []
    assert empty_resp.json()["active_run_id"] is None

    bad_scope_resp = await client.get("/api/assistant/threads/invalid_scope/h1")
    assert bad_scope_resp.status_code == 400
    assert bad_scope_resp.json()["error"]["code"] == "invalid_scope"

    unknown_live = await client.get("/api/assistant/runs/nonexistent-run-id/live")
    assert unknown_live.status_code == 404

    # 2. PUT /migrate 幂等迁移（空线程落库 + 脱敏，非空线程不覆盖）。
    mig_1 = await client.put(
        "/api/assistant/threads/websocket/ws-hist-1/migrate",
        json={
            "title": "旧版长连接对话",
            "messages": [
                {"role": "user", "content": "Authorization: Bearer super-secret-migrate-token"},
                {"role": "assistant", "content": "收到，我来帮你看。"},
            ],
        },
    )
    assert mig_1.status_code == 200
    migrated = mig_1.json()
    assert len(migrated["messages"]) == 2
    assert "super-secret-migrate-token" not in migrated["messages"][0]["content"]

    mig_2 = await client.put(
        "/api/assistant/threads/websocket/ws-hist-1/migrate",
        json={
            "messages": [{"role": "user", "content": "试图覆盖已有消息"}],
        },
    )
    assert mig_2.status_code == 200
    assert len(mig_2.json()["messages"]) == 2
    assert mig_2.json()["messages"][1]["content"] == "收到，我来帮你看。"

    # 3. DELETE 清空线程（204）。
    del_resp = await client.delete("/api/assistant/threads/websocket/ws-hist-1")
    assert del_resp.status_code == 204
    after_del = await client.get("/api/assistant/threads/websocket/ws-hist-1")
    assert after_del.status_code == 200
    assert after_del.json()["messages"] == []

    # 4. 取消传播：启动慢速子套件，在绑定 child_run_id 后调用 /cancel，确认子任务一并 cancelled。
    slow_gate = asyncio.Event()
    first_case_hit = asyncio.Event()
    target_app = FastAPI()

    @target_app.get("/slow")
    async def slow_endpoint():
        first_case_hit.set()
        await slow_gate.wait()
        return {"ok": True}

    target_server = await LocalServer(target_app).start()

    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        await request.json()
        chunks = _sse_tool_call_chunks(
            "call_cancel_1",
            "run_console_ai_suite",
            {
                "title": "待取消的慢套件",
                "url": f"{target_server.base_url}/slow",
                "rounds": 2,
                "cooldown_ms": 5000,
                "cases": [{"label": "慢接口", "method": "GET", "url": "/slow"}],
            },
        )

        async def gen():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-cancel"},
        )
        start_resp = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "console",
                "messages": [{"role": "user", "content": "执行慢接口多轮测试"}],
            },
        )
        assert start_resp.status_code == 202
        run_id = start_resp.json()["run_id"]

        await asyncio.wait_for(first_case_hit.wait(), timeout=5)
        # 等待 run_bound 记录 child_run_id。
        live = app.state.assistant_runs.live[run_id]
        for _ in range(50):
            if live.child_run_id:
                break
            await asyncio.sleep(0.02)
        child_run_id = live.child_run_id
        assert child_run_id is not None

        cancel_resp = await client.post(f"/api/assistant/runs/{run_id}/cancel")
        assert cancel_resp.status_code == 200, cancel_resp.text
        cancel_data = cancel_resp.json()
        assert cancel_data["cancelled"] is True
        assert cancel_data["status"] == "cancelled"
        assert cancel_data["child_run_id"] == child_run_id

        # 确认底层 console 子运行也已取消并从活跃集合移除。
        assert child_run_id not in app.state.console_runs.active
        child_detail = await client.get(f"/api/console/history/{child_run_id}")
        assert child_detail.status_code == 200
        assert child_detail.json()["status"] == "cancelled"

        # 助手 live 流以 cancelled 错误帧收尾。
        frames = await _collect_ndjson(client, f"/api/assistant/runs/{run_id}/live")
        assert frames[-1]["type"] == "error"
        assert frames[-1]["code"] == "cancelled"
    finally:
        slow_gate.set()
        await llm_server.stop()
        await target_server.stop()


async def test_stream_tools_fallback_uses_non_streaming_with_tools_first(client):
    """网关拒绝 stream+tools（400）时，先尝试非流式 chat_completion+tools 并完成工具调用。"""
    seen: list[tuple[bool, bool]] = []
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        is_stream = bool(body.get("stream"))
        has_tools = bool(body.get("tools"))
        seen.append((is_stream, has_tools))

        # 拒绝 stream + tools，接受非流式 + tools！
        if is_stream and has_tools:
            return JSONResponse({"error": "stream with tools unsupported"}, status_code=400)

        if not is_stream and has_tools:
            if not any(m.get("role") == "tool" for m in body["messages"]):
                return JSONResponse(
                    {
                        "id": "cmpl_nonstream_1",
                        "model": "mock-fallback",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_read_nonstream",
                                            "type": "function",
                                            "function": {
                                                "name": "read_current_test_result",
                                                "arguments": '{"include_body": true}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                )
            return JSONResponse(
                {
                    "id": "cmpl_nonstream_2",
                    "model": "mock-fallback",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "通过非流式工具调用读取到状态码 502。",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )

        return JSONResponse({"error": "unexpected path"}, status_code=500)

    llm_server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-fb"},
        )
        start_resp = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "console",
                "messages": [{"role": "user", "content": "看看当前接口报错"}],
                "current_test": {
                    "page": "console",
                    "has_result": True,
                    "response": {"status": 502, "body": "Bad Gateway"},
                },
            },
        )
        assert start_resp.status_code == 202
        run_id = start_resp.json()["run_id"]

        frames = await _collect_ndjson(client, f"/api/assistant/runs/{run_id}/live")
        assert frames[-1]["type"] == "done"
        assert "502" in frames[-1]["text"]
        assert (True, True) in seen
        assert (False, True) in seen
    finally:
        await llm_server.stop()


async def test_model_testing_ephemeral_api_key_never_persisted_or_leaked(app, client, settings):
    """model_testing 工具仅在内存中使用 ephemeral_api_key，绝不落库、不回显且结束后清空。"""
    seen_auth_headers: list[str] = []
    mock_probe_app = FastAPI()

    @mock_probe_app.get("/v1/models")
    async def list_models(request: Request):
        seen_auth_headers.append(request.headers.get("authorization", ""))
        return {"data": [{"id": "demo-chat-1", "owned_by": "demo"}]}

    @mock_probe_app.post("/v1/chat/completions")
    async def probe_completions(request: Request):
        seen_auth_headers.append(request.headers.get("authorization", ""))
        return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}

    probe_server = await LocalServer(mock_probe_app).start()

    step = 0
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        nonlocal step
        step += 1
        body = await request.json()
        if step == 1:
            chunks = _sse_tool_call_chunks(
                "call_mt_discover",
                "discover_model_testing_models",
                {"base_url": f"{probe_server.base_url}/v1", "use_saved_key": False},
            )
        elif step == 2:
            # 校验 discover 工具结果未包含明文临时密钥
            assert "sk-ephemeral-never-store-987654" not in json.dumps(body, ensure_ascii=False)
            chunks = _sse_tool_call_chunks(
                "call_mt_suite",
                "run_model_testing_ai_suite",
                {
                    "base_url": f"{probe_server.base_url}/v1",
                    "use_saved_key": False,
                    "models": ["demo-chat-1"],
                    "checks": ["availability"],
                    "rounds": 1,
                    "cooldown_ms": 0,
                },
            )
        else:
            assert "sk-ephemeral-never-store-987654" not in json.dumps(body, ensure_ascii=False)
            chunks = _sse_text_chunks("探测完成：demo-chat-1 可用。")

        async def gen():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    secret_key = "sk-ephemeral-never-store-987654"
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-mt"},
        )
        start_resp = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "model_testing",
                "messages": [{"role": "user", "content": "帮我拉取并探测模型可用性"}],
                "ephemeral_api_key": secret_key,
            },
        )
        assert start_resp.status_code == 202
        run_id = start_resp.json()["run_id"]

        frames = await _collect_ndjson(client, f"/api/assistant/runs/{run_id}/live")
        assert frames[-1]["type"] == "done"
        assert step == 3
        assert seen_auth_headers.count(f"Bearer {secret_key}") >= 2
        assert any(f["type"] == "run_bound" and f["scope"] == "model_testing" for f in frames)
        assert any(f["type"] == "case_link" for f in frames)
        assert secret_key not in json.dumps(frames, ensure_ascii=False)

        # 全库扫描确认 ephemeral_api_key 未进入任何表。
        conn = sqlite3.connect(settings.database_path)
        try:
            for table in ("assistant_threads", "assistant_messages", "model_test_runs"):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                assert secret_key not in str(rows)
        finally:
            conn.close()
    finally:
        await llm_server.stop()
        await probe_server.stop()


async def test_bare_custom_ephemeral_api_key_echoed_in_user_prompt_is_scrubbed_from_db(
    client, settings
):
    """非 sk- 裸 ephemeral_api_key 若被用户复述在 prompt 中，落库前须由 agent.context 脱敏。"""
    bare_chat_key = "AIzaSyBareCustomChatKey987654"
    bare_suite_key = "AIzaSyBareCustomSuiteKey112233"

    mock_probe_app = FastAPI()

    @mock_probe_app.post("/v1/chat/completions")
    async def probe_completions(request: Request):
        await request.json()
        return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}

    probe_server = await LocalServer(mock_probe_app).start()

    step = 0
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        nonlocal step
        step += 1
        body = await request.json()
        serialized = json.dumps(body, ensure_ascii=False)
        assert bare_chat_key not in serialized
        assert bare_suite_key not in serialized

        if step == 1:
            chunks = _sse_text_chunks("已核对当前配置，不会记录任何明文密钥。")
        elif step == 2:
            chunks = _sse_tool_call_chunks(
                "call_mt_bare_key_suite",
                "run_model_testing_ai_suite",
                {
                    "base_url": f"{probe_server.base_url}/v1",
                    "use_saved_key": False,
                    "models": ["demo-bare-1"],
                    "checks": ["availability"],
                    "rounds": 1,
                    "cooldown_ms": 0,
                },
            )
        else:
            chunks = _sse_text_chunks("模型探测已完成。")

        async def gen():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-mt"},
        )

        # 路径 1：普通问答落库（不触发 run_bound，直接写回已有 history_id）。
        chat_start = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "model_testing",
                "history_id": "mt-bare-chat-hist",
                "messages": [
                    {
                        "role": "user",
                        "content": f"帮我确认使用 {bare_chat_key} 时的探测说明",
                    }
                ],
                "ephemeral_api_key": bare_chat_key,
            },
        )
        assert chat_start.status_code == 202
        chat_frames = await _collect_ndjson(
            client, f"/api/assistant/runs/{chat_start.json()['run_id']}/live"
        )
        assert chat_frames[-1]["type"] == "done"

        chat_thread = (
            await client.get("/api/assistant/threads/model_testing/mt-bare-chat-hist")
        ).json()
        assert len(chat_thread["messages"]) == 2
        assert bare_chat_key not in chat_thread["messages"][0]["content"]
        assert "[REDACTED]" in chat_thread["messages"][0]["content"]

        # 路径 2：触发套件运行与 run_bound 落库。
        suite_start = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "model_testing",
                "messages": [
                    {
                        "role": "user",
                        "content": f"请使用 {bare_suite_key} 执行模型探测",
                    }
                ],
                "ephemeral_api_key": bare_suite_key,
            },
        )
        assert suite_start.status_code == 202
        suite_frames = await _collect_ndjson(
            client, f"/api/assistant/runs/{suite_start.json()['run_id']}/live"
        )
        assert suite_frames[-1]["type"] == "done"
        bound = next(f for f in suite_frames if f["type"] == "run_bound")
        suite_thread = (
            await client.get(f"/api/assistant/threads/model_testing/{bound['history_id']}")
        ).json()
        assert len(suite_thread["messages"]) == 2
        assert bare_suite_key not in suite_thread["messages"][0]["content"]
        assert "[REDACTED]" in suite_thread["messages"][0]["content"]

        # 直接扫描 SQLite 表确认裸临时密钥绝不落库。
        conn = sqlite3.connect(settings.database_path)
        try:
            for table in ("assistant_threads", "assistant_messages", "model_test_runs"):
                raw_rows = str(conn.execute(f"SELECT * FROM {table}").fetchall())
                assert bare_chat_key not in raw_rows
                assert bare_suite_key not in raw_rows
        finally:
            conn.close()
    finally:
        await llm_server.stop()
        await probe_server.stop()


async def test_cross_scope_hallucinated_tool_call_is_refused_end_to_end(app, client):
    """即使上游模型在 console 页幻觉调用 run_websocket_ai_suite，引擎也必须拒绝并回传错误。"""
    step = 0
    seen_tool_responses: list[dict[str, Any]] = []
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        nonlocal step
        step += 1
        body = await request.json()
        if step == 1:
            chunks = _sse_tool_call_chunks(
                "call_cross_1",
                "run_websocket_ai_suite",
                {"url": "ws://127.0.0.1:9999/ws"},
            )
        else:
            tool_msg = body["messages"][-1]
            seen_tool_responses.append(json.loads(tool_msg["content"]))
            chunks = _sse_text_chunks("当前处于接口测试页，无法跨页执行长连接测试。")

        async def gen():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream")

    llm_server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{llm_server.base_url}/v1", "model": "mock-cross"},
        )
        start_resp = await client.post(
            "/api/assistant/runs",
            json={
                "scope": "console",
                "messages": [{"role": "user", "content": "帮我测一下 ws://127.0.0.1:9999/ws"}],
            },
        )
        assert start_resp.status_code == 202
        run_id = start_resp.json()["run_id"]

        frames = await _collect_ndjson(client, f"/api/assistant/runs/{run_id}/live")
        assert frames[-1]["type"] == "done"
        assert not any(f["type"] == "run_bound" for f in frames)
        assert seen_tool_responses == [
            {
                "error": "tool_not_allowed_for_scope",
                "scope": "console",
                "tool": "run_websocket_ai_suite",
            }
        ]
        assert app.state.stream_suites.active == {}
    finally:
        await llm_server.stop()


async def test_lifespan_registers_all_stage2_managers_and_unwinds_in_safe_order(
    settings, monkeypatch
):
    """lifespan 挂载全部 Stage 2 管理器，且关闭时先停 assistant_runs 再停域运行器，最后释放 DB。"""
    order: list[str] = []

    for owner, method_name, label in (
        (AssistantRunManager, "shutdown", "assistant_runs"),
        (ModelTestManager, "shutdown", "model_tests"),
        (StreamSuiteRunner, "shutdown", "stream_suites"),
        (ConsoleRunManager, "shutdown", "console_runs"),
        (Database, "dispose", "database"),
    ):
        original = getattr(owner, method_name)

        async def wrapped(self, _orig=original, _label=label):
            order.append(_label)
            return await _orig(self)

        monkeypatch.setattr(owner, method_name, wrapped)

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert hasattr(app.state, "assistant_threads")
        assert hasattr(app.state, "console_runs")
        assert hasattr(app.state, "stream_suites")
        assert hasattr(app.state, "model_tests")
        assert hasattr(app.state, "assistant_runs")

    assert order.index("assistant_runs") == 0
    assert order.index("database") == len(order) - 1
    assert set(order) == {
        "assistant_runs",
        "model_tests",
        "stream_suites",
        "console_runs",
        "database",
    }
