import asyncio

import pytest
from support.local_server import LocalServer


@pytest.fixture
async def target():
    """启动真实回环目标，供 SSE 与 WebSocket 用例共用。

    实现：与控制台测试一致，每个用例独立起停，端口由系统分配，
    避免并行或残留连接互相影响。
    """
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


async def collect(client, session_id, *, until_finished=True, timeout=8.0, wait_ms=50):
    """反复增量读取会话，直到结束或超时，返回（会话, 全部事件）。

    作用：把“轮询 + 游标推进”封成一个测试辅助，避免每个用例重复写循环。
    实现：始终用上一次的 ``next_cursor`` 作为 ``after``，
    因此能同时验证游标语义（不漏、不重）。
    """
    cursor = 0
    events = []
    session = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        response = await client.get(
            f"/api/streams/{session_id}", params={"after": cursor, "wait_ms": wait_ms}
        )
        assert response.status_code == 200, response.text
        page = response.json()
        session = page["session"]
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if page["finished"] and until_finished:
            return session, events
        if until_finished and session["finished"]:
            return session, events
        await asyncio.sleep(0.02)
    return session, events


async def test_websocket_echo_roundtrip_records_both_directions(client, target):
    """WebSocket：握手、接收问候、发送并收到回显，收发都要记入时间线。

    实现：连接后等待 open 与两条 welcome，再发送一条消息，
    断言收到 ``echo:...``、``sent_count`` 递增，且方向字段区分 in/out。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "websocket", "url": f"{target.ws_url}/ws", "max_duration_ms": 20_000},
    )
    assert response.status_code == 202, response.text
    session_id = response.json()["id"]

    def incoming(data: dict) -> bool:
        return any(
            event["kind"] == "text" and event["data"] == "welcome-2" for event in data["events"]
        )

    for _ in range(200):
        page = (await client.get(f"/api/streams/{session_id}")).json()
        if incoming(page) and page["session"]["status"] == "open":
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError(f"websocket did not open: {page}")

    sent = await client.post(f"/api/streams/{session_id}/send", json={"message": "ping-from-test"})
    assert sent.status_code == 200, sent.text

    for _ in range(200):
        page = (await client.get(f"/api/streams/{session_id}")).json()
        if any(event["data"] == "echo:ping-from-test" for event in page["events"]):
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError(f"no echo received: {page}")

    outbound = [e for e in page["events"] if e["direction"] == "out"]
    assert outbound and outbound[0]["data"] == "ping-from-test"
    assert page["session"]["sent_count"] >= 1

    stopped = await client.post(f"/api/streams/{session_id}/stop")
    assert stopped.status_code == 200


async def test_websocket_scheduled_sends_run_before_stop(client, target):
    """WebSocket：预置消息应按顺序自动发出并记录为 out 方向。

    实现：启动时带两条待发消息，断言两条都出现在时间线里、
    且目标回显也到达——一次性验证“发送调度 + 收发对齐”。
    """
    response = await client.post(
        "/api/streams",
        json={
            "kind": "websocket",
            "url": f"{target.ws_url}/ws",
            "send": ["first", "second"],
            "send_interval_ms": 50,
            "max_duration_ms": 20_000,
        },
    )
    session_id = response.json()["id"]
    collected = []
    for _ in range(300):
        page = (await client.get(f"/api/streams/{session_id}")).json()
        collected = page["events"]
        echoed = {e["data"] for e in collected}
        if {"echo:first", "echo:second"} <= echoed:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError(f"scheduled messages were not echoed: {collected}")
    await client.post(f"/api/streams/{session_id}/stop")
    inputs = (await client.get(f"/api/streams/{session_id}/inputs")).json()
    assert inputs["url"] == f"{target.ws_url}/ws"
    assert inputs["scheduled_messages"] == ["first", "second"]


async def test_saved_websocket_inputs_are_scrubbed_and_new_connections_get_new_ids(client, target):
    url = f"{target.ws_url}/ws"
    request = {
        "url": url,
        "headers": {"Authorization": "Bearer fixture-private-value", "X-Trace": "example"},
        "subprotocols": ["example"],
        "send": ['{"token":"fixture-body-secret"}', "fixture-body-secret"],
    }
    first = await client.post("/api/streams", json=request)
    assert first.status_code == 202
    first_id = first.json()["id"]
    await client.post(f"/api/streams/{first_id}/stop")
    saved = (await client.get(f"/api/streams/{first_id}/inputs")).json()
    assert saved["request_headers"] == {"Authorization": "[redacted]", "X-Trace": "example"}
    assert saved["subprotocols"] == ["example"]
    assert "fixture-body-secret" not in str(saved)
    second = await client.post("/api/streams", json={"url": saved["url"], "send": ["retry"]})
    assert second.status_code == 202
    second_id = second.json()["id"]
    await client.post(f"/api/streams/{second_id}/stop")
    ids = {item["id"] for item in (await client.get("/api/streams")).json()["items"]}
    assert len({first_id, second_id}) == 2 and {first_id, second_id} <= ids


async def test_websocket_scheduled_sends_do_not_noise_after_target_close(client, target):
    """目标在等待发送间隔期间就关闭时，不应产生「Send failed」噪声事件。

    实现：连接 ``/ws-close``（发一条后立刻以 1000 关闭），
    预置 3 条消息 + 100ms 间隔——后两条必然撞上已关闭连接。
    断言时间线里没有 ``Send failed`` 文案，收尾由读循环记录的 close 事件承担。
    """
    response = await client.post(
        "/api/streams",
        json={
            "kind": "websocket",
            "url": f"{target.ws_url}/ws-close",
            "send": ["m1", "m2", "m3"],
            "send_interval_ms": 100,
            "max_duration_ms": 10_000,
        },
    )
    session_id = response.json()["id"]
    session, events = await collect(client, session_id, timeout=6.0)
    assert session["stop_reason"] == "target_closed"
    assert session["close_code"] == 1000
    texts = [event["data"] or "" for event in events]
    assert not any("Send failed" in text for text in texts), texts


async def test_websocket_target_close_reports_code(client, target):
    """WebSocket：目标用 1000 关闭时应记录关闭码与原因。

    实现：``/ws-close`` 发一条后立即关闭，断言 close_code/close_reason
    与 ``target_closed`` 结束原因。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "websocket", "url": f"{target.ws_url}/ws-close", "max_duration_ms": 10_000},
    )
    session_id = response.json()["id"]
    session, events = await collect(client, session_id, timeout=6.0)
    assert session["stop_reason"] == "target_closed"
    assert session["close_code"] == 1000
    assert session["close_reason"] == "fixture done"
    assert any(event["kind"] == "close" for event in events)


