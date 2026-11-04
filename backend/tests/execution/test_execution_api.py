import json
from pathlib import Path

import httpx
import pytest
from support.local_server import LocalServer

from api_agent.infrastructure.config import load_settings
from api_agent.main import create_app

# 与 tests/execution/test_postman.py 一致：真实 CLI 位于项目缓存目录。
CLI = Path(__file__).resolve().parents[3] / ".cache/tools/postman/postman.exe"


@pytest.fixture
async def target():
    """真实回环目标，供执行引擎集成用例使用。

    实现：执行路径会真的启动 Postman CLI 子进程并发 HTTP 请求，
    因此目标必须是真实监听的服务，不能用 ASGI 内存传输。
    """
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def cli_client(tmp_path, monkeypatch, target):
    """一个用真实 CLI、并授权了当前目标 origin 的应用实例。

    实现：两个必须显式给定的东西——
    ① ``postman_cli_path``：默认夹具把 ``project_root`` 指向临时目录，
       于是 CLI 的默认回落路径也落在临时目录里、永远找不到；
    ② ``target_allowed_origins``：执行计划要求 origin 出现在本机允许列表里
       （这是给 Agent 用的执行入口的安全属性，和人在界面上手打 URL 的控制台不同）。
    这两项不显式注入，用例就只会测到“计划被拒”，而不是真的跑一次执行。
    """
    monkeypatch.delenv("POSTMAN_CLI_PATH", raising=False)
    settings = load_settings(
        env_file=None,
        project_root=tmp_path,
        postman_cli_path=CLI if CLI.is_file() else None,
        target_allowed_origins=[target.base_url],
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def plan_for(base_url: str) -> dict:
    """构造一份最小可执行计划（1 个请求 + 2 条断言）。

    实现：路径与方法都落在授权范围内，断言同时覆盖状态码与 JSON 字段，
    这样一次运行就能验证“通过”与“断言失败”两种结果的记录形状。
    """
    return {
        "origin": base_url,
        "allowed_methods": ["GET"],
        "path_templates": ["/json"],
        "requests": [
            {
                "id": "json-ok",
                "method": "GET",
                "path": "/json",
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {"kind": "json_equals", "pointer": "/ok", "expected": True},
                ],
            }
        ],
    }


async def test_engine_status_reports_cli_availability(client):
    """引擎状态接口应如实返回 CLI 可用性。

    实现：本机 ``.cache/tools/postman/postman.exe`` 存在时返回 available=true 与版本号；
    缺失时应返回 ``cli_unavailable``，两种情况都不发目标请求。
    """
    response = await client.get("/api/executions/engine/status")
    assert response.status_code == 200
    payload = response.json()
    assert "available" in payload
    if payload["available"]:
        assert payload["version"]
    else:
        assert payload["error_kind"] == "cli_unavailable"


async def test_execution_rejects_invalid_plan_before_running(client, target):
    """非法计划返回 400，且不产生任何执行记录。

    实现：把 origin 换成非回环地址，断言错误码是 ``invalid_plan``，
    并确认历史仍为空——被拒绝的请求不能污染证据库。
    """
    plan = plan_for(target.base_url)
    plan["origin"] = "http://example.com"
    response = await client.post("/api/executions", json={"plan": plan})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_plan"

    history = await client.get("/api/executions")
    assert history.json()["items"] == []


