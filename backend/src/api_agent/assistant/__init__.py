"""Page-scoped AI assistant persistence models, tool registry, runner, and routes."""

from api_agent.assistant.models import (
    VALID_PAGE_SCOPES,
    AssistantMessageRecord,
    AssistantThreadRecord,
    PageScope,
)
from api_agent.assistant.registry import (
    SCOPE_TOOL_WHITELIST,
    SUITE_STARTING_TOOLS,
    AssistantToolRegistry,
    ToolExecutionContext,
)
from api_agent.assistant.routes import router as assistant_router
from api_agent.assistant.runner import ActiveAssistantRun, AssistantRunManager, LiveAssistantRun
from api_agent.assistant.thread_store import AssistantThreadRepository, scrub_assistant_fields

__all__ = [
    "ActiveAssistantRun",
    "AssistantMessageRecord",
    "AssistantRunManager",
    "AssistantThreadRecord",
    "AssistantThreadRepository",
    "AssistantToolRegistry",
    "LiveAssistantRun",
    "PageScope",
    "SCOPE_TOOL_WHITELIST",
    "SUITE_STARTING_TOOLS",
    "ToolExecutionContext",
    "VALID_PAGE_SCOPES",
    "assistant_router",
    "scrub_assistant_fields",
]
