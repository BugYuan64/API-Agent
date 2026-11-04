"""A provider's malformed JSON shape is a supplier error, never a server crash."""

import json

import httpx
import pytest

from api_agent.llm.client import LLMResponseError, OpenAIClient
from api_agent.llm.schemas import ChatCompletionRequest


@pytest.fixture
def provider(monkeypatch):
    responses, requests = [], []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        # Emit raw JSON bytes: HTTPX's json= encoder correctly rejects these
        # malformed values, while a real supplier can still send them over TCP.
        return httpx.Response(200, content=json.dumps(responses.pop(0), ensure_ascii=True).encode())

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return original(*args, **kwargs)

    monkeypatch.setattr("api_agent.llm.client.httpx.AsyncClient", factory)
    return responses, requests


def completion(**changes):
    return {"choices": [{"message": {"content": "fixture"}}], **changes}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": ["unsupported"]}}]},
        completion(usage=[1]),
        {"choices": [{"message": {"content": "\ud800"}}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "fixture", "arguments": {"value": float("nan")}}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"message": {"tool_calls": [{"function": ["invalid"]}]}}]},
        {"choices": [{"message": {"tool_calls": [{"function": {"arguments": "[]"}}]}}]},
    ],
)
async def test_malformed_completion_is_normalized_to_supplier_error(provider, payload):
    responses, _ = provider
    responses.append(payload)
    client = OpenAIClient(base_url="http://127.0.0.1:9/v1")
    with pytest.raises(LLMResponseError):
        await client.chat_completion(
            ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])
        )


async def test_full_completion_url_also_builds_the_models_endpoint(provider):
    responses, requests = provider
    responses.append({"data": [{"id": "model-b"}, {"id": " "}, {"id": "model-b"}, "model-a"]})
    client = OpenAIClient(base_url="http://127.0.0.1:9/v1/chat/completions/")
    assert await client.list_models() == ["model-a", "model-b"]
    assert requests[0].url.path == "/v1/models"


async def test_invalid_supplier_body_is_not_embedded_in_the_public_error(provider):
    responses, _ = provider
    responses.append({"vendor_secret": "fixture-private-diagnostic"})
    client = OpenAIClient(base_url="http://127.0.0.1:9/v1")
    with pytest.raises(LLMResponseError) as error:
        await client.chat_completion(
            ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])
        )
    assert "fixture-private-diagnostic" not in str(error.value)


async def test_invalid_unicode_completion_is_a_502_at_http_boundary(client, provider):
    responses, _ = provider
    await client.put(
        "/api/settings/llm",
        json={"enabled": True, "base_url": "http://127.0.0.1:9/v1", "model": "fixture"},
    )
    responses.append({"choices": [{"message": {"content": "\ud800"}}]})
    response = await client.post(
        "/api/llm/chat", json={"messages": [{"role": "user", "content": "fixture"}]}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_response_error"
