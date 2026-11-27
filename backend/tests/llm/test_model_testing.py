"""Tests for model testing service: discovery, classification, probing, and key origin isolation."""

import asyncio
import json

import httpx
import pytest

from api_agent.infrastructure.config import load_settings
from api_agent.llm.testing import (
    build_initial_profile,
    detect_protocol,
    infer_model_category,
    normalize_base_url,
    same_origin,
)


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        env_file=None,
        project_root=tmp_path,
        llm_mode="openai",
        llm_base_url="https://saved.example/v1",
        llm_api_key="fictional-saved-key",
        llm_model="gpt-4o",
        llm_timeout_seconds=20,
    )


def test_normalize_base_url_and_same_origin():
    assert normalize_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    assert (
        normalize_base_url("https://api.example.com/v1/chat/completions")
        == "https://api.example.com/v1"
    )
    assert normalize_base_url("https://api.example.com/v1/models") == "https://api.example.com/v1"
    assert normalize_base_url("provider.example.com") == "https://provider.example.com"
    assert normalize_base_url("localhost:8000/v1/") == "http://localhost:8000/v1"
    assert normalize_base_url("ftp://api.example.com/v1") is None
    assert normalize_base_url("https://user:pass@api.example.com/v1") is None
    assert normalize_base_url("not-a-url") is None
    assert normalize_base_url("   ") is None

    assert same_origin("https://SAVED.example:443/v2", "https://saved.example/v1") is True
    assert same_origin("http://saved.example/v1", "https://saved.example/v1") is False
    assert same_origin("https://other.example/v1", "https://saved.example/v1") is False


@pytest.mark.parametrize(
    ("model_id", "raw", "expected_type", "expected_source"),
    [
        ("custom-x", {"type": "rerank"}, "rerank", "metadata"),
        ("text-embedding-3-large", {}, "embedding", "inferred"),
        ("bge-reranker-v2-m3", {}, "rerank", "inferred"),
        ("dall-e-3", {}, "image", "inferred"),
        ("whisper-1", {}, "audio", "inferred"),
        ("sora-2", {}, "video", "inferred"),
        ("omni-moderation-latest", {}, "moderation", "inferred"),
        ("deepseek-r1", {}, "reasoning", "inferred"),
        ("o3-mini", {}, "reasoning", "inferred"),
        ("gpt-4o", {}, "chat", "inferred"),
        ("demo1-custom", {}, "unknown", "unknown"),
    ],
)
def test_infer_model_category(model_id, raw, expected_type, expected_source):
    category, source = infer_model_category(model_id, raw)
    assert category == expected_type
    assert source == expected_source


def test_build_initial_profile_respects_openrouter_style_metadata():
    profile = build_initial_profile(
        {
            "id": "acme/vision-pro",
            "owned_by": "acme",
            "created": 1730000000,
            "architecture": {
                "input_modalities": ["text", "image", "audio"],
                "output_modalities": ["text"],
            },
            "supported_parameters": ["tools", "temperature"],
            "secret_blob": "should-be-filtered",
        }
    )
    assert profile is not None
    assert profile.owned_by == "acme"
    assert profile.tool_calling.status == "inferred"
    assert profile.tool_calling.source == "metadata"
    assert [m.modality for m in profile.input_modalities] == ["text", "image", "audio"]
    assert all(m.source == "metadata" for m in profile.input_modalities)
    assert [m.modality for m in profile.output_modalities] == ["text"]
    assert "secret_blob" not in profile.raw_metadata


