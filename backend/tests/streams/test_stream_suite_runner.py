"""Tests for non-polluting WebSocket probes, multi-round AI suites, replay, and tools."""

import asyncio
import json
import time

import pytest
from sqlalchemy import func, select
from support.local_server import LocalServer

from api_agent.assistant.thread_store import AssistantThreadRepository
from api_agent.streams.client import WebSocketTestClient
from api_agent.streams.models import StreamEventRecord, StreamSessionRecord
from api_agent.streams.runner import StreamSuiteRunner
from api_agent.streams.schemas import StreamStart
from api_agent.streams.tools import (
    WEBSOCKET_TOOL_DEFINITIONS,
    StreamAiCaseSpec,
    StreamAiSuiteSpec,
    read_websocket_sub_case,
    run_websocket_ai_suite,
)


@pytest.fixture
async def target():
    """启动真实回环 WebSocket 服务供多轮套件与工具测试使用。"""
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


async def test_websocket_test_client_default_does_not_pollute_stream_sessions(app, client, target):
    """WebSocketTestClient.execute 默认不写 stream_sessions，避免内部探针污染历史列表。"""
    ws_client = WebSocketTestClient(app.state.streams, [])
    result = await ws_client.execute(
        StreamStart(
            url=f"{target.ws_url}/ws-close",
            send=["probe-1"],
            max_duration_ms=5_000,
        )
    )
    assert result["session"]["opened_at"] is not None
    assert result["session"]["stop_reason"] == "target_closed"
    assert result["session"]["close_code"] == 1000
    assert any(event["data"] == "bye" for event in result["events"])

    # 顶层历史接口、数据库表与内存 history 均不能残留内部探针记录。
    assert (await client.get("/api/streams")).json()["items"] == []
    assert app.state.streams.active == {}
    assert app.state.streams.history == {}
    async with app.state.database.session() as session:
        count = await session.scalar(select(func.count()).select_from(StreamSessionRecord))
        event_count = await session.scalar(select(func.count()).select_from(StreamEventRecord))
    assert count == 0
    assert event_count == 0


