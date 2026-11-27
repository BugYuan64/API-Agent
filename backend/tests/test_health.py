import asyncio

import httpx


def test_health_boots_offline_without_exposing_configuration(tmp_path):
    from api_agent.infrastructure.config import load_settings
    from api_agent.main import create_app

    settings = load_settings(env_file=None, project_root=tmp_path, llm_api_key="fixture-private")

    async def check():
        transport = httpx.ASGITransport(app=create_app(settings))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {
                "status": "ok",
                "service": "API-Agent",
                "version": "0.1.0",
                "llm_mode": "openai",
            }
            assert "fixture-private" not in response.text
            assert str(tmp_path) not in response.text
            assert (await client.get("/openapi.json")).status_code == 200

    asyncio.run(check())
    assert not (tmp_path / "data").exists()