async def test_discover_endpoint_parses_models_and_enforces_origin_key_boundary(
    client, monkeypatch
):
    recorded: list[httpx.Request] = []
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.url.host == "unauthorized.example":
            return httpx.Response(401, json={"error": "invalid api key"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o", "owned_by": "openai", "created": 1700000000},
                    {"id": "text-embedding-3-small", "owned_by": "openai"},
                    {"id": "gpt-4o", "owned_by": "duplicate"},
                ]
            },
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    # 1. Same-origin reuse with default saved base_url
    resp = await client.post("/api/model-testing/discover", json={"use_saved_key": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert [item["id"] for item in body["models"]] == ["gpt-4o", "text-embedding-3-small"]
    assert recorded[-1].headers.get("Authorization") == "Bearer fictional-saved-key"
    assert "fictional-saved-key" not in resp.text

    # 2. Cross-origin must NOT forward saved key even when use_saved_key=True
    cross = await client.post(
        "/api/model-testing/discover",
        json={"base_url": "https://other.example/v1", "use_saved_key": True},
    )
    assert cross.status_code == 200
    assert cross.json()["ok"] is True
    assert recorded[-1].headers.get("Authorization") is None

    # 3. Explicit custom API key is used and never echoed
    custom = await client.post(
        "/api/model-testing/discover",
        json={"base_url": "https://other.example/v1", "api_key": "ephemeral-user-key"},
    )
    assert custom.status_code == 200
    assert recorded[-1].headers.get("Authorization") == "Bearer ephemeral-user-key"
    assert "ephemeral-user-key" not in custom.text

    # 4. Upstream 401 returns structured ok=False without raising 500
    unauth = await client.post(
        "/api/model-testing/discover",
        json={"base_url": "https://unauthorized.example/v1", "api_key": "bad-key"},
    )
    assert unauth.status_code == 200
    assert unauth.json()["ok"] is False
    assert unauth.json()["http_status"] == 401
    assert "bad-key" not in unauth.text

    # 5. Invalid base_url returns 400
    invalid = await client.post(
        "/api/model-testing/discover",
        json={"base_url": "not-a-url"},
    )
    assert invalid.status_code == 400


async def test_probe_endpoint_covers_chat_tool_vision_embedding_and_specialized(
    client, monkeypatch
):
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload.get("model")
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(
                200, json={"data": [{"url": "https://img.example/generated.png"}]}
            )
        if payload.get("tools"):
            if model == "gpt-4o":
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "probe_ping",
                                                "arguments": '{"echo":"ok"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(400, json={"error": "tools unsupported"})

        messages = payload.get("messages") or []
        content = messages[0].get("content") if messages else None
        if isinstance(content, list):
            if model == "gpt-4o":
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]},
                )
            return httpx.Response(400, json={"error": "vision unsupported"})

        # Availability chat completion
        msg = {"role": "assistant", "content": "OK"}
        if model == "deepseek-r1":
            msg["reasoning_content"] = "thinking step"
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://probe.example/v1",
            "api_key": "probe-secret",
            "models": ["gpt-4o", "deepseek-r1", "text-embedding-3-small", "dall-e-3", "sora-2"],
            "checks": ["availability", "tool_calling", "input_modalities", "output_modalities"],
        },
    )
    assert resp.status_code == 200
    assert "probe-secret" not in resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["tested_count"] == 5
    assert data["available_count"] == 4
    assert data["tool_supported_count"] == 1

    by_id = {item["id"]: item for item in data["results"]}

    gpt = by_id["gpt-4o"]
    assert gpt["availability"]["status"] == "available"
    assert gpt["tool_calling"]["status"] == "supported"
    assert any(
        m["modality"] == "image" and m["status"] == "supported" and m["source"] == "probed"
        for m in gpt["input_modalities"]
    )
    assert any(
        m["modality"] == "text" and m["status"] == "supported" and m["source"] == "probed"
        for m in gpt["output_modalities"]
    )

    r1 = by_id["deepseek-r1"]
    assert r1["model_type"] == "reasoning"
    assert r1["availability"]["status"] == "available"
    assert r1["tool_calling"]["status"] == "unsupported"
    assert any(
        m["modality"] == "image" and m["status"] == "unsupported" and m["source"] == "probed"
        for m in r1["input_modalities"]
    )
    assert any(
        m["modality"] == "reasoning" and m["status"] == "supported" and m["source"] == "probed"
        for m in r1["output_modalities"]
    )

    emb = by_id["text-embedding-3-small"]
    assert emb["availability"]["status"] == "available"
    assert emb["tool_calling"]["status"] == "unsupported"
    assert any(
        m["modality"] == "embedding" and m["status"] == "supported" and m["source"] == "probed"
        for m in emb["output_modalities"]
    )

    img = by_id["dall-e-3"]
    assert img["availability"]["status"] == "available"
    assert img["availability"]["source"] == "probed"
    assert any(
        m["modality"] == "image" and m["status"] == "supported" and m["source"] == "probed"
        for m in img["output_modalities"]
    )

    video = by_id["sora-2"]
    assert video["availability"]["status"] == "unsupported"
    assert video["availability"]["source"] == "untested"


