"""OpenAI-compatible client with tool-calling capabilities."""

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
