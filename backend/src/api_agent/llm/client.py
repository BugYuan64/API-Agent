"""通过 httpx 调用 OpenAI 兼容的 Chat Completions 与模型列表接口。

依赖配置、共享错误和响应解析器；仅支持非流式传输及工具调用数据，
不执行返回的工具、不自动重试，也不验证供应商具备全部 OpenAI 能力。
"""

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
    """封装可配置的 OpenAI 兼容 HTTP 客户端，传输后统一校验供应商响应。

    支持 Settings 或直接传入配置；工具调用只做序列化和解析，不执行函数，
    使用 Chat Completions 协议，不保证任意供应商扩展字段兼容。
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
        """保存连接参数；提供 Settings 时优先采用其中配置，此处不探测服务。"""
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
        """返回启用且 Base URL 非空的本地配置状态，不证明端点可达或模型可用。"""
        return bool(self.enabled and self.base_url and self.base_url.strip())

    def _get_base_url(self) -> str:
        """返回去除末尾斜杠的配置地址；缺失时抛 LLMConfigError。"""
        if not self.base_url:
            raise LLMConfigError(
                "大模型 Base URL 尚未配置。请在前端控制台「系统设置」中配置服务地址。"
            )
        return self.base_url.rstrip("/")

    def _build_url(self, path: str = "/chat/completions") -> str:
        """拼接接口路径，兼容配置到 /chat/completions 的地址；缺配置沿用配置错误。"""
        base = self._get_base_url()
        # Accept a configured completion endpoint for both chat and model listing.
        if base.endswith("/chat/completions"):
            base = base.removesuffix("/chat/completions")
        return f"{base}{path}"

    def _build_headers(self) -> dict[str, str]:
        """返回 JSON 请求头；仅在非空密钥存在时加入 Bearer 鉴权。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key.strip():
            headers["Authorization"] = f"Bearer {self.api_key.strip()}"
        return headers

    def _format_messages(self, messages) -> list[dict[str, Any]]:
        """把 ChatMessage 序列转换为对话 JSON 列表，将工具参数字典编码为 JSON 字符串。"""
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
        """发送非流式 Chat Completions 请求并返回规范化文本、工具调用和 usage。

        请求模型优先于默认模型；工具仅传递定义，参数解析不等于执行，
        配置、传输及响应结构错误由对应 LLMError 子类向外传播。
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
        """对指定路径发送一次异步 HTTP 请求并返回解码后的 JSON。

        禁用环境代理，模型列表超时最多 15 秒；仅接受 HTTP 200，鉴权、
        连接、超时及非法 JSON 转为稳定 LLM 错误，不附带供应商响应正文。
        """
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
        """发送最多生成一个 token 的真实对话请求，返回成功状态、耗时及响应模型。

        探测会消耗模型额度；失败沿用对话请求错误，不证明工具调用可用。
        """
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
        """获取并返回去重排序的供应商模型 ID；传输或列表结构错误向外传播。"""
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