async def test_probe_rejects_blank_model_ids(client):
    resp = await client.post(
        "/api/model-testing/probe",
        json={"base_url": "https://probe.example/v1", "models": ["   "]},
    )
    assert resp.status_code == 422


async def test_discover_bare_domain_auto_falls_back_to_v1_models(client, monkeypatch):
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/models":
            return httpx.Response(404, json={"error": "not found"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "mimo-v2.6-pro"}]})
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/discover",
        json={"base_url": "bare.example.com", "api_key": "k"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["base_url"] == "https://bare.example.com/v1"
    assert body["models"][0]["id"] == "mimo-v2.6-pro"
    assert body["models"][0]["model_type"] == "chat"


async def test_probe_auto_falls_back_to_responses_api(client, monkeypatch):
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "chat_completions_disabled",
                        "message": "Please use /v1/responses instead.",
                    }
                },
            )
        if request.url.path.endswith("/responses"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "output": [
                            {
                                "type": "function_call",
                                "name": "probe_ping",
                                "arguments": '{"echo":"ok"}',
                            }
                        ]
                    },
                )
            if isinstance(payload.get("input"), list):
                return httpx.Response(200, json={"output": [{"type": "message", "content": "OK"}]})
            return httpx.Response(
                200,
                json={
                    "output": [
                        {"type": "reasoning", "summary": []},
                        {"type": "message", "content": "OK"},
                    ]
                },
            )
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://responses.example/v1",
            "api_key": "k",
            "models": ["GPT-6.1-Sol"],
            "checks": ["availability", "tool_calling", "input_modalities"],
        },
    )
    assert resp.status_code == 200
    item = resp.json()["results"][0]
    assert item["availability"]["status"] == "available"
    assert "/responses" in (item["availability"]["detail"] or "")
    assert item["tool_calling"]["status"] == "supported"
    assert any(
        m["modality"] == "image" and m["status"] == "supported" for m in item["input_modalities"]
    )
    assert any(
        m["modality"] == "reasoning" and m["status"] == "supported"
        for m in item["output_modalities"]
    )


def test_detect_protocol_and_new_suffixes():
    assert detect_protocol("https://api.openai.com/v1") == "openai"
    assert detect_protocol("https://api.anthropic.com/v1") == "anthropic"
    assert detect_protocol("https://generativelanguage.googleapis.com/v1beta") == "gemini"
    assert detect_protocol("https://proxy.example/v1", "anthropic") == "anthropic"
    assert detect_protocol("https://proxy.example/v1beta", "auto") == "gemini"
    assert (
        normalize_base_url("https://api.anthropic.com/v1/messages")
        == "https://api.anthropic.com/v1"
    )
    assert normalize_base_url("https://proxy.example/v1/rerank") == "https://proxy.example/v1"
    assert (
        normalize_base_url("https://proxy.example/v1/images/generations")
        == "https://proxy.example/v1"
    )


