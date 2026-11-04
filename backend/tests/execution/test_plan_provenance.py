import hashlib
import json

from api_agent.execution.models import ExecutionRecord
from api_agent.execution.service import ExecutionService
from api_agent.llm.client import OpenAIClient
from api_agent.llm.schemas import ChatCompletionResponse


def input_plan():
    return {
        "origin": "http://127.0.0.1:9100",
        "allowed_methods": ["GET"],
        "path_templates": ["/json"],
        "requests": [
            {
                "id": "case",
                "method": "GET",
                "path": "/json",
                "assertions": [{"kind": "status", "expected": 200}],
            }
        ],
    }


def result():
    return {
        "status": "passed",
        "engine": "native",
        "reserved_requests": 1,
        "observed_attempts": 1,
        "unknown_requests": 0,
        "executions": [
            {
                "id": "case",
                "status": "passed",
                "http_status": 200,
                "assertions": [{"index": 0, "kind": "status", "passed": True}],
            }
        ],
    }


async def test_execution_freezes_plan_and_hash_for_later_diagnosis(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]

    async def run(self, plan, engine=None):
        return result()

    monkeypatch.setattr(ExecutionService, "run", run)
    response = await client.post("/api/executions", json={"plan": input_plan()})
    assert response.status_code == 201
    execution = response.json()
    assert execution["plan"]["requests"][0]["assertions"][0]["expected"] == 200
    encoded = json.dumps(execution["plan"], sort_keys=True, ensure_ascii=True).encode()
    assert execution["plan_sha256"] == hashlib.sha256(encoded).hexdigest()
    history = (await client.get("/api/executions")).json()["items"][0]
    assert history["plan_sha256"] == execution["plan_sha256"]


async def test_diagnosis_by_execution_id_uses_saved_plan_not_editor(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]

    async def run(self, plan, engine=None):
        return result()

    contexts = []

    async def model(self, request):
        contexts.append(json.loads(request.messages[-1].content))
        return ChatCompletionResponse(
            id="fixture",
            model="fixture",
            content=json.dumps(
                {
                    "summary": "原始计划与结果匹配",
                    "hypotheses": [],
                    "coverage_gaps": [],
                }
            ),
        )

    monkeypatch.setattr(ExecutionService, "run", run)
    monkeypatch.setattr(OpenAIClient, "chat_completion", model)
    await client.put(
        "/api/settings/llm",
        json={"enabled": True, "base_url": "http://127.0.0.1:9101/v1", "model": "fixture"},
    )
    execution = (await client.post("/api/executions", json={"plan": input_plan()})).json()
    path = f"/api/agent/executions/{execution['id']}/diagnose"
    response = await client.post(path, json={"contract": "GET /json returns 200"})
    assert response.status_code == 200
    assert contexts[0]["plan"]["requests"][0]["assertions"][0]["expected"] == 200
    edited = input_plan()
    edited["requests"][0]["assertions"][0]["expected"] = 404
    rejected = await client.post(path, json={"contract": "fixture", "plan": edited})
    assert rejected.status_code == 422
    assert len(contexts) == 1


async def test_old_history_without_plan_is_explicitly_not_diagnosable(client, app):
    await client.put(
        "/api/settings/llm",
        json={"enabled": True, "base_url": "http://127.0.0.1:9101/v1", "model": "fixture"},
    )
    async with app.state.database.session() as session:
        record = ExecutionRecord(label="legacy", **result())
        session.add(record)
        await session.commit()
        execution_id = record.id
    response = await client.post(
        f"/api/agent/executions/{execution_id}/diagnose", json={"contract": "fixture"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "execution_plan_unavailable"
