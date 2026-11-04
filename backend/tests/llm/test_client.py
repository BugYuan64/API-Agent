import pytest
from support.local_server import LocalServer

from api_agent.infrastructure.config import ConfigurationError, load_settings
from api_agent.llm.client import (
    LLMAuthenticationError,
    LLMConfigError,
    LLMTimeoutError,
    OpenAIClient,
)
from api_agent.llm.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    FunctionDefinition,
    ToolDefinition,
)


@pytest.fixture
async def target():
    """提供本地回环服务端点，模拟兼容 OpenAI 协议的模型接口。"""
    server = await LocalServer().start()
    try:
        yield server
    finally:
        await server.stop()


def test_config_llm_settings_validation(tmp_path):
    """测试 LLM 配置的归一化与校验规则。"""
    settings = load_settings(
        env_file=None,
        project_root=tmp_path,
        llm_mode="openai",
        llm_base_url="https://api.openai.com/v1/",
        llm_model="gpt-4o-mini",
        llm_api_key="sk-test-12345",
    )
    assert settings.llm_mode == "openai"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_api_key.get_secret_value() == "sk-test-12345"

    settings_empty = load_settings(
        env_file=None,
        project_root=tmp_path,
        llm_base_url="",
        llm_api_key="",
    )
    assert settings_empty.llm_base_url is None
    assert settings_empty.llm_api_key is None

    for bad_url in ("ftp://api.openai.com/v1", "not-a-url", "http://"):
        with pytest.raises(ConfigurationError):
            load_settings(env_file=None, project_root=tmp_path, llm_base_url=bad_url)


async def test_openai_client_requires_base_url(tmp_path):
    """未配置 Base URL 时调用应抛出清晰的配置异常。"""
    client = OpenAIClient(base_url=None, enabled=True)
    assert not client.is_configured
    with pytest.raises(LLMConfigError, match="Base URL 尚未配置"):
        await client.chat_completion(
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="hello")])
        )


async def test_openai_client_text_response(tmp_path, target):
    """验证普通非流式 Response 模式的文本回复与 Token 统计解析。"""
    client = OpenAIClient(
        base_url=f"{target.base_url}/v1",
        model="gpt-4o",
        enabled=True,
    )
    assert client.is_configured

    req = ChatCompletionRequest(
        messages=[
            ChatMessage(role="system", content="You are a helpful assistant."),
            ChatMessage(role="user", content="Hello test!"),
        ]
    )
    res = await client.chat_completion(req)
    assert res.content == "Mock OpenAI-compatible response from LocalServer."
    assert res.finish_reason == "stop"
    assert res.model == "gpt-4o"
    assert res.usage.get("total_tokens") == 27
    assert len(res.tool_calls) == 0


async def test_openai_client_tool_calling(tmp_path, target):
    """验证工具定义（Tools）的传递与模型返回 tool_calls 的参数反序列化。"""
    client = OpenAIClient(
        base_url=f"{target.base_url}/v1",
        enabled=True,
    )

    tools = [
        ToolDefinition(
            type="function",
            function=FunctionDefinition(
                name="search_database",
                description="Search data records by query keywords.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search keywords"},
                        "limit": {"type": "integer", "description": "Max number of records"},
                    },
                    "required": ["query"],
                },
            ),
        )
    ]
    req = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="Search database for customers")],
        tools=tools,
        tool_choice="auto",
    )
    res = await client.chat_completion(req)
    assert res.finish_reason == "tool_calls"
    assert len(res.tool_calls) == 1

    tc = res.tool_calls[0]
    assert tc.id == "call_mock_123456"
    assert tc.type == "function"
    assert tc.function.name == "search_database"
    assert tc.function.arguments == {"query": "mock test value", "limit": 10}


async def test_openai_client_auth_failure(tmp_path, target):
    """测试 401 密钥错误被正确捕获为 LLMAuthenticationError。"""
    client = OpenAIClient(
        base_url=f"{target.base_url}/v1",
        api_key="bad-key-value",
        enabled=True,
    )
    with pytest.raises(LLMAuthenticationError, match="LLM authentication failed"):
        await client.chat_completion(
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
        )


async def test_openai_client_timeout(tmp_path, target):
    """测试响应超时被正确映射为 LLMTimeoutError。"""
    client = OpenAIClient(
        base_url=f"{target.base_url}/slow",
        timeout_seconds=0.2,
        enabled=True,
    )
    with pytest.raises(LLMTimeoutError, match="timed out"):
        await client.chat_completion(
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])
        )


async def test_openai_client_ping(tmp_path, target):
    """测试客户端连通性探测功能。"""
    client = OpenAIClient(
        base_url=f"{target.base_url}/v1",
        enabled=True,
    )
    result = await client.ping()
    assert result["ok"] is True
    assert result["latency_ms"] >= 0
    assert "successful" in result["message"]