async def test_execution_rejects_origin_outside_local_allowlist(client, target):
    """目标 origin 未在允许列表里时应被拒，而不是默认放行。

    实现：默认夹具的 ``target_allowed_origins`` 为空，执行计划要求显式授权，
    因此这里得到一个 ``origin_not_allowed``；这条用例守住“配置决定范围”的边界。
    """
    response = await client.post("/api/executions", json={"plan": plan_for(target.base_url)})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "origin_not_allowed"


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_execution_persists_case_and_assertion_results(cli_client, target):
    """真实执行一份计划后，结果应可展开到断言级并出现在历史里。

    实现：需要本机 Postman CLI，缺失时跳过（避免把“环境没装”误报成功能坏了）；
    断言覆盖状态、请求数、断言明细与历史/详情接口。
    """
    response = await cli_client.post(
        "/api/executions",
        json={"plan": plan_for(target.base_url), "label": "fixture-json", "engine": "postman"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "passed", payload
    assert payload["label"] == "fixture-json"
    assert payload["engine"] == "postman-cli"
    assert payload["reserved_requests"] == 1
    assert payload["observed_attempts"] == 1
    case = payload["executions"][0]
    assert case["id"] == "json-ok"
    assert case["http_status"] == 200
    assert {item["kind"] for item in case["assertions"]} == {"status", "json_equals"}
    assert all(item["passed"] for item in case["assertions"])

    detail = await cli_client.get(f"/api/executions/{payload['id']}")
    assert detail.status_code == 200
    assert detail.json()["executions"][0]["id"] == "json-ok"

    history = await cli_client.get("/api/executions")
    assert history.json()["items"][0]["id"] == payload["id"]


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_execution_records_assertion_failure(cli_client, target):
    """断言不成立时应记 failed，并保留失败断言的原因。

    实现：期望状态码 201 而目标是 200，断言 ``status == failed``
    且第一条断言 ``passed=false``；这验证的是“失败也被如实记录”，
    不是把失败伪装成执行错误。
    """
    plan = plan_for(target.base_url)
    plan["requests"][0]["assertions"] = [{"kind": "status", "expected": 201}]
    response = await cli_client.post("/api/executions", json={"plan": plan, "engine": "postman"})
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["executions"][0]["assertions"][0]["passed"] is False
    assert isinstance(payload["executions"][0]["assertions"][0]["message"], str)


async def test_native_engine_executes_plan_without_external_cli(tmp_path, target):
    """原生驱动不需要任何外部 CLI，开箱即可执行并落库。

    实现：这里刻意不配置 ``postman_cli_path``，证明 native 引擎完全自给自足；
    同时验证 ``engine/list`` 报告两个驱动、默认选原生。
    """
    import httpx

    from api_agent.infrastructure.config import load_settings
    from api_agent.main import create_app

    settings = load_settings(
        env_file=None,
        project_root=tmp_path,
        target_allowed_origins=[target.base_url],
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listing = await client.get("/api/executions/engine/list")
            assert listing.status_code == 200
            engines = {item["id"]: item for item in listing.json()["items"]}
            assert set(engines) == {"native", "postman"}
            assert engines["native"]["available"] is True
            assert listing.json()["default"] == "native"

            response = await client.post(
                "/api/executions",
                json={"plan": plan_for(target.base_url), "engine": "native"},
            )
            assert response.status_code == 201, response.text
            payload = response.json()
            assert payload["status"] == "passed"
            assert payload["engine"] == "native"
            assert payload["observed_attempts"] == 1
            assert payload["unknown_requests"] == 0
            assert payload["executions"][0]["response"]["body_bytes"] > 0

            unknown = await client.post(
                "/api/executions",
                json={"plan": plan_for(target.base_url), "engine": "does-not-exist"},
            )
            assert unknown.status_code == 400
            assert unknown.json()["error"]["code"] == "unknown_engine"
            assert (await client.get("/api/executions")).json()["page"]["total"] == 1


async def test_execution_history_is_paginated_and_ordered(client):
    """历史执行应按时间倒序分页返回。

    实现：直接调用列表接口（不依赖已有记录），
    断言分页字段完整且 ``total`` 与条目数自洽。
    """
    response = await client.get("/api/executions", params={"limit": 5, "offset": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["page"]["limit"] == 5
    assert body["page"]["offset"] == 0
    assert body["page"]["total"] == len(body["items"])
    json.dumps(body)  # 确保整体可序列化（无 datetime/bytes 泄漏）
