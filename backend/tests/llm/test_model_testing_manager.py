"""Tests for ModelTestRepository, ModelTestManager, run routes, and model_testing tools."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from api_agent.assistant.thread_store import AssistantThreadRepository
from api_agent.infrastructure.config import load_settings
from api_agent.infrastructure.database import Database
from api_agent.llm.models import ModelTestRunRecord
from api_agent.llm.testing import (
    _probe_single_model,
    build_initial_profile,
    build_probe_observation,
    probe_single_model,
)
from api_agent.llm.testing_manager import (
    DEFAULT_AI_BADGE,
    ModelTestingConfigError,
    ModelTestManager,
    ModelTestRepository,
    resolve_testing_credentials,
)
from api_agent.llm.testing_schemas import (
    ModelTestAiSuiteSpec,
    ModelTestRunStartRequest,
)
from api_agent.llm.tools import (
    MODEL_TESTING_TOOL_DEFINITIONS,
    discover_model_testing_models,
    read_model_testing_sub_case,
    run_model_testing_ai_suite,
)


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        env_file=None,
        project_root=tmp_path,
        llm_mode="openai",
        llm_base_url="https://saved.example/v1",
        llm_api_key="fictional-saved-key-9999",
        llm_model="gpt-4o",
        llm_timeout_seconds=20,
    )


def _patch_testing_httpx(monkeypatch, handler):
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)


def _default_openai_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path.endswith("/models"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "acme/vision-pro",
                        "owned_by": "acme",
                        "created": 1730000000,
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                        "supported_parameters": ["tools"],
                    },
                    {"id": "gpt-4o", "owned_by": "openai"},
                ]
            },
        )
    payload = json.loads(request.content.decode("utf-8"))
    model = payload.get("model")
    if payload.get("stream") is True:
        sse = (
            'data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"K"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            content=sse.encode("utf-8"),
            headers={"Content-Type": "text/event-stream"},
        )
    if payload.get("tools"):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "probe_ping",
                                        "arguments": '{"echo":"ok"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
    messages = payload.get("messages") or []
    content = messages[0].get("content") if messages else None
    if isinstance(content, list):
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )
    if model == "echo-secret-fail":
        auth = request.headers.get("Authorization") or ""
        return httpx.Response(401, json={"error": {"message": f"Rejected credential: {auth}"}})
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
    )


async def test_resolve_testing_credentials_rules(settings):
    async with Database(settings) as db:
        async with db.session() as session:
            # 1. Default saved base_url + same-origin saved key reuse
            url, key, used_saved, has_custom = await resolve_testing_credentials(
                session,
                settings,
                base_url=None,
                api_key=None,
                use_saved_key=True,
            )
            assert url == "https://saved.example/v1"
            assert key == "fictional-saved-key-9999"
            assert used_saved is True
            assert has_custom is False

            # 2. Cross-origin strictly blocks saved key reuse even when use_saved_key=True
            url2, key2, used_saved2, has_custom2 = await resolve_testing_credentials(
                session,
                settings,
                base_url="https://other.example/v1",
                api_key=None,
                use_saved_key=True,
            )
            assert url2 == "https://other.example/v1"
            assert key2 == ""
            assert used_saved2 is False
            assert has_custom2 is False

            # 3. Custom ephemeral key overrides saved key
            url3, key3, used_saved3, has_custom3 = await resolve_testing_credentials(
                session,
                settings,
                base_url="https://other.example/v1/chat/completions",
                api_key="  ephemeral-custom-key-42  ",
                use_saved_key=True,
            )
            assert url3 == "https://other.example/v1"
            assert key3 == "ephemeral-custom-key-42"
            assert used_saved3 is False
            assert has_custom3 is True

            # 4. use_saved_key=False omits saved key on same-origin too
            url4, key4, used_saved4, has_custom4 = await resolve_testing_credentials(
                session,
                settings,
                base_url="https://saved.example/v1",
                api_key=None,
                use_saved_key=False,
            )
            assert url4 == "https://saved.example/v1"
            assert key4 == ""
            assert used_saved4 is False
            assert has_custom4 is False

            # 5. Invalid URL raises ModelTestingConfigError without FastAPI dependency
            with pytest.raises(ModelTestingConfigError) as exc_info:
                await resolve_testing_credentials(
                    session,
                    settings,
                    base_url="not-a-valid-url",
                    api_key=None,
                    use_saved_key=False,
                )
            assert exc_info.value.code == "invalid_base_url"


async def test_probe_single_model_preserves_initial_profile_and_builds_observation():
    initial = build_initial_profile(
        {
            "id": "acme/custom-reasoning-x",
            "owned_by": "acme-corp",
            "created": 1730000123,
            "type": "reasoning",
            "context_length": 131072,
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text", "reasoning"],
            },
            "supported_parameters": ["tools"],
        }
    )
    assert initial is not None
    assert _probe_single_model is probe_single_model

    async with httpx.AsyncClient(transport=httpx.MockTransport(_default_openai_handler)) as client:
        probed = await probe_single_model(
            client,
            "https://probe.example/v1",
            {"Content-Type": "application/json"},
            "acme/custom-reasoning-x",
            {"availability", "streaming"},
            "openai",
            initial_profile=initial,
        )

    # Initial profile metadata must be preserved rather than discarded.
    assert probed.owned_by == "acme-corp"
    assert probed.created == 1730000123
    assert probed.model_type == "reasoning"
    assert probed.type_source == "metadata"
    assert probed.raw_metadata["context_length"] == 131072
    assert probed.tool_calling.status == "inferred"
    assert probed.availability.status == "available"
    assert probed.streaming.status == "supported"
    assert probed.streaming.ttft_ms is not None
    # Original initial profile must not be mutated.
    assert initial.availability.status == "untested"

    observation = build_probe_observation(
        probed,
        base_url="https://probe.example/v1",
        protocol="openai",
        checks=["availability", "streaming"],
    )
    assert observation["protocol"] == "openai"
    assert observation["endpoint_path"] == "/chat/completions"
    assert observation["checks_executed"] == ["availability", "streaming"]
    assert observation["latency_ms"] is not None
    assert observation["ttft_ms"] is not None
    assert observation["capability_summary"]["availability"] == "available"
    assert observation["capability_summary"]["streaming"] == "supported"


async def test_manual_background_run_survives_observer_disconnect_and_replays_sub_case(
    app, client, monkeypatch
):
    gate = asyncio.Event()
    first_seen = asyncio.Event()

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        if payload.get("model") == "acme/vision-pro":
            first_seen.set()
            await gate.wait()
        return _default_openai_handler(request)

    _patch_testing_httpx(monkeypatch, slow_handler)

    discovered = build_initial_profile(
        {
            "id": "acme/vision-pro",
            "owned_by": "acme-lab",
            "context_length": 65536,
            "architecture": {"input_modalities": ["text", "image"]},
        }
    )
    assert discovered is not None

    start_resp = await client.post(
        "/api/model-testing/runs",
        json={
            "base_url": "https://probe.example/v1",
            "api_key": "ephemeral-manual-secret-1234",
            "models": ["acme/vision-pro", "gpt-4o"],
            "checks": ["availability", "tool_calling"],
            "concurrency": 2,
            "discovered_profiles": [discovered.model_dump(mode="json")],
        },
    )
    assert start_resp.status_code == 202
    assert "ephemeral-manual-secret-1234" not in start_resp.text
    run_id = start_resp.json()["id"]
    assert start_resp.headers["Location"] == f"/api/model-testing/runs/{run_id}"
    assert start_resp.json()["source"] == "manual"
    assert start_resp.json()["badge"] is None
    assert start_resp.json()["has_custom_api_key"] is True
    assert start_resp.json()["used_saved_key"] is False

    await asyncio.wait_for(first_seen.wait(), timeout=2.0)

    # Active runs endpoint exposes the in-flight run for cross-page reattach.
    active_resp = await client.get("/api/model-testing/runs/active")
    assert active_resp.status_code == 200
    assert any(item["id"] == run_id for item in active_resp.json()["items"])

    # Deleting an active run must fail with 409 model_test_run_active.
    del_active = await client.delete(f"/api/model-testing/runs/{run_id}")
    assert del_active.status_code == 409
    assert del_active.json()["error"]["code"] == "model_test_run_active"

    # Subscribe to live stream, consume the first snapshot frame, then disconnect early!
    manager: ModelTestManager = app.state.model_tests
    sub = manager.subscribe(run_id)
    first_frame = await anext(sub)
    assert first_frame["snapshot"] is True
    assert first_frame["run_id"] == run_id
    await sub.aclose()

    # Background worker MUST still be running after observer disconnect!
    assert manager.is_active(run_id) is True

    # Release the gate and await background completion.
    gate.set()
    completed = await manager.wait_for_completion(run_id)
    assert completed["status"] == "completed"
    assert manager.is_active(run_id) is False
    assert manager.active_runs() == []

    # Reconnecting to /live after completion yields a single done replay frame.
    live_after = await client.get(f"/api/model-testing/runs/{run_id}/live")
    assert live_after.status_code == 200
    assert "ephemeral-manual-secret-1234" not in live_after.text
    frames = [json.loads(line) for line in live_after.text.splitlines() if line.strip()]
    assert len(frames) == 1
    assert frames[0]["type"] == "done"
    assert frames[0]["status"] == "completed"

    # Verify history list, full detail, and slash-containing sub-case replay endpoint.
    list_resp = await client.get("/api/model-testing/runs")
    assert list_resp.status_code == 200
    assert list_resp.json()["page"]["total"] == 1
    assert list_resp.json()["items"][0]["id"] == run_id

    detail_resp = await client.get(f"/api/model-testing/runs/{run_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "completed"
    by_id = {p["id"]: p for p in detail["models_snapshot"]}
    assert by_id["acme/vision-pro"]["owned_by"] == "acme-lab"
    assert by_id["acme/vision-pro"]["raw_metadata"]["context_length"] == 65536

    case_resp = await client.get(f"/api/model-testing/runs/{run_id}/cases/r1:acme/vision-pro")
    assert case_resp.status_code == 200
    sub_case = case_resp.json()
    assert sub_case["case_id"] == "r1:acme/vision-pro"
    assert sub_case["model_id"] == "acme/vision-pro"
    assert sub_case["observation"]["endpoint_path"] == "/chat/completions"
    assert sub_case["request_snapshot"]["has_custom_api_key"] is True
    assert "api_key" not in sub_case["request_snapshot"]
    assert "ephemeral-manual-secret-1234" not in case_resp.text


async def test_ai_multi_round_suite_cooldown_single_annotated_run_and_zero_plaintext_key(
    settings, monkeypatch
):
    _patch_testing_httpx(monkeypatch, _default_openai_handler)
    ephemeral_secret = "ephemeral-ultra-secret-token-778899"

    async with Database(settings) as db:
        manager = ModelTestManager(db, settings)
        case_links: list[dict] = []
        observed_stages: list[str] = []

        spec = ModelTestAiSuiteSpec(
            base_url="https://ai-probe.example/v1",
            api_key=ephemeral_secret,
            use_saved_key=False,
            models=["gpt-4o", "echo-secret-fail"],
            checks=["availability", "tool_calling"],
            rounds=2,
            cooldown_ms=100,
            concurrency=2,
            title="AI 稳定性双轮探测",
        )

        started_at = time.monotonic()
        async with db.session() as session:
            initial = await manager.start_ai_multi_round_run(
                session,
                spec,
                on_case_done=lambda link: case_links.append(link),
            )

        async for frame in manager.subscribe(initial["id"]):
            observed_stages.append(frame["type"])

        elapsed_ms = (time.monotonic() - started_at) * 1000
        final = await manager.wait_for_completion(initial["id"])

        assert elapsed_ms >= 80
        assert "cooling_down" in observed_stages
        assert observed_stages[-1] == "done"

        # Single persisted run annotated with AI badge and 2 rounds × 2 models = 4 sub-cases.
        runs, total = await manager.repo.list_runs()
        assert total == 1
        assert len(runs) == 1
        assert final["source"] == "ai"
        assert final["badge"] == DEFAULT_AI_BADGE
        assert final["total_rounds"] == 2
        assert final["completed_rounds"] == 2
        assert final["cooldown_ms"] == 100
        assert len(final["rounds"]) == 2
        assert len(final["sub_cases"]) == 4

        expected_case_ids = {
            "r1:gpt-4o",
            "r1:echo-secret-fail",
            "r2:gpt-4o",
            "r2:echo-secret-fail",
        }
        assert {sc["case_id"] for sc in final["sub_cases"]} == expected_case_ids
        assert {link["case_id"] for link in case_links} == expected_case_ids
        for link in case_links:
            assert link["type"] == "case_link"
            assert link["scope"] == "model_testing"
            assert link["history_id"] == final["id"]
            assert link["tone"] in {"success", "warning", "danger", "neutral"}
            if link["case_id"].endswith(":echo-secret-fail"):
                assert link["tone"] == "danger"
            elif link["case_id"].endswith(":gpt-4o"):
                assert link["tone"] == "success"

        # Verify ZERO plaintext api_key anywhere in raw SQLite storage!
        conn = sqlite3.connect(settings.database_path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info('model_test_runs')")]
            assert "api_key" not in cols
            rows = conn.execute("SELECT * FROM model_test_runs").fetchall()
            raw_dump = json.dumps(rows, ensure_ascii=False, default=str)
            assert ephemeral_secret not in raw_dump
            assert "fictional-saved-key-9999" not in raw_dump
        finally:
            conn.close()

        assert manager.active == {}
        assert manager.live == {}


async def test_cancel_during_cooldown_stops_promptly_and_preserves_round_one(
    app, client, monkeypatch
):
    _patch_testing_httpx(monkeypatch, _default_openai_handler)

    start_resp = await client.post(
        "/api/model-testing/runs",
        json={
            "base_url": "https://cooldown.example/v1",
            "api_key": "cooldown-ephemeral-key",
            "models": ["gpt-4o"],
            "checks": ["availability"],
            "rounds": 3,
            "cooldown_ms": 5000,
        },
    )
    assert start_resp.status_code == 202
    run_id = start_resp.json()["id"]
    manager: ModelTestManager = app.state.model_tests

    # Wait until round 1 completes and manager enters cooling_down.
    async def wait_for_cooldown() -> None:
        async for frame in manager.subscribe(run_id):
            if frame["type"] == "cooling_down":
                return

    await asyncio.wait_for(wait_for_cooldown(), timeout=3.0)

    t0 = time.monotonic()
    cancel_resp = await client.post(f"/api/model-testing/runs/{run_id}/cancel")
    cancel_elapsed = time.monotonic() - t0
    assert cancel_resp.status_code == 200
    assert cancel_elapsed < 1.5
    cancelled = cancel_resp.json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["completed_rounds"] == 1
    assert [sc["case_id"] for sc in cancelled["sub_cases"]] == ["r1:gpt-4o"]
    assert manager.active == {}
    assert manager.live == {}


async def test_delete_and_clear_cascade_assistant_threads(app, client, monkeypatch):
    _patch_testing_httpx(monkeypatch, _default_openai_handler)
    threads = AssistantThreadRepository(app.state.database)
    app.state.assistant_threads = threads

    r1_resp = await client.post(
        "/api/model-testing/runs",
        json={
            "base_url": "https://del.example/v1",
            "models": ["gpt-4o"],
            "checks": ["availability"],
        },
    )
    r2_resp = await client.post(
        "/api/model-testing/runs",
        json={
            "base_url": "https://del.example/v1",
            "models": ["gpt-4o"],
            "checks": ["availability"],
        },
    )
    run_1 = r1_resp.json()["id"]
    run_2 = r2_resp.json()["id"]

    manager: ModelTestManager = app.state.model_tests
    await manager.wait_for_completion(run_1)
    await manager.wait_for_completion(run_2)

    await threads.append_message("model_testing", run_1, "user", "测试线程 1")
    await threads.append_message("model_testing", run_2, "user", "测试线程 2")
    await threads.append_message("console", run_2, "user", "其他作用域线程应保留")

    # Single delete cascades only run_1's model_testing thread.
    del_resp = await client.delete(f"/api/model-testing/runs/{run_1}")
    assert del_resp.status_code == 204
    assert await threads.get_thread("model_testing", run_1) is None
    assert await threads.get_thread("model_testing", run_2) is not None
    assert (await client.get(f"/api/model-testing/runs/{run_1}")).status_code == 404

    # Clear all terminal runs cascades model_testing scope threads without touching console scope.
    clear_resp = await client.delete("/api/model-testing/runs")
    assert clear_resp.status_code == 204
    assert (await client.get("/api/model-testing/runs")).json()["page"]["total"] == 0
    assert await threads.get_thread("model_testing", run_2) is None
    assert await threads.get_thread("console", run_2) is not None


async def test_recover_and_shutdown_lifecycle(settings, monkeypatch):
    blocker = asyncio.Event()
    entered = asyncio.Event()

    async def blocking_handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await blocker.wait()
        return _default_openai_handler(request)

    _patch_testing_httpx(monkeypatch, blocking_handler)

    async with Database(settings) as db:
        # Insert leftover queued & running rows simulating a hard crash.
        async with db.session() as session:
            session.add_all(
                [
                    ModelTestRunRecord(
                        id="stale-queued-1",
                        base_url="https://stale.example/v1",
                        status="queued",
                    ),
                    ModelTestRunRecord(
                        id="stale-running-1",
                        base_url="https://stale.example/v1",
                        status="running",
                    ),
                    ModelTestRunRecord(
                        id="done-ok-1",
                        base_url="https://stale.example/v1",
                        status="completed",
                    ),
                ]
            )
            await session.commit()

        manager = ModelTestManager(db, settings)
        recovered = await manager.recover()
        assert recovered == 2
        assert await manager.recover() == 0

        assert (await manager.repo.get_run("stale-queued-1"))["status"] == "interrupted"
        assert (await manager.repo.get_run("stale-running-1"))["status"] == "interrupted"
        assert (await manager.repo.get_run("done-ok-1"))["status"] == "completed"

        # Start an active background run, then verify shutdown cancels and recycles it cleanly.
        async with db.session() as session:
            started = await manager.start_manual_run(
                session,
                ModelTestRunStartRequest(
                    base_url="https://shutdown.example/v1",
                    api_key="shutdown-ephemeral-secret",
                    models=["gpt-4o"],
                    checks=["availability"],
                ),
            )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert manager.is_active(started["id"]) is True

        await manager.shutdown()
        assert manager.active == {}
        assert manager.live == {}
        after = await manager.repo.get_run(started["id"])
        assert after is not None
        assert after["status"] == "cancelled"


async def test_model_testing_ai_tools_discover_suite_and_read_sub_case(settings, monkeypatch):
    _patch_testing_httpx(monkeypatch, _default_openai_handler)

    tool_names = [tool.function.name for tool in MODEL_TESTING_TOOL_DEFINITIONS]
    assert tool_names == [
        "discover_model_testing_models",
        "run_model_testing_ai_suite",
        "read_model_testing_sub_case",
    ]

    async with Database(settings) as db:
        manager = ModelTestManager(db, settings)
        repo = ModelTestRepository(db)

        # 1. Discover tool
        async with db.session() as session:
            disc = await discover_model_testing_models(
                session,
                settings,
                {"base_url": "https://saved.example/v1", "use_saved_key": True},
            )
            assert disc["ok"] is True
            assert disc["count"] == 2
            assert disc["used_saved_key"] is True
            assert "fictional-saved-key-9999" not in json.dumps(disc, ensure_ascii=False)

            bad_disc = await discover_model_testing_models(
                session, settings, {"base_url": "not-a-url"}
            )
            assert bad_disc["ok"] is False
            assert bad_disc["error"] == "invalid_base_url"

        # 2. Multi-round AI suite tool (cooldown clamp, hooks, and case_links)
        events_order: list[str] = []
        bound_payloads: list[dict] = []
        emitted_links: list[dict] = []

        async def on_run_bound(info: dict) -> None:
            events_order.append("bound")
            bound_payloads.append(info)

        async def on_case_done(link: dict) -> None:
            events_order.append(f"case:{link['case_id']}")
            emitted_links.append(link)

        async with db.session() as session:
            suite_res = await run_model_testing_ai_suite(
                manager,
                session,
                {
                    "base_url": "https://saved.example/v1",
                    "use_saved_key": True,
                    "models": ["gpt-4o"],
                    "checks": ["availability", "streaming"],
                    "rounds": 2,
                    "cooldown_ms": 50,
                    "title": "工具套件测试",
                },
                on_case_done=on_case_done,
                on_run_bound=on_run_bound,
            )

        assert suite_res["ok"] is True
        assert suite_res["scope"] == "model_testing"
        assert suite_res["source"] == "ai"
        assert suite_res["badge"] == DEFAULT_AI_BADGE
        assert suite_res["cooldown_ms"] == 50
        assert suite_res["total_rounds"] == 2
        assert suite_res["completed_rounds"] == 2
        assert events_order[0] == "bound"
        assert len(bound_payloads) == 1
        assert bound_payloads[0]["history_id"] == suite_res["run_id"]
        assert len(emitted_links) == 2
        assert [link["case_id"] for link in suite_res["case_links"]] == [
            "r1:gpt-4o",
            "r2:gpt-4o",
        ]
        assert all(link["type"] == "case_link" for link in suite_res["case_links"])

        # Verify cooldown_ms out-of-range values are clamped cleanly (default 500, max 60000).
        async with db.session() as session:
            clamped = await run_model_testing_ai_suite(
                manager,
                session,
                {
                    "base_url": "https://saved.example/v1",
                    "models": ["gpt-4o"],
                    "checks": ["availability"],
                    "rounds": 1,
                    "cooldown_ms": 999_999,
                },
            )
            assert clamped["ok"] is True
            assert clamped["cooldown_ms"] == 60_000

        # 3. Read sub-case tool
        sub_res = await read_model_testing_sub_case(repo, suite_res["run_id"], "r2:gpt-4o")
        assert sub_res["ok"] is True
        assert sub_res["sub_case"]["case_id"] == "r2:gpt-4o"
        assert sub_res["sub_case"]["round_index"] == 2
        assert sub_res["sub_case"]["observation"]["endpoint_path"] == "/chat/completions"

        missing = await read_model_testing_sub_case(repo, suite_res["run_id"], "r9:missing")
        assert missing["ok"] is False
        assert missing["error"] == "not_found"
