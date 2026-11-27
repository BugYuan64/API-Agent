"""Tests for ConsoleRepository, ConsoleRunManager, sub-case replay APIs, and console AI tools."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from support.local_server import LocalServer

from api_agent.assistant import AssistantThreadRepository
from api_agent.console.repository import ConsoleRepository
from api_agent.console.runner import ConsoleRunManager
from api_agent.console.service import ConsoleRequest
from api_agent.console.tools import (
    CONSOLE_TOOL_DEFINITIONS,
    ConsoleAiCaseSpec,
    ConsoleAiSuiteSpec,
    read_console_sub_case,
    run_console_ai_suite,
)
from api_agent.infrastructure.database import Database


@pytest.fixture
async def target():
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


async def test_console_repository_crud_sub_cases_sanitization_and_recover(settings):
    async with Database(settings) as database:
        repo = ConsoleRepository(database)
        secret_pw = "repo-secret-password-9999"
        secret_tok = "repo-header-token-8888"

        created = await repo.create_run(
            method="POST",
            url="http://127.0.0.1:9100/login?token=url-secret-1111",
            body_mode="json",
            request_headers={"Authorization": f"Bearer {secret_tok}", "X-Trace": "trace-repo"},
            request_body=json.dumps({"password": secret_pw, "padding": "a" * 9000}),
            source="ai",
            title="AI 边界测试 Bearer title-secret-2222",
            badge="AI 测试",
            status="running",
            total_rounds=2,
            cooldown_ms=100,
        )
        run_id = created["id"]
        assert created["source"] == "ai"
        assert created["badge"] == "AI 测试"
        assert created["status"] == "running"
        assert created["total_rounds"] == 2
        assert created["cooldown_ms"] == 100
        assert secret_pw not in (created["request_body"] or "")
        assert "title-secret-2222" not in (created["title"] or "")

        sub1 = await repo.append_sub_case(
            run_id,
            {
                "round_index": 2,
                "case_index": 0,
                "label": "第二轮用例",
                "method": "GET",
                "url": "http://127.0.0.1:9100/items",
                "request_headers": {"X-Trace": "t2"},
                "body_mode": "none",
                "ok": True,
                "status_code": 200,
                "elapsed_ms": 14.6,
                "response_headers": {"X-Trace": ["r2"]},
                "response_preview": '{"ok":true}',
                "response_bytes": 11,
                "http_version": "HTTP/1.1",
                "response_reason": "OK",
            },
        )
        sub0 = await repo.append_sub_case(
            run_id,
            {
                "round_index": 1,
                "case_index": 0,
                "label": "第一轮敏感用例",
                "method": "POST",
                "url": "http://127.0.0.1:9100/login?api_key=sub-url-secret",
                "request_headers": {"Authorization": f"Bearer {secret_tok}", "X-Trace": "t1"},
                "body_mode": "json",
                "request_body": json.dumps({"padding": "x" * 9000, "password": secret_pw}),
                "ok": True,
                "status_code": 200,
                "elapsed_ms": 22,
                "response_headers": {
                    "Set-Cookie": ["sid=sub-cookie-secret; HttpOnly"],
                    "X-Trace": ["r1"],
                },
                "full_response": json.dumps({"echo": secret_pw, "padding": "y" * 9000}),
                "response_bytes": 9040,
                "http_version": "HTTP/1.1",
                "response_reason": "OK",
                "redirects": [{"status": 302, "url": "http://127.0.0.1:9100/next"}],
                "sse": {
                    "events": [{"event": "message", "data": "ok"}],
                    "stop_reason": "target_closed",
                },
                "assertion_summary": {
                    "passed": True,
                    "checks": [{"kind": "status", "passed": True}],
                },
            },
        )
        assert sub1["elapsed_ms"] == 15

        finalized = await repo.finalize_run(
            run_id,
            status="completed",
            ok=True,
            status_code=200,
            elapsed_ms=123.4,
            response_headers={"X-Trace": ["resp-trace"]},
            response_preview='{"done":true}',
            response_bytes=13,
            http_version="HTTP/1.1",
            response_reason="OK",
            redirects_json=[{"status": 302, "url": "http://127.0.0.1:9100/final"}],
            sse_json={"events": [], "stop_reason": "target_closed"},
            summary="全部通过",
        )
        assert finalized["status"] == "completed"
        assert finalized["summary"] == "全部通过"
        assert finalized["http_version"] == "HTTP/1.1"
        assert finalized["response_reason"] == "OK"
        assert len(finalized["redirects"]) == 1
        assert finalized["sse"] == {"events": [], "stop_reason": "target_closed"}
        # Sub-cases must be ordered by (round_index, case_index).
        assert [c["id"] for c in finalized["sub_cases"]] == [sub0["id"], sub1["id"]]

        items, total = await repo.list_history(limit=10, offset=0)
        assert total == 1
        assert items[0]["id"] == run_id
        assert items[0]["sub_case_count"] == 2
        assert items[0]["badge"] == "AI 测试"
        assert "request_body" not in items[0]

        replayed = await repo.get_sub_case(run_id, sub0["id"])
        assert replayed is not None
        dump = json.dumps(replayed, default=str)
        for secret in (secret_pw, secret_tok, "sub-url-secret", "sub-cookie-secret"):
            assert secret not in dump
        assert replayed["request_headers"]["X-Trace"] == "t1"
        assert isinstance(replayed["response_headers"]["Set-Cookie"], list)
        assert replayed["assertion_summary"]["passed"] is True
        assert await repo.get_sub_case("wrong-parent", sub0["id"]) is None

        # recover() only marks running/queued records, leaving completed untouched.
        stale = await repo.create_run(
            method="GET",
            url="http://127.0.0.1:9100/stale",
            status="running",
        )
        stale_queued = await repo.create_run(
            method="GET",
            url="http://127.0.0.1:9100/queued",
            status="queued",
        )
        assert await repo.recover() == 2
        recovered = await repo.get_detail(stale["id"])
        recovered_q = await repo.get_detail(stale_queued["id"])
        untouched = await repo.get_detail(run_id)
        assert recovered is not None and recovered["status"] == "interrupted"
        assert recovered["error_kind"] == "recovered_after_restart"
        assert recovered_q is not None and recovered_q["status"] == "interrupted"
        assert untouched is not None and untouched["status"] == "completed"

        # delete() removes parent and cascades sub-cases.
        assert await repo.delete(run_id) is True
        assert await repo.delete(run_id) is False
        assert await repo.get_detail(run_id) is None
        assert await repo.get_sub_case(run_id, sub0["id"]) is None

        assert await repo.clear() == 2
        assert (await repo.list_history())[1] == 0


async def test_background_single_run_survives_observer_disconnect_and_reattaches(app, client):
    upstream = FastAPI()
    first_sent = asyncio.Event()
    proceed = asyncio.Event()
    finished = asyncio.Event()

    @upstream.post("/sse")
    async def stream():
        async def events():
            yield b"data: part-1\n\n"
            first_sent.set()
            await proceed.wait()
            yield b"data: part-2\n\n"
            finished.set()

        return StreamingResponse(events(), media_type="text/event-stream")

    target_server = await LocalServer(upstream).start()
    management = LocalServer(app)
    try:
        await management.start()

        start_resp = await client.post(
            "/api/console/runs",
            json={"url": target_server.base_url + "/sse", "method": "POST"},
        )
        assert start_resp.status_code == 202, start_resp.text
        run_id = start_resp.json()["id"]
        assert start_resp.json()["status"] == "running"

        await asyncio.wait_for(first_sent.wait(), timeout=3)

        # Active runs endpoint lists the in-flight run, and DELETE rejects with 409.
        active = await client.get("/api/console/runs/active")
        assert active.status_code == 200
        assert any(item["id"] == run_id for item in active.json()["items"])

        delete_active = await client.delete(f"/api/console/history/{run_id}")
        assert delete_active.status_code == 409
        assert delete_active.json()["error"]["code"] == "console_run_active"

        # Observer #1 connects, reads sse_start + part-1, then disconnects mid-stream!
        async with (
            httpx.AsyncClient(base_url=management.base_url, trust_env=False) as browser,
            browser.stream("GET", f"/api/console/runs/{run_id}/live") as resp1,
        ):
            assert resp1.status_code == 200
            lines = resp1.aiter_lines()
            assert json.loads(await anext(lines)) == {"type": "sse_start"}
            evt1 = json.loads(await anext(lines))
            assert evt1 == {"type": "sse_event", "event": {"event": "message", "data": "part-1"}}
            # Intentionally close stream without waiting for completion!

        await asyncio.sleep(0.05)
        # Worker MUST still be alive in background!
        assert run_id in app.state.console_runs.active
        assert not finished.is_set()

        # Observer #2 reconnects mid-run: replays past frames, then receives part-2 & result.
        async with (
            httpx.AsyncClient(base_url=management.base_url, trust_env=False) as browser,
            browser.stream("GET", f"/api/console/runs/{run_id}/live") as resp2,
        ):
            lines2 = resp2.aiter_lines()
            assert json.loads(await anext(lines2)) == {"type": "sse_start"}
            assert json.loads(await anext(lines2))["event"]["data"] == "part-1"
            proceed.set()
            assert json.loads(await anext(lines2))["event"]["data"] == "part-2"
            final = json.loads(await anext(lines2))
            assert final["type"] == "result"
            assert final["result"]["history_id"] == run_id
            assert len(final["result"]["response"]["sse"]["events"]) == 2

        await asyncio.wait_for(finished.wait(), timeout=3)
        await app.state.console_runs.wait_for_completion(run_id)
        # Active & live maps must be cleanly recycled!
        assert run_id not in app.state.console_runs.active
        assert run_id not in app.state.console_runs.live

        detail = (await client.get(f"/api/console/history/{run_id}")).json()
        assert detail["status"] == "completed"
        assert detail["ok"] is True
        assert detail["http_version"] == "HTTP/1.1"
        assert detail["response_reason"] == "OK"
        assert len(detail["sse"]["events"]) == 2

        # Re-observing after completion returns terminal frame from DB.
        post_live = await client.get(f"/api/console/runs/{run_id}/live")
        assert post_live.status_code == 200
        terminal = json.loads(post_live.text.splitlines()[0])
        assert terminal["type"] == "done"
        assert terminal["status"] == "completed"
    finally:
        proceed.set()
        if getattr(app.state, "console_runs", None) is not None:
            await app.state.console_runs.shutdown()
        await management.stop()
        await target_server.stop()


async def test_legacy_live_endpoint_survives_observer_disconnect(app, client):
    upstream = FastAPI()
    first_sent = asyncio.Event()
    proceed = asyncio.Event()
    done = asyncio.Event()

    @upstream.post("/sse")
    async def stream():
        async def events():
            yield b"data: hello\n\n"
            first_sent.set()
            await proceed.wait()
            yield b"data: world\n\n"
            done.set()

        return StreamingResponse(events(), media_type="text/event-stream")

    target_server = await LocalServer(upstream).start()
    management = LocalServer(app)
    try:
        await management.start()
        run_id = None
        async with (
            httpx.AsyncClient(base_url=management.base_url, trust_env=False) as browser,
            browser.stream(
                "POST",
                "/api/console/requests/live",
                json={"url": target_server.base_url + "/sse", "method": "POST"},
            ) as response,
        ):
            assert response.status_code == 200
            run_id = response.headers.get("x-console-run-id")
            assert run_id
            lines = response.aiter_lines()
            assert json.loads(await anext(lines)) == {"type": "sse_start"}
            assert json.loads(await anext(lines))["event"]["data"] == "hello"
            # Disconnect before upstream finishes!

        await asyncio.sleep(0.05)
        assert run_id in app.state.console_runs.active
        proceed.set()
        await asyncio.wait_for(done.wait(), timeout=3)
        detail = await app.state.console_runs.wait_for_completion(run_id)
        assert detail["status"] == "completed"
        assert len(detail["sse"]["events"]) == 2
        assert run_id not in app.state.console_runs.active
        assert run_id not in app.state.console_runs.live

        history = (await client.get("/api/console/history")).json()["items"]
        assert len(history) == 1
        assert history[0]["id"] == run_id
    finally:
        proceed.set()
        if getattr(app.state, "console_runs", None) is not None:
            await app.state.console_runs.shutdown()
        await management.stop()
        await target_server.stop()


async def test_ai_multi_round_suite_with_cooldown_persists_one_parent_and_sub_cases(
    app, client, target
):
    runs = ConsoleRunManager(app.state.database, app.state.settings)
    app.state.console_runs = runs
    case_callbacks: list[dict] = []

    async def on_case(link: dict) -> None:
        case_callbacks.append(link)

    spec = ConsoleAiSuiteSpec(
        title="回声接口边界回归",
        url=target.base_url,
        rounds=2,
        cooldown_ms=100,
        headers={"Authorization": "Bearer suite-secret-token", "X-Suite": "keep-me"},
        cases=[
            ConsoleAiCaseSpec(
                label="正常 JSON",
                method="GET",
                url="/json",
                expected_status=200,
                expect_contains="ok",
            ),
            ConsoleAiCaseSpec(
                label="带体回显",
                method="POST",
                url="/echo?mode=ai",
                body_mode="json",
                body='{"password":"case-secret-pw","hello":"world"}',
                expected_status=[200, 201],
            ),
        ],
    )

    started_at = time.monotonic()
    run_info = await runs.start_ai_suite(spec, on_case_done=on_case)
    run_id = run_info["id"]
    assert run_info["source"] == "ai"
    assert run_info["badge"] == "AI 测试"
    assert run_info["total_rounds"] == 2
    assert run_info["total_cases"] == 4

    observed_types: list[str] = []
    async for raw_line in runs.observe(run_id):
        frame = json.loads(raw_line.decode("utf-8"))
        observed_types.append(frame["type"])

    elapsed = time.monotonic() - started_at
    # 4 executions -> 3 cooldowns of 100ms each.
    assert elapsed >= 0.25
    assert "case_done" in observed_types
    assert "cooling_down" in observed_types
    assert observed_types[-1] == "done"
    assert len(case_callbacks) == 4
    assert all(cb["type"] == "case_link" and cb["scope"] == "console" for cb in case_callbacks)
    assert run_id not in runs.active
    assert run_id not in runs.live

    # Top-level history contains ONLY ONE annotated parent entry!
    history = (await client.get("/api/console/history")).json()
    assert history["page"]["total"] == 1
    parent = history["items"][0]
    assert parent["id"] == run_id
    assert parent["source"] == "ai"
    assert parent["badge"] == "AI 测试"
    assert parent["title"] == "回声接口边界回归"
    assert parent["status"] == "completed"
    assert parent["total_rounds"] == 2
    assert parent["cooldown_ms"] == 100
    assert parent["sub_case_count"] == 4

    detail = (await client.get(f"/api/console/history/{run_id}")).json()
    assert len(detail["sub_cases"]) == 4
    assert [(c["round_index"], c["case_index"]) for c in detail["sub_cases"]] == [
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
    ]
    assert detail["summary"] and "4 条用例" in detail["summary"]

    # Sub-case replay endpoint returns full sanitized snapshot.
    echo_case_id = detail["sub_cases"][1]["id"]
    replay_resp = await client.get(f"/api/console/history/{run_id}/cases/{echo_case_id}")
    assert replay_resp.status_code == 200, replay_resp.text
    replay = replay_resp.json()
    assert replay["id"] == echo_case_id
    assert replay["parent_id"] == run_id
    assert replay["method"] == "POST"
    assert replay["status_code"] == 200
    assert replay["assertion_summary"]["passed"] is True
    assert replay["request_headers"]["X-Suite"] == "keep-me"
    serialized = json.dumps(replay)
    assert "suite-secret-token" not in serialized
    assert "case-secret-pw" not in serialized

    missing = await client.get(f"/api/console/history/{run_id}/cases/nonexistent")
    assert missing.status_code == 404


async def test_cancel_ai_suite_during_cooldown_is_prompt_and_preserves_completed_cases(
    app, client, target
):
    runs = ConsoleRunManager(app.state.database, app.state.settings)
    app.state.console_runs = runs
    first_done = asyncio.Event()

    def on_case(link: dict) -> None:
        first_done.set()

    spec = ConsoleAiSuiteSpec(
        title="长冷却取消测试",
        url=target.base_url + "/json",
        rounds=3,
        cooldown_ms=10_000,
        cases=[ConsoleAiCaseSpec(label="心跳探测", method="GET")],
    )
    run_info = await runs.start_ai_suite(spec, on_case_done=on_case)
    run_id = run_info["id"]

    await asyncio.wait_for(first_done.wait(), timeout=3)
    # Give worker a tick to enter cooling_down state.
    await asyncio.sleep(0.05)

    t0 = time.monotonic()
    cancel_resp = await client.post(f"/api/console/runs/{run_id}/cancel")
    cancel_duration = time.monotonic() - t0
    assert cancel_resp.status_code == 200, cancel_resp.text
    assert cancel_duration < 1.5
    cancelled = cancel_resp.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_kind"] == "cancelled"
    assert len(cancelled["sub_cases"]) == 1
    assert run_id not in runs.active
    assert run_id not in runs.live


async def test_shutdown_and_assistant_thread_cleanup_on_delete_and_clear(app, client, target):
    runs = ConsoleRunManager(app.state.database, app.state.settings)
    threads = AssistantThreadRepository(app.state.database)
    app.state.console_runs = runs
    app.state.assistant_threads = threads

    # Start a slow request and shut down the manager cleanly.
    slow_run = await runs.start_single(
        ConsoleRequest(method="GET", url=target.base_url + "/slow", timeout_ms=5000)
    )
    await asyncio.sleep(0.05)
    assert slow_run["id"] in runs.active
    await runs.shutdown()
    assert runs.active == {}
    assert runs.live == {}
    after_shutdown = await runs.repo.get_detail(slow_run["id"])
    assert after_shutdown is not None
    assert after_shutdown["status"] == "cancelled"

    # Create a second finished history item and bind assistant threads across two scopes.
    r2 = await client.post(
        "/api/console/requests",
        json={"method": "GET", "url": target.base_url + "/json"},
    )
    assert r2.status_code == 200
    second_id = r2.json()["history_id"]

    await threads.append_message("console", slow_run["id"], "user", "分析第一条")
    await threads.append_message("console", second_id, "user", "分析第二条")
    await threads.append_message("websocket", "ws-1", "user", "保留长连接对话")

    del_one = await client.delete(f"/api/console/history/{slow_run['id']}")
    assert del_one.status_code == 204
    assert await threads.get_thread("console", slow_run["id"]) is None
    assert await threads.get_thread("console", second_id) is not None
    assert await threads.get_thread("websocket", "ws-1") is not None

    del_all = await client.delete("/api/console/history")
    assert del_all.status_code == 204
    assert await threads.get_thread("console", second_id) is None
    assert await threads.get_thread("websocket", "ws-1") is not None


async def test_console_tools_run_ai_suite_clamping_hooks_and_read_sub_case(app, target):
    tool_names = [tool.function.name for tool in CONSOLE_TOOL_DEFINITIONS]
    assert tool_names == ["run_console_ai_suite", "read_console_sub_case"]

    runs = ConsoleRunManager(app.state.database, app.state.settings)
    bound_events: list[dict] = []
    case_events: list[dict] = []

    async def on_bound(payload: dict) -> None:
        bound_events.append(payload)

    async def on_case(link: dict) -> None:
        case_events.append(link)

    try:
        output = await run_console_ai_suite(
            runs,
            {
                "title": "工具驱动接口测试",
                "url": target.base_url,
                "cooldown_ms": -250,  # clamped to 0
                "rounds": 2,
                "cases": [
                    {"name": "健康检查", "method": "get", "url": "/json", "expected_status": 200},
                    {
                        "label": "提交体校验",
                        "method": "post",
                        "url": "/echo",
                        "body": {"k": "v"},
                        "expected_status": 200,
                    },
                ],
            },
            on_run_bound=on_bound,
            on_case_done=on_case,
        )
        # Tool output must be strictly JSON-serializable.
        encoded = json.dumps(output, ensure_ascii=False)
        assert encoded

        assert output["ok"] is True
        assert output["scope"] == "console"
        assert output["source"] == "ai"
        assert output["badge"] == "AI 测试"
        assert output["cooldown_ms"] == 0
        assert output["total_rounds"] == 2
        assert output["stats"] == {"total": 4, "passed": 4, "failed": 0, "error": 0}
        assert len(output["case_links"]) == 4
        assert len(bound_events) == 1
        assert bound_events[0]["history_id"] == output["history_id"]
        assert len(case_events) == 4

        first_case_id = output["case_links"][0]["case_id"]
        found = await read_console_sub_case(runs.repo, output["history_id"], first_case_id)
        assert json.dumps(found, ensure_ascii=False)
        assert found["found"] is True
        assert found["case"]["id"] == first_case_id
        assert found["case_link"]["case_id"] == first_case_id

        not_found = await read_console_sub_case(runs.repo, output["history_id"], "missing-case")
        assert not_found["found"] is False

        # Verify default cooldown fallback from settings_context and upper clamp at 60000.
        defaulted = await run_console_ai_suite(
            runs,
            {
                "url": target.base_url + "/json",
                "cases": [{"label": "单条", "method": "GET"}],
            },
            settings_context={"cooldownMs": 75},
        )
        assert defaulted["cooldown_ms"] == 75

        clamped_high = await run_console_ai_suite(
            runs,
            {
                "url": target.base_url + "/json",
                "cooldown_ms": 999_999,
                "cases": [{"label": "单条", "method": "GET"}],
            },
        )
        assert clamped_high["cooldown_ms"] == 60_000
    finally:
        await runs.shutdown()
