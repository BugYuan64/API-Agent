"""History boundaries use the real API and SQLite; outbound HTTP stays synthetic."""

import importlib
import importlib.util
import json
from urllib.parse import urlencode

import httpx
import pytest
from sqlalchemy import select

from api_agent.console.models import ConsoleRequestRecord
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.targets import redact_headers
from api_agent.streams.models import StreamSessionRecord


def transport_response(monkeypatch, response):
    original = httpx.AsyncClient

    def client(**kwargs):
        return original(transport=httpx.MockTransport(response), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


@pytest.mark.parametrize("mode", ["json", "text", "form"])
async def test_history_learns_complete_bodies_before_cleaning_and_truncating(
    client, app, monkeypatch, mode
):
    password = "synthetic-password-with-percent%and+plus"
    token = "synthetic-response-token-private"
    header_token = "synthetic-header-token-private"
    fields = {"echo": password, "padding": "z" * 9000, "password": password}
    body = urlencode(fields) if mode == "form" else json.dumps(fields)
    response_body = json.dumps({"echo": token, "padding": "q" * 9000, "access_token": token})

    def respond(request):
        assert request.headers["token"] == header_token
        assert request.content.decode() == body
        return httpx.Response(
            200,
            text=response_body,
            headers=[
                ("Set-Cookie", "session=synthetic-cookie-private; Path=/"),
                ("X-Trace", "response-trace"),
            ],
        )

    transport_response(monkeypatch, respond)
    result = await client.post(
        "/api/console/requests",
        json={
            "method": "POST",
            "url": "http://127.0.0.1:9100/login?token=url-token-private",
            "headers": {"token": header_token, "X-Trace": "request-trace"},
            "body_mode": mode,
            "body": body,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["response"]["body"] == response_body
    assert result.json()["response"]["headers"]["set-cookie"] == [
        "session=synthetic-cookie-private; Path=/"
    ]
    history = (await client.get("/api/console/history")).json()["items"][0]
    async with app.state.database.session() as session:
        stored = await session.get(ConsoleRequestRecord, history["id"])
        persisted = {
            key: getattr(stored, key)
            for key in (
                "url",
                "request_headers",
                "body_preview",
                "response_headers",
                "response_preview",
            )
        }
    for boundary in (history, persisted):
        serialized = json.dumps(boundary)
        for secret in (
            password,
            token,
            header_token,
            "url-token-private",
            "synthetic-cookie-private",
        ):
            assert secret not in serialized
        assert boundary["request_headers"]["X-Trace"] == "request-trace"
        assert boundary["response_headers"]["x-trace"] == ["response-trace"]
        assert isinstance(boundary["response_headers"]["set-cookie"], list)
        assert len(boundary["body_preview"]) <= 8192
        assert len(boundary["response_preview"]) <= 8192


def legacy_console():
    return ConsoleRequestRecord(
        method="POST",
        url="http://127.0.0.1:9100/login?access_token=old-query-private",
        request_headers={"X-Auth-Token": "old-header-private", "X-Trace": "keep-trace"},
        body_mode="text",
        body_preview='{"password":"old-password-private","echo":"old-response-private"}',
        ok=False,
        error_kind="request_failed",
        error_message="failed old-password-private old-header-private",
        response_headers={"Set-Cookie": ["session=old-cookie-private; Path=/"]},
        response_preview='{"access_token":"old-response-private","echo":"old-password-private"}',
        response_bytes=100,
        elapsed_ms=2,
    )


async def test_legacy_history_reads_clean_all_fields_and_sibling_echoes(client, app):
    async with app.state.database.session() as session:
        record = legacy_console()
        session.add(record)
        await session.commit()
        record_id = record.id
    response = await client.get("/api/console/history")
    assert response.status_code == 200
    saved = response.json()["items"][0]
    assert saved["id"] == record_id
    for secret in (
        "old-password-private",
        "old-response-private",
        "old-header-private",
        "old-query-private",
        "old-cookie-private",
    ):
        assert secret not in response.text
    assert saved["request_headers"]["X-Trace"] == "keep-trace"
    assert isinstance(saved["response_headers"]["Set-Cookie"], list)
    assert saved["error_kind"] == "request_failed"


async def test_history_sanitizer_migrates_existing_rows_in_place_and_is_idempotent(settings):
    spec = importlib.util.find_spec("api_agent.infrastructure.history")
    assert spec is not None, "startup needs a shared history sanitization entry point"
    sanitize_history = importlib.import_module("api_agent.infrastructure.history").sanitize_history
    database = Database(settings)
    await database.migrate_async()
    try:
        async with database.session() as session:
            console = legacy_console()
            stream = StreamSessionRecord(
                kind="sse",
                url="http://127.0.0.1:9100/events",
                request_headers={"Token": "old-stream-private", "X-Trace": "keep-stream"},
                status="closed",
                settings={"max_events": 8},
            )
            session.add_all([console, stream])
            await session.commit()
            ids = console.id, stream.id
            created = console.created_at, stream.created_at
        assert await sanitize_history(database) == 2
        assert await sanitize_history(database) == 0
        async with database.session() as session:
            console = (await session.scalars(select(ConsoleRequestRecord))).one()
            stream = (await session.scalars(select(StreamSessionRecord))).one()
            assert (console.id, stream.id) == ids
            # SQLite preserves timestamps, but may return them without timezone.
            assert (
                console.created_at.replace(tzinfo=None),
                stream.created_at.replace(tzinfo=None),
            ) == (created[0].replace(tzinfo=None), created[1].replace(tzinfo=None))
            assert "old-stream-private" not in json.dumps(stream.request_headers)
            assert stream.request_headers["X-Trace"] == "keep-stream"
            assert stream.settings == {"max_events": 8}
            assert stream.url == "http://127.0.0.1:9100/events"
            assert "old-password-private" not in console.body_preview
    finally:
        await database.dispose()


def test_redact_headers_covers_token_aliases_and_preserves_multivalue_shape():
    safe = redact_headers(
        {
            "Token": "private",
            "X-Auth-Token": "private",
            "X-Access-Token": "private",
            "X-Trace": "trace",
            "Set-Cookie": ["a=secret", "b=secret"],
        }
    )
    assert safe == {
        "Token": "[redacted]",
        "X-Auth-Token": "[redacted]",
        "X-Access-Token": "[redacted]",
        "X-Trace": "trace",
        "Set-Cookie": ["[redacted]", "[redacted]"],
    }