async def test_multi_round_ai_suite_persists_one_parent_and_hidden_children(app, client, target):
    """多轮 AI WebSocket 套件只生成一条带「AI 测试」徽标的顶层历史，并支持子用例只读回放。"""
    runner = StreamSuiteRunner(app.state.streams)
    app.state.stream_suites = runner
    callbacks: list[dict] = []

    spec = StreamAiSuiteSpec(
        url=f"{target.ws_url}/ws",
        title="多轮回显与关闭验证",
        headers={"Authorization": "Bearer ws-suite-secret-token", "X-Suite": "yes"},
        rounds=2,
        cooldown_ms=100,
        cases=[
            StreamAiCaseSpec(
                label="回显测试",
                send=["hello-ws"],
                send_interval_ms=0,
                max_events=5,
                max_duration_ms=5_000,
                expect_contains="echo:hello-ws",
                expect_min_events=3,
            ),
            StreamAiCaseSpec(
                label="正常关闭测试",
                url=f"{target.ws_url}/ws-close",
                max_events=10,
                max_duration_ms=5_000,
                expect_contains="bye",
                expect_close_code=1000,
            ),
        ],
    )

    started_at = time.monotonic()
    started = await runner.start_ai_suite(spec, on_case_done=callbacks.append)
    parent_id = started["id"]
    final = await runner.wait_for_completion(parent_id)
    elapsed = time.monotonic() - started_at

    # 4 次执行之间有 3 次 100ms 冷却，总耗时应体现真实冷却，且全部 socket 已回收。
    assert elapsed >= 0.25
    assert final["status"] == "completed"
    assert final["badge"] == "AI 测试"
    assert final["completed_cases"] == 4
    assert final["passed_cases"] == 4
    assert final["failed_cases"] == 0
    assert final["error_cases"] == 0
    assert len(callbacks) == 4
    assert len(final["case_links"]) == 4
    assert all(link["type"] == "case_link" and link["scope"] == "websocket" for link in callbacks)
    assert app.state.streams.active == {}
    assert runner.active == {}

    # 顶层历史只出现 1 条父记录，子用例完全隐藏不污染列表。
    history = (await client.get("/api/streams")).json()
    assert history["page"]["total"] == 1
    assert len(history["items"]) == 1
    parent_row = history["items"][0]
    assert parent_row["id"] == parent_id
    assert parent_row["source"] == "ai"
    assert parent_row["title"] == "多轮回显与关闭验证"
    assert parent_row["badge"] == "AI 测试"
    assert parent_row["total_rounds"] == 2
    assert parent_row["cooldown_ms"] == 100
    assert parent_row["sub_case_count"] == 4
    assert parent_row["summary"]
    assert parent_row["request_headers"] == {"Authorization": "[redacted]", "X-Suite": "yes"}
    assert "ws-suite-secret-token" not in json.dumps(parent_row, ensure_ascii=False)

    # include_children=True 可见全部 5 行（1 父 + 4 子）。
    all_rows, all_total = await app.state.streams.repo.list_sessions(include_children=True)
    assert all_total == 5 and len(all_rows) == 5

    # 详情接口暴露相同分组元数据与子用例计数。
    detail = (await client.get(f"/api/streams/{parent_id}")).json()
    assert detail["finished"] is True
    assert detail["session"]["badge"] == "AI 测试"
    assert detail["session"]["sub_case_count"] == 4
    assert detail["session"]["total_rounds"] == 2

    # 子用例列表按 round_index, case_index 排序。
    cases_resp = await client.get(f"/api/streams/{parent_id}/cases")
    assert cases_resp.status_code == 200
    cases_body = cases_resp.json()
    assert cases_body["total"] == 4
    assert [(c["round_index"], c["case_index"]) for c in cases_body["items"]] == [
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
    ]

    # 单条子用例只读回放快照包含脱敏输入参数与完整收发事件流。
    first_case_id = cases_body["items"][0]["id"]
    replay_resp = await client.get(f"/api/streams/{parent_id}/cases/{first_case_id}")
    assert replay_resp.status_code == 200
    replay = replay_resp.json()
    assert replay["readonly"] is True
    assert replay["session_id"] == parent_id
    assert replay["case_id"] == first_case_id
    assert replay["inputs"]["scheduled_messages"] == ["hello-ws"]
    assert replay["inputs"]["request_headers"]["Authorization"] == "[redacted]"
    assert any(ev["data"] == "echo:hello-ws" for ev in replay["events"])

    # 跨父 ID 访问子用例必须 404，防止越权串读。
    assert (await client.get(f"/api/streams/other-parent/cases/{first_case_id}")).status_code == 404


