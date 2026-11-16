"""A failed transaction must not acknowledge or discard timeline events."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from api_agent.infrastructure.database import Database
from api_agent.streams.models import StreamEventRecord
from api_agent.streams.service import ActiveStream, StreamManager, StreamStart


@pytest.mark.parametrize("stage", ["transport", "flush"])
async def test_shutdown_awaits_terminal_stream_still_finalizing(settings, monkeypatch, stage):
    database = Database(settings)
    await database.migrate_async()
    manager = StreamManager(database, settings)
    finalizing = asyncio.Event()
    release = asyncio.Event()

    async def terminal_but_still_owns_resources(stream, payload, url):
        stream.finish("closed", "target_closed")
        if stage == "transport":
            finalizing.set()
            await release.wait()

    original_flush = manager._flush

    async def delayed_flush(stream, *, final=False):
        if stage == "flush" and final:
            finalizing.set()
            await release.wait()
        await original_flush(stream, final=final)

    monkeypatch.setattr(manager, "_run_websocket", terminal_but_still_owns_resources)
    monkeypatch.setattr(manager, "_flush", delayed_flush)
    stream = await manager.start(StreamStart(kind="websocket", url="http://127.0.0.1:9"), [])
    shutdown = None
    try:
        await asyncio.wait_for(finalizing.wait(), 2)
        shutdown = asyncio.create_task(manager.shutdown())
        done, _ = await asyncio.wait({shutdown}, timeout=0.02)
        assert not done, "shutdown must wait for resource teardown, not merely terminal status"
        release.set()
        await asyncio.wait_for(shutdown, 2)
        assert stream.task.done(), "terminal state is not proof of completed teardown"
        assert stream.id not in manager.active
        assert stream.stop_reason == "target_closed"
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        await stream.task
        await database.dispose()


async def test_failed_flush_retries_original_events_before_new_events(settings, monkeypatch):
    database = Database(settings)
    await database.migrate_async()
    manager = StreamManager(database, settings)
    stream = ActiveStream(id="fixture-stream", kind="websocket", url="http://127.0.0.1:9")
    await manager._persist_new(stream, StreamStart(kind="websocket", url=stream.url), stream.url)
    stream.append("message", "first")
    original = database.session

    @asynccontextmanager
    async def failed_session():
        # An event arriving during a failed write must also survive.
        stream.append("message", "second")
        raise OSError("fixture unavailable database")
        yield  # pragma: no cover

    try:
        monkeypatch.setattr(database, "session", failed_session)
        await manager._flush(stream)
        monkeypatch.setattr(database, "session", original)
        stream.append("message", "third")
        await manager._flush(stream, final=True)
        await manager._flush(stream, final=True)
        async with database.session() as session:
            rows = (
                await session.scalars(
                    select(StreamEventRecord).order_by(StreamEventRecord.sequence)
                )
            ).all()
        assert [row.data for row in rows] == ["first", "second", "third"]
        assert [row.sequence for row in rows] == [1, 2, 3]
        assert not stream.pending
    finally:
        await database.dispose()