async def test_websocket_rejection_is_recorded(client, target):
    """WebSocket：握手被拒（1008）应落成 error 而不是静默失败。

    实现：``/ws-reject`` 在 accept 前关闭，客户端抛出 ``InvalidStatus``，
    断言状态为 error 且带 http_status（缺省为 403 时也接受）。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "websocket", "url": f"{target.ws_url}/ws-reject", "max_duration_ms": 5000},
    )
    session_id = response.json()["id"]
    session, _events = await collect(client, session_id, timeout=5.0)
    assert session["finished"] is True
    assert session["status"] in {"error", "closed"}
    assert session["error_kind"] in {"handshake_rejected", "connect_failed"} or (
        session["stop_reason"] == "target_closed"
    )


async def test_stream_rejects_non_loopback_and_sse(client, target):
    remote = await client.post(
        "/api/streams", json={"kind": "websocket", "url": "ws://example.com/ws"}
    )
    assert remote.status_code == 400
    rejected = await client.post(
        "/api/streams", json={"kind": "sse", "url": f"{target.base_url}/sse"}
    )
    assert rejected.status_code == 422


async def test_stopped_session_can_be_deleted_with_its_events(client, target):
    """已结束的会话应可删除，事件时间线随会话一起消失。

    实现：连一个会正常结束的 SSE，等它落库后 ``DELETE``；
    断言 204、历史列表不再出现该 ID、再读该会话变 404（事件已随 CASCADE 删除）。
    """
    response = await client.post(
        "/api/streams", json={"kind": "websocket", "url": f"{target.ws_url}/ws-close"}
    )
    session_id = response.json()["id"]
    for _ in range(200):
        page = (await client.get(f"/api/streams/{session_id}", params={"after": 0})).json()
        if page["session"]["finished"]:
            break
        await asyncio.sleep(0.02)
    assert page["session"]["finished"] is True

    assert (await client.delete(f"/api/streams/{session_id}")).status_code == 204

    history = await client.get("/api/streams")
    assert all(item["id"] != session_id for item in history.json()["items"])
    replay = await client.get(f"/api/streams/{session_id}", params={"after": 0, "wait_ms": 0})
    assert replay.status_code == 404


async def test_deleting_a_running_session_stops_it_first(client, target):
    """删除在跑的会话必须先收尾，不能留下继续写入的后台任务。

    实现：连 ``/sse-forever``（不会自己结束），确认它在跑后直接 ``DELETE``；
    断言 204、历史里没有它、再读为 404。若实现直接删数据库行，
    内存任务仍会继续写事件并把记录“写回来”，本用例就会失败。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "websocket", "url": f"{target.ws_url}/ws", "max_duration_ms": 30_000},
    )
    session_id = response.json()["id"]
    for _ in range(200):
        page = (await client.get(f"/api/streams/{session_id}", params={"after": 0})).json()
        if page["events"]:
            break
        await asyncio.sleep(0.02)
    assert page["session"]["finished"] is False

    assert (await client.delete(f"/api/streams/{session_id}")).status_code == 204

    history = await client.get("/api/streams")
    assert all(item["id"] != session_id for item in history.json()["items"])
    assert (
        await client.get(f"/api/streams/{session_id}", params={"after": 0, "wait_ms": 0})
    ).status_code == 404


async def test_deleting_an_unknown_session_is_404(client):
    """删除不存在的会话返回 404，不伪装成功。"""
    assert (await client.delete("/api/streams/missing")).status_code == 404