async def test_probe_specialized_endpoints_rerank_moderation_image_tts_and_asr(client, monkeypatch):
    original = httpx.AsyncClient
    image_attempts: list[dict] = []
    asr_seen: list[tuple[str, bytes]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rerank"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["model"] == "bge-reranker-v2-m3"
            assert len(payload["documents"]) >= 2
            return httpx.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": 0.96}]},
            )
        if path.endswith("/moderations"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["model"] == "omni-moderation-latest"
            return httpx.Response(200, json={"results": [{"flagged": False}]})
        if path.endswith("/images/generations"):
            payload = json.loads(request.content.decode("utf-8"))
            image_attempts.append(payload)
            if payload.get("size") == "256x256":
                return httpx.Response(400, json={"error": "Unsupported size 256x256"})
            return httpx.Response(200, json={"data": [{"b64_json": "AAAA"}]})
        if path.endswith("/audio/speech"):
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["model"] == "tts-1"
            return httpx.Response(
                200, content=b"ID3mockmp3bytes", headers={"Content-Type": "audio/mpeg"}
            )
        if path.endswith("/audio/transcriptions"):
            content_type = request.headers.get("content-type", "")
            asr_seen.append((content_type, request.content))
            return httpx.Response(200, json={"text": ""})
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://specialized.example/v1",
            "api_key": "spec-key",
            "models": [
                "bge-reranker-v2-m3",
                "omni-moderation-latest",
                "dall-e-3",
                "tts-1",
                "whisper-1",
            ],
            "checks": ["availability"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["available_count"] == 5
    assert len(image_attempts) == 2
    assert "size" not in image_attempts[1]
    assert asr_seen and "multipart/form-data" in asr_seen[0][0]
    assert b"RIFF" in asr_seen[0][1]

    by_id = {item["id"]: item for item in body["results"]}
    assert "/rerank" in (by_id["bge-reranker-v2-m3"]["availability"]["detail"] or "")
    assert "/moderations" in (by_id["omni-moderation-latest"]["availability"]["detail"] or "")
    assert "/images/generations" in (by_id["dall-e-3"]["availability"]["detail"] or "")
    assert "/audio/speech" in (by_id["tts-1"]["availability"]["detail"] or "")
    assert any(
        m["modality"] == "audio" and m["status"] == "supported" and m["source"] == "probed"
        for m in by_id["tts-1"]["output_modalities"]
    )
    assert "/audio/transcriptions" in (by_id["whisper-1"]["availability"]["detail"] or "")
    assert any(
        m["modality"] == "audio" and m["status"] == "supported" and m["source"] == "probed"
        for m in by_id["whisper-1"]["input_modalities"]
    )
    assert any(
        m["modality"] == "text" and m["status"] == "supported" and m["source"] == "probed"
        for m in by_id["whisper-1"]["output_modalities"]
    )


async def test_probe_streaming_ttft_and_structured_output_with_fallback(client, monkeypatch):
    original = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload.get("model")
        if payload.get("stream") is True:
            if model == "gpt-4o":
                sse = (
                    'data: {"choices":[{"delta":{"content":"O"}}]}\n\n'
                    'data: {"choices":[{"delta":{"content":"K"}}]}\n\n'
                    "data: [DONE]\n\n"
                )
                return httpx.Response(
                    200, content=sse.encode("utf-8"), headers={"Content-Type": "text/event-stream"}
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]},
            )
        rf = payload.get("response_format") or {}
        if rf.get("type") == "json_schema":
            if model == "gpt-4o":
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"role": "assistant", "content": '{"status":"ok"}'}}
                        ]
                    },
                )
            return httpx.Response(400, json={"error": "json_schema unsupported"})
        if rf.get("type") == "json_object":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": '{"status":"ok"}'}}]
                },
            )
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://adv.example/v1",
            "api_key": "adv-key",
            "models": ["gpt-4o", "legacy-chat"],
            "checks": ["streaming", "structured_output"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["streaming_supported_count"] == 1
    assert data["structured_output_supported_count"] == 2
    by_id = {item["id"]: item for item in data["results"]}

    gpt = by_id["gpt-4o"]
    assert gpt["streaming"]["status"] == "supported"
    assert gpt["streaming"]["ttft_ms"] is not None
    assert gpt["structured_output"]["status"] == "supported"
    assert any(
        m["modality"] == "json" and m["status"] == "supported" and m["source"] == "probed"
        for m in gpt["output_modalities"]
    )

    legacy = by_id["legacy-chat"]
    assert legacy["streaming"]["status"] == "partial"
    assert legacy["structured_output"]["status"] == "partial"
    assert any(
        m["modality"] == "json" and m["status"] == "supported" and m["source"] == "probed"
        for m in legacy["output_modalities"]
    )


async def test_discover_and_probe_anthropic_messages_protocol(client, monkeypatch):
    original = httpx.AsyncClient
    seen_headers: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}]},
            )
        if request.method == "POST" and request.url.path == "/v1/messages":
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("stream") is True:
                sse = (
                    "event: message_start\n"
                    'data: {"type":"message_start"}\n\n'
                    "event: content_block_delta\n"
                    'data: {"type":"content_block_delta",'
                    '"delta":{"type":"text_delta","text":"OK"}}\n\n'
                )
                return httpx.Response(
                    200, content=sse.encode("utf-8"), headers={"Content-Type": "text/event-stream"}
                )
            tool_choice = payload.get("tool_choice") or {}
            if tool_choice.get("name") == "emit_probe":
                return httpx.Response(
                    200,
                    json={
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_s",
                                "name": "emit_probe",
                                "input": {"status": "ok"},
                            }
                        ]
                    },
                )
            if payload.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu_1",
                                "name": "probe_ping",
                                "input": {"echo": "ok"},
                            }
                        ]
                    },
                )
            content = payload["messages"][0]["content"]
            if isinstance(content, list):
                assert content[0]["type"] == "image"
                return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "thinking", "thinking": "short"},
                        {"type": "text", "text": "OK"},
                    ]
                },
            )
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    disc = await client.post(
        "/api/model-testing/discover",
        json={"base_url": "https://api.anthropic.com/v1", "api_key": "ant-secret"},
    )
    assert disc.status_code == 200
    assert "ant-secret" not in disc.text
    disc_data = disc.json()
    assert disc_data["ok"] is True
    assert disc_data["protocol"] == "anthropic"
    assert disc_data["models"][0]["id"] == "claude-sonnet-4-5"
    assert seen_headers[-1].get("x-api-key") == "ant-secret"
    assert seen_headers[-1].get("anthropic-version") == "2023-06-01"

    probe = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "ant-secret",
            "models": ["claude-sonnet-4-5"],
            "checks": [
                "availability",
                "tool_calling",
                "input_modalities",
                "streaming",
                "structured_output",
            ],
        },
    )
    assert probe.status_code == 200
    assert "ant-secret" not in probe.text
    pdata = probe.json()
    assert pdata["protocol"] == "anthropic"
    item = pdata["results"][0]
    assert item["availability"]["status"] == "available"
    assert item["tool_calling"]["status"] == "supported"
    assert item["streaming"]["status"] == "supported"
    assert item["streaming"]["ttft_ms"] is not None
    assert item["structured_output"]["status"] == "supported"
    assert any(
        m["modality"] == "reasoning" and m["status"] == "supported"
        for m in item["output_modalities"]
    )
    assert any(
        m["modality"] == "image" and m["status"] == "supported" for m in item["input_modalities"]
    )


