"""Stateful multi-step chain tests across ExecutionPlan (native & postman) and Tasks."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from support.local_server import LocalServer

from api_agent.execution.native import NativeHttpEngine
from api_agent.execution.postman import PostmanRunner
from api_agent.execution.schemas import ExecutionPlan

CLI = Path(__file__).resolve().parents[3] / ".cache/tools/postman/postman.exe"


def _build_stateful_crud_app():
    app = FastAPI()
    state: dict = {
        "token": "synthetic-session-jwt-99887766",
        "next_id": 100,
        "items": {},
        "requests": [],
    }

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        state["requests"].append(("POST", "/api/login", None))
        if body.get("password") != "ok-pass":
            return JSONResponse({"code": "401", "msg": "bad credentials"}, status_code=401)
        return JSONResponse(
            {"code": "200", "data": {"token": state["token"], "user": body.get("email")}},
            headers={"X-Trace-Id": "trace-login-001"},
        )

    @app.post("/api/items")
    async def create_item(request: Request, token: str | None = Header(default=None)):
        state["requests"].append(("POST", "/api/items", token))
        if token != state["token"]:
            return JSONResponse({"code": "401", "msg": "unauthorized"}, status_code=401)
        body = await request.json()
        if body.get("malicious_id"):
            return JSONResponse({"code": "200", "data": {"id": "../admin", "bad_header": "a\r\nb"}})
        item_id = state["next_id"]
        state["next_id"] += 1
        item = {"id": item_id, "name": body["name"], "tag": body.get("tag", "")}
        state["items"][item_id] = item
        return JSONResponse({"code": "200", "data": item}, headers={"X-Created-Id": str(item_id)})

    @app.get("/api/items/{item_id}")
    async def get_item(
        item_id: int, q: str | None = None, token: str | None = Header(default=None)
    ):
        state["requests"].append(("GET", f"/api/items/{item_id}", token))
        if token != state["token"]:
            return JSONResponse({"code": "401", "msg": "unauthorized"}, status_code=401)
        item = state["items"].get(item_id)
        if item is None:
            return JSONResponse({"code": "404", "msg": "not found"}, status_code=404)
        return JSONResponse({"code": "200", "data": {**item, "q": q}})

    @app.put("/api/items/{item_id}")
    async def update_item(item_id: int, request: Request, token: str | None = Header(default=None)):
        state["requests"].append(("PUT", f"/api/items/{item_id}", token))
        if token != state["token"]:
            return JSONResponse({"code": "401", "msg": "unauthorized"}, status_code=401)
        body = await request.json()
        if body.get("force_fail"):
            return JSONResponse({"code": "500", "msg": "forced failure"}, status_code=200)
        state["items"][item_id]["name"] = body["name"]
        return JSONResponse({"code": "200", "data": state["items"][item_id]})

    @app.delete("/api/items/{item_id}")
    async def delete_item(item_id: int, token: str | None = Header(default=None)):
        state["requests"].append(("DELETE", f"/api/items/{item_id}", token))
        if token != state["token"]:
            return JSONResponse({"code": "401", "msg": "unauthorized"}, status_code=401)
        state["items"].pop(item_id, None)
        return JSONResponse({"code": "200", "data": {"deleted": item_id}})

    return app, state


@pytest.fixture
async def crud_target():
    app, state = _build_stateful_crud_app()
    server = await LocalServer(app).start()
    try:
        yield server, state
    finally:
        await server.stop()


def _make_crud_plan(origin: str) -> dict:
    return {
        "schema_version": "1",
        "origin": origin,
        "allowed_methods": ["GET", "POST", "PUT", "DELETE"],
        "path_templates": ["/api/login", "/api/items", "/api/items/{id}"],
        "variables": {"run_tag": "chain-v1"},
        "requests": [
            {
                "id": "login",
                "method": "POST",
                "path": "/api/login",
                "body": {"email": "tester@example.com", "password": "ok-pass"},
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {"kind": "json_equals", "pointer": "/code", "expected": "200"},
                ],
                "extract": [
                    {
                        "name": "auth_token",
                        "source": "json",
                        "pointer": "/data/token",
                        "sensitive": True,
                    },
                    {"name": "trace_id", "source": "header", "header_name": "X-Trace-Id"},
                ],
            },
            {
                "id": "create",
                "method": "POST",
                "path": "/api/items",
                "headers": {"token": "{{auth_token}}", "X-Trace-Id": "{{trace_id}}"},
                "body": {"name": "widget-{{run_tag}}", "tag": "{{run_tag}}"},
                "depends_on": ["login"],
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {
                        "kind": "json_equals",
                        "pointer": "/data/name",
                        "expected": "widget-{{run_tag}}",
                    },
                ],
                "extract": [
                    {"name": "item_id", "source": "json", "pointer": "/data/id"},
                    {"name": "create_status", "source": "status"},
                ],
            },
            {
                "id": "read",
                "method": "GET",
                "path": "/api/items/{{item_id}}",
                "query": {"q": "{{run_tag}}"},
                "headers": {"token": "{{auth_token}}"},
                "depends_on": ["create"],
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {"kind": "json_equals", "pointer": "/data/id", "expected": "{{item_id}}"},
                    {"kind": "json_equals", "pointer": "/data/q", "expected": "{{run_tag}}"},
                ],
            },
            {
                "id": "cleanup",
                "method": "DELETE",
                "path": "/api/items/{{item_id}}",
                "headers": {"token": "{{auth_token}}"},
                "depends_on": ["create"],
                "always_run": True,
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {"kind": "json_equals", "pointer": "/data/deleted", "expected": "{{item_id}}"},
                ],
            },
        ],
    }


def test_stateful_plan_static_validation_enforces_binding_and_security():
    valid = ExecutionPlan.model_validate(_make_crud_plan("http://127.0.0.1:8080"))
    assert len(valid.requests) == 4

    # 1. Forward reference to a variable extracted in a later step must be rejected
    bad_forward = _make_crud_plan("http://127.0.0.1:8080")
    bad_forward["requests"][0]["query"] = {"premature": "{{item_id}}"}
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(bad_forward)

    # 2. Sensitive initial variable name must be rejected
    bad_secret_var = _make_crud_plan("http://127.0.0.1:8080")
    bad_secret_var["variables"]["api_key"] = "hardcoded"
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(bad_secret_var)

    # 3. Hardcoded non-JSON header without bound placeholder must still be rejected
    bad_static_header = _make_crud_plan("http://127.0.0.1:8080")
    bad_static_header["requests"][1]["headers"] = {"token": "static-secret-literal"}
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(bad_static_header)

    # 4. Reserved header even with bound variable must be rejected
    bad_reserved = _make_crud_plan("http://127.0.0.1:8080")
    bad_reserved["requests"][1]["headers"] = {"Host": "{{trace_id}}"}
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(bad_reserved)

    # 5. Dynamic variable inside a literal (non-{param}) path template segment must be rejected
    bad_literal_seg = _make_crud_plan("http://127.0.0.1:8080")
    bad_literal_seg["path_templates"] = ["/api/login", "/api/items"]
    bad_literal_seg["requests"] = bad_literal_seg["requests"][:2]
    bad_literal_seg["requests"][1]["path"] = "/api/{{trace_id}}"
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(bad_literal_seg)

    # 6. Unknown or forward depends_on ID must be rejected
    bad_dep = _make_crud_plan("http://127.0.0.1:8080")
    bad_dep["requests"][0]["depends_on"] = ["create"]
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(bad_dep)


async def test_native_engine_executes_stateful_crud_chain_and_redacts_sensitive_extract(
    crud_target,
):
    server, state = crud_target
    plan = ExecutionPlan.model_validate(_make_crud_plan(server.base_url))
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "passed", result
    assert result.observed_attempts == 4
    assert result.unknown_requests == 0
    assert [c.status for c in result.executions] == ["passed", "passed", "passed", "passed"]
    assert state["items"] == {}

    # Extracted sensitive token must be redacted in CaseResult.extracted
    login_ext = {item.name: item for item in result.executions[0].extracted}
    assert login_ext["auth_token"].ok is True
    assert login_ext["auth_token"].sensitive is True
    assert login_ext["auth_token"].value == "[REDACTED]"
    assert login_ext["trace_id"].value == "trace-login-001"

    create_ext = {item.name: item for item in result.executions[1].extracted}
    assert create_ext["item_id"].value == 100
    assert create_ext["create_status"].value == 200


async def test_native_engine_skips_failed_dependency_and_runs_always_run_teardown(crud_target):
    server, state = crud_target
    data = _make_crud_plan(server.base_url)
    # Insert a failing update step between create and read, and keep cleanup always_run=True
    data["requests"] = [
        data["requests"][0],
        data["requests"][1],
        {
            "id": "update-fails",
            "method": "PUT",
            "path": "/api/items/{{item_id}}",
            "headers": {"token": "{{auth_token}}"},
            "body": {"name": "x", "force_fail": True},
            "depends_on": ["create"],
            "assertions": [{"kind": "json_equals", "pointer": "/code", "expected": "200"}],
        },
        {
            **data["requests"][2],
            "depends_on": ["update-fails"],
        },
        data["requests"][3],  # always_run=True teardown
    ]
    plan = ExecutionPlan.model_validate(data)
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "failed"
    statuses = {c.id: (c.status, c.error_kind) for c in result.executions}
    assert statuses["login"] == ("passed", None)
    assert statuses["create"] == ("passed", None)
    assert statuses["update-fails"] == ("failed", None)
    assert statuses["read"] == ("skipped", "dependency_failed")
    assert statuses["cleanup"] == ("passed", None)
    assert result.unknown_requests == 0
    assert result.observed_attempts == 4  # read was skipped, not sent
    assert state["items"] == {}  # teardown still cleaned up the created item!


async def test_native_engine_blocks_unsafe_runtime_variable_injection(crud_target):
    server, state = crud_target
    data = _make_crud_plan(server.base_url)
    data["requests"][1]["body"] = {"name": "evil", "malicious_id": True}
    data["requests"][1]["assertions"] = [{"kind": "status", "expected": 200}]
    data["requests"][1]["extract"] = [
        {"name": "item_id", "source": "json", "pointer": "/data/id"},
        {"name": "bad_hdr", "source": "json", "pointer": "/data/bad_header"},
    ]
    data["requests"] = [
        data["requests"][0],
        data["requests"][1],
        data["requests"][2],  # path /api/items/../admin -> blocked
        {
            "id": "bad-header-step",
            "method": "GET",
            "path": "/api/items/1",
            "headers": {"token": "{{auth_token}}", "X-Bad": "{{bad_hdr}}"},
            "assertions": [{"kind": "status", "expected": 200}],
        },
    ]
    plan = ExecutionPlan.model_validate(data)
    result = await NativeHttpEngine().run(plan, {})
    assert result.status == "error"
    assert result.executions[2].status == "error"
    assert result.executions[2].error_kind == "unsafe_variable_value"
    assert result.executions[3].status == "error"
    assert result.executions[3].error_kind == "unsafe_variable_value"
    # Neither unsafe request was sent to the target server
    assert len(state["requests"]) == 2


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_postman_engine_executes_identical_stateful_crud_chain(crud_target, tmp_path):
    server, state = crud_target
    plan = ExecutionPlan.model_validate(_make_crud_plan(server.base_url))
    result = await PostmanRunner(CLI, tmp_path).run(plan, {})
    assert result.status == "passed", result
    assert result.observed_attempts == 4
    assert [c.status for c in result.executions] == ["passed", "passed", "passed", "passed"]
    assert state["items"] == {}
    login_ext = {item.name: item for item in result.executions[0].extracted}
    assert login_ext["auth_token"].ok is True
    assert login_ext["auth_token"].value == "[REDACTED]"
    assert state["token"] not in json.dumps(
        [e.model_dump(mode="json") for e in result.executions[0].extracted]
    )


async def test_document_task_executes_stateful_chain_and_redacts_secrets(client, crud_target):
    server, state = crud_target
    catalog = {
        "title": "有状态 CRUD 任务链",
        "variables": {"run_tag": "task-chain"},
        "cases": [
            {
                "name": "登录提取 token",
                "method": "POST",
                "path": "/api/login",
                "body": {"email": "task@example.com", "password": "ok-pass"},
                "assertions": [{"kind": "status", "expected": 200}],
                "extract": [
                    {
                        "name": "auth_token",
                        "source": "json",
                        "pointer": "/data/token",
                        "sensitive": True,
                    }
                ],
            },
            {
                "name": "创建资源提取 id",
                "method": "POST",
                "path": "/api/items",
                "headers": {"token": "{{auth_token}}"},
                "body": {"name": "task-{{run_tag}}"},
                "assertions": [{"kind": "status", "expected": 200}],
                "extract": [{"name": "item_id", "source": "json", "pointer": "/data/id"}],
            },
            {
                "name": "查询新建资源",
                "method": "GET",
                "path": "/api/items/{{item_id}}?q={{run_tag}}",
                "headers": {"token": "{{auth_token}}"},
                "assertions": [
                    {"kind": "status", "expected": 200},
                    {"kind": "json_equals", "pointer": "/data/id", "expected": "{{item_id}}"},
                    {"kind": "json_equals", "pointer": "/data/q", "expected": "{{run_tag}}"},
                ],
            },
            {
                "name": "删除清理资源",
                "method": "DELETE",
                "path": "/api/items/{{item_id}}",
                "headers": {"token": "{{auth_token}}"},
                "assertions": [{"kind": "status", "expected": 200}],
            },
            {
                "name": "引用缺失变量应跳过",
                "method": "GET",
                "path": "/api/items/{{missing_var}}",
                "assertions": [{"kind": "status", "expected": 200}],
            },
        ],
    }
    started = await client.post(
        "/api/tasks",
        json={"origin": server.base_url, "allow_writes": True, "catalog": catalog},
    )
    assert started.status_code == 202, started.text
    task_id = started.json()["id"]
    async with asyncio.timeout(15):
        while True:
            record = (await client.get(f"/api/tasks/{task_id}")).json()
            if record["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.02)

    assert record["status"] == "completed", record
    assert [r["status"] for r in record["results"]] == [
        "passed",
        "passed",
        "passed",
        "passed",
        "skipped",
    ]
    assert record["results"][4]["reason"] == "missing_variable:missing_var"
    assert record["results"][0]["extracted"][0]["name"] == "auth_token"
    assert record["results"][0]["extracted"][0]["value"] == "[REDACTED]"
    assert state["token"] not in json.dumps(record, ensure_ascii=False)
    assert state["items"] == {}

    # 历史快照中纯占位符凭据头应保留，真实密码仍脱敏，保证 rerun 重测链路可用
    assert record["snapshot"]["cases"][0]["body"]["password"] == "[REDACTED]"
    assert record["snapshot"]["cases"][1]["headers"]["token"] == "{{auth_token}}"
    restored_login = {
        **record["snapshot"]["cases"][0],
        "body": {"email": "task@example.com", "password": "ok-pass"},
    }
    rerun = await client.post(
        f"/api/tasks/{task_id}/rerun",
        json={
            "origin": server.base_url,
            "allow_writes": True,
            "case_edits": {"0": restored_login},
        },
    )
    assert rerun.status_code == 202, rerun.text
    rerun_id = rerun.json()["id"]
    async with asyncio.timeout(15):
        while True:
            rerun_record = (await client.get(f"/api/tasks/{rerun_id}")).json()
            if rerun_record["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.02)
    assert rerun_record["status"] == "completed", rerun_record
    assert [r["status"] for r in rerun_record["results"]] == [
        "passed",
        "passed",
        "passed",
        "passed",
        "skipped",
    ]
    assert state["token"] not in json.dumps(rerun_record, ensure_ascii=False)
    assert state["items"] == {}
