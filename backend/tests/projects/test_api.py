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


async def test_project_update_delete_and_environment_rollback(client):
    """验证项目更新/级联删除与环境追加式版本回滚的完整 CRUD 与乐观锁语义。"""
    p1 = await create_project(client, "Alpha")
    p2 = await create_project(client, "Beta")

    # 更新项目名称与说明，重名拒绝 409，空载荷拒绝 422。
    updated = await client.put(
        f"/api/projects/{p1}", json={"name": "Alpha Renamed", "description": "updated"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Alpha Renamed"
    assert updated.json()["description"] == "updated"

    conflict_name = await client.put(f"/api/projects/{p1}", json={"name": "Beta"})
    assert conflict_name.status_code == 409
    assert conflict_name.json()["error"]["code"] == "project_exists"

    empty_update = await client.put(f"/api/projects/{p1}", json={})
    assert empty_update.status_code == 422

    # 创建环境 -> 更新到 v2 -> 回滚到 v1（追加生成 v3）。
    created_env = await client.post(f"/api/projects/{p1}/environments", json=environment_data())
    assert created_env.status_code == 201
    env_id = created_env.json()["id"]

    v2_body = environment_data(origin="http://127.0.0.1:9002", expected_version=1)
    v2_body.pop("name")
    v2 = await client.put(f"/api/environments/{env_id}", json=v2_body)
    assert v2.status_code == 200
    assert v2.json()["current_version"] == 2

    bad_lock = await client.post(
        f"/api/environments/{env_id}/rollback", json={"target_version": 1, "expected_version": 1}
    )
    assert bad_lock.status_code == 409
    assert bad_lock.json()["error"]["code"] == "environment_version_conflict"

    missing_target = await client.post(
        f"/api/environments/{env_id}/rollback", json={"target_version": 9, "expected_version": 2}
    )
    assert missing_target.status_code == 404

    rolled = await client.post(
        f"/api/environments/{env_id}/rollback", json={"target_version": 1, "expected_version": 2}
    )
    assert rolled.status_code == 200
    assert rolled.json()["current_version"] == 3
    assert rolled.json()["config"]["version"] == 3
    assert rolled.json()["config"]["origin"] == "http://127.0.0.1:9000"

    versions = (await client.get(f"/api/environments/{env_id}/versions")).json()["versions"]
    assert [item["version"] for item in versions] == [1, 2, 3]

    # 删除带环境与版本的项目应级联清理并返回 204；重复删除返回 404。
    deleted = await client.delete(f"/api/projects/{p1}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/projects/{p1}")).status_code == 404
    assert (await client.get(f"/api/environments/{env_id}")).status_code == 404
    assert (await client.delete(f"/api/projects/{p1}")).status_code == 404
    assert (await client.get(f"/api/projects/{p2}")).status_code == 200


async def test_spa_fallback_does_not_turn_unknown_non_get_api_into_405(tmp_path):
    """挂载前端 SPA 回落时，未知 /api/* 的 POST/PUT/DELETE 返回 404 而非 405，已知路由仍保 405。"""
    from api_agent.infrastructure.config import load_settings
    from api_agent.main import create_app

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><body>spa</body></html>", encoding="utf-8"
    )
    settings = load_settings(env_file=None, project_root=tmp_path, frontend_dist=dist)
    assert settings.frontend_root == dist
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                resp = await client.request(method, "/api/does-not-exist/subpath")
                assert resp.status_code == 404, (method, resp.status_code, resp.text)
                assert resp.json()["error"]["code"] == "http_error"

            # 已存在的 API 路由收到不支持方法仍应返回 405。
            method_not_allowed = await client.patch("/api/projects")
            assert method_not_allowed.status_code == 405

            # 非 /api 的前端 history 路由正常回落到 index.html。
            spa_page = await client.get("/model-testing")
            assert spa_page.status_code == 200
            assert "spa" in spa_page.text
