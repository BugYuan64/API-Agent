"""Unit tests for Smart Group Testing AI tools (``api_agent.tasks.tools``)."""

from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError
from support.local_server import LocalServer

from api_agent.agent.service import AgentService
from api_agent.tasks.schemas import Catalog
from api_agent.tasks.service import DEFAULT_AI_BADGE
from api_agent.tasks.tools import (
    TASKS_TOOL_DEFINITIONS,
    TasksAiSuiteSpec,
    read_task_sub_case,
    resolve_cooldown_ms,
    resolve_tests_per_url,
    run_tasks_ai_suite,
)


@pytest.fixture
async def target():
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


class ScriptedPlanningAgent:
    """Deterministic planning double recording requested per-operation case counts."""

    def __init__(self, settings):
        self.settings = settings
        self.client = object()
        self.calls: list[dict] = []

    async def structured(self, stage, data, output_type, budget, validate=None):
        self.calls.append({"stage": stage, "data": data})
        if stage == "task_title":
            return output_type.model_validate({"title": "AI 规划测试集"})
        if stage == "task_cases":
            groups = []
            for operation in data["operations"]:
                remaining = operation["remaining_cases"]
                base_path = operation["path"].split("?", 1)[0]
                method = operation["method"]
                cases = []
                for idx in range(remaining):
                    if method == "POST":
                        cases.append(
                            {
                                "name": f"边界用例 {idx + 1}",
                                "path": base_path,
                                "body": {"seq": idx + 1, "tag": f"ai-{idx + 1}"},
                                "assertions": [{"kind": "status", "expected": 200}],
                            }
                        )
                    else:
                        cases.append(
                            {
                                "name": f"查询用例 {idx + 1}",
                                "path": f"{base_path}?ai_case={idx + 1}",
                                "assertions": [{"kind": "status", "expected": 200}],
                            }
                        )
                groups.append({"cases": cases})
            result = output_type.model_validate({"groups": groups})
            if validate:
                validate(result)
            return result
        raise AssertionError(f"Unexpected planning stage: {stage}")


def test_tool_definitions_and_spec_validation():
    names = [tool.function.name for tool in TASKS_TOOL_DEFINITIONS]
    assert names == ["run_tasks_ai_suite", "read_task_sub_case"]

    run_params = TASKS_TOOL_DEFINITIONS[0].function.parameters["properties"]
    assert "tests_per_url" not in run_params
    assert "testsPerUrl" not in run_params
    assert {"origin", "prompt_or_doc", "rounds", "cooldown_ms", "cases"} <= set(run_params)

    spec = TasksAiSuiteSpec(origin="  http://127.0.0.1:8080  ", prompt_or_doc="测试列表接口")
    assert spec.origin == "http://127.0.0.1:8080"
    assert spec.rounds == 1
    assert spec.cooldown_ms == 500
    assert spec.allow_writes is True
    assert spec.observation_ms == 15_000
    assert spec.wall_time_seconds == 600
    assert spec.cases is None

    # Extra tests_per_url from an AI payload is silently ignored, never stored.
    ignored = TasksAiSuiteSpec.model_validate(
        {"origin": "http://127.0.0.1:8080", "prompt_or_doc": "hi", "tests_per_url": 9}
    )
    assert not hasattr(ignored, "tests_per_url")

    with pytest.raises(ValidationError):
        TasksAiSuiteSpec(origin="   ", prompt_or_doc="x")
    with pytest.raises(ValidationError):
        TasksAiSuiteSpec(origin="http://127.0.0.1:8080", prompt_or_doc="x", rounds=21)
    with pytest.raises(ValidationError):
        TasksAiSuiteSpec(origin="http://127.0.0.1:8080", prompt_or_doc="x", cooldown_ms=70_000)


def test_settings_context_resolution_and_clamping():
    assert resolve_tests_per_url(None) == 3
    assert resolve_tests_per_url({}) == 3
    assert resolve_tests_per_url({"tests_per_url": 2}) == 2
    assert resolve_tests_per_url({"testsPerUrl": 5}) == 5
    assert resolve_tests_per_url({"tests_per_url": 0}) == 1
    assert resolve_tests_per_url({"tests_per_url": -4}) == 1
    assert resolve_tests_per_url({"tests_per_url": 99}) == 10
    assert resolve_tests_per_url({"tests_per_url": True}) == 3
    assert resolve_tests_per_url({"page": "tasks", "settings": {"tests_per_url": 4}}) == 4

    assert resolve_cooldown_ms({}, None) == 500
    assert resolve_cooldown_ms({"cooldown_ms": 0}, {"cooldown_ms": 900}) == 0
    assert resolve_cooldown_ms({"cooldown_ms": 120}, {"cooldown_ms": 900}) == 120
    assert resolve_cooldown_ms({}, {"cooldownMs": 250}) == 250
    assert resolve_cooldown_ms({}, {"settings": {"cooldown_ms": 0}}) == 0
    assert resolve_cooldown_ms({"cooldown_ms": 99_999}, None) == 60_000
    assert resolve_cooldown_ms({"cooldown_ms": -10}, None) == 0


