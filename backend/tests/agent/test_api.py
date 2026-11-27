import asyncio

from api_agent.agent.service import AgentService
from api_agent.llm.client import OpenAIClient

from .test_runtime import initial, request_for
from .test_service import ScriptedModel, generate_request, generated


async def enable(client):
    response = await client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "base_url": "http://127.0.0.1:9101/v1",
            "model": "fixture-model",
        },
    )
    assert response.status_code == 200


def install_model(monkeypatch, model):
    async def complete(self, request):
        return await model.chat_completion(request)

    monkeypatch.setattr(OpenAIClient, "chat_completion", complete)


async def test_generate_api_respects_config_and_returns_runnable_plan(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]
    payload = generate_request().model_dump()
    disabled = await client.post("/api/agent/generate", json=payload)
    assert disabled.status_code == 503
    assert disabled.json()["error"]["code"] == "model_not_configured"
    await enable(client)
    install_model(monkeypatch, ScriptedModel(generated()))
    response = await client.post("/api/agent/generate", json=payload)
    assert response.status_code == 200
    agent = AgentService(app.state.settings, ScriptedModel())
    assert agent.execution.validate(response.json()["plan"])["valid"] is True


async def test_bad_model_output_is_safe_error_not_500(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]
    await enable(client)
    install_model(monkeypatch, ScriptedModel("secret-vendor-error", "secret-vendor-error"))
    response = await client.post("/api/agent/generate", json=generate_request().model_dump())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_model_output"
    assert "secret-vendor-error" not in response.text


async def test_runs_return_immediately_and_can_be_cancelled(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]
    await enable(client)

    class Slow(ScriptedModel):
        async def chat_completion(self, request):
            await asyncio.sleep(30)

    install_model(monkeypatch, Slow())
    response = await client.post(
        "/api/agent/runs", json=request_for("http://127.0.0.1:9100").model_dump()
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    await asyncio.sleep(0.02)
    cancelled = await client.post(f"/api/agent/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    detail = await client.get(f"/api/agent/runs/{run_id}")
    assert detail.json()["budget"]["model_call"]["consumed"] == 1
    report = await client.get(f"/api/agent/runs/{run_id}/report")
    assert report.status_code == 200
    assert "text/markdown" in report.headers["content-type"]
    assert "cancelled" in report.text


async def test_missing_run_and_malformed_id_are_404(client):
    for run_id in ("not-an-id", "b67fd824-dbe1-48b2-8222-4ca22510af35"):
        response = await client.get(f"/api/agent/runs/{run_id}")
        assert response.status_code == 404


async def test_scope_rejected_before_background_job_is_created(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9999"]
    await enable(client)
    install_model(monkeypatch, ScriptedModel(initial()))
    response = await client.post(
        "/api/agent/runs", json=request_for("http://127.0.0.1:9100").model_dump()
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "origin_not_allowed"


async def test_manager_shutdown_cancels_and_persists_active_job(client, app, monkeypatch):
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]
    await enable(client)

    class Slow(ScriptedModel):
        async def chat_completion(self, request):
            await asyncio.sleep(30)

    install_model(monkeypatch, Slow())
    response = await client.post(
        "/api/agent/runs", json=request_for("http://127.0.0.1:9100").model_dump()
    )
    run_id = response.json()["id"]
    await asyncio.sleep(0.02)
    await app.state.agents.shutdown()
    assert app.state.agents.store.read(run_id)["status"] == "cancelled"
    assert not app.state.agents.tasks


async def test_completed_run_can_be_deleted(client, app, monkeypatch):
    """终态运行应可删除证据文件，删除后查询与报告都变 404。

    实现：跑一次会被立即取消的运行（终态），``DELETE`` 后断言 204，
    再 GET 记录与报告均为 404；重复删除同样 404。
    """
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]
    await enable(client)

    class Slow(ScriptedModel):
        async def chat_completion(self, request):
            await asyncio.sleep(30)

    install_model(monkeypatch, Slow())
    created = await client.post(
        "/api/agent/runs", json=request_for("http://127.0.0.1:9100").model_dump()
    )
    run_id = created.json()["id"]
    await asyncio.sleep(0.02)
    await client.post(f"/api/agent/runs/{run_id}/cancel")

    assert (await client.delete(f"/api/agent/runs/{run_id}")).status_code == 204
    assert (await client.get(f"/api/agent/runs/{run_id}")).status_code == 404
    assert (await client.get(f"/api/agent/runs/{run_id}/report")).status_code == 404
    assert (await client.delete(f"/api/agent/runs/{run_id}")).status_code == 404


async def test_active_run_must_be_cancelled_before_deleting(client, app, monkeypatch):
    """运行中的记录不能直接删：后台任务仍在写同一文件，删除会与写入竞争。

    实现：起一个卡在模型调用里的运行，确认 ``DELETE`` 返回 409 且记录仍在。
    """
    app.state.settings.target_allowed_origins = ["http://127.0.0.1:9100"]
    await enable(client)

    class Slow(ScriptedModel):
        async def chat_completion(self, request):
            await asyncio.sleep(30)

    install_model(monkeypatch, Slow())
    created = await client.post(
        "/api/agent/runs", json=request_for("http://127.0.0.1:9100").model_dump()
    )
    run_id = created.json()["id"]
    await asyncio.sleep(0.02)

    blocked = await client.delete(f"/api/agent/runs/{run_id}")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "agent_run_active"
    assert (await client.get(f"/api/agent/runs/{run_id}")).status_code == 200

    await client.post(f"/api/agent/runs/{run_id}/cancel")