async def test_dynamic_llm_settings_api(client, target):
    """验证通过前端控制台配置 API 动态修改与持久化大模型设置（代替 .env 注入）。"""
    # 1. 默认未启用
    get_res = await client.get("/api/settings/llm")
    assert get_res.status_code == 200
    config = get_res.json()
    assert config["enabled"] is False
    assert config["has_api_key"] is False

    # 2. 未配置时发对话请求应返回 400 明确指引前往系统设置
    chat_unconfigured = await client.post(
        "/api/llm/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert chat_unconfigured.status_code == 400
    assert chat_unconfigured.json()["error"]["code"] == "llm_not_configured"

    # 3. 前端提交保存大模型设置（包含 base_url、key、model）
    put_res = await client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "provider": "openai",
            "base_url": f"{target.base_url}/v1",
            "api_key": "sk-console-token-12345",
            "model": "deepseek-chat",
            "timeout_seconds": 45.0,
        },
    )
    assert put_res.status_code == 200
    updated = put_res.json()
    assert updated["enabled"] is True
    assert updated["base_url"] == f"{target.base_url}/v1"
    assert updated["model"] == "deepseek-chat"
    assert updated["has_api_key"] is True
    assert "sk-console-token" not in str(updated)

    # 4. 测试 ping 接口（测试已保存的配置）
    ping_res = await client.post("/api/settings/llm/ping")
    assert ping_res.status_code == 200
    assert ping_res.json()["ok"] is True

    # 5. 测试 ping 接口（测试临时传入的预览配置）
    ping_preview = await client.post(
        "/api/settings/llm/ping",
        json={"base_url": f"{target.base_url}/v1", "model": "preview-test"},
    )
    assert ping_preview.status_code == 200
    assert ping_preview.json()["ok"] is True

    # 6. 发送对话请求，带工具调用
    chat_res = await client.post(
        "/api/llm/chat",
        json={
            "messages": [{"role": "user", "content": "run diagnostic"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_crm_smoke",
                        "description": "Run crm smoke test plan",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
    )
    assert chat_res.status_code == 200
    chat_data = chat_res.json()
    assert chat_data["finish_reason"] == "tool_calls"
    assert len(chat_data["tool_calls"]) == 1
    assert chat_data["tool_calls"][0]["function"]["name"] == "run_crm_smoke"
    assert chat_data["tool_calls"][0]["function"]["arguments"]["limit"] == 10

    # 7. 再次更新，留空 api_key 时应保留原有密钥
    put_preserve = await client.put(
        "/api/settings/llm",
        json={
            "enabled": True,
            "base_url": f"{target.base_url}/v1",
            "api_key": None,  # 保持原 Key
            "model": "deepseek-reasoner",
        },
    )
    assert put_preserve.status_code == 200
    assert put_preserve.json()["has_api_key"] is True
    assert put_preserve.json()["model"] == "deepseek-reasoner"


async def test_openai_client_list_models(tmp_path, target):
    """验证从兼容端点拉取可用模型清单。"""
    client = OpenAIClient(base_url=f"{target.base_url}/v1", enabled=True)
    models = await client.list_models()
    assert "gpt-4o" in models
    assert "deepseek-chat" in models
    assert models == sorted(models)


async def test_list_models_api_uses_saved_and_preview_config(client, target):
    """模型列表接口既支持已保存配置，也支持未保存的预览配置。"""
    # 未配置时应明确提示
    unconfigured = await client.post("/api/settings/llm/models")
    assert unconfigured.status_code == 400
    assert unconfigured.json()["error"]["code"] == "llm_not_configured"

    # 预览配置（不保存）
    preview = await client.post(
        "/api/settings/llm/models", json={"base_url": f"{target.base_url}/v1"}
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["ok"] is True
    assert body["count"] == 3
    assert "gpt-4o-mini" in body["models"]

    # 鉴权失败应如实反馈而不是 500
    bad_key = await client.post(
        "/api/settings/llm/models",
        json={"base_url": f"{target.base_url}/v1", "api_key": "bad-key-value"},
    )
    assert bad_key.status_code == 200
    assert bad_key.json()["ok"] is False
    assert "鉴权失败" in bad_key.json()["message"]

    # 保存配置后不传参也可拉取
    await client.put(
        "/api/settings/llm",
        json={"enabled": True, "base_url": f"{target.base_url}/v1", "model": "gpt-4o"},
    )
    saved = await client.post("/api/settings/llm/models")
    assert saved.status_code == 200
    assert saved.json()["count"] == 3
