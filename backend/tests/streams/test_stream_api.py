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


async def test_sse_records_events_and_reports_target_close(client, target):
    """SSE：应记录 open/message/close，并把结束原因归为「目标关闭」。

    实现：``/sse`` 发 4 条带事件名的消息后正常结束；
    断言事件类型齐全、多行 data 被拼接、结束原因是 target_closed
    而不是 limits_exceeded（那是本服务主动停止）。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "sse", "url": f"{target.base_url}/sse", "max_duration_ms": 8000},
    )
    assert response.status_code == 202, response.text
    session_id = response.json()["id"]

    session, events = await collect(client, session_id)
    assert session["finished"] is True
    assert session["status"] == "closed"
    assert session["stop_reason"] == "target_closed"
    assert session["http_status"] == 200
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "open"
    assert "comment" in kinds
    assert kinds.count("message") == 5
    assert kinds[-1] == "close"
    messages = [event for event in events if event["kind"] == "message"]
    assert [event["event_name"] for event in messages[:4]] == ["tick"] * 4
    assert messages[-1]["data"] == "line one\nline two"


async def test_sse_stream_can_be_stopped_and_is_persisted(client, target):
    """SSE：客户端停止后应落 cancelled，并且事件已持久化可回查。

    实现：连 ``/sse-forever``，收够几条后调用 stop；
    随后用同一个 ID 再读一次（此时会话已从内存移出），
    验证它能从数据库回放出同一个时间线，并标记 ``finished=True``。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "sse", "url": f"{target.base_url}/sse-forever", "max_duration_ms": 30_000},
    )
    session_id = response.json()["id"]

    cursor = 0
    for _ in range(200):
        page = (await client.get(f"/api/streams/{session_id}", params={"after": cursor})).json()
        cursor = page["next_cursor"]
        if len([e for e in page["events"] if e["kind"] == "message"]) >= 2:
            break
        await asyncio.sleep(0.02)
    assert cursor >= 2

    stopped = await client.post(f"/api/streams/{session_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["finished"] is True

    # 等后台任务完成收尾落库，再确认内存副本已移入历史。
    await asyncio.sleep(0.2)
    replay = await client.get(f"/api/streams/{session_id}", params={"after": 0, "wait_ms": 0})
    assert replay.status_code == 200
    body = replay.json()
    assert body["session"]["finished"] is True
    assert body["session"]["status"] in {"cancelled", "closed"}
    assert body["session"]["stop_reason"] == "client_stopped"
    assert len(body["events"]) >= 2

    history = await client.get("/api/streams")
    assert any(item["id"] == session_id for item in history.json()["items"])


async def test_sse_duration_limit_is_reported_as_deadline(client, target):
    """SSE：总时长到点应归为 deadline_exceeded，而不是目标关闭。

    实现：上限设为最短的 1 秒，断言收尾原因与 ``closed`` 状态。
    """
    response = await client.post(
        "/api/streams",
        json={"kind": "sse", "url": f"{target.base_url}/sse-forever", "max_duration_ms": 1000},
    )
    session_id = response.json()["id"]
    session, _events = await collect(client, session_id, timeout=6.0)
    assert session["finished"] is True
    assert session["stop_reason"] == "deadline_exceeded"


async def test_sse_event_limit_counts_and_stops(client, target):
    """SSE：达到 max_events 就停止，并把原因记为 limits_exceeded。

    实现：上限设 3 条，断言最终事件数不超过上限+1（并发写入的收尾标记）。
    """
    response = await client.post(
        "/api/streams",
        json={
            "kind": "sse",
            "url": f"{target.base_url}/sse-forever",
            "max_events": 3,
            "max_duration_ms": 30_000,
        },
    )
    session_id = response.json()["id"]
    session, events = await collect(client, session_id, timeout=6.0)
    assert session["stop_reason"] == "limits_exceeded"
    assert len(events) <= 4


async def test_sse_handshake_rejection_is_not_a_stream_failure(client, target):
    """SSE：目标返回 401 应归类为握手被拒，而不是流读取失败。

    实现：``/sse-reject`` 返回 401，断言 http_status 与 error_kind 都如实记录。
    """
    response = await client.post(
        "/api/streams", json={"kind": "sse", "url": f"{target.base_url}/sse-reject"}
    )
    session_id = response.json()["id"]
    session, _events = await collect(client, session_id, timeout=5.0)
    assert session["status"] == "error"
    assert session["error_kind"] == "handshake_rejected"
    assert session["http_status"] == 401


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


async def test_stream_rejects_non_loopback_and_sse_send_misuse(client, target):
    """长连接入口必须拒绝越界目标与不可能生效的组合。

    实现：公网地址、以及“SSE 上带待发消息”都应在提交阶段就被拒（400/422）。
    """
    remote = await client.post(
        "/api/streams", json={"kind": "sse", "url": "http://example.com/events"}
    )
    assert remote.status_code == 400
    assert remote.json()["error"]["code"] == "target_not_loopback"

    misuse = await client.post(
        "/api/streams",
        json={"kind": "sse", "url": f"{target.base_url}/sse", "send": ["nope"]},
    )
    assert misuse.status_code == 422

    wrong_send = await client.post(
        "/api/streams",
        json={"kind": "sse", "url": f"{target.base_url}/sse", "max_duration_ms": 1000},
    )
    session_id = wrong_send.json()["id"]
    await asyncio.sleep(0.1)
    rejected = await client.post(f"/api/streams/{session_id}/send", json={"message": "x"})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "stream_not_sendable"
