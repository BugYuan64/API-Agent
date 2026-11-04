import httpx

from api_agent.llm.client import OpenAIClient
from api_agent.llm.schemas import ChatCompletionRequest, ChatMessage


async def test_supplier_nested_token_details_preserve_numeric_usage(monkeypatch):
    original = httpx.AsyncClient

    def respond(request):
        return httpx.Response(
            200,
            json={
                "id": "compatible-supplier",
                "model": "configured-model",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 30,
                    "total_tokens": 130,
                    "prompt_tokens_details": {"cached_tokens": 20},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    def client(**kwargs):
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    result = await OpenAIClient(
        base_url="https://fixture.invalid/v1", enabled=True
    ).chat_completion(ChatCompletionRequest(messages=[ChatMessage(role="user", content="fixture")]))
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
