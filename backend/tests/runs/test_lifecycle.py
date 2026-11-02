import asyncio
from datetime import timedelta

import httpx
import pytest

from api_agent.infrastructure.api import ApiError
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.orm import new_id, utc_now
from api_agent.main import create_app
from api_agent.runs.models import BudgetLedger, DispatchAttempt, Run, RunSnapshot
from api_agent.runs.repository import RunRepository
from api_agent.runs.state_machine import check_transition
from api_agent.runs.worker import RunWorker


async def create_run(client, project_id, actions=None, limits=None):
    response = await client.post(
        "/api/runs",
        json={
            "project_id": project_id,
            "actions": actions if actions is not None else [{"name": "one"}, {"name": "two"}],
            "resource_limits": limits or {"max_actions": 10, "work_timeout_ms": 10_000},
        },
    )
    assert response.status_code == 202
    assert response.headers["location"] == f"/api/runs/{response.json()['id']}"
    return response.json()


async def wait_status(client, run_id, statuses, timeout=3):
    async with asyncio.timeout(timeout):
        while True:
            response = await client.get(f"/api/runs/{run_id}")
            if response.json()["status"] in statuses:
                return response.json()
            await asyncio.sleep(0.01)


async def test_simulation_run_returns_before_completion_and_saves_trace(client, project_id):
    run = await create_run(client, project_id, [{"name": "slow", "duration_ms": 100}])
    assert run["status"] == "queued"
    assert run["run_mode"] == "simulation"
    assert run["actual_llm_status"] == "not_applicable"
    result = await wait_status(client, run["id"], {"completed"})
    assert result["stop_reason"] == "completed"
    assert result["current_action"] == 1

    snapshot = (await client.get(f"/api/runs/{run['id']}/snapshot")).json()
    assert snapshot["actions"][0]["name"] == "slow"
    assert snapshot["specification_id"] is None
    events = (await client.get(f"/api/runs/{run['id']}/events")).json()
    assert [event["sequence"] for event in events["items"]] == [1, 2, 3]
    assert events["items"][1]["tool_name"] == "simulation_action"
    attempts = (await client.get(f"/api/runs/{run['id']}/attempts")).json()
    assert attempts[0]["status"] == "completed"
    budget = (await client.get(f"/api/runs/{run['id']}/budget")).json()
    assert [entry["status"] for entry in budget] == ["reserved", "consumed"]
    assert (await client.post(f"/api/runs/{run['id']}/cancel")).json()["status"] == "completed"


async def test_completed_actions_do_not_double_count_against_the_budget(client, project_id):
    # Two actions with a limit of two must both run: a finished action settles its
    # own reservation instead of counting reserved and consumed together.
    run = await create_run(
        client,
        project_id,
        [{"name": "one"}, {"name": "two"}],
        {"max_actions": 2, "work_timeout_ms": 10_000},
    )
    result = await wait_status(client, run["id"], {"completed"})
    assert result["stop_reason"] == "completed"
    assert result["current_action"] == 2
    ledger = (await client.get(f"/api/runs/{run['id']}/budget")).json()
    assert sorted(entry["status"] for entry in ledger) == [
        "consumed",
        "consumed",
        "reserved",
        "reserved",
    ]
    assert all(entry["related_id"] for entry in ledger)
    attempts = (await client.get(f"/api/runs/{run['id']}/attempts")).json()
    attempt_ids = [attempt["id"] for attempt in attempts]
    assert sorted(entry["related_id"] for entry in ledger) == sorted(attempt_ids * 2)