async def test_observer_disconnect_active_reattach_and_delete_cascade(app, client, target):
    """观察者断线不取消后台套件；支持 /active 查询、/live 重连与删除级联清理助手线程。"""
    runner = StreamSuiteRunner(app.state.streams)
    app.state.stream_suites = runner
    threads = AssistantThreadRepository(app.state.database)
    app.state.assistant_threads = threads

    spec = StreamAiSuiteSpec(
        url=f"{target.ws_url}/ws-close",
        title="断线重连测试套件",
        rounds=2,
        cooldown_ms=250,
        cases=[StreamAiCaseSpec(label="快速关闭探测", max_duration_ms=5_000)],
    )
    started = await runner.start_ai_suite(spec)
    suite_id = started["id"]

    # 模拟前端切页导致首个观察流中途断开。
    observer = runner.observe(suite_id)
    first_frame = json.loads(await anext(observer))
    assert first_frame["type"] == "progress"
    assert first_frame["suite_id"] == suite_id
    await observer.aclose()

    # 后台套件必须仍在运行，且暴露于 /api/streams/active，且删除被 409 stream_active 拒绝。
    assert runner.is_active(suite_id)
    active_resp = await client.get("/api/streams/active")
    assert active_resp.status_code == 200
    active_data = active_resp.json()
    assert any(
        item["id"] == suite_id and item["kind"] == "ai_suite" for item in active_data["items"]
    )
    assert any(item["id"] == suite_id for item in active_data["ai_suites"])

    delete_active = await client.delete(f"/api/streams/{suite_id}")
    assert delete_active.status_code == 409
    assert delete_active.json()["error"]["code"] == "stream_active"

    # 通过 HTTP /live 重新接入，能完整收到冷却/子用例/终态帧。
    live_resp = await client.get(f"/api/streams/suites/{suite_id}/live")
    assert live_resp.status_code == 200
    frames = [json.loads(line) for line in live_resp.text.splitlines() if line.strip()]
    frame_types = [frame["type"] for frame in frames]
    assert "case_done" in frame_types
    assert frame_types[-1] == "done"
    assert frames[-1]["status"] == "completed"
    assert frames[-1]["completed_cases"] == 2

    # 绑定一个助手线程后删除该套件，断言子用例、事件与助手线程全部级联清除。
    await threads.append_message("websocket", suite_id, "user", "帮我复盘这次 WebSocket 测试")
    assert await threads.get_thread("websocket", suite_id) is not None

    deleted = await client.delete(f"/api/streams/{suite_id}")
    assert deleted.status_code == 204
    assert await threads.get_thread("websocket", suite_id) is None
    assert await app.state.streams.repo.list_sub_cases(suite_id) == []
    assert (await client.get(f"/api/streams/{suite_id}")).status_code == 404


async def test_suite_cancel_during_cooldown_stops_promptly(app, client, target):
    """在长冷却期调用取消接口应立即中断冷却、标记 cancelled 并回收资源。"""
    runner = StreamSuiteRunner(app.state.streams)
    app.state.stream_suites = runner
    spec = StreamAiSuiteSpec(
        url=f"{target.ws_url}/ws-close",
        title="冷却期取消验证",
        rounds=3,
        cooldown_ms=10_000,
        cases=[StreamAiCaseSpec(label="单次探测", max_duration_ms=5_000)],
    )
    started = await runner.start_ai_suite(spec)
    suite_id = started["id"]

    # 等待第 1 轮完成并进入 cooling_down 阶段。
    async with asyncio.timeout(5):
        while True:
            live = runner.live.get(suite_id)
            if live is not None and live.stage == "cooling_down":
                break
            await asyncio.sleep(0.01)

    t0 = time.monotonic()
    cancel_resp = await client.post(f"/api/streams/suites/{suite_id}/cancel")
    elapsed = time.monotonic() - t0

    assert cancel_resp.status_code == 200
    assert elapsed < 1.5
    body = cancel_resp.json()
    assert body["status"] == "cancelled"
    assert body["completed_cases"] == 1
    assert runner.active == {}
    assert runner.live == {}
    assert app.state.streams.active == {}

    stored = await app.state.streams.repo.get_session(suite_id)
    assert stored is not None
    assert stored.status == "cancelled"
    assert stored.stop_reason == "client_stopped"
    assert len(await app.state.streams.repo.list_sub_cases(suite_id)) == 1


async def test_suite_recover_and_shutdown(app, target):
    """recover 把重启遗留 running 套件标为 interrupted；shutdown 回收全部运行中套件。"""
    runner = StreamSuiteRunner(app.state.streams)
    orphan = await app.state.streams.repo.create_parent_suite(
        session_id="orphan-suite-id",
        url=f"{target.ws_url}/ws",
        origin=target.ws_url,
        title="崩溃遗留套件",
    )
    assert orphan.status == "running"
    assert await runner.recover() == 1
    assert await runner.recover() == 0
    recovered = await app.state.streams.repo.get_session("orphan-suite-id")
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert recovered.stop_reason == "recovered_after_restart"

    started = await runner.start_ai_suite(
        StreamAiSuiteSpec(
            url=f"{target.ws_url}/ws-close",
            rounds=3,
            cooldown_ms=10_000,
            cases=[StreamAiCaseSpec(label="关闭前用例", max_duration_ms=5_000)],
        )
    )
    async with asyncio.timeout(5):
        while True:
            live = runner.live.get(started["id"])
            if live is not None and live.stage == "cooling_down":
                break
            await asyncio.sleep(0.01)
    await runner.shutdown()
    assert runner.active == {}
    final_row = await app.state.streams.repo.get_session(started["id"])
    assert final_row is not None
    assert final_row.status == "cancelled"


