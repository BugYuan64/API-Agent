"""Tests for migration 20261127_0008, AssistantThreadRepository, and history sanitization."""

from __future__ import annotations

import json
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select

from api_agent.assistant import (
    VALID_PAGE_SCOPES,
    AssistantMessageRecord,
    AssistantThreadRecord,
    AssistantThreadRepository,
)
from api_agent.console.models import ConsoleRequestRecord, ConsoleSubCaseRecord
from api_agent.infrastructure.database import BACKEND_ROOT, Database
from api_agent.infrastructure.history import (
    sanitize_assistant_message,
    sanitize_console_sub_case,
    sanitize_history,
    sanitize_model_test_run,
)
from api_agent.llm.models import ModelTestRunRecord
from api_agent.streams.models import StreamEventRecord, StreamSessionRecord


def _alembic_config(settings) -> Config:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.sync_database_url.replace("%", "%%"))
    cfg.attributes["settings"] = settings
    return cfg


def _sqlite_schema(db_path) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        columns: dict[str, set[str]] = {}
        indexes: dict[str, set[str]] = {}
        for table in tables:
            columns[table] = {
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            indexes[table] = {
                row[1] for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall()
            }
        return tables, columns, indexes
    finally:
        conn.close()


async def test_migration_0008_fresh_upgrade_and_downgrade(settings):
    cfg = _alembic_config(settings)
    command.upgrade(cfg, "head")

    tables, columns, indexes = _sqlite_schema(settings.database_path)
    assert {
        "console_requests",
        "console_request_cases",
        "stream_sessions",
        "stream_events",
        "model_test_runs",
        "assistant_threads",
        "assistant_messages",
    } <= tables

    assert {
        "source",
        "title",
        "badge",
        "status",
        "summary",
        "total_rounds",
        "cooldown_ms",
        "http_version",
        "response_reason",
        "redirects_json",
        "sse_json",
    } <= columns["console_requests"]

    assert {
        "id",
        "parent_id",
        "round_index",
        "case_index",
        "label",
        "method",
        "url",
        "request_headers",
        "body_mode",
        "request_body",
        "ok",
        "status_code",
        "elapsed_ms",
        "error_kind",
        "error_message",
        "response_headers",
        "response_preview",
        "response_bytes",
        "http_version",
        "response_reason",
        "redirects_json",
        "sse_json",
        "assertion_summary",
        "created_at",
    } == columns["console_request_cases"]

    assert {
        "source",
        "title",
        "badge",
        "parent_session_id",
        "round_index",
        "case_index",
        "case_label",
        "cooldown_ms",
        "total_rounds",
        "summary",
    } <= columns["stream_sessions"]

    # ModelTestRunRecord must never persist plaintext api_key.
    assert "api_key" not in columns["model_test_runs"]
    assert {
        "id",
        "title",
        "source",
        "badge",
        "status",
        "error_message",
        "base_url",
        "protocol",
        "resolved_protocol",
        "used_saved_key",
        "has_custom_api_key",
        "timeout_seconds",
        "concurrency",
        "checks",
        "target_models",
        "cooldown_ms",
        "total_rounds",
        "completed_rounds",
        "discover_meta",
        "models_snapshot",
        "rounds_json",
        "sub_cases_json",
        "summary_json",
        "summary_text",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    } == columns["model_test_runs"]

    assert {
        "id",
        "scope",
        "history_id",
        "title",
        "created_at",
        "updated_at",
    } == columns["assistant_threads"]
    assert {
        "id",
        "thread_id",
        "role",
        "content",
        "blocks_json",
        "run_id",
        "created_at",
    } == columns["assistant_messages"]

    assert "ix_console_request_cases_parent_id" in indexes["console_request_cases"]
    assert "ix_stream_sessions_parent_session_id" in indexes["stream_sessions"]
    assert "ix_assistant_threads_scope" in indexes["assistant_threads"]
    assert "ix_assistant_threads_history_id" in indexes["assistant_threads"]
    assert "ix_assistant_messages_thread_id" in indexes["assistant_messages"]

    command.downgrade(cfg, "20261120_0007")
    tables_after, columns_after, _ = _sqlite_schema(settings.database_path)
    for dropped in (
        "console_request_cases",
        "model_test_runs",
        "assistant_threads",
        "assistant_messages",
    ):
        assert dropped not in tables_after
    assert "source" not in columns_after["console_requests"]
    assert "sse_json" not in columns_after["console_requests"]
    assert "parent_session_id" not in columns_after["stream_sessions"]
    assert "cooldown_ms" not in columns_after["stream_sessions"]

    # Round-trip re-upgrade must succeed cleanly as well.
    command.upgrade(cfg, "head")
    tables_reup, _, _ = _sqlite_schema(settings.database_path)
    assert "assistant_messages" in tables_reup
    assert "model_test_runs" in tables_reup


async def test_migration_0008_preserves_pre_0008_rows_and_cascades(settings):
    cfg = _alembic_config(settings)
    command.upgrade(cfg, "20261120_0007")

    conn = sqlite3.connect(settings.database_path)
    try:
        conn.execute(
            """
            INSERT INTO console_requests (
                id, project_id, method, url, request_headers, body_mode, body_preview,
                request_body, ok, status_code, elapsed_ms, error_kind, error_message,
                response_headers, response_preview, response_bytes, created_at
            ) VALUES (
                'legacy-console-1', NULL, 'GET', 'http://127.0.0.1:9000/ping', '{}',
                'none', NULL, NULL, 1, 200, 12.5, NULL, NULL, '{}', '{"ok":true}', 11,
                '2026-11-20 00:00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO stream_sessions (
                id, project_id, kind, url, request_headers, status, stop_reason,
                error_kind, error_message, http_status, content_type, subprotocol,
                close_code, close_reason, event_count, byte_count, sent_count,
                truncated_count, settings, scheduled_messages, opened_at, closed_at, created_at
            ) VALUES (
                'legacy-stream-1', NULL, 'websocket', 'ws://127.0.0.1:9000/ws', '{}',
                'closed', 'target_closed', NULL, NULL, 101, NULL, NULL, 1000, '',
                1, 5, 1, 0, '{}', '["hi"]', NULL, NULL, '2026-11-20 00:00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO stream_events (
                id, session_id, sequence, direction, kind, event_name,
                data, data_bytes, truncated, elapsed_ms, created_at
            ) VALUES (
                'legacy-event-1', 'legacy-stream-1', 1, 'in', 'text', NULL,
                'hello', 5, 0, 10, '2026-11-20 00:00:01'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    database = Database(settings)
    try:
        async with database.session() as session:
            legacy_console = await session.get(ConsoleRequestRecord, "legacy-console-1")
            assert legacy_console is not None
            assert legacy_console.source == "manual"
            assert legacy_console.status == "completed"
            assert legacy_console.total_rounds == 1
            assert legacy_console.cooldown_ms == 500
            assert legacy_console.badge is None
            assert legacy_console.title is None

            legacy_stream = await session.get(StreamSessionRecord, "legacy-stream-1")
            assert legacy_stream is not None
            assert legacy_stream.source == "manual"
            assert legacy_stream.parent_session_id is None
            assert legacy_stream.round_index == 1
            assert legacy_stream.case_index == 0
            assert legacy_stream.cooldown_ms == 500
            assert legacy_stream.total_rounds == 1

            legacy_event = await session.get(StreamEventRecord, "legacy-event-1")
            assert legacy_event is not None
            assert legacy_event.data == "hello"

            # Verify FK ON DELETE CASCADE for console_request_cases and self-FK stream_sessions.
            parent_req = ConsoleRequestRecord(
                method="POST",
                url="http://127.0.0.1:9000/suite",
                source="ai",
                title="AI 接口测试",
                badge="AI 测试",
                status="completed",
                total_rounds=2,
                cooldown_ms=500,
            )
            session.add(parent_req)
            await session.flush()
            sub_case = ConsoleSubCaseRecord(
                parent_id=parent_req.id,
                round_index=1,
                case_index=0,
                label="边界测试",
                method="POST",
                url="http://127.0.0.1:9000/suite",
                ok=True,
                status_code=200,
                elapsed_ms=15,
            )
            session.add(sub_case)

            parent_ws = StreamSessionRecord(
                kind="websocket",
                url="ws://127.0.0.1:9000/ws",
                source="ai",
                title="AI 长连接测试",
                badge="AI 测试",
                total_rounds=2,
                cooldown_ms=500,
            )
            session.add(parent_ws)
            await session.flush()
            child_ws = StreamSessionRecord(
                kind="websocket",
                url="ws://127.0.0.1:9000/ws",
                source="ai",
                parent_session_id=parent_ws.id,
                round_index=1,
                case_index=0,
                case_label="Ping-Pong",
            )
            session.add(child_ws)
            await session.flush()
            child_event = StreamEventRecord(
                session_id=child_ws.id,
                sequence=1,
                direction="in",
                kind="text",
                data="pong",
                data_bytes=4,
            )
            session.add(child_event)
            await session.commit()

            parent_req_id, sub_case_id = parent_req.id, sub_case.id
            parent_ws_id, child_ws_id, child_event_id = parent_ws.id, child_ws.id, child_event.id

        async with database.session() as session:
            await session.execute(
                delete(ConsoleRequestRecord).where(ConsoleRequestRecord.id == parent_req_id)
            )
            await session.execute(
                delete(StreamSessionRecord).where(StreamSessionRecord.id == parent_ws_id)
            )
            await session.commit()

        async with database.session() as session:
            assert await session.get(ConsoleSubCaseRecord, sub_case_id) is None
            assert await session.get(StreamSessionRecord, child_ws_id) is None
            assert await session.get(StreamEventRecord, child_event_id) is None
    finally:
        await database.dispose()

    # Downgrade back to 0007 and confirm pre-0008 legacy rows survive intact.
    command.downgrade(cfg, "20261120_0007")
    conn = sqlite3.connect(settings.database_path)
    try:
        assert (
            conn.execute("SELECT url FROM console_requests WHERE id='legacy-console-1'").fetchone()[
                0
            ]
            == "http://127.0.0.1:9000/ping"
        )
        assert (
            conn.execute("SELECT url FROM stream_sessions WHERE id='legacy-stream-1'").fetchone()[0]
            == "ws://127.0.0.1:9000/ws"
        )
        assert (
            conn.execute("SELECT data FROM stream_events WHERE id='legacy-event-1'").fetchone()[0]
            == "hello"
        )
    finally:
        conn.close()


async def test_assistant_thread_repository_crud_cascade_and_scrubbing(settings):
    async with Database(settings) as database:
        repo = AssistantThreadRepository(database)

        assert await repo.get_thread("console", "hist-1") is None
        with pytest.raises(ValueError, match="Unsupported assistant scope"):
            await repo.get_or_create_thread("invalid_scope", "hist-1")
        with pytest.raises(ValueError, match="history_id"):
            await repo.get_or_create_thread("console", "   ")

        created = await repo.get_or_create_thread(
            "console",
            "hist-1",
            title="调试 sk-secretkey1234567890 接口",
        )
        assert created["scope"] == "console"
        assert created["history_id"] == "hist-1"
        assert created["messages"] == []
        assert "sk-secretkey1234567890" not in (created["title"] or "")

        again = await repo.get_or_create_thread("console", "hist-1")
        assert again["id"] == created["id"]

        msg1 = await repo.append_message(
            "console",
            "hist-1",
            "user",
            (
                "请用 Bearer bearer-secret-token 测试 "
                "https://alice:topsecret@127.0.0.1:8000/v1 并联系 13800138000 / ops@example.com"
            ),
            blocks=[
                {
                    "type": "tool_call",
                    "headers": {
                        "X-Auth-Token": "header-secret-9999",
                        "X-Trace": "keep-trace-id",
                    },
                    "payload": {
                        "password": "body-password-8888",
                        "note": "echo header-secret-9999",
                    },
                }
            ],
            run_id="run-001",
        )
        msg2 = await repo.append_message(
            "console",
            "hist-1",
            "assistant",
            "已完成测试，可点击回放子用例，不会回显 sk-anotherkey12345678。",
            blocks=[
                {
                    "type": "case_link",
                    "case_id": "r1:c0",
                    "label": "GET /health",
                    "round": 1,
                    "ok": True,
                }
            ],
            run_id="run-001",
        )
        assert msg1["role"] == "user"
        assert msg2["role"] == "assistant"

        loaded = await repo.get_thread("console", "hist-1")
        assert loaded is not None
        assert [m["id"] for m in loaded["messages"]] == [msg1["id"], msg2["id"]]
        serialized = json.dumps(loaded, ensure_ascii=False)
        for secret in (
            "bearer-secret-token",
            "topsecret",
            "13800138000",
            "ops@example.com",
            "header-secret-9999",
            "body-password-8888",
            "sk-anotherkey12345678",
        ):
            assert secret not in serialized
        assert loaded["messages"][0]["blocks"][0]["headers"]["X-Trace"] == "keep-trace-id"
        assert loaded["messages"][1]["blocks"][0] == {
            "type": "case_link",
            "case_id": "r1:c0",
            "label": "GET /health",
            "round": 1,
            "ok": True,
        }

        # Non-empty thread is NOT overwritten by replace_or_migrate_messages by default.
        preserved = await repo.replace_or_migrate_messages(
            "console",
            "hist-1",
            [{"role": "user", "content": "should not overwrite"}],
        )
        assert len(preserved["messages"]) == 2
        assert preserved["messages"][0]["id"] == msg1["id"]

        # Empty thread IS hydrated and scrubbed by replace_or_migrate_messages.
        hydrated = await repo.replace_or_migrate_messages(
            "model_testing",
            "run-legacy-1",
            [
                {"role": "user", "content": "旧消息 api_key=sk-legacykey1234567890"},
                {
                    "role": "assistant",
                    "content": "旧回复",
                    "blocks": [{"type": "summary", "has_custom_api_key": True}],
                },
            ],
            title="模型探测",
        )
        assert len(hydrated["messages"]) == 2
        assert "sk-legacykey1234567890" not in hydrated["messages"][0]["content"]
        assert hydrated["messages"][1]["blocks"] == [
            {"type": "summary", "has_custom_api_key": True}
        ]

        # Force replacement updates an existing thread explicitly.
        replaced = await repo.replace_or_migrate_messages(
            "model_testing",
            "run-legacy-1",
            [{"role": "assistant", "content": "已替换为新消息"}],
            force=True,
        )
        assert len(replaced["messages"]) == 1
        assert replaced["messages"][0]["content"] == "已替换为新消息"

        # Verify all four canonical scopes are isolated and cascade delete cleanly.
        for scope in VALID_PAGE_SCOPES:
            await repo.append_message(scope, "shared-id", "user", f"hello {scope}")

        assert await repo.delete_thread("console", "hist-1") is True
        assert await repo.delete_thread("console", "hist-1") is False
        assert await repo.get_thread("console", "hist-1") is None

        # Verify messages cascaded out of assistant_messages table as well.
        async with database.session() as session:
            orphans = (
                await session.scalars(
                    select(AssistantMessageRecord).where(
                        AssistantMessageRecord.thread_id == created["id"]
                    )
                )
            ).all()
            assert orphans == []

        # clear_scope only deletes the requested scope.
        removed = await repo.clear_scope("model_testing")
        assert removed == 2
        assert await repo.get_thread("model_testing", "shared-id") is None
        assert await repo.get_thread("tasks", "shared-id") is not None
        assert await repo.get_thread("websocket", "shared-id") is not None


async def test_sanitize_history_idempotent_across_all_tables(settings):
    async with Database(settings) as database:
        async with database.session() as session:
            console = ConsoleRequestRecord(
                method="POST",
                url="http://127.0.0.1:9100/api?token=console-query-secret",
                request_headers={"X-Auth-Token": "console-hdr-secret", "X-Trace": "trace-c"},
                body_mode="json",
                body_preview='{"password":"console-body-secret"}',
                request_body='{"password":"console-body-secret"}',
                response_headers={"Set-Cookie": ["sid=console-cookie-secret; Path=/"]},
                response_preview='{"echo":"console-body-secret"}',
                title="AI 测试 Bearer console-title-secret",
                summary="泄露了 console-hdr-secret",
                redirects_json=[
                    {
                        "url": "http://127.0.0.1:9100/next?access_token=redir-secret-1",
                        "headers": {"X-Auth-Token": "redir-hdr-secret", "X-Trace": "keep-r"},
                    }
                ],
                sse_json={"events": [{"data": "sk-consolessetoken123456"}]},
            )
            session.add(console)
            await session.flush()

            sub_case = ConsoleSubCaseRecord(
                parent_id=console.id,
                round_index=1,
                case_index=0,
                label="用例 sk-subcaselabel123456",
                method="POST",
                url="http://127.0.0.1:9100/sub?api_key=sub-query-secret",
                request_headers={"X-Auth-Token": "sub-hdr-secret", "X-Trace": "trace-sub"},
                body_mode="json",
                request_body='{"password":"sub-pass-secret","echo":"sub-resp-secret"}',
                ok=False,
                status_code=500,
                elapsed_ms=33,
                error_kind="http_500",
                error_message="failed with sub-pass-secret and sub-hdr-secret",
                response_headers={
                    "Set-Cookie": ["sid=sub-cookie-secret; HttpOnly"],
                    "X-Trace": "resp-trace",
                },
                response_preview='{"access_token":"sub-resp-secret","echo":"sub-pass-secret"}',
                response_bytes=64,
                redirects_json=[{"location": "http://127.0.0.1/r?token=sub-redir-secret"}],
                sse_json={"events": [{"data": "Bearer sub-sse-secret"}]},
                assertion_summary={"pointer": "/password", "actual": "sub-pass-secret"},
            )

            stream = StreamSessionRecord(
                kind="websocket",
                url="ws://127.0.0.1:9100/ws?token=ws-query-secret",
                request_headers={"Authorization": "Bearer ws-auth-secret", "X-Trace": "trace-ws"},
                status="closed",
                scheduled_messages=['{"password":"ws-msg-secret"}'],
                title="WS 会话 sk-wstitlekey12345678",
                summary="错误回显 ws-auth-secret",
            )

            model_run = ModelTestRunRecord(
                title="探测任务 sk-runtitlekey12345678",
                source="ai",
                badge="AI 测试",
                status="completed",
                base_url="https://user:urlpass-secret@127.0.0.1:8080/v1?api_key=url-key-secret",
                protocol="openai",
                resolved_protocol="openai",
                used_saved_key=False,
                has_custom_api_key=True,
                error_message="upstream rejected Bearer model-bearer-secret",
                discover_meta={
                    "headers": {"X-Api-Key": "disc-hdr-secret", "X-Trace": "trace-m"},
                    "provider": "openai",
                },
                models_snapshot=[
                    {"id": "gpt-4o", "has_custom_api_key": True, "note": "ok"},
                ],
                rounds_json=[{"round": 1, "note": "echo disc-hdr-secret"}],
                sub_cases_json=[
                    {
                        "case_id": "r1:c0",
                        "headers": {"Authorization": "Bearer subcase-model-secret"},
                        "error": "failed for model-bearer-secret",
                    }
                ],
                summary_json={
                    "total": 1,
                    "has_custom_api_key": True,
                    "contact": "qa@example.com",
                },
                summary_text="摘要包含 13900139000 与 sk-summarysecret123456",
            )

            thread = AssistantThreadRecord(scope="tasks", history_id="task-99", title="任务助手")
            session.add_all([sub_case, stream, model_run, thread])
            await session.flush()

            message = AssistantMessageRecord(
                thread_id=thread.id,
                role="assistant",
                content="测试命中敏感值 Bearer chat-bearer-secret 与 chat-hdr-secret",
                blocks_json=[
                    {
                        "type": "case_link",
                        "case_id": "r1:c0",
                        "headers": {"X-Auth-Token": "chat-hdr-secret", "X-Trace": "trace-chat"},
                    }
                ],
            )
            session.add(message)
            await session.commit()

        assert await sanitize_history(database) == 5
        assert await sanitize_history(database) == 0

        async with database.session() as session:
            sub = (await session.scalars(select(ConsoleSubCaseRecord))).one()
            run = (await session.scalars(select(ModelTestRunRecord))).one()
            msg = (await session.scalars(select(AssistantMessageRecord))).one()

            sub_dump = json.dumps(
                {
                    "label": sub.label,
                    "url": sub.url,
                    "request_headers": sub.request_headers,
                    "request_body": sub.request_body,
                    "response_headers": sub.response_headers,
                    "response_preview": sub.response_preview,
                    "error_message": sub.error_message,
                    "redirects_json": sub.redirects_json,
                    "sse_json": sub.sse_json,
                    "assertion_summary": sub.assertion_summary,
                },
                ensure_ascii=False,
            )
            for secret in (
                "sk-subcaselabel123456",
                "sub-query-secret",
                "sub-hdr-secret",
                "sub-pass-secret",
                "sub-resp-secret",
                "sub-cookie-secret",
                "sub-redir-secret",
                "sub-sse-secret",
            ):
                assert secret not in sub_dump
            assert sub.request_headers["X-Trace"] == "trace-sub"
            assert isinstance(sub.response_headers["Set-Cookie"], list)
            assert sub.response_headers["X-Trace"] == "resp-trace"

            run_dump = json.dumps(
                {
                    "title": run.title,
                    "base_url": run.base_url,
                    "error_message": run.error_message,
                    "discover_meta": run.discover_meta,
                    "models_snapshot": run.models_snapshot,
                    "rounds_json": run.rounds_json,
                    "sub_cases_json": run.sub_cases_json,
                    "summary_json": run.summary_json,
                    "summary_text": run.summary_text,
                },
                ensure_ascii=False,
            )
            for secret in (
                "sk-runtitlekey12345678",
                "urlpass-secret",
                "url-key-secret",
                "model-bearer-secret",
                "disc-hdr-secret",
                "subcase-model-secret",
                "qa@example.com",
                "13900139000",
                "sk-summarysecret123456",
            ):
                assert secret not in run_dump
            assert run.has_custom_api_key is True
            assert run.used_saved_key is False
            assert run.discover_meta["headers"]["X-Trace"] == "trace-m"
            assert run.models_snapshot[0]["has_custom_api_key"] is True
            assert run.summary_json["has_custom_api_key"] is True

            msg_dump = json.dumps(
                {"content": msg.content, "blocks_json": msg.blocks_json},
                ensure_ascii=False,
            )
            assert "chat-bearer-secret" not in msg_dump
            assert "chat-hdr-secret" not in msg_dump
            assert msg.blocks_json[0]["headers"]["X-Trace"] == "trace-chat"
            assert msg.blocks_json[0]["case_id"] == "r1:c0"

            # Direct calls on already-sanitized rows must remain idempotent.
            assert sanitize_console_sub_case(sub) is False
            assert sanitize_model_test_run(run) is False
            assert sanitize_assistant_message(msg) is False


def test_sanitize_console_sub_case_learns_full_payload_beyond_preview_limit():
    long_secret = "late-password-secret-beyond-8k"
    full_body = json.dumps({"padding": "a" * 9000, "password": long_secret})
    full_response = json.dumps({"echo": long_secret, "padding": "b" * 9000})
    record = ConsoleSubCaseRecord(
        parent_id="parent-1",
        label="大报文脱敏",
        method="POST",
        url="http://127.0.0.1:9100/big",
        request_headers={},
        body_mode="json",
        request_body=full_body,
        response_preview=full_response[:8192],
    )

    assert (
        sanitize_console_sub_case(record, full_body=full_body, full_response=full_response) is True
    )
    assert long_secret not in (record.request_body or "")
    assert long_secret not in (record.response_preview or "")
    assert len(record.response_preview or "") <= 8192
    assert sanitize_console_sub_case(record) is False
