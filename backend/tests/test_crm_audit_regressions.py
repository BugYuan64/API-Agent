"""Regression tests for CRM full-test audit findings A.1 through A.7."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, WebSocket
from fastapi.responses import JSONResponse
from support.local_server import LocalServer

from api_agent.execution.postman import PostmanRunner
from api_agent.execution.report import normalize_report
from api_agent.execution.schemas import ExecutionPlan
from api_agent.infrastructure.privacy import Context
from api_agent.streams.runner import StreamSuiteRunner
from api_agent.streams.schemas import StreamAiCaseSpec, StreamAiSuiteSpec

CLI = Path(__file__).resolve().parents[2] / ".cache/tools/postman/postman.exe"


def test_privacy_email_regex_is_linear_time_no_redos():
    """A.1: 200k-char non-email payload (and with trailing @) must scrub in < 50ms."""
    ctx = Context()
    huge_no_at = "A" * 200_000
    t0 = time.perf_counter()
    assert ctx.text(huge_no_at) == huge_no_at
    dt1 = time.perf_counter() - t0
    assert dt1 < 0.05, f"ReDoS regression without @: {dt1:.4f}s"

    huge_with_at = "A" * 100_000 + " contact admin@example.com " + "B" * 100_000
    t1 = time.perf_counter()
    cleaned = ctx.text(huge_with_at)
    dt2 = time.perf_counter() - t1
    assert dt2 < 0.05, f"ReDoS regression with @: {dt2:.4f}s"
    assert "[EMAIL]" in cleaned
    assert "admin@example.com" not in cleaned


def test_postman_normalize_report_populates_actual_and_non_sensitive_extract():
    """A.4: Postman report normalization populates actual observations & non-sensitive extracts."""
    plan = ExecutionPlan.model_validate(
        {
            "origin": "http://127.0.0.1:9000",
            "allowed_methods": ["POST"],
            "path_templates": ["/api/login"],
            "requests": [
                {
                    "id": "login",
                    "method": "POST",
                    "path": "/api/login",
                    "assertions": [
                        {"kind": "status", "expected": 200},
                        {"kind": "json_equals", "pointer": "/code", "expected": "200"},
                        {"kind": "json_exists", "pointer": "/data/user"},
                    ],
                    "extract": [
                        {
                            "name": "auth_token",
                            "source": "json",
                            "pointer": "/data/token",
                            "sensitive": True,
                        },
                        {"name": "user_name", "source": "json", "pointer": "/data/user"},
                        {"name": "trace_id", "source": "header", "header_name": "X-Trace-Id"},
                        {"name": "http_code", "source": "status"},
                    ],
                }
            ],
        }
    )
    body = json.dumps({"code": "200", "data": {"token": "jwt-secret-999", "user": "alice"}})
    raw_report = {
        "run": {
            "executions": [
                {
                    "item": {"name": "login"},
                    "response": {
                        "code": 200,
                        "status": "OK",
                        "responseTime": 15,
                        "header": [
                            {"key": "Content-Type", "value": "application/json"},
                            {"key": "X-Trace-Id", "value": "trace-abc"},
                        ],
                        "stream": body,
                    },
                    "assertions": [
                        {"assertion": "a0_status"},
                        {"assertion": "a1_json_equals"},
                        {"assertion": "a2_json_exists"},
                        {"assertion": "e0_auth_token"},
                        {"assertion": "e1_user_name"},
                        {"assertion": "e2_trace_id"},
                        {"assertion": "e3_http_code"},
                    ],
                }
            ]
        }
    }
    result = normalize_report(raw_report, plan, exit_code=0, capture=True)
    assert result.status == "passed"
    case = result.executions[0]
    assert case.assertions[0].actual == 200
    assert case.assertions[0].actual_present is True
    assert case.assertions[0].actual_type == "number"
    assert case.assertions[1].actual == "200"
    assert case.assertions[1].actual_present is True
    assert case.assertions[2].actual == "alice"

    extracted = {item.name: item for item in case.extracted}
    assert extracted["auth_token"].value == "[REDACTED]"
    assert extracted["auth_token"].sensitive is True
    assert extracted["user_name"].value == "alice"
    assert extracted["trace_id"].value == "trace-abc"
    assert extracted["http_code"].value == 200


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_postman_engine_enforces_depends_on_always_run_and_missing_var(tmp_path):
    """A.2 + A.4: Postman skips failed dependencies & missing vars without sending requests."""
    app = FastAPI()
    hits: list[tuple[str, str]] = []

    @app.post("/api/fail")
    async def fail_step():
        hits.append(("POST", "/api/fail"))
        return JSONResponse({"code": "500"}, status_code=200)

    @app.get("/api/after/{item_id}")
    async def after_step(item_id: str):
        hits.append(("GET", f"/api/after/{item_id}"))
        return JSONResponse({"code": "200"})

    @app.delete("/api/cleanup")
    async def cleanup_step():
        hits.append(("DELETE", "/api/cleanup"))
        return JSONResponse({"code": "200", "cleaned": True})

    server = await LocalServer(app).start()
    try:
        plan = ExecutionPlan.model_validate(
            {
                "origin": server.base_url,
                "allowed_methods": ["GET", "POST", "DELETE"],
                "path_templates": ["/api/fail", "/api/after/{id}", "/api/cleanup"],
                "timeout_ms": 90_000,
                "requests": [
                    {
                        "id": "step1",
                        "method": "POST",
                        "path": "/api/fail",
                        "assertions": [
                            {"kind": "status", "expected": 200},
                            {"kind": "json_equals", "pointer": "/code", "expected": "200"},
                        ],
                        "extract": [{"name": "item_id", "source": "json", "pointer": "/data/id"}],
                    },
                    {
                        "id": "dep-skipped",
                        "method": "GET",
                        "path": "/api/after/{{item_id}}",
                        "depends_on": ["step1"],
                        "assertions": [{"kind": "status", "expected": 200}],
                    },
                    {
                        "id": "missing-var-skipped",
                        "method": "GET",
                        "path": "/api/after/{{item_id}}",
                        "assertions": [{"kind": "status", "expected": 200}],
                    },
                    {
                        "id": "teardown",
                        "method": "DELETE",
                        "path": "/api/cleanup",
                        "depends_on": ["step1"],
                        "always_run": True,
                        "assertions": [
                            {"kind": "status", "expected": 200},
                            {"kind": "json_equals", "pointer": "/cleaned", "expected": True},
                        ],
                    },
                ],
            }
        )
        result = await PostmanRunner(CLI, tmp_path).run(plan, {})
        assert result.status == "failed"
        statuses = {c.id: (c.status, c.error_kind) for c in result.executions}
        assert statuses["step1"] == ("failed", None)
        assert statuses["dep-skipped"] == ("skipped", "dependency_failed")
        assert statuses["missing-var-skipped"] == ("skipped", "missing_variable")
        assert statuses["teardown"] == ("passed", None)
        # Neither skipped case sent a fallback HTTP request
        assert hits == [("POST", "/api/fail"), ("DELETE", "/api/cleanup")]
        assert result.observed_attempts == 2
        assert result.unknown_requests == 0
        # Teardown assertion carries actual observation
        teardown = next(c for c in result.executions if c.id == "teardown")
        assert teardown.assertions[0].actual == 200
        assert teardown.assertions[1].actual is True
    finally:
        await server.stop()


async def test_engine_list_and_status_use_consistent_ids_and_driver_names(client):
    """A.4: /engine/list and /engine/status both expose consistent id and engine fields."""
    listing = await client.get("/api/executions/engine/list")
    assert listing.status_code == 200
    items = {item["id"]: item for item in listing.json()["items"]}
    assert items["native"]["engine"] == "native"
    assert items["postman"]["engine"] == "postman-cli"

    for engine_id, expected_driver in [("native", "native"), ("postman", "postman-cli")]:
        status = await client.get("/api/executions/engine/status", params={"engine": engine_id})
        assert status.status_code == 200
        body = status.json()
        assert body["id"] == engine_id
        assert body["engine"] == expected_driver


async def test_ws_ai_suite_persists_assertion_failure_after_cache_eviction(app):
    """A.5: Failed WS assertion status survives cache eviction and server restart."""
    ws_app = FastAPI()

    @ws_app.websocket("/ws")
    async def ws_echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("hello-actual")
        await websocket.close(code=1000)

    server = await LocalServer(ws_app).start()
    try:
        runner: StreamSuiteRunner = app.state.stream_suites
        spec = StreamAiSuiteSpec(
            url=f"{server.base_url.replace('http://', 'ws://', 1)}/ws",
            title="WS 断言失败持久化测试",
            rounds=1,
            cooldown_ms=0,
            cases=[
                StreamAiCaseSpec(
                    label="期望不匹配内容",
                    expect_contains="never-sent-text",
                    max_duration_ms=2000,
                )
            ],
        )
        started = await runner.start_ai_suite(spec)
        suite_id = started["id"]
        done = await runner.wait_for_completion(suite_id)
        assert done["failed_cases"] == 1
        assert done["case_links"][0]["status"] == "failed"

        # Evict in-memory completed cache to simulate restart / FIFO eviction
        runner._completed.clear()
        reloaded = await runner.wait_for_completion(suite_id)
        assert reloaded["failed_cases"] == 1
        assert reloaded["passed_cases"] == 0
        assert reloaded["case_links"][0]["status"] == "failed"
        assert reloaded["case_links"][0]["tone"] == "danger"
        assert reloaded["case_links"][0].get("notes")
    finally:
        await server.stop()


async def test_task_static_token_direct_rerun_and_runtime_headers(client, app):
    """A.6: Static token task reruns directly without sending [REDACTED] and keeps disk clean."""
    target_app = FastAPI()
    seen_tokens: list[str | None] = []

    @target_app.get("/api/protected")
    async def protected(token: str | None = Header(default=None)):
        seen_tokens.append(token)
        if token == "live-secret-token-xyz":
            return JSONResponse({"ok": True})
        if token == "rotated-secret-token-456":
            return JSONResponse({"ok": True, "rotated": True})
        return JSONResponse({"ok": False}, status_code=401)

    server = await LocalServer(target_app).start()
    try:
        catalog = {
            "title": "静态 Token 重测验证",
            "cases": [
                {
                    "name": "受保护接口",
                    "method": "GET",
                    "path": "/api/protected",
                    "headers": {"token": "live-secret-token-xyz"},
                    "assertions": [{"kind": "status", "expected": 200}],
                }
            ],
        }
        start_resp = await client.post(
            "/api/tasks",
            json={"origin": server.base_url, "allow_writes": False, "catalog": catalog},
        )
        assert start_resp.status_code == 202
        first_id = start_resp.json()["id"]
        async with asyncio.timeout(10):
            while True:
                first_rec = (await client.get(f"/api/tasks/{first_id}")).json()
                if first_rec["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
        assert first_rec["results"][0]["status"] == "passed"
        assert first_rec["snapshot"]["cases"][0]["headers"]["token"] == "[REDACTED]"
        assert "live-secret-token-xyz" not in json.dumps(first_rec)

        # 1) Direct rerun WITHOUT case_edits restores in-memory secret cleanly
        rerun1 = await client.post(
            f"/api/tasks/{first_id}/rerun",
            json={"origin": server.base_url, "allow_writes": False},
        )
        assert rerun1.status_code == 202
        rerun1_id = rerun1.json()["id"]
        async with asyncio.timeout(10):
            while True:
                rec1 = (await client.get(f"/api/tasks/{rerun1_id}")).json()
                if rec1["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
        assert rec1["results"][0]["status"] == "passed"
        assert rec1["snapshot"]["cases"][0]["headers"]["token"] == "[REDACTED]"
        assert "live-secret-token-xyz" not in json.dumps(rec1)

        # 2) Simulate restart (clear ephemeral cache) + rerun with runtime_headers
        app.state.tasks._ephemeral_headers.clear()
        rerun2 = await client.post(
            f"/api/tasks/{first_id}/rerun",
            json={
                "origin": server.base_url,
                "allow_writes": False,
                "runtime_headers": {"token": "rotated-secret-token-456"},
            },
        )
        assert rerun2.status_code == 202
        rerun2_id = rerun2.json()["id"]
        async with asyncio.timeout(10):
            while True:
                rec2 = (await client.get(f"/api/tasks/{rerun2_id}")).json()
                if rec2["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
        assert rec2["results"][0]["status"] == "passed"
        assert rec2["snapshot"]["cases"][0]["headers"]["token"] == "[REDACTED]"
        assert "rotated-secret-token-456" not in json.dumps(rec2)
        assert seen_tokens == [
            "live-secret-token-xyz",
            "live-secret-token-xyz",
            "rotated-secret-token-456",
        ]
    finally:
        await server.stop()


def test_console_sse_duration_not_halved():
    """A.7: console/service.py no longer silently halves timeout_ms for sse_duration_ms."""
    src = (
        Path(__file__).resolve().parents[1] / "src" / "api_agent" / "console" / "service.py"
    ).read_text(encoding="utf-8")
    assert "min(payload.sse_duration_ms, payload.timeout_ms)" in src
    assert "timeout_ms // 2" not in src