async def test_run_tasks_ai_suite_enforces_tests_per_url_and_cannot_be_overridden(app, target):
    """AI cannot override settings_context.tests_per_url; planning obeys clamped page setting."""
    manager = app.state.tasks
    planning_agent = ScriptedPlanningAgent(app.state.settings)
    task_agent = AgentService(app.state.settings, None)

    doc_text = json.dumps(
        {
            "title": "回显文档",
            "endpoints": [
                {
                    "name": "获取 JSON",
                    "method": "GET",
                    "path": "/json",
                    "assertions": [{"kind": "status", "expected": 200}],
                },
                {
                    "name": "提交回显",
                    "method": "POST",
                    "path": "/echo",
                    "body": {"seed": 0},
                    "assertions": [{"kind": "status", "expected": 200}],
                },
            ],
        },
        ensure_ascii=False,
    )

    bound_events: list[dict] = []
    case_events: list[dict] = []

    async def on_run_bound(payload: dict) -> None:
        bound_events.append(payload)

    async def on_case_done(link: dict) -> None:
        case_events.append(link)

    outcome = await run_tasks_ai_suite(
        manager,
        planning_agent,
        task_agent,
        {
            "origin": target.base_url,
            "prompt_or_doc": doc_text,
            "tests_per_url": 10,  # Attempted AI override must be ignored!
            "instructions": "重点验证边界输入",
        },
        settings_context={"testsPerUrl": 2, "cooldownMs": 75},
        on_run_bound=on_run_bound,
        on_case_done=on_case_done,
    )

    # Planning stage must have requested exactly 2 generated cases per endpoint, not 10.
    case_gen_calls = [call for call in planning_agent.calls if call["stage"] == "task_cases"]
    assert len(case_gen_calls) == 1
    operations = case_gen_calls[0]["data"]["operations"]
    assert [op["remaining_cases"] for op in operations] == [2, 2]
    assert case_gen_calls[0]["data"]["instructions"] == "重点验证边界输入"

    # Exactly ONE annotated task persisted with source="ai", badge="AI 测试", tests_per_url=2.
    assert outcome["ok"] is True
    assert outcome["scope"] == "tasks"
    assert outcome["source"] == "ai"
    assert outcome["badge"] == DEFAULT_AI_BADGE == "AI 测试"
    assert outcome["tests_per_url"] == 2
    assert outcome["cooldown_ms"] == 75
    assert outcome["total_rounds"] == 1
    # 2 endpoints × (2 generated tests + 1 contract example) = 6 cases
    assert outcome["planned_cases_per_round"] == 6
    assert outcome["executed_cases"] == 6
    assert outcome["counts"]["passed"] == 6

    assert len(bound_events) == 1
    assert bound_events[0]["history_id"] == outcome["history_id"]
    assert bound_events[0]["source"] == "ai"
    assert bound_events[0]["badge"] == "AI 测试"
    assert bound_events[0]["tests_per_url"] == 2

    assert len(case_events) == 6
    assert case_events == outcome["case_links"]
    assert all(link["type"] == "case_link" and link["scope"] == "tasks" for link in case_events)
    assert all(link["tone"] == "success" and link["status"] == "passed" for link in case_events)

    listed = manager.list_tasks(limit=20)
    assert len(listed) == 1
    assert listed[0]["id"] == outcome["history_id"]
    assert listed[0]["source"] == "ai"
    assert listed[0]["badge"] == "AI 测试"
    assert listed[0]["tests_per_url"] == 2


