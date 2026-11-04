from datetime import timedelta

import pytest

from api_agent.infrastructure.database import Database
from api_agent.infrastructure.orm import utc_now
from api_agent.projects.models import Project
from api_agent.runs.models import Run, RunSnapshot
from api_agent.runs.repository import RunRepository
from api_agent.runs.worker import RunWorker


@pytest.fixture
async def running_run(settings):
    database = Database(settings)
    await database.migrate_async()
    async with database.session() as session:
        project = Project(name="Cancel race")
        session.add(project)
        await session.flush()
        snapshot = RunSnapshot(
            project_id=project.id,
            actions=[{"name": "must-not-start", "duration_ms": 1, "outcome": "completed"}],
            resource_limits={"max_actions": 1},
        )
        run = Run(
            snapshot=snapshot,
            status="running",
            action_count=1,
            deadline=utc_now() + timedelta(seconds=30),
        )
        session.add(run)
        await session.commit()
        run_id = run.id
    try:
        yield database, run_id
    finally:
        await database.dispose()


async def test_cancel_committed_after_run_read_prevents_dispatch(running_run):
    database, run_id = running_run
    async with database.session() as stale:
        repository = RunRepository(stale)
        run = await repository.get(run_id)
        async with database.session() as cancelling:
            await RunRepository(cancelling).request_cancel(run_id)
        attempt = await repository.begin_attempt(run, run.snapshot.actions[0], 1)
        assert attempt is None
    async with database.session() as session:
        repository = RunRepository(session)
        assert await repository.list_attempts(run_id) == []
        assert await repository.list_budget(run_id) == []


async def test_worker_handles_cancel_at_dispatch_boundary_as_cancelled(running_run, monkeypatch):
    database, run_id = running_run
    original = RunRepository.begin_attempt

    async def cancel_before_attempt(repository, run, action, limit):
        async with database.session() as session:
            await RunRepository(session).request_cancel(run_id)
        return await original(repository, run, action, limit)

    monkeypatch.setattr(RunRepository, "begin_attempt", cancel_before_attempt)
    await RunWorker(database).execute(run_id)
    async with database.session() as session:
        repository = RunRepository(session)
        run = await repository.get(run_id)
        assert run.status == "cancelled"
        assert run.stop_reason == "cancelled"
        assert run.current_action == 0
        assert await repository.list_attempts(run_id) == []
        assert await repository.list_budget(run_id) == []