async def test_actions_over_the_declared_limit_are_rejected_at_creation(client, project_id):
    response = await client.post(
        "/api/runs",
        json={
            "project_id": project_id,
            "actions": [{"name": "one"}, {"name": "two"}, {"name": "three"}],
            "resource_limits": {"max_actions": 2, "work_timeout_ms": 10_000},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_empty_run_completes_and_failed_action_does_not_corrupt_events(client, project_id):
    empty = await create_run(client, project_id, [])
    assert (await wait_status(client, empty["id"], {"completed"}))["stop_reason"] == "completed"
    failed = await create_run(client, project_id, [{"name": "bad", "outcome": "failed"}])
    result = await wait_status(client, failed["id"], {"failed"})
    assert result["stop_reason"] == "workflow_error"
    assert result["workflow_error"]
    events = (await client.get(f"/api/runs/{failed['id']}/events")).json()
    assert events["items"][-1]["observation_summary"]["outcome"] == "failed"


async def test_running_cancellation_is_idempotent_and_does_not_start_next_action(
    client, project_id
):
    run = await create_run(
        client,
        project_id,
        [{"name": "wait", "duration_ms": 500}, {"name": "must-not-run"}],
    )
    await wait_status(client, run["id"], {"running"})
    first = await client.post(f"/api/runs/{run['id']}/cancel")
    assert first.json()["status"] == "cancelling"
    second = await client.post(f"/api/runs/{run['id']}/cancel")
    assert second.json()["status"] == "cancelling"
    result = await wait_status(client, run["id"], {"cancelled"})
    assert result["stop_reason"] == "cancelled"
    assert result["current_action"] == 1
    attempts = (await client.get(f"/api/runs/{run['id']}/attempts")).json()
    assert len(attempts) == 1


async def test_queued_cancellation_and_pagination(client, app, project_id):
    await app.state.worker.stop()
    run = await create_run(client, project_id)
    result = await client.post(f"/api/runs/{run['id']}/cancel")
    assert result.json()["status"] == "cancelled"
    assert result.json()["ended_at"] is not None
    assert (await client.post(f"/api/runs/{run['id']}/cancel")).json()["status"] == "cancelled"
    app.state.worker.start()
    await asyncio.sleep(0.1)
    assert (await client.get(f"/api/runs/{run['id']}")).json()["status"] == "cancelled"
    page = await client.get("/api/runs?limit=1&offset=0")
    assert page.json()["page"]["total"] == 1
    assert len(page.json()["items"]) == 1
    assert (await client.get("/api/runs/missing")).status_code == 404
    assert (await client.post("/api/runs", json={"project_id": "missing"})).status_code == 404


async def test_claim_is_atomic_and_terminal_transitions_are_rejected(settings):
    database = Database(settings)
    await database.migrate_async()
    async with database.session() as session:
        from api_agent.projects.models import Project

        project = Project(name="Claim")
        session.add(project)
        await session.flush()
        snapshot = RunSnapshot(project_id=project.id, actions=[], resource_limits={})
        snapshot.id = new_id()
        run = Run(snapshot_id=snapshot.id, action_count=0)
        session.add_all([snapshot, run])
        await session.commit()
        run_id = run.id
    try:
        async with database.session() as first, database.session() as second:
            results = await asyncio.gather(
                RunRepository(first).claim(run_id, utc_now() + timedelta(seconds=5)),
                RunRepository(second).claim(run_id, utc_now() + timedelta(seconds=5)),
            )
            assert sorted(results) == [False, True]
        for current, target in [
            ("queued", "completed"),
            ("completed", "running"),
            ("cancelled", "cancelling"),
            ("running", "interrupted"),
        ]:
            with pytest.raises(ApiError):
                check_transition(current, target)
        # The persisted update path must use the same table as check_transition:
        # cancelling may only end cancelled/failed, so completing it is rejected.
        # The persisted update path shares the transition table with check_transition:
        # the claim above left the run in `running`, so a contract-illegal target
        # updates no rows, and terminal states cannot be re-finished.
        async with database.session() as session:
            repository = RunRepository(session)
            assert not await repository.finish(run_id, "cancelled", "cancelled")
            assert await repository.finish(run_id, "failed", "workflow_error")
            assert not await repository.finish(run_id, "failed", "workflow_error")
            assert not await repository.finish(run_id, "completed", "completed")
        with pytest.raises(ApiError):
            async with database.session() as session:
                await RunRepository(session).finish(run_id, "interrupted", "interrupted")
    finally:
        await database.dispose()


async def test_restart_marks_dispatch_unknown_and_queued_runs_resume(settings):
    database = Database(settings)
    await database.migrate_async()
    async with database.session() as session:
        from api_agent.projects.models import Project

        project = Project(name="Recovery")
        session.add(project)
        await session.flush()
        snapshot = RunSnapshot(
            project_id=project.id,
            actions=[{"name": "in-flight", "duration_ms": 1}],
            resource_limits={"max_actions": 10, "work_timeout_ms": 1000},
        )
        interrupted = Run(snapshot=snapshot, status="running", action_count=1)
        session.add_all([snapshot, interrupted])
        await session.flush()
        attempt = DispatchAttempt(run_id=interrupted.id, sequence=1, status="dispatching")
        session.add(attempt)
        await session.flush()
        session.add(
            BudgetLedger(
                run_id=interrupted.id,
                resource="simulation_action",
                action="reserve",
                amount=1,
                status="reserved",
                related_id=attempt.id,
            )
        )
        queued_snapshot = RunSnapshot(project_id=project.id, actions=[], resource_limits={})
        queued = Run(snapshot=queued_snapshot, action_count=0)
        session.add_all([queued_snapshot, queued])
        await session.commit()
        interrupted_id, queued_id = interrupted.id, queued.id
    worker = RunWorker(database)
    assert await worker.recover() == 1
    async with database.session() as session:
        assert (await session.get(Run, interrupted_id)).status == "interrupted"
        assert (await session.get(DispatchAttempt, attempt.id)).status == "unknown"
        assert (await session.get(Run, queued_id)).status == "queued"
        ledger = await RunRepository(session).list_budget(interrupted_id)
        assert [row.status for row in ledger] == ["reserved", "consumed"]
    # A second recovery is idempotent and does not charge the same attempt again.
    assert await worker.recover() == 0
    async with database.session() as session:
        assert len(await RunRepository(session).list_budget(interrupted_id)) == 2
    worker.start()
    async with asyncio.timeout(2):
        while True:
            async with database.session() as session:
                if (await session.get(Run, queued_id)).status == "completed":
                    break
            await asyncio.sleep(0.01)
    await worker.stop()
    async with database.session() as session:
        assert (await session.get(Run, interrupted_id)).current_action == 0
        assert len(await RunRepository(session).list_attempts(interrupted_id)) == 1
    await database.dispose()


async def test_actual_application_restart_preserves_queued_run(client, app, settings, project_id):
    await app.state.worker.stop()
    run = await create_run(client, project_id, [{"name": "after-restart"}])
    await app.state.database.dispose()
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as new_client:
            result = await wait_status(new_client, run["id"], {"completed"})
            assert result["current_action"] == 1
    from api_agent.infrastructure.database import Database

    app.state.database = Database(settings)
    await app.state.database.migrate_async()
    from api_agent.runs.worker import RunWorker

    app.state.worker = RunWorker(app.state.database)
