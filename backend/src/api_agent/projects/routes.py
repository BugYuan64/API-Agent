"""HTTP endpoints for project and environment configuration."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import Page, PageInfo
from api_agent.infrastructure.database import session_for
from api_agent.projects.schemas import (
    EnvironmentCreate,
    EnvironmentHistoryResponse,
    EnvironmentResponse,
    EnvironmentRollback,
    EnvironmentUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
)
from api_agent.projects.service import ProjectService

router = APIRouter(prefix="/api", tags=["projects"])
Session = Annotated[AsyncSession, Depends(session_for)]
Pagination = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(data: ProjectCreate, session: Session):
    """创建项目。

    实现：请求体由 ``ProjectCreate`` 校验（长度、禁止多余字段），
    再交给 ``ProjectService.create_project`` 处理唯一约束与提交；
    成功返回 201 和项目详情，重名时由 service 抛 409。
    """
    return await ProjectService(session).create_project(data)


@router.get("/projects", response_model=Page[ProjectResponse])
async def list_projects(session: Session, limit: Pagination = 50, offset: Offset = 0):
    """分页列出项目。

    实现：``limit`` 由 ``Pagination`` 限制在 1..200、``offset`` 不小于 0；
    服务层返回 ``(当页 ORM 对象, 总数)``，这里用 ``Page[ProjectResponse]`` 包成
    ``{items, page:{limit, offset, total}}`` 的统一分页结构。
    """
    projects, total = await ProjectService(session).list_projects(limit, offset)
    return Page[ProjectResponse](
        items=[ProjectResponse.model_validate(item) for item in projects],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, session: Session):
    """按 ID 查询单个项目。

    实现：直接透传给服务层；不存在时服务层抛 ``not_found``，由全局处理器渲染 404。
    """
    return await ProjectService(session).get_project(project_id)


@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: str, data: ProjectUpdate, session: Session):
    """更新项目名称或说明；不存在返回 404，重名返回 409。"""
    return await ProjectService(session).update_project(project_id, data)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, session: Session):
    """删除项目及其下属环境与历史版本；不存在返回 404。"""
    await ProjectService(session).delete_project(project_id)


@router.post(
    "/projects/{project_id}/environments", response_model=EnvironmentResponse, status_code=201
)
async def create_environment(project_id: str, data: EnvironmentCreate, session: Session):
    """在项目下创建环境（自动生成第 1 版配置）。

    实现：路径里的 project_id 与请求体一起交给服务层；
    服务层先校验项目存在，再一次性写入环境行与 version=1 的配置行。
    """
    return await ProjectService(session).create_environment(project_id, data)


@router.get("/projects/{project_id}/environments", response_model=Page[EnvironmentResponse])
async def list_environments(
    project_id: str, session: Session, limit: Pagination = 50, offset: Offset = 0
):
    """分页列出某项目下的环境及其当前配置。

    实现：与项目列表同样的分页约定；服务层已把每个环境与它的当前版本组装好，
    这里只需要再套一层 ``Page`` 包装。
    """
    items, total = await ProjectService(session).list_environments(project_id, limit, offset)
    return Page[EnvironmentResponse](
        items=items, page=PageInfo(limit=limit, offset=offset, total=total)
    )


@router.get("/environments/{environment_id}", response_model=EnvironmentResponse)
async def get_environment(
    environment_id: str, session: Session, version: int | None = Query(None, ge=1)
):
    """查询环境配置，可选择查看某个历史版本。

    实现：``version`` 为可选查询参数且必须 ≥1；不传时服务层回落到
    ``current_version``，传了则精确取该版本（不存在返回 404）。
    """
    return await ProjectService(session).get_environment(environment_id, version)


@router.get("/environments/{environment_id}/versions", response_model=EnvironmentHistoryResponse)
async def environment_history(environment_id: str, session: Session):
    """返回环境的完整版本历史（追加式审计视图）。

    实现：服务层返回环境元信息 + 升序的全部版本列表；
    该接口不分页，因为版本数量受创建/更新频次约束，体量可控。
    """
    return await ProjectService(session).environment_history(environment_id)


@router.put("/environments/{environment_id}", response_model=EnvironmentResponse)
async def update_environment(environment_id: str, data: EnvironmentUpdate, session: Session):
    """更新环境配置——实为“追加新版本 + 前移指针”。

    实现：请求体里的 ``expected_version`` 作为乐观锁条件；
    版本不匹配或并发抢先时返回 409，前端需重新拉取最新版本再改。
    """
    return await ProjectService(session).update_environment(environment_id, data)


@router.post("/environments/{environment_id}/rollback", response_model=EnvironmentResponse)
async def rollback_environment(environment_id: str, data: EnvironmentRollback, session: Session):
    """将环境配置回滚到指定历史版本（追加新版本并前移指针，不改写既有版本记录）。"""
    return await ProjectService(session).rollback_environment(environment_id, data)
