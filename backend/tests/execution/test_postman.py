import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from api_agent.execution.postman import PostmanRunner
from api_agent.execution.schemas import ExecutionPlan

CLI = Path(__file__).resolve().parents[3] / ".cache/tools/postman/postman.exe"


@pytest.fixture
def target():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("token")))
            if self.path == "/slow":
                time.sleep(1.5)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/private")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                body = {"status": "ok", "token": "PRIVATE_RESPONSE"}
                if self.path == "/prototype":
                    body = {"value": {}}
                if self.path == "/prototype-match":
                    body = {"value": {"__proto__": {"role": "admin"}}}
                if self.path == "/prototype-nested-match":
                    body = {"value": {"nested": {"__proto__": {"role": "admin"}}}}
                if self.path == "/array":
                    body = {"items": ["a", "b"]}
                self.wfile.write(json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def make_plan(origin, path="/health", expected=200, **changes):
    return ExecutionPlan.model_validate(
        {
            "origin": origin,
            "allowed_methods": ["GET"],
            "path_templates": [path],
            "requests": [
                {
                    "id": "case",
                    "method": "GET",
                    "path": path,
                    "assertions": [{"kind": "status", "expected": expected}],
                }
            ],
            **changes,
        }
    )


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_real_cli_pass_failure_and_secret_isolation(target, tmp_path):
    origin, seen = target
    runner = PostmanRunner(CLI, tmp_path)
    assert (await runner.status())["available"]
    plan = make_plan(
        origin, auth_ref={"kind": "header", "header_name": "token", "env_var": "CRM_TOKEN"}
    )
    result = await runner.run(plan, {"CRM_TOKEN": "PRIVATE_REQUEST"})
    assert result.status == "passed", result
    assert seen == [("/health", "PRIVATE_REQUEST")]
    # 凭据只出现在请求侧；响应体是目标自己返回的内容，应原样带出。
    assert "PRIVATE_REQUEST" not in result.model_dump_json()
    assert result.executions[0].response.body is not None
    assert "PRIVATE_RESPONSE" in result.executions[0].response.body
    assert list(tmp_path.iterdir()) == []
    failed = await runner.run(make_plan(origin, expected=201), {})
    assert failed.status == "failed", failed
    assert failed.executions[0].assertions[0].message is not None


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_omit_mode_strips_payload_but_keeps_verdict(target, tmp_path):
    origin, _ = target
    runner = PostmanRunner(CLI, tmp_path, omit_response_payload=True)
    result = await runner.run(make_plan(origin), {})
    assert result.status == "passed", result
    assert result.executions[0].response.omitted is True
    assert result.executions[0].response.body is None


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_real_cli_does_not_follow_redirects(target, tmp_path):
    origin, seen = target
    result = await PostmanRunner(CLI, tmp_path).run(make_plan(origin, "/redirect", 302), {})
    assert result.status == "passed", result
    assert [path for path, _ in seen] == ["/redirect"]


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_real_cli_timeout_and_cancellation_clean_workspaces(target, tmp_path, monkeypatch):
    origin, seen = target
    runner = PostmanRunner(CLI, tmp_path)
    result = await runner.run(make_plan(origin, "/slow", timeout_ms=200), {})
    assert result.status == "timed_out", result
    assert list(tmp_path.iterdir()) == []
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def record_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if str(args[0]) == str(CLI):
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    task = asyncio.create_task(runner.run(make_plan(origin, "/slow"), {}))
    async with asyncio.timeout(10):
        while not any(path == "/slow" for path, _ in seen):
            await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []
    assert processes and all(process.returncode is not None for process in processes)


async def test_missing_cli_reports_unavailability_without_paths(tmp_path):
    runner = PostmanRunner(tmp_path / "private-path.exe", tmp_path)
    assert (await runner.status()) == {"available": False, "error_kind": "cli_unavailable"}
    result = await runner.run(make_plan("http://127.0.0.1:1"), {})
    assert result.error_kind == "cli_unavailable"
    assert "private-path" not in result.model_dump_json()


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_real_cli_connection_error_is_not_product_failure(tmp_path):
    result = await PostmanRunner(CLI, tmp_path).run(make_plan("http://127.0.0.1:1"), {})
    assert result.status == "error", result


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_real_independent_mcp_cli_http_pipeline(target):
    from api_agent.infrastructure.config import load_settings
    from api_agent.tools.client import call_tool

    origin, seen = target
    settings = load_settings(env_file=None, target_allowed_origins=[origin])
    result = await call_tool(settings, "run_plan", make_plan(origin).model_dump(mode="json"))
    assert result["status"] == "passed", result
    assert result["observed_attempts"] == 1
    assert seen == [("/health", None)]


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
@pytest.mark.parametrize(
    ("path", "assertion"),
    [
        (
            "/prototype",
            {
                "kind": "json_equals",
                "pointer": "/value",
                "expected": {"__proto__": {"role": "admin"}},
            },
        ),
        ("/array", {"kind": "json_exists", "pointer": "/items/length"}),
    ],
)
async def test_real_cli_does_not_pass_incorrect_json_or_array_assertions(
    target, tmp_path, path, assertion
):
    origin, _ = target
    payload = make_plan(origin, path).model_dump(mode="json")
    payload["requests"][0]["assertions"] = [assertion]
    result = await PostmanRunner(CLI, tmp_path).run(ExecutionPlan.model_validate(payload), {})
    assert result.status == "failed", result


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/prototype-match", {"__proto__": {"role": "admin"}}),
        ("/prototype-nested-match", {"nested": {"__proto__": {"role": "admin"}}}),
    ],
)
async def test_real_cli_json_comparison_preserves_reserved_json_keys(
    target, tmp_path, path, expected
):
    origin, _ = target
    data = make_plan(origin, path).model_dump(mode="json")
    data["requests"][0]["assertions"] = [
        {"kind": "json_equals", "pointer": "/value", "expected": expected},
    ]
    result = await PostmanRunner(CLI, tmp_path).run(ExecutionPlan.model_validate(data), {})
    assert result.status == "passed", result


@pytest.mark.skipif(not CLI.is_file(), reason="Project Postman CLI not installed")
async def test_real_cli_array_pointer_accepts_only_canonical_in_range_indexes(target, tmp_path):
    origin, _ = target
    data = make_plan(origin, "/array").model_dump(mode="json")
    data["requests"] = [
        {
            "id": "index",
            "method": "GET",
            "path": "/array",
            "assertions": [
                {"kind": "json_equals", "pointer": "/items/0", "expected": "a"},
            ],
        },
        *[
            {
                "id": f"invalid-{i}",
                "method": "GET",
                "path": "/array",
                "assertions": [
                    {"kind": "json_exists", "pointer": f"/items/{token}"},
                ],
            }
            for i, token in enumerate(["length", "01", "-", "2"])
        ],
    ]
    result = await PostmanRunner(CLI, tmp_path).run(ExecutionPlan.model_validate(data), {})
    assert result.status == "failed"
    assert [case.status for case in result.executions] == [
        "passed",
        "failed",
        "failed",
        "failed",
        "failed",
    ]
