"""一个轻量的本地 SSE / WebSocket 测试目标，用于手动验证长连接界面。

实现：用 uvicorn 监听 127.0.0.1:9100（避开本项目自身 8010 与常见 8000/8080）。
- /sse: 每 300ms 发一条带 index 的事件，发 5 条后自然关闭；
- /sse-forever: 无限流，用于验证手动停止；
- /ws: 握手后先发两条问候，然后回显每条消息；
- /ws-close: 发一条后立刻以 1000 关闭；
- /ws-reject: 握手阶段以 1008 拒绝。

仅用于本机调试。启动后按 Ctrl+C 退出。
"""

import asyncio

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

app = FastAPI(title="API-Agent stream fixture")


@app.get("/sse")
async def sse():
    """提供有限的多行 SSE 样例，返回 StreamingResponse，便于观察自然关闭。"""

    async def events():
        """按固定间隔产生演示事件和多行 data；取消由流式响应关闭传播。"""
        yield b": stream-start\n\n"
        for i in range(5):
            yield f'event: tick\nid: {i}\ndata: {{"index": {i}}}\n\n'.encode()
            await asyncio.sleep(0.3)
        yield b"data: last line one\ndata: line two\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/sse-forever")
async def sse_forever():
    """提供持续 SSE 样例，用于手动停止、时长与事件预算测试。"""

    async def events():
        """循环产生单调递增事件，直到客户端断开；不写入业务数据。"""
        i = 0
        while True:
            yield f"data: {i}\n\n".encode()
            i += 1
            await asyncio.sleep(0.2)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """接受连接并回显文本；目标断开正常退出，只用于回环测试。"""
    await websocket.accept()
    await websocket.send_text("welcome-1")
    await websocket.send_text("welcome-2")
    try:
        while True:
            message = await websocket.receive_text()
            await websocket.send_text(f"echo: {message}")
    except WebSocketDisconnect:
        return


@app.websocket("/ws-close")
async def ws_close(websocket: WebSocket):
    """接受后发送一条消息，再以正常关闭码结束连接。"""
    await websocket.accept()
    await websocket.send_text("bye")
    await websocket.close(code=1000, reason="done")


@app.websocket("/ws-reject")
async def ws_reject(websocket: WebSocket):
    """在接受前关闭握手，演示被拒连接的错误路径。"""
    await websocket.close(code=1008, reason="not allowed")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9100, log_level="warning")
