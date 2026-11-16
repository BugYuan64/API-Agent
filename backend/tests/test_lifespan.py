"""Application ownership must cover partial startup and exceptional teardown."""

import pytest

from api_agent.agent.manager import AgentManager
from api_agent.infrastructure.database import Database
from api_agent.main import create_app
from api_agent.streams.models import StreamSessionRecord
from api_agent.streams.service import StreamManager


async def test_startup_recovers_orphaned_streams(settings):
    database = Database(settings)
    await database.migrate_async()
    async with database.session() as session:
        row = StreamSessionRecord(kind="sse", url="http://127.0.0.1:9", status="open")
        session.add(row)
        await session.commit()
        session_id = row.id
    await database.dispose()

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with app.state.database.session() as session:
            recovered = await session.get(StreamSessionRecord, session_id)
            assert recovered.status == "closed"
            assert recovered.stop_reason == "recovered_after_restart"
            assert recovered.closed_at is not None


async def test_startup_failure_disposes_database(settings, monkeypatch):
    disposed = []

    async def broken_migration(self):
        raise RuntimeError("fixture migration failure")

    async def dispose(self):
        disposed.append(self)

    monkeypatch.setattr(Database, "migrate_async", broken_migration)
    monkeypatch.setattr(Database, "dispose", dispose)
    app = create_app(settings)
    with pytest.raises(RuntimeError, match="fixture migration failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup should fail")
    assert len(disposed) == 1


async def test_shutdown_failure_still_closes_other_resources(settings, monkeypatch):
    app = create_app(settings)
    closed = []

    async def fail():
        raise RuntimeError("fixture agent shutdown failure")

    async def streams():
        closed.append("streams")

    async def database():
        closed.append("database")

    # Inject before acquisition so registered cleanup callbacks see the failure.
    for owner, name, replacement in (
        (AgentManager, "shutdown", fail),
        (StreamManager, "shutdown", streams),
        (Database, "dispose", database),
    ):
        original = getattr(owner, name)

        async def cleanup(self, original=original, replacement=replacement):
            await original(self)
            await replacement()

        monkeypatch.setattr(owner, name, cleanup)
    with pytest.raises(RuntimeError, match="fixture agent shutdown failure"):
        async with app.router.lifespan_context(app):
            pass
    assert closed == ["streams", "database"]