async def test_discover_and_probe_gemini_protocol(client, monkeypatch):
    original = httpx.AsyncClient
    seen_headers: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        path = request.url.path
        if request.method == "GET" and path == "/v1beta/models":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "displayName": "Gemini 2.5 Flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/text-embedding-004",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                },
            )
        if path.endswith(":embedContent"):
            return httpx.Response(200, json={"embedding": {"values": [0.1, 0.2]}})
        if path.endswith(":streamGenerateContent"):
            sse = 'data: {"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}\n\n'
            return httpx.Response(
                200, content=sse.encode("utf-8"), headers={"Content-Type": "text/event-stream"}
            )
        if path.endswith(":generateContent"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "probe_ping",
                                                "args": {"echo": "ok"},
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                )
            gen_cfg = payload.get("generationConfig") or {}
            if gen_cfg.get("responseMimeType") == "application/json":
                return httpx.Response(
                    200,
                    json={"candidates": [{"content": {"parts": [{"text": '{"status":"ok"}'}]}}]},
                )
            parts = payload["contents"][0]["parts"]
            if any("inlineData" in part for part in parts):
                return httpx.Response(
                    200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "plan", "thought": True},
                                    {"text": "OK"},
                                ]
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    disc = await client.post(
        "/api/model-testing/discover",
        json={
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "gem-secret",
        },
    )
    assert disc.status_code == 200
    assert "gem-secret" not in disc.text
    disc_data = disc.json()
    assert disc_data["protocol"] == "gemini"
    assert [m["id"] for m in disc_data["models"]] == ["gemini-2.5-flash", "text-embedding-004"]
    assert seen_headers[-1].get("x-goog-api-key") == "gem-secret"

    probe = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "api_key": "gem-secret",
            "models": ["gemini-2.5-flash", "text-embedding-004"],
            "checks": [
                "availability",
                "tool_calling",
                "input_modalities",
                "streaming",
                "structured_output",
            ],
        },
    )
    assert probe.status_code == 200
    assert "gem-secret" not in probe.text
    pdata = probe.json()
    assert pdata["protocol"] == "gemini"
    assert pdata["available_count"] == 2
    by_id = {item["id"]: item for item in pdata["results"]}
    gem = by_id["gemini-2.5-flash"]
    assert gem["availability"]["status"] == "available"
    assert gem["tool_calling"]["status"] == "supported"
    assert gem["streaming"]["status"] == "supported"
    assert gem["structured_output"]["status"] == "supported"
    assert any(
        m["modality"] == "reasoning" and m["status"] == "supported"
        for m in gem["output_modalities"]
    )
    assert any(
        m["modality"] == "json" and m["status"] == "supported" for m in gem["output_modalities"]
    )
    assert by_id["text-embedding-004"]["availability"]["status"] == "available"


