"""OpenAI-compatible client with function/tool-calling and customizable base_url."""

import json
import logging
import time
from typing import Any

import httpx

from api_agent.infrastructure.config import Settings
from api_agent.llm.errors import (
    LLMAuthenticationError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMResponseError,
    LLMTimeoutError,
)
from api_agent.llm.response import parse_completion, parse_models
from api_agent.llm.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)

logger = logging.getLogger("api_agent.llm")


class OpenAIClient:
    """与 OpenAI 协议兼容的大模型客户端，支持自定义 Base URL、模型与工具调用。

    作用：提供非流式 Response 模式的模型调用管道，能自动序列化 tools 定义
    并解析模型返回的 tool_calls；不受第三方 SDK 锁定，支持任何兼容 OpenAI 规范的端点。
    支持直接传入配置（前端动态配置）或基于 Settings 初始化。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "gpt-4o",
        timeout_seconds: float = 60.0,
        enabled: bool = True,
        settings: Settings | None = None,
    ):
        if settings is not None:
            self.base_url = settings.llm_base_url
            self.api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
            self.model = settings.llm_model
            self.timeout_seconds = settings.llm_timeout_seconds
            self.enabled = settings.llm_mode != "disabled"
        else:
            self.base_url = base_url.rstrip("/") if base_url else None
            self.api_key = api_key
            self.model = model
            self.timeout_seconds = timeout_seconds
            self.enabled = enabled

    @property
    def is_configured(self) -> bool:
        """判断是否已配置 Base URL 并处于启用状态。"""
        return bool(self.enabled and self.base_url and self.base_url.strip())

    def _get_base_url(self) -> str:
        """获取并校验已配置的 Base URL，未配置时抛出可读的配置异常。"""
        if not self.base_url:
            raise LLMConfigError(
                "大模型 Base URL 尚未配置。请在前端控制台「系统设置」中配置服务地址。"
            )
        return self.base_url.rstrip("/")

    def _build_url(self, path: str = "/chat/completions") -> str:
        """按规范拼接 Base URL 与路径。"""
        base = self._get_base_url()
        # Accept a configured completion endpoint for both chat and model listing.
        if base.endswith("/chat/completions"):
            base = base.removesuffix("/chat/completions")
        return f"{base}{path}"

    def _build_headers(self) -> dict[str, str]:
        """构造调用请求头，按规范注入 Bearer 密钥（若已配置）。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key.strip():
            headers["Authorization"] = f"Bearer {self.api_key.strip()}"
        return headers

    def _format_messages(self, messages) -> list[dict[str, Any]]:
        """把内部 ChatMessage 模型转为 OpenAI API 接收的 JSON 列表。"""
        payload_messages = []
        for msg in messages:
            item: dict[str, Any] = {"role": msg.role}
            if msg.content is not None:
                item["content"] = msg.content
            if msg.name:
                item["name"] = msg.name
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                # 模型返回的 tool_calls 序列化为规范结构
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": (
                                json.dumps(tc.function.arguments, ensure_ascii=True)
                                if isinstance(tc.function.arguments, dict)
                                else str(tc.function.arguments)
                            ),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            payload_messages.append(item)
        return payload_messages

    async def chat_completion(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """执行一次完整的对话请求（非流式 Response 模式，支持工具调用）。

        实现：
        1. 组装符合 OpenAI 规范的 payload，明确指定 ``stream=False``；
        2. 若传入 ``tools``，组装 ``tools`` 与 ``tool_choice`` 字段；
        3. 发起异步 HTTP POST，捕获连接、超时与非 2xx 异常；
        4. 解析 ``choices[0].message``，若包含 ``tool_calls`` 则将其参数解析为 Python 字典。
        """
        url = self._build_url("/chat/completions")
        model_name = request.model or self.model

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": self._format_messages(request.messages),
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [tool.model_dump(mode="json") for tool in request.tools]
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        logger.debug(
            "Sending request to OpenAI-compatible LLM",
            extra={
                "context": {
                    "url": url,
                    "model": model_name,
                    "message_count": len(request.messages),
                    "tool_count": len(request.tools or []),
                }
            },
        )

        raw = await self._request("POST", "/chat/completions", payload=payload)
        return parse_completion(raw, model_name)

    async def _request(self, method: str, path: str, *, payload=None) -> Any:
        """One transport/error boundary for chat and model discovery."""
        url = self._build_url(path)
        timeout = min(15.0, self.timeout_seconds) if path == "/models" else self.timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.request(
                    method, url, headers=self._build_headers(), json=payload
                )
        except httpx.TimeoutException:
            raise LLMTimeoutError(f"LLM request timed out after {timeout}s") from None
        except (httpx.HTTPError, OSError):
            raise LLMConnectionError("Could not connect to the configured LLM endpoint") from None
        if response.status_code in {401, 403}:
            if path == "/models":
                raise LLMAuthenticationError(
                    f"鉴权失败 (HTTP {response.status_code})：请检查 API Key 是否有效"
                )
            raise LLMAuthenticationError(
                f"LLM authentication failed (HTTP {response.status_code}). Check your LLM_API_KEY."
            )
        if response.status_code != 200:
            raise LLMResponseError(f"LLM endpoint returned HTTP {response.status_code}")
        try:
            return response.json()
        except (ValueError, RecursionError):
            raise LLMResponseError("LLM response is not a valid JSON document") from None

    async def ping(self) -> dict[str, Any]:
        """快速探测 Base URL 与密钥的连通性。"""
        started = time.monotonic()
        # 发送最小化的 1 Token 探测请求
        request = ChatCompletionRequest(
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0.0,
        )
        res = await self.chat_completion(request)
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        return {
            "ok": True,
            "latency_ms": elapsed_ms,
            "model": res.model,
            "message": "Connection successful",
        }

    async def list_models(self) -> list[str]:
        """Fetch, validate and normalize the configured supplier's model IDs."""
        return parse_models(await self._request("GET", "/models"))


# Preserve imports used by existing service clients.
__all__ = [
    "OpenAIClient",
    "LLMError",
    "LLMConfigError",
    "LLMAuthenticationError",
    "LLMConnectionError",
    "LLMTimeoutError",
    "LLMResponseError",
]
