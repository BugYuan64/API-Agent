"""Regression coverage for the eight findings in the 2026-11-02 review."""

import asyncio
import gzip
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from support.local_server import LocalServer, wait_until
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from api_agent.console.service import ConsoleRequest, send
from api_agent.execution.report import normalize_report
from api_agent.execution.schemas import ExecutionPlan
from api_agent.infrastructure.config import load_settings
from api_agent.infrastructure.orm import utc_now
from api_agent.infrastructure.targets import TargetError
from api_agent.runs.models import Run
from api_agent.runs.repository import RunRepository
from api_agent.runs.schemas import RunCreate
from api_agent.runs.service import RunService
from api_agent.streams.models import StreamEventRecord, StreamSessionRecord
from api_agent.streams.service import StreamStart
from api_agent.tools import client as mcp_client


@asynccontextmanager
async def servers():
    first, second = await LocalServer().start(), await LocalServer().start()
    try:
        yield first, second
    finally:
        await first.stop()
        await second.stop()


@pytest.mark.parametrize("destination", ["off_list", "domain"])
async def test_http_redirect_revalidates_each_target(settings, destination):
    async with servers() as (src, dst):
        location = f"{dst.base_url}/json" if destination == "off_list" else "http://localhost:1/"

        @src.server.config.app.get("/review-jump")
        async def jump():
            return RedirectResponse(location)

        settings.target_allowed_origins = [src.base_url]
        with pytest.raises(TargetError):
            await send(
                settings, ConsoleRequest(url=f"{src.base_url}/review-jump", follow_redirects=True)
            )


async def test_http_authorized_redirect_strips_original_credentials(settings):
    async with servers() as (src, dst):
        observed = {}

        @dst.server.config.app.get("/review-headers")
        async def headers(request: Request):
            observed.update(request.headers)
            return {"ok": True}

        @src.server.config.app.get("/review-jump")
        async def jump():
            return RedirectResponse(f"{dst.base_url}/review-headers")

        settings.target_allowed_origins = [src.base_url, dst.base_url]
        result = await send(
            settings,
            ConsoleRequest(
                url=f"{src.base_url}/review-jump",
                follow_redirects=True,
                headers={
                    "Authorization": "Bearer PRIVATE",
                    "X-Custom-Credential": "PRIVATE",
                    "Cookie": "session=PRIVATE",
                },
            ),
        )
        assert result.ok and result.response.status == 200
        assert "PRIVATE" not in str(observed)
        assert len(result.response.redirects) == 1


async def test_http_same_origin_redirect_preserves_request(settings):
    async with servers() as (src, _dst):
        settings.target_allowed_origins = [src.base_url]
        result = await send(
            settings, ConsoleRequest(url=f"{src.base_url}/redirect", follow_redirects=True)
        )
        assert result.ok and result.response.status == 200
        assert len(result.response.redirects) == 1


async def test_http_redirect_cycle_is_bounded(settings):
    async with servers() as (src, _dst):

        @src.server.config.app.get("/review-cycle")
        async def cycle():
            return RedirectResponse("/review-cycle")

        result = await send(
            settings, ConsoleRequest(url=f"{src.base_url}/review-cycle", follow_redirects=True)
        )
        assert not result.ok
        assert result.error_kind == "request_failed"


async def test_websocket_redirect_is_rejected_before_second_connection(app):
    hits = []

    async def echo(ws):
        hits.append(True)
        await ws.close()

    async with serve(echo, "127.0.0.1", 0) as dst:
        port = dst.sockets[0].getsockname()[1]

        def redirect(connection, request):
            return Response(
                302,
                "Found",
                Headers({"Location": f"ws://127.0.0.1:{port}/", "Content-Length": "0"}),
                b"",
            )

        async with serve(echo, "127.0.0.1", 0, process_request=redirect) as src:
            origin = f"http://127.0.0.1:{src.sockets[0].getsockname()[1]}"
            stream = await app.state.streams.start(
                StreamStart(kind="websocket", url=origin), [origin]
            )
            await asyncio.wait_for(stream.task, 5)
            assert not hits
            assert stream.http_status == 302
            assert stream.stop_reason == "handshake_rejected"


