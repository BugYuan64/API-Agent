"""Management endpoints for document recognition and durable automated test tasks."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from api_agent.agent.routes import Session, agent_for, api_error, generation_agent_for
from api_agent.agent.service import AgentError, AgentService, CallBudget
from api_agent.infrastructure.api import ApiError, not_found
from api_agent.tasks.chat import AssistantChat, ChatTurns, ReviewChat, review_frames
from api_agent.tasks.generation import TestShortfall
from api_agent.tasks.planning import TaskPlanningService
from api_agent.tasks.project_document import create_project_document, require_local_browser
from api_agent.tasks.schemas import (
    Catalog,
    RecognizeDocument,
    RegenerateTests,
    RerunTask,
    StartTask,
)
from api_agent.tasks.service import TaskActiveError, public_task, report

router = APIRouter(prefix="/api/tasks", tags=["document tasks"])
project_picker = asyncio.Lock()
planner = TaskPlanningService()


@router.post("/generate-document")
async def generate_project_document(request: Request, session: Session):
    """Only the native desktop picker can select a directory for analysis."""
    require_local_browser(request)
    if project_picker.locked():
        raise ApiError(409, "project_document_active", "已有项目选择或文档生成正在进行")
    async with project_picker:
        agent = await generation_agent_for(request, session)
        await session.rollback()
        return await create_project_document(request, agent)


class GeneratedChatTitle(BaseModel):
    """A concise title based on the first assistant conversation question."""

    title: str = Field(min_length=1, max_length=80)


class ChatTitleRequest(BaseModel):
    """Bound the first question used to name a chat-only history."""

    question: str = Field(min_length=1, max_length=4000)


@router.post("/chat/title", response_model=GeneratedChatTitle)
async def generate_chat_title(payload: ChatTitleRequest, request: Request, session: Session):
    """Ask the configured model to summarize the first user question for its history title."""
    agent = await agent_for(request, session)
    try:
        async with asyncio.timeout(20):
            return await agent.structured(
                "chat_title",
                {"question": payload.question},
                GeneratedChatTitle,
                CallBudget(limit=1),
            )
    except (AgentError, ApiError, TimeoutError):
        return GeneratedChatTitle(title=payload.question.strip()[:80] or "AI 对话")


async def optional_generation_agent(request: Request, session: Session) -> AgentService | None:
    """Resolve the generation model when configured, or None so local parsing remains available."""
    try:
        return await generation_agent_for(request, session)
    except ApiError as error:
        if error.code != "model_not_configured":
            raise
        return None


@router.post("/recognize", response_model=Catalog)
async def recognize_document(payload: RecognizeDocument, request: Request, session: Session):
    """Recognize standard documents locally; use a bounded model call for nonstandard text."""
    try:
        agent = await optional_generation_agent(request, session)
        return await planner.recognize_and_draft(agent, payload)
    except TestShortfall as error:
        raise ApiError(422, "test_quota_unmet", str(error)) from None
    except AgentError as error:
        if str(error) == "model_budget_exhausted":
            raise ApiError(
                422, "test_quota_unmet", "test 生成已达到模型调用预算，草稿保持不变。"
            ) from None
        raise api_error(error) from None
    except (ValueError, TypeError, RecursionError, AttributeError, KeyError):
        raise ApiError(
            400, "invalid_document", "请检查文档格式、接口引用与用例数量（每次最多 200 条）"
        ) from None
    except TimeoutError:
        raise ApiError(
            504, "recognition_timeout", "文档识别或 test 生成超时，未替换原草稿"
        ) from None


@router.post("/regenerate", response_model=Catalog)
async def regenerate_tests(payload: RegenerateTests, request: Request, session: Session):
    """Keep the draft intact unless every selected group has its requested quota."""
    if any(not 0 <= index < len(payload.catalog.endpoints) for index in payload.selected):
        raise ApiError(400, "unknown_operation", "所选接口已不在草稿中")
    try:
        agent = await generation_agent_for(request, session)
        return await planner.regenerate_selected(agent, payload)
    except TestShortfall as error:
        raise ApiError(422, "test_quota_unmet", str(error)) from None
    except AgentError as error:
        if str(error) == "model_budget_exhausted":
            raise ApiError(
                422, "test_quota_unmet", "test 生成已达到模型调用预算，草稿保持不变。"
            ) from None
        raise api_error(error) from None
    except (ValueError, TypeError, RecursionError, AttributeError, KeyError):
        raise ApiError(
            400, "invalid_draft", "草稿无法重新生成，请检查接口或数量（最多 200 条）"
        ) from None
    except TimeoutError:
        raise ApiError(504, "recognition_timeout", "test 生成超时，未替换原草稿") from None


async def task_agent(request: Request, session: Session) -> AgentService:
    """Keep deterministic test execution available without optional AI summaries."""
    try:
        return await agent_for(request, session)
    except ApiError as error:
        if error.code != "model_not_configured":
            raise
        return AgentService(request.app.state.settings, None)


@router.post("", status_code=202)
async def start_task(payload: StartTask, request: Request, session: Session):
    """Accept a generated task after validating scope and complete case count."""
    try:
        return public_task(
            request.app.state.tasks.start(payload, await task_agent(request, session))
        )
    except ValueError:
        raise ApiError(400, "task_rejected", "目标、用例数量或输入配置未通过校验") from None


@router.get("")
async def list_tasks(request: Request):
    """Return bounded persisted history, including tasks created before a page refresh."""
    return {"items": request.app.state.tasks.list_tasks(limit=50)}


def read_record(request, task_id):
    """Map missing or malformed artifact IDs to a stable management 404."""
    try:
        return request.app.state.tasks.get_task(task_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Task") from None


@router.get("/{task_id}")
async def read_task(task_id: str, request: Request):
    """Return frozen cases, progress and evidence without re-executing requests."""
    return public_task(read_record(request, task_id))


@router.get("/{task_id}/live")
async def live_task(task_id: str, request: Request):
    """Publish current wait state, SSE events, and summary updates without re-executing cases."""
    record = read_record(request, task_id)
    return StreamingResponse(
        request.app.state.tasks.observe(record),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def assistant_response(
    payload: ChatTurns,
    request: Request,
    session: Session,
    record,
    *,
    model=None,
    enable_assistant_tools: bool = False,
):
    """Run a conversational stream independently of the task's execution and summary."""
    if (
        payload.messages[-1].role != "user"
        or sum(len(item.content) for item in payload.messages) > 32000
    ):
        raise ApiError(422, "invalid_review_messages", "请发送有效问题，或缩短对话内容")
    agent = await agent_for(request, session)
    llm_fallback = None
    if enable_assistant_tools and agent.client is not None:
        llm_fallback = {"base_url": agent.client.base_url, "api_key": agent.client.api_key}
    knowledge = getattr(request.app.state, "knowledge", None) if enable_assistant_tools else None
    await session.rollback()
    return StreamingResponse(
        review_frames(
            agent,
            record,
            payload,
            model,
            knowledge=knowledge,
            llm_fallback=llm_fallback,
            enable_assistant_tools=enable_assistant_tools,
        ),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/chat")
async def chat_with_assistant(payload: AssistantChat, request: Request, session: Session):
    """Use the global model, with current test page context, reader tool and knowledge base."""
    record = read_record(request, payload.task_id) if payload.task_id else None
    return await assistant_response(payload, request, session, record, enable_assistant_tools=True)


@router.post("/{task_id}/chat")
async def discuss_task(task_id: str, payload: ReviewChat, request: Request, session: Session):
    """Retain the existing task-scoped assistant API for callers with a task ID."""
    return await assistant_response(
        payload,
        request,
        session,
        read_record(request, task_id),
        model=payload.model,
        enable_assistant_tools=False,
    )


@router.get("/{task_id}/document")
async def download_document(task_id: str, request: Request):
    """Serve the original local attachment as a download, never as executable site content."""
    from urllib.parse import quote

    document = read_record(request, task_id)["snapshot"].get("document")
    if document is None:
        raise not_found("Document")
    filename = document["filename"].replace("\\", "/").rsplit("/", 1)[-1]
    return PlainTextResponse(
        document["text"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{task_id}/rerun", status_code=202)
async def rerun_task(task_id: str, payload: RerunTask, request: Request, session: Session):
    """Start a distinct run using the recorded cases, optional edits and current settings."""
    source = read_record(request, task_id)
    try:
        return public_task(
            request.app.state.tasks.rerun(payload, await task_agent(request, session), source)
        )
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, "task_rejected", "目标或历史用例未通过校验") from None


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """Stop a task while preserving all completed case results."""
    read_record(request, task_id)
    return public_task(await request.app.state.tasks.cancel(task_id))


@router.get("/{task_id}/report")
async def export_report(task_id: str, request: Request):
    """Download complete or partial Markdown reports from saved evidence."""
    record = read_record(request, task_id)
    return PlainTextResponse(
        report(record),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="task-{record["id"]}.md"'},
    )


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: str, request: Request):
    """Delete only terminal artifacts, never race an active worker's writes."""
    try:
        request.app.state.tasks.delete_task(task_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Task") from None
    except TaskActiveError:
        raise ApiError(409, "task_active", "请先取消正在运行的任务") from None
    threads = getattr(request.app.state, "assistant_threads", None)
    if threads is not None:
        await threads.delete_thread("tasks", task_id)