async def test_websocket_tools_run_suite_and_read_sub_case(app, target):
    """验证 streams/tools.py 导出的定义、冷却收敛、回调绑定与子用例快照读取。"""
    tool_names = [tool.function.name for tool in WEBSOCKET_TOOL_DEFINITIONS]
    assert tool_names == ["run_websocket_ai_suite", "read_websocket_sub_case"]

    runner = StreamSuiteRunner(app.state.streams)
    bound_events: list[dict] = []
    case_events: list[dict] = []

    async def on_bound(payload: dict) -> None:
        bound_events.append(payload)

    async def on_case(link: dict) -> None:
        case_events.append(link)

    result = await run_websocket_ai_suite(
        runner,
        {
            "url": f"{target.ws_url}/ws",
            "title": "工具调用多轮测试",
            "headers": {"Authorization": "Bearer tool-secret-value"},
            "rounds": 1,
            # 超出上限的值应被收敛到 60000，这里传负数验证下限收敛到 0。
            "cooldown_ms": -50,
            "cases": [
                {
                    "label": "回显断言通过",
                    "messages": ["tool-ping"],
                    "send_interval_ms": 0,
                    "max_events": 5,
                    "max_duration_ms": 5_000,
                    "expect_contains": "echo:tool-ping",
                },
                {
                    "label": "握手拒绝边界",
                    "url": f"{target.ws_url}/ws-reject",
                    "max_duration_ms": 5_000,
                    "expect_handshake_reject": True,
                },
            ],
        },
        on_run_bound=on_bound,
        on_case_done=on_case,
    )

    assert len(bound_events) == 1
    assert bound_events[0]["scope"] == "websocket"
    assert bound_events[0]["history_id"] == result["history_id"]
    assert bound_events[0]["cooldown_ms"] == 0

    assert result["status"] == "completed"
    assert result["badge"] == "AI 测试"
    assert result["cooldown_ms"] == 0
    assert result["passed_cases"] == 2
    assert result["failed_cases"] == 0
    assert len(result["case_links"]) == 2
    assert len(case_events) == 2

    first_case_id = result["case_links"][0]["case_id"]
    snapshot = await read_websocket_sub_case(
        app.state.streams.repo, result["history_id"], first_case_id
    )
    assert snapshot["found"] is True
    assert snapshot["readonly"] is True
    assert snapshot["inputs"]["request_headers"]["Authorization"] == "[redacted]"
    assert any(event["data"] == "echo:tool-ping" for event in snapshot["events"])

    missing = await read_websocket_sub_case(
        app.state.streams.repo, result["history_id"], "missing-case"
    )
    assert missing["found"] is False

    # 验证未传 cooldown_ms 时继承 settings_context 中的 cooldownMs，且默认回落 500。
    inherited = await run_websocket_ai_suite(
        runner,
        {
            "url": f"{target.ws_url}/ws-close",
            "cases": [{"label": "单轮继承冷却", "max_duration_ms": 5_000}],
        },
        settings_context={"cooldownMs": 120},
    )
    assert inherited["cooldown_ms"] == 120
    default_cd = await run_websocket_ai_suite(
        runner,
        {
            "url": f"{target.ws_url}/ws-close",
            "cases": [{"label": "默认 500ms 冷却", "max_duration_ms": 5_000}],
        },
    )
    assert default_cd["cooldown_ms"] == 500
