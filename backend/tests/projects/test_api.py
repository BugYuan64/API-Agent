import httpx
import pytest


async def create_project(client, name="Project"):
    response = await client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def environment_data(**overrides):
    data = {
        "name": "local",
        "origin": "http://127.0.0.1:9000",
        "allowed_methods": ["GET", "POST"],
        "path_templates": ["/items", "/items/{item_id}"],
        "auth_ref": {"kind": "bearer", "env_var": "TARGET_TOKEN"},
        "replay_policy": {"allow_replay": False, "allow_non_idempotent": False},
        "sensitive_field_rules": ["/token", "/profile/email"],
    }
    data.update(overrides)
    return data


async def test_project_environment_versions_survive_restart(client, app, settings):
    project_id = await create_project(client)
    response = await client.post(
        f"/api/projects/{project_id}/environments", json=environment_data()
    )
    assert response.status_code == 201
    environment = response.json()
    assert environment["current_version"] == 1
    assert "fixture-token" not in response.text

    environment_id = environment["id"]
    updated = environment_data(origin="http://127.0.0.1:9001", expected_version=1)
    updated.pop("name")
    response = await client.put(f"/api/environments/{environment_id}", json=updated)
    assert response.status_code == 200
    assert response.json()["current_version"] == 2
    assert response.json()["config"]["origin"] == "http://127.0.0.1:9001"

    # Simulate a restart: drop every connection the running app holds, then
    # build a fresh app over the same database file. No background worker
    # needs stopping — the simulation run module was removed.
    await app.state.database.dispose()
    from api_agent.main import create_app

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as new_client:
            old = await new_client.get(f"/api/environments/{environment_id}?version=1")
            current = await new_client.get(f"/api/environments/{environment_id}")
            history = await new_client.get(f"/api/environments/{environment_id}/versions")
            assert old.json()["config"]["origin"] == "http://127.0.0.1:9000"
            assert current.json()["config"]["version"] == 2
            assert len(history.json()["versions"]) == 2


async def test_version_conflict_and_duplicate_names(client, project_id):
    response = await client.post(
        f"/api/projects/{project_id}/environments", json=environment_data()
    )
    environment_id = response.json()["id"]
    invalid_update = environment_data(expected_version=2)
    invalid_update.pop("name")
    response = await client.put(f"/api/environments/{environment_id}", json=invalid_update)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "environment_version_conflict"
    response = await client.post(
        f"/api/projects/{project_id}/environments", json=environment_data()
    )
    assert response.status_code == 409
    response = await client.post("/api/projects", json={"name": "Course project"})
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "https://user:fixture-secret@example.com"),
        ("origin", "http://example.com/api"),
        ("allowed_methods", ["TRACE"]),
        ("allowed_methods", ["GET", "GET"]),
        ("path_templates", ["items"]),
        ("path_templates", ["/items?token=value"]),
        ("path_templates", ["/items/*"]),
        ("auth_ref", {"kind": "bearer", "env_var": "fixture-secret"}),
        ("sensitive_field_rules", ["token"]),
    ],
)
async def test_invalid_environment_is_safe_and_creates_nothing(client, project_id, field, value):
    data = environment_data(**{field: value})
    response = await client.post(f"/api/projects/{project_id}/environments", json=data)
    assert response.status_code == 422
    body = response.json()
    assert body["schema_version"] == "1"
    assert body["error"]["code"] == "validation_error"
    assert "fixture-secret" not in response.text
    assert body["error"]["request_id"]
    assert "input" not in response.text


async def test_missing_project_and_stable_pagination(client):
    response = await client.post("/api/projects/missing/environments", json=environment_data())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    for index in range(3):
        await create_project(client, f"Project {index}")
    first = await client.get("/api/projects?limit=2&offset=0")
    second = await client.get("/api/projects?limit=2&offset=2")
    assert first.status_code == 200
    assert first.json()["page"] == {"schema_version": "1", "limit": 2, "offset": 0, "total": 3}
    assert len(first.json()["items"]) == 2
    assert len(second.json()["items"]) == 1
    from datetime import datetime

    assert (
        datetime.fromisoformat(first.json()["items"][0]["created_at"]).utcoffset().total_seconds()
        == 0
    )
    assert (await client.get("/api/projects?limit=201")).status_code == 422