@asynccontextmanager
async def sse_target(parts, extra_headers=""):
    tasks = set()

    async def handler(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n"
                    + extra_headers
                    + "\r\n"
                ).encode()
            )
            for part in parts:
                writer.write(part)
                await writer.drain()
                await asyncio.sleep(0)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            tasks.discard(asyncio.current_task())

    async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize(
    "body",
    [b"data: " + b"X" * 100_000, (b"data: " + b"X" * 1000 + b"\n") * 100],
    ids=["unfinished-line", "unfinished-event"],
)
async def test_sse_bounds_unfinished_lines_and_events(app, body):
    app.state.settings.stream_max_bytes = 1024
    async with sse_target([body]) as url:
        stream = await app.state.streams.start(StreamStart(kind="sse", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    assert stream.stop_reason == "limits_exceeded"
    assert stream.byte_count <= 1024


async def test_sse_preserves_split_utf8_crlf_and_data_spaces(app):
    body = ": hello\r\ndata:   中文\r\ndata: second\r\n\r\n".encode()
    async with sse_target([bytes([b]) for b in body]) as url:
        stream = await app.state.streams.start(StreamStart(kind="sse", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    messages = [e.data for e in stream.events if e.kind == "message"]
    assert messages == ["  中文\nsecond"]
    assert stream.byte_count == len(body)


async def test_sse_retains_complete_event_at_exact_byte_limit(app):
    app.state.settings.stream_max_bytes = 1024
    body = b"data: " + b"X" * 1016 + b"\n\n"
    assert len(body) == 1024
    async with sse_target([body]) as url:
        stream = await app.state.streams.start(StreamStart(kind="sse", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    assert [e.data for e in stream.events if e.kind == "message"] == ["X" * 1016]
    assert stream.stop_reason == "limits_exceeded"
    assert stream.byte_count == 1024


async def test_sse_rejects_compressed_payload_before_decompression(app):
    app.state.settings.stream_max_bytes = 1024
    body = gzip.compress(b"data: " + b"X" * 100_000 + b"\n\n")
    async with sse_target([body], "Content-Encoding: gzip\r\n") as url:
        stream = await app.state.streams.start(StreamStart(kind="sse", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    assert stream.error_kind == "unsupported_content_encoding"
    assert stream.byte_count == 0


@pytest.mark.parametrize("fragments", [["X" * 2048], ["X" * 600, "Y" * 600]])
async def test_websocket_bounds_whole_and_fragmented_messages(app, fragments):
    app.state.settings.stream_max_bytes = 1024

    async def target(ws):
        try:
            await ws.send(fragments if len(fragments) > 1 else fragments[0])
            await ws.wait_closed()
        except ConnectionError:
            pass

    async with serve(target, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        stream = await app.state.streams.start(StreamStart(kind="websocket", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    assert stream.stop_reason == "limits_exceeded"
    assert stream.byte_count <= 1024


@pytest.mark.parametrize("message", ["X" * 1024, b"X" * 1024], ids=["text", "binary"])
async def test_websocket_retains_complete_message_at_exact_byte_limit(app, message):
    app.state.settings.stream_max_bytes = 1024

    async def target(ws):
        await ws.send(message)
        await ws.wait_closed()

    async with serve(target, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        stream = await app.state.streams.start(StreamStart(kind="websocket", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    expected_kind = "binary" if isinstance(message, bytes) else "text"
    events = [event for event in stream.events if event.kind == expected_kind]
    assert len(events) == 1 and events[0].data_bytes == 1024
    assert stream.stop_reason == "limits_exceeded"


async def test_websocket_empty_fragments_do_not_accumulate_memory(app):
    async def fragments():
        for _ in range(4000):
            yield ""
        yield "marker"
        await asyncio.Event().wait()

    async def target(ws):
        task = asyncio.create_task(ws.send(fragments()))
        try:
            await ws.wait_closed()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with serve(target, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        stream = await app.state.streams.start(StreamStart(kind="websocket", url=url), [])

        def receiver_locals():
            coroutine = stream.task.get_coro()
            while coroutine is not None:
                frame = getattr(coroutine, "cr_frame", None)
                if frame is not None and frame.f_code.co_name == "_run_websocket":
                    return frame.f_locals if frame.f_locals.get("fragment") == "marker" else None
                coroutine = getattr(coroutine, "cr_await", None)
            return None

        try:
            state = await wait_until(receiver_locals)
            # Measure accumulated list overhead after all empty frames were consumed.
            assert sum(sys.getsizeof(v) for v in state.values() if isinstance(v, list)) < 4096
        finally:
            await app.state.streams.stop(stream.id)


async def test_websocket_empty_message_is_still_recorded(app):
    async def target(ws):
        await ws.send("")
        await ws.close()

    async with serve(target, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        stream = await app.state.streams.start(StreamStart(kind="websocket", url=url), [])
        await asyncio.wait_for(stream.task, 5)
    assert [event.data for event in stream.events if event.kind == "text"] == [""]


async def test_stream_start_atomically_reserves_slots(app, monkeypatch):
    manager = app.state.streams
    app.state.settings.stream_max_sessions = 1
    persist = manager._persist_new

    async def delayed_persist(*args):
        await asyncio.sleep(0.02)
        await persist(*args)

    async def hold(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_persist_new", delayed_persist)
    monkeypatch.setattr(manager, "_run", hold)
    results = await asyncio.gather(
        *(manager.start(StreamStart(kind="sse", url="http://127.0.0.1:1/"), []) for _ in range(3)),
        return_exceptions=True,
    )
    try:
        assert sum(not isinstance(r, Exception) for r in results) == 1
        assert len(manager.active) == 1
    finally:
        await manager.shutdown()


async def test_stream_start_releases_slot_on_persistence_failure(app, monkeypatch):
    manager = app.state.streams
    app.state.settings.stream_max_sessions = 1
    persist = manager._persist_new

    async def fail(*args):
        raise RuntimeError("fixture persistence failure")

    monkeypatch.setattr(manager, "_persist_new", fail)
    with pytest.raises(RuntimeError):
        await manager.start(StreamStart(kind="sse", url="http://127.0.0.1:1/"), [])
    assert not manager.active
    monkeypatch.setattr(manager, "_persist_new", persist)
    stream = await manager.start(StreamStart(kind="sse", url="http://127.0.0.1:1/"), [])
    await asyncio.wait_for(stream.task, 5)


async def test_stream_start_releases_slot_when_cancelled_during_persistence(app, monkeypatch):
    manager = app.state.streams
    app.state.settings.stream_max_sessions = 1
    entered = asyncio.Event()

    async def persist(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_persist_new", persist)
    task = asyncio.create_task(
        manager.start(StreamStart(kind="sse", url="http://127.0.0.1:1/"), [])
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not manager.active


async def test_immediate_stop_releases_reserved_slot(app):
    manager = app.state.streams
    stream = await manager.start(StreamStart(kind="sse", url="http://127.0.0.1:1/"), [])
    stopped = await manager.stop(stream.id)
    assert stopped.finished
    assert stopped.stop_reason == "client_stopped"
    assert stream.id not in manager.active


@pytest.mark.parametrize(
    "initial,concurrent,expected",
    [("running", "completed", "completed"), ("queued", "running", "cancelling")],
)
async def test_cancel_uses_database_state_instead_of_stale_identity(
    app, project_id, initial, concurrent, expected
):
    await app.state.worker.stop()
    async with app.state.database.session() as db:
        run = await RunService(db).create(RunCreate(project_id=project_id, actions=[]))
        if initial == "running":
            await RunRepository(db).claim(run.id, utc_now() + timedelta(seconds=30))
    async with app.state.database.session() as cancel_db:
        stale = await cancel_db.get(Run, run.id)
        assert stale.status == initial
        async with app.state.database.session() as worker_db:
            repo = RunRepository(worker_db)
            if concurrent == "completed":
                await repo.finish(run.id, "completed", "completed")
            else:
                await repo.claim(run.id, utc_now() + timedelta(seconds=30))
        result = await RunRepository(cancel_db).request_cancel(run.id)
        assert result.status == expected
    async with app.state.database.session() as db:
        assert (await db.get(Run, run.id)).status == expected


async def test_execution_validates_project_before_running(client, monkeypatch):
    calls = []

    class Service:
        def parse(self, plan):
            pass

        def resolve_engine(self, name):
            return None

        async def run(self, plan, engine=None):
            calls.append(plan)
            return {"status": "passed"}

    monkeypatch.setattr("api_agent.execution.routes._service", lambda request: Service())
    response = await client.post("/api/executions", json={"plan": {}, "project_id": "missing"})
    assert response.status_code == 404
    assert not calls
    assert (await client.get("/api/executions")).json()["page"]["total"] == 0


async def test_mcp_client_preserves_omit_setting(tmp_path, monkeypatch):
    captured = {}

    class Client:
        def __init__(self, params, **kwargs):
            captured.update(params.env)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(mcp_client, "Client", Client)
    settings = load_settings(env_file=None, project_root=tmp_path, omit_response_payload=True)
    async with mcp_client.open_client(settings):
        pass
    assert captured.get("OMIT_RESPONSE_PAYLOAD") == "true"


def test_omit_response_also_omits_failed_assertion_values():
    plan = ExecutionPlan.model_validate(
        {
            "origin": "http://127.0.0.1:8080",
            "allowed_methods": ["GET"],
            "path_templates": ["/token"],
            "requests": [
                {
                    "id": "token",
                    "method": "GET",
                    "path": "/token",
                    "assertions": [
                        {"kind": "json_equals", "pointer": "/token", "expected": "expected"}
                    ],
                }
            ],
        }
    )
    report = {
        "run": {
            "executions": [
                {
                    "item": {"name": "token"},
                    "response": {"code": 200},
                    "assertions": [
                        {
                            "assertion": "a0_json_equals",
                            "error": {
                                "message": "expected 'PRIVATE_TOKEN' to deeply equal 'expected'"
                            },
                        }
                    ],
                }
            ]
        }
    }
    result = normalize_report(report, plan, 1, capture=False)
    assert result.status == "failed"
    assert result.executions[0].assertions[0].passed is False
    assert "PRIVATE_TOKEN" not in result.model_dump_json()
    assert "PRIVATE_TOKEN" in normalize_report(report, plan, 1, capture=True).model_dump_json()


async def test_real_mcp_omit_mode_keeps_failed_verdict_without_private_values(tmp_path):
    cli = Path(__file__).resolve().parents[2] / ".cache/tools/postman/postman.exe"
    if not cli.is_file():
        pytest.skip("Project Postman CLI not installed")
    async with servers() as (target, _other):
        settings = load_settings(
            env_file=None,
            project_root=tmp_path,
            postman_cli_path=cli,
            target_allowed_origins=[target.base_url],
            omit_response_payload=True,
        )
        plan = {
            "origin": target.base_url,
            "allowed_methods": ["POST"],
            "path_templates": ["/echo"],
            "requests": [
                {
                    "id": "private",
                    "method": "POST",
                    "path": "/echo",
                    "body": {"token": "PRIVATE_RESPONSE_VALUE"},
                    "assertions": [
                        {"kind": "json_equals", "pointer": "/body", "expected": "expected"}
                    ],
                }
            ],
        }
        result = await mcp_client.call_tool(settings, "run_plan", plan)
    assert result["status"] == "failed"
    assert result["executions"][0]["response"]["omitted"] is True
    assert result["executions"][0]["assertions"][0]["passed"] is False
    assert "PRIVATE_RESPONSE_VALUE" not in str(result)


@pytest.mark.parametrize("count", [500, 650, 1100])
async def test_stored_stream_replay_exposes_all_pages(app, client, count):
    async with app.state.database.session() as db:
        db.add(
            StreamSessionRecord(
                id="stored-review",
                kind="sse",
                url="http://127.0.0.1:1/",
                request_headers={},
                status="closed",
                event_count=count,
                settings={},
            )
        )
        await db.flush()
        db.add_all(
            StreamEventRecord(
                session_id="stored-review",
                sequence=i,
                direction="in",
                kind="message",
                data=str(i),
                data_bytes=1,
                truncated=False,
                elapsed_ms=i,
            )
            for i in range(1, count + 1)
        )
        await db.commit()
    cursor, seen = 0, []
    while True:
        page = (await client.get(f"/api/streams/stored-review?after={cursor}")).json()
        assert page["finished"] is True
        seen.extend(e["sequence"] for e in page["events"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
        assert page["events"]
    assert seen == list(range(1, count + 1))
