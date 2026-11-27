"""AI 助手小Y 当前测试结果读取工具、状态帧流与知识库注入回归测试。"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from support.local_server import LocalServer


async def test_assistant_calls_current_test_reader_tool_and_emits_status_frames(client):
    """触发读取工具时应发出 thinking -> reading_data -> thinking 帧，且绝不泄漏凭据。"""
    seen_requests: list[dict] = []
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        body = await request.json()
        seen_requests.append(body)
        assert body["stream"] is True

        # 第 1 轮：模型决定调用 read_current_test_result 工具
        if len(seen_requests) == 1:
            assert body.get("tools")
            assert body["tools"][0]["function"]["name"] == "read_current_test_result"

            async def tool_chunks():
                yield (
                    'data: {"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":'
                    '[{"index":0,"id":"call_read_1","type":"function","function":'
                    '{"name":"read_current_test_result","arguments":"{\\"include_body\\":"}}]}}]}\n\n'
                )
                yield (
                    'data: {"choices":[{"index":0,"delta":{"tool_calls":'
                    '[{"index":0,"function":{"arguments":"true}"}}]},'
                    '"finish_reason":"tool_calls"}]}\n\n'
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(tool_chunks(), media_type="text/event-stream")

        # 第 2 轮：模型收到脱敏后的当前测试结果并返回根因分析与解决办法
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_read_1"
        assert "500" in tool_msg["content"]
        assert "Column 'status' cannot be null" in tool_msg["content"]
        assert "secret-live-bearer-token" not in tool_msg["content"]

        async def answer_chunks():
            chunk_2 = json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": (
                                    "请求体缺少必填字段 `status`，"
                                    '请在 JSON 中补充 `"status": 1` 后重发。'
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            yield 'data: {"choices":[{"index":0,"delta":{"content":"### 根因分析\\n"}}]}\n\n'
            yield f"data: {chunk_2}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(answer_chunks(), media_type="text/event-stream")

    server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={"enabled": True, "base_url": f"{server.base_url}/v1", "model": "fixture-tool"},
        )
        response = await client.post(
            "/api/tasks/chat",
            json={
                "messages": [
                    {"role": "user", "content": "帮我分析当前测试为什么报 500，怎么解决？"}
                ],
                "current_test": {
                    "page": "console",
                    "has_result": True,
                    "request": {
                        "method": "PUT",
                        "url": "http://127.0.0.1:8088/api/customer/12",
                        "headers": {"Authorization": "Bearer secret-live-bearer-token"},
                        "body_mode": "json",
                        "body": '{"name": "测试客户"}',
                    },
                    "response": {
                        "status": 500,
                        "reason": "Internal Server Error",
                        "body": '{"code":"500","message":"Column \'status\' cannot be null"}',
                    },
                },
            },
        )
        assert response.status_code == 200, response.text
        frames = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        status_phases = [f["phase"] for f in frames if f["type"] == "status"]
        assert status_phases == ["thinking", "reading_data", "thinking"]
        reading_frame = next(f for f in frames if f.get("phase") == "reading_data")
        assert reading_frame["message"] == "读取数据中"
        assert reading_frame["tool"] == "read_current_test_result"
        assert frames[-1]["type"] == "done"
        assert "status" in frames[-1]["text"]
        assert len(seen_requests) == 2
        # 系统提示词应附带知识库参考资料
        assert "知识库" in seen_requests[0]["messages"][0]["content"]
    finally:
        await server.stop()


async def test_assistant_falls_back_when_provider_rejects_stream_tools(client):
    """若上游网关不支持 stream+tools（返回 400），自动回退为本地读取注入并流式分析。"""
    calls = 0
    mock_llm = FastAPI()

    @mock_llm.post("/v1/chat/completions")
    async def completions(request: Request):
        nonlocal calls
        calls += 1
        body = await request.json()
        if body.get("tools"):
            return JSONResponse({"error": "tools not supported in stream mode"}, status_code=400)
        assert any("handshake_rejected" in str(m.get("content")) for m in body["messages"])

        async def chunks():
            chunk = json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "握手返回 401，请检查鉴权头。\n"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            yield f"data: {chunk}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    server = await LocalServer(mock_llm).start()
    try:
        await client.put(
            "/api/settings/llm",
            json={
                "enabled": True,
                "base_url": f"{server.base_url}/v1",
                "model": "fixture-no-tools",
            },
        )
        response = await client.post(
            "/api/tasks/chat",
            json={
                "messages": [{"role": "user", "content": "这个 WebSocket 为什么连不上？"}],
                "current_test": {
                    "page": "websocket",
                    "has_result": True,
                    "session": {
                        "url": "ws://127.0.0.1:8088/api/ws/chat",
                        "status": "error",
                        "http_status": 401,
                        "error_kind": "handshake_rejected",
                    },
                },
            },
        )
        assert response.status_code == 200, response.text
        frames = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        status_phases = [f["phase"] for f in frames if f["type"] == "status"]
        assert status_phases == ["thinking", "reading_data", "thinking"]
        assert frames[-1]["type"] == "done"
        assert "401" in frames[-1]["text"]
        assert calls == 2
    finally:
        await server.stop()
