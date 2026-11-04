import asyncio
import hashlib
import json

import pytest
from support.local_server import LocalServer

from api_agent.agent.runtime import AgentRuntime
from api_agent.agent.schemas import AgentRunRequest
from api_agent.agent.service import AgentService
from api_agent.agent.store import AgentStore
from api_agent.infrastructure.config import load_settings
from api_agent.llm.client import LLMTimeoutError

from .test_service import ScriptedModel


def initial():
    return {
        "requests": [
            {
                "id": "initial",
                "method": "GET",
                "path": "/json",
                "query": {"size": "12"},
                "assertions": [{"kind": "status", "expected": 400}],
            }
        ],
        "intents": [
            {
                "case_id": "initial",
                "category": "boundary",
                "basis": "contract",
                "source_quote": "size=12 返回 400",
                "rationale": "输入边界",
            }
        ],
    }


def diagnosis(round_index=0, case_id="initial"):
    return {
        "summary": "返回 200，期望 400",
        "hypotheses": [
            {
                "id": "H1",
                "cause": "可能未验证 size",
                "next_check": "改变 size",
                "evidence_ids": [f"r{round_index}:{case_id}:a0"],
            }
        ],
        "coverage_gaps": [],
    }


def followup():
    return {
        "continue_testing": True,
        "reason": "只改变 size",
        "mutations": [
            {
                "parent_id": "initial",
                "hypothesis_id": "H1",
                "location": "query",
                "pointer": "/size",
                "value": "10",
                "reason": "验证另一边界",
                "assertions": [{"kind": "status", "expected": 400}],
            }
        ],
    }


def request_for(origin, **limits):
    return AgentRunRequest(
        contract="GET /json: size=12 返回 400",
        scope={
            "origin": origin,
            "allowed_methods": ["GET"],
            "path_templates": ["/json"],
            "delay_ms": 0,
        },
        max_cases=1,
        limits={"target_requests": 2, "model_calls": 6, "max_rounds": 2, **limits},
    )


@pytest.fixture
async def target():
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


def runtime_for(tmp_path, origin, model):
    settings = load_settings(env_file=None, project_root=tmp_path, target_allowed_origins=[origin])
    agent = AgentService(settings, model)
    store = AgentStore(settings.artifact_dir / "agent")
    return AgentRuntime(agent, store), store


async def test_real_native_execution_adaptation_and_durable_report(tmp_path, target):
    runtime, store = runtime_for(
        tmp_path,
        target.base_url,
        ScriptedModel(
            initial(),
            diagnosis(),
            followup(),
            diagnosis(1, "r1-m0"),
            {
                "overview": "两次观察到断言差异，校验缺失尚待确认。",
                "recommendations": ["检查校验实现"],
            },
        ),
    )
    record = runtime.create(request_for(target.base_url))
    frozen = json.dumps(record["snapshot"], sort_keys=True)
    await runtime.run(record, request_for(target.base_url))
    saved = store.read(record["id"])
    assert saved["status"] == "completed"
    assert saved["stop_reason"] == "target_budget_exhausted"
    assert saved["budget"]["target_http"]["consumed"] == 2
    assert saved["budget"]["target_http"]["observed_attempts"] == 2
    assert saved["budget"]["model_call"]["consumed"] == 5
    assert len(saved["rounds"]) == 2
    assert saved["rounds"][0]["result"]["executions"][0]["http_status"] == 200
    assert saved["rounds"][1]["plan"]["requests"][0]["query"] == {"size": "10"}
    assert json.dumps(saved["snapshot"], sort_keys=True) == frozen
    report = store.report(record["id"])
    assert "已证实的观察事实" in report
    assert "未证实的假设" in report
    assert "r1:r1-m0:a0" in report
    assert "费用未知" in report


async def test_diagnosis_outage_preserves_assertions_and_partial_report(tmp_path, target):
    runtime, store = runtime_for(
        tmp_path,
        target.base_url,
        ScriptedModel(
            initial(),
            LLMTimeoutError("private-error-password"),
            {"overview": "模型诊断不可用，已有 HTTP 事实保留。", "recommendations": []},
        ),
    )
    record = runtime.create(request_for(target.base_url))
    await runtime.run(record, request_for(target.base_url))
    saved = store.read(record["id"])
    assert saved["actual_llm_status"] == "degraded"
    assert saved["rounds"][0]["evidence"][1]["passed"] is False
    assert saved["report_status"] == "partial"
    assert "private-error-password" not in store.report(record["id"])
    assert "model_timeout" in store.report(record["id"])


async def test_model_budget_stops_new_calls_but_keeps_real_execution(tmp_path, target):
    runtime, store = runtime_for(tmp_path, target.base_url, ScriptedModel(initial()))
    request = request_for(target.base_url, model_calls=1)
    record = runtime.create(request)
    await runtime.run(record, request)
    saved = store.read(record["id"])
    assert saved["budget"]["target_http"]["observed_attempts"] == 1
    assert saved["budget"]["model_call"]["consumed"] == 1
    assert saved["stop_reason"] == "model_budget_exhausted"
    assert "未评估" in store.report(record["id"])


