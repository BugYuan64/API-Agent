import asyncio

import pytest
from websockets.asyncio.server import serve

from api_agent.infrastructure.database import Database
from api_agent.streams.models import StreamSessionRecord
from api_agent.streams.service import StreamManager, StreamStart


@pytest.fixture
async def manager(settings):
    database = Database(settings)
    await database.migrate_async()
    streams = StreamManager(database, settings)
    try:
        yield streams
    finally:
        await streams.shutdown()
        await database.dispose()


@pytest.mark.parametrize("mode", ["scheduled", "manual", "mixed"])
async def test_silent_websocket_sends_share_event_budget(manager, mode):
    received = []

    async def silent(connection):
        async for message in connection:
            received.append(message)

    async with serve(silent, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = await manager.start(
            StreamStart(
                kind="websocket",
                url=f"ws://127.0.0.1:{port}",
                max_events=3,
                send=["preset"] * 20 if mode != "manual" else [],
                send_interval_ms=0,
            ),
            [],
        )
        try:
            async with asyncio.timeout(3):
                while stream.status == "connecting":
                    await asyncio.sleep(0.001)
                if mode != "scheduled":
                    await asyncio.gather(
                        *(manager.send_text(stream.id, "manual") for _ in range(10)),
                        return_exceptions=True,
                    )
                while stream.sent_count < 2:
                    await asyncio.sleep(0.001)
                await asyncio.sleep(0.05)
            assert stream.event_count == 3
            assert stream.stop_reason == "limits_exceeded"
            assert stream.sent_count == 2
            assert len(received) == 2
            assert stream.byte_count == 0
            assert [event.sequence for event in stream.events] == [1, 2, 3]
        finally:
            await manager.stop(stream.id)


async def test_echo_and_manual_sends_cannot_overrun_shared_budget(manager):
    async def echo(connection):
        async for message in connection:
            await connection.send(message)

    async with serve(echo, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = await manager.start(
            StreamStart(kind="websocket", url=f"ws://127.0.0.1:{port}", max_events=4), []
        )
        try:
            async with asyncio.timeout(3):
                while stream.status == "connecting":
                    await asyncio.sleep(0.001)
                await asyncio.gather(
                    *(manager.send_text(stream.id, "message") for _ in range(10)),
                    return_exceptions=True,
                )
                await asyncio.sleep(0.05)
            assert stream.event_count == 4
            assert stream.stop_reason == "limits_exceeded"
            assert [event.kind for event in stream.events][0] == "open"
        finally:
            await manager.stop(stream.id)


async def test_restart_recovers_rows_and_history_detail_stop_agree(client, app):
    async with app.state.database.session() as session:
        records = [
            StreamSessionRecord(kind="sse", url="http://127.0.0.1:9", status=status)
            for status in ["connecting", "open", "closed"]
        ]
        session.add_all(records)
        await session.commit()
        first_id, second_id, terminal_id = [record.id for record in records]
    assert await app.state.streams.recover() == 2
    assert await app.state.streams.recover() == 0
    history = (await client.get("/api/streams")).json()["items"]
    by_id = {row["id"]: row for row in history}
    for session_id in [first_id, second_id]:
        detail = (await client.get(f"/api/streams/{session_id}")).json()
        stopped = await client.post(f"/api/streams/{session_id}/stop")
        assert stopped.status_code == 200
        assert detail["session"]["status"] == by_id[session_id]["status"] == "closed"
        assert detail["session"]["stop_reason"] == "recovered_after_restart"
        assert stopped.json() == detail["session"]
        assert detail["session"]["closed_at"] is not None
        assert detail["finished"] is True
    assert by_id[terminal_id]["stop_reason"] is None


async def test_stream_start_rejects_reserved_headers(manager):
    """长连接请求头必须与控制台共用同一份策略。

    回归来源：``StreamStart`` 此前只限制头的数量，``Host``、``Connection``、
    ``Sec-WebSocket-Key`` 等由传输层管理的头可被用户覆盖，改变握手语义。
    """
    import pytest
    from pydantic import ValidationError

    for reserved in ("Host", "Connection", "Sec-WebSocket-Key"):
        with pytest.raises(ValidationError):
            StreamStart(kind="websocket", url="ws://127.0.0.1:9/ws", headers={reserved: "x"})
    # 合法鉴权头仍然放行
    accepted = StreamStart(
        kind="sse", url="http://127.0.0.1:9/events", headers={"Authorization": "Bearer token"}
    )
    assert accepted.headers["Authorization"] == "Bearer token"
