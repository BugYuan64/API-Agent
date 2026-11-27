import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from support.local_server import LocalServer

from api_agent.infrastructure.orm import as_utc, utc_now
from api_agent.infrastructure.targets import TargetError, redact_headers, validate_target


@pytest.fixture
async def target():
    """启动一个真实的回环目标服务供本模块使用。

    实现：每个测试自己起停一个实例，保证端口与状态互不干扰；
    测试结束无论成功失败都会关闭，避免句柄泄漏。
    """
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


async def test_live_console_delivers_events_before_target_finishes_and_saves_history(app, client):
    upstream = FastAPI()
    proceed = asyncio.Event()
    closed = asyncio.Event()

    @upstream.post("/sse")
    async def stream():
        async def events():
            yield b": comment\r\n\r\ndata: first\r"
            yield b"\n\r"
            await proceed.wait()
            yield "\ndata: 中文\ndata: next\n\n".encode()
            closed.set()

        return StreamingResponse(events(), media_type="text/event-stream")

    target_server = await LocalServer(upstream).start()
    management = LocalServer(app)
    try:
        await management.start()
        async with (
            httpx.AsyncClient(base_url=management.base_url, trust_env=False) as browser,
            browser.stream(
                "POST",
                "/api/console/requests/live",
                json={"url": target_server.base_url + "/sse", "method": "POST"},
            ) as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/x-ndjson"
            lines = response.aiter_lines()
            assert json.loads(await anext(lines)) == {"type": "sse_start"}
            first = json.loads(await anext(lines))
            assert first == {"type": "sse_event", "event": {"event": "message", "data": "first"}}
            assert not closed.is_set()
            assert (await client.get("/api/console/history")).json()["items"] == []
            proceed.set()
            second = json.loads(await anext(lines))
            assert second["event"]["data"] == "中文\nnext"
            final = json.loads(await anext(lines))
            assert final["type"] == "result"
            assert final["result"]["response"]["sse"] == {
                "events": [first["event"], second["event"]],
                "stop_reason": "target_closed",
            }
            assert [line async for line in lines] == []
        assert len((await client.get("/api/console/history")).json()["items"]) == 1
    finally:
        proceed.set()
        await management.stop()
        await target_server.stop()


async def test_live_console_preserves_target_and_policy_results(client, target):
    completed = await client.post(
        "/api/console/requests/live",
        json={"url": target.base_url + "/json"},
    )
    assert len(completed.text.splitlines()) == 1
    assert completed.json()["result"]["response"]["status"] == 200
    rejected = await client.post(
        "/api/console/requests/live",
        json={"url": "http://user:pass@127.0.0.1:8010/health"},
    )
    assert rejected.json()["type"] == "error"
    assert rejected.json()["status"] == 400
    assert rejected.json()["error"]["code"] == "target_credentials_not_allowed"
    assert len((await client.get("/api/console/history")).json()["items"]) == 1


async def test_console_returns_response_body_headers_and_history(client, target):
    """控制台请求应返回真实回包，并落一条脱敏的历史记录。

    实现：断言响应体被完整解析回 JSON、耗时为正、历史列表能取到同一条记录，
    并且 ``Authorization`` 在落库时已脱敏——即时回包保留原值即可，
    但持久化副本不能存凭据。
    """
    response = await client.post(
        "/api/console/requests",
        json={
            "method": "POST",
            "url": f"{target.base_url}/echo?mode=console",
            "headers": {"Authorization": "Bearer secret-token", "X-Trace": "trace-1"},
            "body_mode": "json",
            "body": '{"hello":"world"}',
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["history_id"]
    assert payload["response"]["status"] == 200
    assert payload["response"]["body_bytes"] > 0
    echoed = __import__("json").loads(payload["response"]["body"])
    assert echoed["method"] == "POST"
    assert echoed["x-trace"] == "trace-1"
    assert echoed["query"] == {"mode": "console"}

    history = await client.get("/api/console/history")
    assert history.status_code == 200
    items = history.json()["items"]
    assert items[0]["id"] == payload["history_id"]
    stored = {name.lower(): value for name, value in items[0]["request_headers"].items()}
    assert stored["authorization"] == "[redacted]"
    assert stored["x-trace"] == "trace-1"
    assert items[0]["status_code"] == 200


async def test_console_records_transport_failure_without_faking_status(client):
    """连不上时应记一条失败历史，而不是伪造 HTTP 状态码。

    实现：把端口指向一个刚关闭的监听端口，断言 ``ok=false``、
    ``error_kind=request_failed`` 且 ``response`` 为 None。
    """
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    response = await client.post(
        "/api/console/requests",
        json={"method": "GET", "url": f"http://127.0.0.1:{port}/nobody"},
    )
    assert response.status_code == 502, response.text
    payload = response.json()
    assert payload["ok"] is False
    assert payload["response"] is None
    assert payload["error_kind"] == "request_failed"
    assert payload["result_code"] == "CONNECTION_FAILED"


async def test_console_classifies_timeout(client, target):
    """超时应单独归类，不能与连接失败混为一谈。

    实现：目标 ``/slow`` 睡 3 秒，客户端 300ms 超时，断言 ``request_timeout``。
    """
    response = await client.post(
        "/api/console/requests",
        json={"method": "GET", "url": f"{target.base_url}/slow", "timeout_ms": 300},
    )
    assert response.status_code == 504
    assert response.json()["error_kind"] == "request_timeout"


async def test_console_does_not_follow_redirects_by_default(client, target):
    """默认不跟随跳转：目标是 302 就必须看到 302。

    实现：``/redirect`` 指向 ``/json``，断言状态为 302 且 ``redirects`` 为空。
    """
    response = await client.post(
        "/api/console/requests",
        json={"method": "GET", "url": f"{target.base_url}/redirect"},
    )
    payload = response.json()
    assert payload["ok"] is True
    assert payload["response"]["status"] == 302
    assert payload["response"]["redirects"] == []


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://127.0.0.1:8080/json#frag", "target_url_invalid"),
        ("http://bad host/json", "target_url_invalid"),
        ("http://user:pass@127.0.0.1:8080/json", "target_credentials_not_allowed"),
        ("http://user:pass@example.com/json", "target_credentials_not_allowed"),
        ("ftp://127.0.0.1/json", "target_scheme_not_allowed"),
    ],
)
async def test_console_rejects_out_of_scope_targets(client, url, code):
    """非法协议、带内嵌凭据或格式非法目标必须在发请求前被拒绝。

    实现：策略拒绝走 400 并带固定错误码，响应体是统一的 ``error`` 结构，
    这样界面能按 code 分支提示，而不是解析自然语言。
    """
    response = await client.post("/api/console/requests", json={"method": "GET", "url": url})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_target_policy_accepts_domains_lan_public_and_enforces_optional_allowlist():
    """默认放行合法域名、局域网 IP、回环与公网 IP；显式配置白名单时仍严格收口。"""
    from api_agent.console.service import build_url

    for raw_url, expected_origin in (
        ("https://API.GitHub.com/repos?page=1", "https://api.github.com"),
        ("http://localhost:8080/health", "http://localhost:8080"),
        ("http://192.168.1.10:8080/json", "http://192.168.1.10:8080"),
        ("https://8.8.8.8/dns-query", "https://8.8.8.8"),
        ("http://[::1]:8080/x", "http://[::1]:8080"),
    ):
        parsed, origin = validate_target(raw_url, [])
        assert origin == expected_origin
        assert build_url(parsed).startswith(expected_origin)

    validate_target("https://api.github.com/repos", ["https://api.github.com"])
    with pytest.raises(TargetError, match="target_origin_not_allowed"):
        validate_target("https://example.com/repos", ["https://api.github.com"])


async def test_console_respects_trust_system_proxy_setting(app, client, target, monkeypatch):
    """系统代理开关应透传为 httpx.AsyncClient 的 trust_env 参数。"""
    seen_trust_env: list[bool] = []
    original_client = httpx.AsyncClient

    def tracking_client(*args, **kwargs):
        seen_trust_env.append(bool(kwargs.get("trust_env")))
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", tracking_client)

    app.state.settings.trust_system_proxy = False
    first = await client.post(
        "/api/console/requests", json={"method": "GET", "url": f"{target.base_url}/json"}
    )
    assert first.status_code == 200
    assert seen_trust_env[-1] is False

    app.state.settings.trust_system_proxy = True
    second = await client.post(
        "/api/console/requests", json={"method": "GET", "url": f"{target.base_url}/json"}
    )
    assert second.status_code == 200
    assert seen_trust_env[-1] is True


async def test_cross_origin_redirect_strips_credentials(client):
    """跟随跨源重定向时必须剥离 Authorization、Cookie 与自定义头，防止凭据泄漏。"""
    from fastapi import Request
    from fastapi.responses import RedirectResponse

    receiver = FastAPI()

    @receiver.get("/check")
    async def check(request: Request):
        return {k.lower(): v for k, v in request.headers.items()}

    other = await LocalServer(receiver).start()
    redirector = FastAPI()

    @redirector.get("/bounce")
    async def bounce():
        return RedirectResponse(url=f"{other.base_url}/check", status_code=302)

    source = await LocalServer(redirector).start()
    try:
        response = await client.post(
            "/api/console/requests",
            json={
                "method": "GET",
                "url": f"{source.base_url}/bounce",
                "headers": {
                    "Authorization": "Bearer top-secret",
                    "Cookie": "sid=secret",
                    "X-Custom": "drop-me",
                },
                "follow_redirects": True,
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["response"]["redirects"][0]["cross_origin"] is True
        seen_headers = json.loads(payload["response"]["body"])
        assert "authorization" not in seen_headers
        assert "cookie" not in seen_headers
        assert "x-custom" not in seen_headers
    finally:
        await source.stop()
        await other.stop()


async def test_console_rejects_reserved_and_invalid_headers(client):
    """保留头必须在入口被拒绝，而不是被静默忽略。

    实现：``Host``/``Content-Length`` 会改变报文语义，
    覆盖它们属于协议违规，返回 422（字段级校验失败）。
    """
    for headers in ({"Host": "evil"}, {"Content-Length": "10"}, {"X-Bad": "line\nbreak"}):
        response = await client.post(
            "/api/console/requests",
            json={"method": "GET", "url": "http://127.0.0.1:9/x", "headers": headers},
        )
        assert response.status_code == 422, headers


def test_scope_policy_maps_stream_schemes_to_http_origins():
    """允许列表里的 http origin 必须同时授权 ws，反之亦然。

    实现：``validate_target`` 接受 ws/wss 并把它们映射回 http/https 比对，
    因此同一主机端口不会因为协议写法不同被误拒。
    """
    schemes = frozenset({"http", "https", "ws", "wss"})
    _parsed, origin = validate_target("ws://127.0.0.1:8080/socket", [], schemes)
    assert origin == "ws://127.0.0.1:8080"
    validate_target("ws://127.0.0.1:8080/socket", ["http://127.0.0.1:8080"], schemes)
    with pytest.raises(TargetError, match="target_origin_not_allowed"):
        validate_target("ws://127.0.0.1:8080/socket", ["http://127.0.0.1:9999"], schemes)


def test_redaction_only_touches_credential_headers():
    """脱敏只应命中凭据类头，其余头必须原样保留以便排查。

    实现：一次给全敏感与非敏感头，断言敏感值被替换、非敏感值不变。
    多值头（``Set-Cookie`` 以列表传入）逐项替换并保持列表形态——
    把它压成单个字符串会丢失“目标发了几个 Set-Cookie”这一事实。
    """
    redacted = redact_headers(
        {"Authorization": "Bearer x", "Cookie": "a=1", "X-Trace": "keep-me", "Set-Cookie": ["a"]}
    )
    assert redacted["Authorization"] == "[redacted]"
    assert redacted["Cookie"] == "[redacted]"
    assert redacted["Set-Cookie"] == ["[redacted]"]
    assert redacted["X-Trace"] == "keep-me"


def test_utc_helper_marks_naive_timestamps():
    """SQLite 读回的 naive 时间必须被补上 UTC 时区。

    实现：naive 值补时区、aware 值保持原样，避免同一响应里出现两种时间语义。
    """
    naive = utc_now().replace(tzinfo=None)
    assert as_utc(naive).tzinfo is not None
    aware = utc_now() + timedelta(seconds=1)
    assert as_utc(aware) == aware


async def test_redirect_is_reported_as_a_real_response_when_not_followed(client, target):
    """默认不跟随跳转时，302 是目标真实回包，不是失败。

    实现：断言外层管理接口仍是 200、``ok`` 为真、``response.status`` 保留 302，
    且 ``redirects`` 为空——空表示“没有被跟随”，不是“目标没有跳转”。
    """
    response = await client.post(
        "/api/console/requests",
        json={"method": "GET", "url": f"{target.base_url}/redirect", "body_mode": "none"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["result_code"] == "HTTP_RESPONSE"
    assert payload["response"]["status"] == 302
    assert payload["response"]["reason"] == "Found"
    assert payload["response"]["redirects"] == []
    assert payload["response"]["headers"]["location"] == ["/json"]


async def test_followed_redirect_chain_exposes_every_hop(client, target):
    """跟随跳转时，每一跳都要带上状态码、原因与目标地址。

    实现：302 指向同源 ``/json``，因此只有一跳且不跨源；断言这三个字段
    来自目标真实回包，界面据此才能画出 ``302 → 200`` 的链路。
    """
    response = await client.post(
        "/api/console/requests",
        json={
            "method": "GET",
            "url": f"{target.base_url}/redirect",
            "body_mode": "none",
            "follow_redirects": True,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["response"]["status"] == 200
    hops = payload["response"]["redirects"]
    assert len(hops) == 1
    hop = hops[0]
    assert hop["status"] == 302
    assert hop["reason"] == "Found"
    assert hop["url"].endswith("/json")
    assert hop["cross_origin"] is False
