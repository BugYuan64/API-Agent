"""A real loopback server used to exercise console and long-connection behaviour.

作用：控制台与 SSE/WebSocket 探针都必须走真实 TCP 连接才能验证，
用 ASGI 内存传输会把握手、分帧和半关闭这些最容易出错的环节跳过。
实现：在本进程里用 uvicorn 监听 ``127.0.0.1`` 的随机端口，
提供几个语义固定的端点（JSON、回显、有限 SSE、无限 SSE、WebSocket 回显/拒绝），
测试结束后优雅关闭。
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse


def build_app() -> FastAPI:
    """构造测试用的目标应用。

    实现：每个端点只做一件事，便于断言“服务端到底发了什么”；
    ``/sse`` 发若干条后主动结束，``/sse-forever`` 保持长连接直到客户端断开，
    正好覆盖“目标主动关闭”与“本服务主动停止”两条路径。
    """
    app = FastAPI()

    @app.get("/json")
    async def json_endpoint():
        """返回一个固定 JSON，用于验证状态码、头与响应体捕获。"""
        return {"ok": True, "items": [1, 2, 3], "nested": {"name": "fixture"}}

    @app.post("/echo")
    async def echo(request: Request):
        """回显请求头与请求体，用于验证头的注入与请求体模式。"""
        body = (await request.body()).decode("utf-8", "replace")
        return {
            "method": request.method,
            "content_type": request.headers.get("content-type"),
            "x-trace": request.headers.get("x-trace"),
            "query": dict(request.query_params),
            "body": body,
        }

    @app.get("/slow")
    @app.post("/slow")
    @app.post("/slow/chat/completions")
    async def slow():
        """故意迟到，用于验证客户端超时分类。"""
        await asyncio.sleep(3)
        return {"ok": True}

    @app.get("/redirect")
    async def redirect():
        """返回 302，用于验证默认不跟随跳转。"""
        return JSONResponse({"moved": True}, status_code=302, headers={"Location": "/json"})

    @app.get("/sse")
    @app.post("/sse")
    async def sse(request: Request):
        """发出 4 条带事件名与多行数据的 SSE 事件，然后正常结束。"""

        async def events() -> AsyncIterator[bytes]:
            yield b": hello comment\n\n"
            for index in range(4):
                payload = f'{{"index": {index}}}'
                chunk = f"event: tick\nid: {index}\ndata: {payload}\n\n"
                yield chunk.encode()
                await asyncio.sleep(0.01)
            yield b"data: line one\ndata: line two\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/sse-forever")
    async def sse_forever():
        """无限 SSE 流，每秒一条，用于验证客户端主动停止与时长上限。"""

        async def events() -> AsyncIterator[bytes]:
            index = 0
            while True:
                yield f"data: {index}\n\n".encode()
                index += 1
                await asyncio.sleep(0.05)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/sse-reject")
    async def sse_reject():
        """直接返回 401，用于验证握手被拒的分类。"""
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        """先发两条问候，然后回显每条消息，最后以 1000 正常关闭。"""
        await websocket.accept(subprotocol=(websocket.scope["subprotocols"] or [None])[0])
        await websocket.send_text("welcome-1")
        await websocket.send_text("welcome-2")
        try:
            while True:
                message = await websocket.receive_text()
                await websocket.send_text(f"echo:{message}")
        except WebSocketDisconnect:
            return

    @app.websocket("/ws-close")
    async def ws_close(websocket: WebSocket):
        """发一条消息后立即用 1000 关闭，用于验证目标关闭路径。"""
        await websocket.accept()
        await websocket.send_text("bye")
        await websocket.close(code=1000, reason="fixture done")

    @app.websocket("/ws-reject")
    async def ws_reject(websocket: WebSocket):
        """握手阶段直接关闭（1008），用于验证拒绝分类。"""
        await websocket.close(code=1008, reason="nope")

    @app.get("/models")
    @app.get("/v1/models")
    async def openai_models(request: Request):
        """模拟 OpenAI 兼容的模型列表端点，支持鉴权失败分支。"""
        auth = request.headers.get("authorization", "")
        if "bad-key" in auth:
            return JSONResponse(
                {"error": {"message": "Incorrect API key provided"}},
                status_code=401,
            )
        return {
            "object": "list",
            "data": [
                {"id": "gpt-4o", "object": "model", "owned_by": "openai"},
                {"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"},
                {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
            ],
        }

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def openai_chat_completions(request: Request):
        """模拟 OpenAI 兼容接口，支持鉴权、文本响应与函数/工具调用。"""
        auth = request.headers.get("authorization", "")
        if "bad-key" in auth:
            return JSONResponse(
                {
                    "error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                    }
                },
                status_code=401,
            )
        body = await request.json()
        model = body.get("model", "gpt-4o")
        tools = body.get("tools")
        # 如果声明了 tools，模拟模型发起结构化工具调用
        if tools and isinstance(tools, list) and len(tools) > 0:
            target_fn = tools[0].get("function", {}).get("name", "test_tool")
            return {
                "id": "chatcmpl-mock-tool-call",
                "object": "chat.completion",
                "created": 1700000000,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_mock_123456",
                                    "type": "function",
                                    "function": {
                                        "name": target_fn,
                                        "arguments": '{"query": "mock test value", "limit": 10}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 35, "completion_tokens": 20, "total_tokens": 55},
            }
        # 未声明 tools 或普通对话，返回标准文本回复
        return {
            "id": "chatcmpl-mock-text-response",
            "object": "chat.completion",
            "created": 1700000000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Mock OpenAI-compatible response from LocalServer.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 15, "completion_tokens": 12, "total_tokens": 27},
        }

    return app


class LocalServer:
    """管理一个真实的回环 HTTP/WebSocket 服务。

    实现：``start`` 用 ``uvicorn.Server`` 监听端口 0 让系统分配空闲端口，
    并轮询 ``server.started`` 确保端口已就绪后再返回 base_url；
    ``stop`` 置 ``should_exit`` 并等待服务任务结束。
    """

    def __init__(self):
        """初始化内部引用（未启动状态）。"""
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task | None = None
        self.base_url = ""
        self.ws_url = ""

    async def start(self) -> "LocalServer":
        """启动服务并返回自身，便于 ``server = await LocalServer().start()``。

        实现：等待 ``server.started`` 而非固定 sleep，避免端口未就绪就发请求；
        超时后抛出明确错误，避免测试挂死。
        """
        config = uvicorn.Config(
            build_app(), host="127.0.0.1", port=0, log_level="error", lifespan="off"
        )
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve(), name="fixture-server")
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("fixture server did not start")
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}"
        return self

    async def stop(self) -> None:
        """请求退出并等待服务任务完成。

        实现：``should_exit`` 会让 uvicorn 自己走完关闭流程；
        任务被取消时吞掉 ``CancelledError``，避免污染测试结果。
        """
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.02):
    """轮询等待条件成立，超时抛出 AssertionError。

    问题：长连接是异步推进的，测试若用固定 sleep 既慢又不稳。
    实现：按 ``interval`` 反复调用 ``predicate``（可以是异步函数），
    直到返回真值或超过总超时。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"condition was not met within {timeout}s")