async def test_run_tasks_ai_suite_multi_round_cooldown_and_callbacks(app, target):
    """Multi-round suite with cooldown_ms=50 streams CaseLinkBlock callbacks and binds first."""
    manager = app.state.tasks
    task_agent = AgentService(app.state.settings, None)
    event_order: list[tuple[str, str]] = []
    bound_payloads: list[dict] = []
    emitted_links: list[dict] = []

    async def on_run_bound(payload: dict) -> None:
        event_order.append(("bound", payload["history_id"]))
        bound_payloads.append(payload)

    async def on_case_done(link: dict) -> None:
        event_order.append(("case", link["case_id"]))
        emitted_links.append(link)

    started_at = time.monotonic()
    outcome = await run_tasks_ai_suite(
        manager,
        None,
        task_agent,
        {
            "origin": target.base_url,
            "prompt_or_doc": "多轮回归验证",
            "title": "多轮 AI 回归",
            "rounds": 2,
            "cooldown_ms": 50,
            "allow_writes": True,
            "cases": [
                {
                    "name": "健康 JSON 成功",
                    "method": "GET",
                    "path": "/json",
                    "assertions": [{"kind": "status", "expected": 200}],
                },
                {
                    "name": "故意断言失败",
                    "method": "POST",
                    "path": "/echo",
                    "body": {"round_probe": True},
                    "assertions": [{"kind": "status", "expected": 201}],
                },
            ],
        },
        settings_context={"tests_per_url": 3},
        on_run_bound=on_run_bound,
        on_case_done=on_case_done,
    )
    elapsed_ms = (time.monotonic() - started_at) * 1000

    assert elapsed_ms >= 40
    assert outcome["ok"] is True
    assert outcome["title"] == "多轮 AI 回归"
    assert outcome["source"] == "ai"
    assert outcome["badge"] == "AI 测试"
    assert outcome["total_rounds"] == 2
    assert outcome["cooldown_ms"] == 50
    assert outcome["executed_cases"] == 4
    assert outcome["counts"]["passed"] == 2
    assert outcome["counts"]["failed"] == 2

    # on_run_bound MUST fire strictly before any on_case_done callback.
    assert len(bound_payloads) == 1
    assert event_order[0] == ("bound", outcome["history_id"])
    assert [cid for kind, cid in event_order[1:] if kind == "case"] == [
        "r1:c0",
        "r1:c1",
        "r2:c0",
        "r2:c1",
    ]
    assert emitted_links == outcome["case_links"]

    for link in emitted_links:
        assert set(link.keys()) == {
            "type",
            "scope",
            "history_id",
            "case_id",
            "round",
            "label",
            "status",
            "tone",
        }
        assert link["type"] == "case_link"
        assert link["scope"] == "tasks"
        assert link["history_id"] == outcome["history_id"]

    assert [link["round"] for link in emitted_links] == [1, 1, 2, 2]
    assert [link["status"] for link in emitted_links] == ["passed", "failed", "passed", "failed"]
    assert [link["tone"] for link in emitted_links] == ["success", "danger", "success", "danger"]
    assert "第 1 轮" in emitted_links[0]["label"]
    assert "第 2 轮" in emitted_links[2]["label"]

    # Persisted as ONE task history entry containing all 4 sub-case results.
    assert len(manager.list_tasks(limit=20)) == 1
    stored = manager.get_task(outcome["history_id"])
    assert stored["source"] == "ai"
    assert stored["badge"] == "AI 测试"
    assert stored["total_rounds"] == 2
    assert stored["cooldown_ms"] == 50
    assert len(stored["results"]) == 4


async def test_run_tasks_ai_suite_caps_explicit_cases_by_tests_per_url(app, target):
    """Even when AI supplies explicit cases, per-URL count is strictly capped by tests_per_url."""
    manager = app.state.tasks
    outcome = await run_tasks_ai_suite(
        manager,
        None,
        None,  # Verify None task_agent gracefully falls back to unconfigured AgentService.
        {
            "origin": target.base_url,
            "prompt_or_doc": "显式多用例裁剪测试",
            "cases": [
                {"name": f"JSON #{i}", "method": "GET", "path": f"/json?i={i}"} for i in range(1, 5)
            ]
            + [{"name": "回显 #1", "method": "POST", "path": "/echo", "body": {"k": "v"}}],
        },
        settings_context={"tests_per_url": 2},
    )

    assert outcome["ok"] is True
    assert outcome["tests_per_url"] == 2
    # 2 kept for GET /json + 1 kept for POST /echo = 3 total
    assert outcome["planned_cases_per_round"] == 3
    assert outcome["executed_cases"] == 3
    assert any("tests_per_url=2" in warning for warning in outcome["warnings"])


