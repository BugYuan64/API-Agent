"""Regression coverage for effective settings and credential origin boundaries."""

import httpx
import pytest

from api_agent.infrastructure.config import load_settings
from api_agent.llm.schemas import LLMConfigUpdate
from api_agent.llm.service import LLMSettingService


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        env_file=None,
        project_root=tmp_path,
        llm_mode="openai",
        llm_base_url="https://saved.example/v1",
        llm_api_key="fictional-saved-key",
        llm_model="environment-model",
        llm_timeout_seconds=24,
    )


@pytest.fixture
def outbound(monkeypatch):
    """Replace external HTTP only; retain real client URL/header/response behavior."""
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})
        return httpx.Response(
            200,
            json={
                "id": "fixture-completion",
                "model": "fixture-model",
                "choices": [
                    {"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}
                ],
                "usage": {},
            },
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.client.httpx.AsyncClient", factory)
    return requests


@pytest.mark.parametrize("path", ["/api/settings/llm", "/api/llm/status"])
async def test_read_settings_uses_environment_without_creating_disabled_override(client, app, path):
    response = await client.get(path)
    assert response.status_code == 200
    data = response.json()
    assert data["base_url"] == "https://saved.example/v1"
    assert data["model"] == "environment-model"
    assert data["has_api_key"] is True
    assert data.get("enabled", data.get("configured")) is True
    assert "fictional-saved-key" not in response.text
    async with app.state.database.session() as session:
        assert await LLMSettingService(session).get_record() is None


async def test_get_client_uses_environment_until_explicit_disabled_override(app, settings):
    async with app.state.database.session() as session:
        service = LLMSettingService(session, settings=settings)
        effective = await service.get_client()
        assert effective.is_configured
        assert effective.api_key == "fictional-saved-key"
        assert effective.model == "environment-model"
        assert await service.get_record() is None
        await service.update(LLMConfigUpdate(enabled=False))
        disabled = await service.get_client()
        assert not disabled.is_configured


@pytest.mark.parametrize("persisted", [True, False])
async def test_switching_shared_model_preserves_other_settings(client, app, persisted):
    if persisted:
        response = await client.put(
            "/api/settings/llm",
            json={"enabled": True, "model": "saved-model", "timeout_seconds": 42},
        )
        assert response.status_code == 200
    original = (await client.get("/api/settings/llm")).json()
    response = await client.patch("/api/settings/llm/model", json={"model": "  new-model  "})
    assert response.status_code == 200
    assert response.json() == {**original, "model": "new-model"}
    assert (await client.get("/api/settings/llm")).json() == response.json()
    async with app.state.database.session() as session:
        record = await LLMSettingService(session).get_record()
        assert record.api_key == "fictional-saved-key"
    assert (await client.patch("/api/settings/llm/model", json={"model": "   "})).status_code == 422


@pytest.mark.parametrize("endpoint", ["ping", "models"])
@pytest.mark.parametrize(
    ("payload", "authorization"),
    [
        ({}, "Bearer fictional-saved-key"),
        ({"base_url": "https://SAVED.example:443/other"}, "Bearer fictional-saved-key"),
        ({"base_url": "https://new.example/v1"}, None),
        ({"base_url": "http://saved.example/v1"}, None),
        ({"base_url": "https://saved.example:444/v1"}, None),
        ({"api_key": ""}, None),
        (
            {"base_url": "https://new.example/v1", "api_key": "fixture-new-key"},
            "Bearer fixture-new-key",
        ),
    ],
)
async def test_preview_reuses_key_only_for_same_origin(
    client, app, outbound, endpoint, payload, authorization
):
    saved = await client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "base_url": "https://saved.example/v1",
            "api_key": "fictional-saved-key",
            "model": "saved-model",
        },
    )
    assert saved.status_code == 200
    response = await client.post(f"/api/settings/llm/{endpoint}", json=payload)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(outbound) == 1
    assert outbound[0].headers.get("Authorization") == authorization


@pytest.mark.parametrize("endpoint", ["ping", "models"])
async def test_probe_uses_environment_without_persisting_override(client, app, outbound, endpoint):
    response = await client.post(f"/api/settings/llm/{endpoint}")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert outbound[0].headers.get("Authorization") == "Bearer fictional-saved-key"
    async with app.state.database.session() as session:
        assert await LLMSettingService(session).get_record() is None


@pytest.mark.parametrize(
    ("base_url", "api_key", "authorization"),
    [
        ("https://new.example/v1", None, None),
        ("https://saved.example:444/v1", None, None),
        ("http://saved.example/v1", None, None),
        ("https://SAVED.example:443/v2", None, "Bearer fictional-saved-key"),
        ("https://saved.example/v1", "", None),
        ("https://new.example/v1", "fixture-new-key", "Bearer fixture-new-key"),
    ],
)
async def test_saved_origin_change_cannot_carry_old_credential(
    client, app, settings, base_url, api_key, authorization
):
    async with app.state.database.session() as session:
        service = LLMSettingService(session)
        await service.update(
            LLMConfigUpdate(
                enabled=True, base_url="https://saved.example/v1", api_key="fictional-saved-key"
            )
        )
        await service.update(LLMConfigUpdate(enabled=True, base_url=base_url, api_key=api_key))
        effective = await service.get_client()
        assert effective._build_headers().get("Authorization") == authorization
