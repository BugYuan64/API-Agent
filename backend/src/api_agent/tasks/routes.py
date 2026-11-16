"""Management endpoints for document recognition and durable automated test tasks."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from api_agent.agent.routes import Agent, Session, agent_for, api_error
from api_agent.agent.service import AgentError, CallBudget
from api_agent.infrastructure.api import ApiError, not_found
from api_agent.tasks.documents import recognize
from api_agent.tasks.schemas import Catalog, DocumentInput, StartTask
from api_agent.tasks.service import report

router = APIRouter(prefix="/api/tasks", tags=["document tasks"])


@router.post("/recognize", response_model=Catalog)
async def recognize_document(payload: DocumentInput, request: Request, session: Session):
    """Recognize standard documents locally; use a bounded model call for nonstandard text."""
    try:
        catalog = recognize(payload.text)
        if catalog is not None:
            return catalog
        agent = await agent_for(request, session)
        async with asyncio.timeout(120):
            return await agent.structured(
                "recognize", {"contract": payload.text}, Catalog, CallBudget(limit=2)
            )
    except AgentError as error:
        raise api_error(error) from None
    except (ValueError, TypeError, RecursionError, AttributeError, KeyError):
        raise ApiError(
            400, "invalid_document", "文档结构或引用不受支持，请检查接口与测试用例格式"
        ) from None
    except TimeoutError:
        raise ApiError(504, "recognition_timeout", "文档识别超时") from None


@router.post("", status_code=202)
async def start_task(payload: StartTask, request: Request, agent: Agent):
    """Accept a generated task after validating scope and complete case count."""
    try:
        return request.app.state.tasks.start(payload, agent)
    except ValueError:
        raise ApiError(400, "task_rejected", "目标、用例数量或输入配置未通过校验") from None


@router.get("")
async def list_tasks(request: Request):
    """Return bounded persisted history, including tasks created before a page refresh."""
    store = request.app.state.tasks.store
    paths = sorted(store.root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[
        :50
    ]
    return {
        "items": [
            {
                key: value
                for key, value in store.read(path.stem).items()
                if key not in {"snapshot", "results", "summary"}
            }
            for path in paths
        ]
    }


def read_record(request, task_id):
    """Map missing or malformed artifact IDs to a stable management 404."""
    try:
        return request.app.state.tasks.store.read(task_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Task") from None


@router.get("/{task_id}")
async def read_task(task_id: str, request: Request):
    """Return frozen cases, progress and evidence without re-executing requests."""
    return read_record(request, task_id)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """Stop a task while preserving all completed case results."""
    read_record(request, task_id)
    return await request.app.state.tasks.cancel(task_id)


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
    record = read_record(request, task_id)
    if task_id in request.app.state.tasks.active or record["status"] in {"queued", "running"}:
        raise ApiError(409, "task_active", "请先取消正在运行的任务")
    request.app.state.tasks.store.delete(task_id)