async def test_run_tasks_ai_suite_with_base_catalog_and_sync_callbacks(app, target):
    """Supports refining a base_catalog, nested settings_context, and synchronous callbacks."""
    manager = app.state.tasks
    planning_agent = ScriptedPlanningAgent(app.state.settings)
    base_catalog = Catalog.model_validate(
        {
            "title": "草稿基线",
            "endpoints": [
                {
                    "name": "基础查询",
                    "method": "GET",
                    "path": "/json",
                    "assertions": [{"kind": "status", "expected": 200}],
                }
            ],
        }
    )

    seen_bound: list[str] = []
    seen_cases: list[str] = []

    outcome = await run_tasks_ai_suite(
        manager,
        planning_agent,
        AgentService(app.state.settings, None),
        {
            "origin": target.base_url,
            "prompt_or_doc": "补充边界查询参数",
            "rounds": 1,
        },
        settings_context={"page": "tasks", "settings": {"tests_per_url": 2, "cooldown_ms": 0}},
        base_catalog=base_catalog,
        on_run_bound=lambda payload: seen_bound.append(payload["history_id"]),
        on_case_done=lambda link: seen_cases.append(link["case_id"]),
    )

    assert outcome["ok"] is True
    assert outcome["tests_per_url"] == 2
    assert outcome["cooldown_ms"] == 0
    assert seen_bound == [outcome["history_id"]]
    assert len(seen_cases) == 3  # 2 generated + 1 contract example


async def test_read_task_sub_case_returns_request_assertions_evidence_and_handles_missing(
    app, target
):
    """read_task_sub_case supports case_id, round+case_index, numeric index, redaction & errors."""
    manager = app.state.tasks
    outcome = await run_tasks_ai_suite(
        manager,
        None,
        AgentService(app.state.settings, None),
        {
            "origin": target.base_url,
            "prompt_or_doc": "子用例只读快照验证",
            "title": "子用例回放任务",
            "rounds": 2,
            "cooldown_ms": 0,
            "cases": [
                {
                    "name": "读取 JSON",
                    "method": "GET",
                    "path": "/json",
                    "headers": {"Authorization": "Bearer super-secret-task-token"},
                    "assertions": [
                        {"kind": "status", "expected": 200},
                        {"kind": "json_equals", "pointer": "/ok", "expected": True},
                    ],
                },
                {
                    "name": "提交数据",
                    "method": "POST",
                    "path": "/echo",
                    "body": {"hello": "subcase"},
                    "assertions": [{"kind": "status", "expected": 200}],
                },
            ],
        },
        settings_context={"tests_per_url": 2},
    )
    task_id = outcome["history_id"]

    # 1) Lookup by canonical case_id ("r2:c1")
    snap = await read_task_sub_case(manager, task_id, "r2:c1")
    assert snap["ok"] is True
    assert snap["found"] is True
    assert snap["scope"] == "tasks"
    assert snap["history_id"] == task_id
    assert snap["task_title"] == "子用例回放任务"
    assert snap["source"] == "ai"
    assert snap["badge"] == "AI 测试"
    assert snap["case_id"] == "r2:c1"
    assert snap["round"] == 2
    assert snap["case_index"] == 1
    assert snap["status"] == "passed"
    assert snap["request"]["method"] == "POST"
    assert snap["request"]["path"] == "/echo"
    assert snap["request"]["url"] == f"{target.base_url}/echo"
    assert snap["request"]["body"] == {"hello": "subcase"}
    assert len(snap["assertions"]) == 1
    assert snap["assertions"][0]["passed"] is True
    assert snap["evidence"]["response"]["status"] == 200
    assert "subcase" in snap["evidence"]["response"]["body"]
    assert snap["case_link"]["type"] == "case_link"
    assert snap["case_link"]["case_id"] == "r2:c1"
    assert snap["case_link"]["tone"] == "success"

    # 2) Lookup by round + case_index without case_id, verifying Authorization secret is scrubbed
    by_coords = await read_task_sub_case(manager, task_id, case_index=0, round=1)
    assert by_coords["found"] is True
    assert by_coords["case_id"] == "r1:c0"
    assert by_coords["request"]["method"] == "GET"
    assert "super-secret-task-token" not in json.dumps(by_coords, ensure_ascii=False)

    # 3) Lookup by numeric execution_index string ("2" -> r2:c0)
    by_exec = await read_task_sub_case(manager, task_id, "2")
    assert by_exec["found"] is True
    assert by_exec["case_id"] == "r2:c0"
    assert by_exec["round"] == 2
    assert by_exec["case_index"] == 0

    # 4) Missing case_id returns structured error + available IDs
    missing_case = await read_task_sub_case(manager, task_id, "r9:c9")
    assert missing_case["ok"] is False
    assert missing_case["found"] is False
    assert missing_case["error"] == "case_not_found"
    assert missing_case["available_case_ids"] == ["r1:c0", "r1:c1", "r2:c0", "r2:c1"]

    # 5) Missing or invalid task_id returns structured task_not_found error
    missing_task = await read_task_sub_case(
        manager, "00000000-0000-0000-0000-000000000000", "r1:c0"
    )
    assert missing_task["ok"] is False
    assert missing_task["found"] is False
    assert missing_task["error"] == "task_not_found"