async def test_probe_stream_consumer_queue_yields_fast_model_before_slow_model(client, monkeypatch):
    original = httpx.AsyncClient
    fast_done = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        model = payload.get("model")
        if model == "slow-model":
            # 等待快模型先完成（加兜底超时），验证并发消费队列跳过 IO 不被慢模型阻塞
            try:
                await asyncio.wait_for(fast_done.wait(), timeout=2.0)
            except TimeoutError:
                pass
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "SLOW_OK"}}]}
            )
        fast_done.set()
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "FAST_OK"}}]}
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/probe/stream",
        json={
            "base_url": "https://queue.example/v1",
            "api_key": "stream-secret-key",
            "models": ["slow-model", "fast-model"],
            "checks": ["availability"],
            "concurrency": 2,
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert "stream-secret-key" not in resp.text

    frames = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert frames[0]["type"] == "start"
    assert frames[0]["total"] == 2
    assert frames[0]["concurrency"] == 2

    started_ids = [f["model_id"] for f in frames if f["type"] == "model_start"]
    assert set(started_ids) == {"slow-model", "fast-model"}

    result_frames = [f for f in frames if f["type"] == "model_result"]
    assert [f["model_id"] for f in result_frames] == ["fast-model", "slow-model"]
    assert [f["completed"] for f in result_frames] == [1, 2]
    assert result_frames[0]["model"]["availability"]["status"] == "available"
    assert result_frames[1]["model"]["availability"]["status"] == "available"

    assert frames[-1]["type"] == "done"
    summary = frames[-1]["result"]
    assert summary["tested_count"] == 2
    assert summary["available_count"] == 2
    assert [item["id"] for item in summary["results"]] == ["slow-model", "fast-model"]


async def test_probe_availability_omits_temperature_falls_back_to_sse_and_surfaces_error_detail(
    client, monkeypatch
):
    original = httpx.AsyncClient
    captured_payloads: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured_payloads.append(payload)
        model = payload.get("model")
        if model == "slow-nonstream":
            if payload.get("stream") is True:
                sse = 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(
                    200, content=sse.encode("utf-8"), headers={"Content-Type": "text/event-stream"}
                )
            raise httpx.ReadTimeout("simulated non-stream timeout")
        if model == "rejected-model":
            return httpx.Response(400, json={"error": {"message": "Upstream quota exceeded"}})
        return httpx.Response(404)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.testing.httpx.AsyncClient", factory)

    resp = await client.post(
        "/api/model-testing/probe",
        json={
            "base_url": "https://compat.example/v1",
            "api_key": "compat-secret",
            "models": ["slow-nonstream", "rejected-model"],
            "checks": ["availability"],
        },
    )
    assert resp.status_code == 200
    assert "compat-secret" not in resp.text
    by_id = {item["id"]: item for item in resp.json()["results"]}

    # 请求应省略 temperature 且预留 >= 64 token 预算
    assert captured_payloads
    for payload in captured_payloads:
        assert "temperature" not in payload
        assert payload.get("max_tokens", 64) >= 64

    # 非流式超时自动回退 SSE 流式成功时应标记为可用
    assert by_id["slow-nonstream"]["availability"]["status"] == "available"
    assert "SSE" in (by_id["slow-nonstream"]["availability"]["detail"] or "")

    # 非 200 错误应透出上游简短错误原因
    assert by_id["rejected-model"]["availability"]["status"] == "unavailable"
    assert "Upstream quota exceeded" in (by_id["rejected-model"]["availability"]["detail"] or "")
