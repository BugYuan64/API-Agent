"""Archived document reads; retired snapshot writers are not mounted by the application."""

import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import Field, field_validator
from sqlalchemy.exc import IntegrityError

from api_agent.agent.routes import Agent, Session
from api_agent.infrastructure.api import ApiError, ApiInput, conflict, not_found
from api_agent.infrastructure.orm import new_id
from api_agent.projects.document_export import render_markdown, render_openapi
from api_agent.projects.document_store import source_summary
from api_agent.projects.models import Project
from api_agent.projects.schemas import ProjectCreate, ProjectResponse
from api_agent.projects.service import ProjectService
from api_agent.projects.source import SourceScanError, scan_directory

router = APIRouter(prefix="/api/projects", tags=["project documents"])
legacy_writer = APIRouter(prefix="/api/projects")


class DirectoryInput(ApiInput):
    """在后端所在电脑读取用户指定的绝对目录，路径只用于本次扫描。"""

    directory: str = Field(min_length=1, max_length=4096)

    @field_validator("directory")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        """拒绝依赖服务器工作目录解释的相对路径及控制字符。"""
        if not Path(value).is_absolute() or any(ord(char) < 32 for char in value):
            raise ValueError("absolute_directory_required")
        return value


class ImportProject(ProjectCreate, DirectoryInput):
    """创建项目并导入源码所需的名称、说明与目录。"""


class GenerateDocument(ApiInput):
    """按界面确认的源码版本生成，重新导入不会改变已受理任务的输入。"""

    source_id: str = Field(min_length=36, max_length=36)


async def scan(directory: str, artifact_dir: Path):
    """在线程中读取目录，文件失败使用安全错误码，不回显本地绝对路径。"""
    try:
        snapshot = await asyncio.to_thread(scan_directory, directory, exclude_paths=[artifact_dir])
    except (SourceScanError, OSError, ValueError):
        raise ApiError(400, "source_unreadable", "无法读取项目目录，请检查路径与文件权限") from None
    if not snapshot.files:
        raise ApiError(400, "source_empty", "目录中没有可读取的源码，请选择项目源代码目录")
    return snapshot


def read_artifact(request: Request, project_id: str, kind: str, item_id: str) -> dict:
    """读取当前项目下的工件；其它项目的 ID 和非法 UUID 均不可跨项目读取。"""
    try:
        return request.app.state.documents.store.read(project_id, kind, item_id)
    except (ValueError, FileNotFoundError):
        raise not_found("Source" if kind == "sources" else "Document") from None


def document_response(record: dict) -> dict:
    """从同一持久记录渲染预览，刷新后包含准确覆盖进度及部分成果。"""
    return {**record, "markdown": render_markdown(record)}


@legacy_writer.post("/import", status_code=201)
async def import_project(data: ImportProject, request: Request, session: Session):
    """先成功扫描和保存源码，再创建项目；数据库拒绝时回收本次孤立快照。"""
    snapshot = await scan(data.directory, request.app.state.settings.artifact_dir)
    project_id = new_id()
    store = request.app.state.documents.store
    source = store.import_source(project_id, snapshot)
    project = Project(id=project_id, name=data.name, description=data.description)
    session.add(project)
    try:
        await session.commit()
    except BaseException as error:
        await session.rollback()
        store.path(project_id, "sources", source["id"]).unlink(missing_ok=True)
        if isinstance(error, IntegrityError):
            raise conflict("project_exists", "A project with this name already exists") from None
        raise
    return {"project": ProjectResponse.model_validate(project), "source": source_summary(source)}


@legacy_writer.post("/{project_id}/source", status_code=201)
async def import_source(project_id: str, data: DirectoryInput, request: Request, session: Session):
    """给已有项目追加独立源码版本，读文件时释放数据库读事务。"""
    await ProjectService(session).get_project(project_id)
    await session.rollback()
    snapshot = await scan(data.directory, request.app.state.settings.artifact_dir)
    record = request.app.state.documents.store.import_source(project_id, snapshot)
    return source_summary(record)


@router.get("/{project_id}/source")
async def latest_source(project_id: str, request: Request, session: Session):
    """返回最新导入的扫描清单，旧项目尚无源码时返回明确 404。"""
    await ProjectService(session).get_project(project_id)
    records = request.app.state.documents.store.list(project_id, "sources")
    if not records:
        raise not_found("Source")
    return source_summary(records[0])


@legacy_writer.post("/{project_id}/documents", status_code=202)
async def generate_document(
    project_id: str,
    data: GenerateDocument,
    request: Request,
    response: Response,
    agent: Agent,
    session: Session,
):
    """复用已配置模型创建后台任务，冻结版本无需保持创建请求连接。"""
    project = await ProjectService(session).get_project(project_id)
    await session.rollback()
    source = read_artifact(request, project_id, "sources", data.source_id)
    record = request.app.state.documents.start(project, source, agent)
    response.headers["Location"] = f"/api/projects/{project_id}/documents/{record['id']}"
    return document_response(record)


@router.get("/{project_id}/documents")
async def list_documents(project_id: str, request: Request, session: Session):
    """返回项目文档历史及覆盖计数，选择详情后读取具体预览。"""
    await ProjectService(session).get_project(project_id)
    records = request.app.state.documents.store.list(project_id, "documents")
    return {"schema_version": "1", "items": [document_response(row) for row in records]}


@router.get("/{project_id}/documents/{document_id}")
async def get_document(project_id: str, document_id: str, request: Request, session: Session):
    """从所属项目读取文档，不触发额外扫描或模型请求。"""
    await ProjectService(session).get_project(project_id)
    return document_response(read_artifact(request, project_id, "documents", document_id))


@legacy_writer.post("/{project_id}/documents/{document_id}/cancel")
async def cancel_document(project_id: str, document_id: str, request: Request, session: Session):
    """等待取消收尾后返回终态，已生成文档仍可下载。"""
    await ProjectService(session).get_project(project_id)
    await session.rollback()
    read_artifact(request, project_id, "documents", document_id)
    record = await request.app.state.documents.cancel(project_id, document_id)
    return document_response(record)


@router.get("/{project_id}/documents/{document_id}/export")
async def export_document(
    project_id: str,
    document_id: str,
    request: Request,
    session: Session,
    format: Literal["markdown", "openapi"] = "markdown",
):
    """下载同一记录的 Markdown 或 OpenAPI JSON，部分结果附带状态与遗漏。"""
    await ProjectService(session).get_project(project_id)
    record = read_artifact(request, project_id, "documents", document_id)
    if format == "openapi":
        content = json.dumps(render_openapi(record), ensure_ascii=False, indent=2)
        media, suffix = "application/json", "openapi.json"
    else:
        content = render_markdown(record)
        media, suffix = "text/markdown", "md"
    return PlainTextResponse(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="api-{document_id}.{suffix}"'},
    )