async def test_wall_deadline_charges_inflight_model_and_saves_partial(tmp_path):
    class SlowModel(ScriptedModel):
        async def chat_completion(self, request):
            await asyncio.sleep(10)

    runtime, store = runtime_for(tmp_path, "http://127.0.0.1:9100", SlowModel())
    request = request_for("http://127.0.0.1:9100", wall_time_seconds=0.1)
    record = runtime.create(request)
    await runtime.run(record, request)
    saved = store.read(record["id"])
    assert saved["status"] == "timed_out"
    assert saved["budget"]["model_call"]["consumed"] == 1
    assert saved["model_calls"][0]["status"] == "unknown"
    assert saved["report_status"] == "partial"


async def test_native_cannot_swallow_outer_deadline_and_start_diagnosis(tmp_path, target):
    output = initial()
    output["requests"][0]["path"] = "/slow"
    model = ScriptedModel(output)
    runtime, store = runtime_for(tmp_path, target.base_url, model)
    request = request_for(target.base_url, wall_time_seconds=0.2)
    request.scope.path_templates = ["/slow"]
    record = runtime.create(request)
    await runtime.run(record, request)
    saved = store.read(record["id"])
    assert saved["status"] == "timed_out"
    assert saved["stop_reason"] == "wall_time_exhausted"
    assert saved["budget"]["model_call"]["consumed"] == 1
    assert len(model.contexts) == 1
    assert saved["rounds"][0]["result"]["status"] == "cancelled"


async def test_store_recovery_never_replays_inflight_requests(tmp_path):
    runtime, store = runtime_for(tmp_path, "http://127.0.0.1:9100", ScriptedModel())
    record = runtime.create(request_for("http://127.0.0.1:9100"))
    record["status"] = "running"
    record["budget"]["target_http"]["reserved"] = 2
    store.save(record)
    store.recover()
    saved = store.read(record["id"])
    assert saved["status"] == "interrupted"
    assert saved["budget"]["target_http"]["consumed"] == 2
    assert saved["budget"]["target_http"]["unknown_requests"] == 2
    assert "interrupted" in store.report(record["id"])


def test_store_rejects_paths_outside_its_generated_id_namespace(tmp_path):
    store = AgentStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../secrets")


def test_frozen_openapi_schema_survives_persistence_with_matching_hash(tmp_path):
    runtime, store = runtime_for(tmp_path, "http://127.0.0.1:9100", ScriptedModel())
    request = request_for("http://127.0.0.1:9100")
    request.contract = {
        "properties": {"password": {"type": "string", "minLength": 8}},
        "example": {"password": "real-input-password"},
    }
    saved = store.read(runtime.create(request)["id"])
    assert saved["snapshot"]["contract"]["properties"]["password"]["minLength"] == 8
    assert saved["snapshot"]["contract"]["example"]["password"] == "[REDACTED]"
    encoded = json.dumps(saved["snapshot"], sort_keys=True, ensure_ascii=True).encode()
    assert saved["snapshot_sha256"] == hashlib.sha256(encoded).hexdigest()


def test_report_includes_frozen_scope_and_per_round_reproduction_inputs(tmp_path):
    runtime, store = runtime_for(tmp_path, "http://127.0.0.1:9100", ScriptedModel())
    record = runtime.create(request_for("http://127.0.0.1:9100"))
    record["rounds"].append(
        {
            "index": 0,
            "plan": {
                "requests": [
                    {
                        "id": "observed",
                        "method": "POST",
                        "path": "/register",
                        "query": {"length": "12"},
                        "body": {"password": "do-not-export"},
                        "assertions": [
                            {
                                "kind": "json_equals",
                                "pointer": "/error/code",
                                "expected": "invalid_phone",
                            }
                        ],
                    }
                ]
            },
            "evidence": [],
            "intents": [],
            "notes": [],
            "diagnosis": None,
        }
    )
    store.save(record)
    report = store.report(record["id"])
    assert "http://127.0.0.1:9100" in report
    assert '"path": "/register"' in report
    assert '"length": "12"' in report
    assert '"pointer": "/error/code"' in report
    assert "do-not-export" not in report


def test_later_secret_learning_does_not_rewrite_frozen_snapshot(tmp_path):
    runtime, store = runtime_for(tmp_path, "http://127.0.0.1:9100", ScriptedModel())
    request = request_for("http://127.0.0.1:9100")
    request.contract = "Use fictional-pass only as synthetic test data"
    record = runtime.create(request)
    before = store.read(record["id"])["snapshot"]
    runtime.agent.context.learn({"body": {"password": "fictional-pass"}})
    runtime.save(record)
    after = store.read(record["id"])
    assert after["snapshot"] == before
    encoded = json.dumps(after["snapshot"], sort_keys=True, ensure_ascii=True).encode()
    assert after["snapshot_sha256"] == hashlib.sha256(encoded).hexdigest()
