import httpx
import pytest

from api_agent.infrastructure.config import load_settings
from api_agent.main import create_app


@pytest.fixture
def settings(tmp_path):
    return load_settings(env_file=None, project_root=tmp_path)


@pytest.fixture
async def app(settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def project_id(client):
    response = await client.post("/api/projects", json={"name": "Course project"})
    assert response.status_code == 201
    return response.json()["id"]


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch):
    for name in (
        "APP_NAME",
        "LOG_LEVEL",
        "DATABASE_URL",
        "ARTIFACT_DIR",
        "LLM_MODE",
        "LLM_API_KEY",
        "TARGET_ALLOWED_ORIGINS",
        "TARGET_AUTH_ENV_VARS",
        "POSTMAN_CLI_PATH",
        "OMIT_RESPONSE_PAYLOAD",
        "PROJECT_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
