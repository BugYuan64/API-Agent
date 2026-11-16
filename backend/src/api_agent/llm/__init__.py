"""导出 OpenAI 兼容客户端、共享错误及常用对话/工具数据结构。

实现分别位于 client、errors、schemas；导入本包不发起模型请求或执行工具。
"""

from api_agent.llm.client import (
    LLMAuthenticationError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAIClient,
)
from api_agent.llm.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    FunctionCall,
    FunctionDefinition,
    LLMStatusResponse,
    ToolCall,
    ToolDefinition,
)

__all__ = [
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "ChatMessage",
    "FunctionCall",
    "FunctionDefinition",
    "LLMAuthenticationError",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMResponseError",
    "LLMStatusResponse",
    "LLMTimeoutError",
    "OpenAIClient",
    "ToolCall",
    "ToolDefinition",
]
