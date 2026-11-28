"""Transactional use cases for project and environment management."""

from copy import deepcopy

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.infrastructure.api import conflict, not_found
from api_agent.infrastructure.orm import new_id, utc_now
from api_agent.projects.models import Environment, EnvironmentVersion, Project
from api_agent.projects.repository import ProjectRepository
from api_agent.projects.schemas import (
    EnvironmentCreate,
    EnvironmentHistoryResponse,
    EnvironmentResponse,
    EnvironmentRollback,
    EnvironmentUpdate,
    EnvironmentVersionResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
)


class ProjectService:
    """项目与环境管理的业务用例层。

    作用：承载事务边界（何时 commit/rollback）、唯一性冲突翻译和响应组装，
    让路由只做参数注入、仓储只做 SQL。
    实现：构造时同时持有 session 与 repository；所有写操作显式提交，
    并把数据库的 IntegrityError 翻译成带稳定错误码的 409。
    """

    def __init__(self, session: AsyncSession):
        """绑定请求级会话并创建配套仓储。

        实现：仓储与会话共享同一连接，因此 service 内的多次查询
        都在同一个事务上下文里，提交/回滚能够一次性覆盖全部改动。
        """
        self.session = session
        self.repository = ProjectRepository(session)

    async def create_project(self, data: ProjectCreate) -> ProjectResponse:
        """新建项目，重名时返回 409。

        实现：直接构造 ORM 对象 add 后 commit；不预先 SELECT 查重，
        而是依赖 ``projects.name`` 的唯一索引，捕获 ``IntegrityError`` 后
        rollback 并抛 ``project_exists``——这样在并发下也不会出现竞态漏洞。
        """
        project = Project(name=data.name, description=data.description)
        self.session.add(project)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise conflict("project_exists", "A project with this name already exists") from None
        return ProjectResponse.model_validate(project)

    async def get_project(self, project_id: str) -> ProjectResponse:
        """按 ID 取项目，缺失时抛 404。

        实现：交给仓储查询，判空后抛 ``not_found``；
        ``model_validate`` 借助 ApiModel 的 ``from_attributes=True`` 直接从 ORM 对象读字段。
        """
        project = await self.repository.get_project(project_id)
        if project is None:
            raise not_found("Project")
        return ProjectResponse.model_validate(project)

    async def list_projects(self, limit: int, offset: int):
        """分页查询项目列表，原样返回 ``(当页对象, 总数)``。

        实现：直接透传给仓储，不做额外包装；由路由层负责包成 ``Page`` 响应。
        """
        return await self.repository.list_projects(limit, offset)

    async def update_project(self, project_id: str, data: ProjectUpdate) -> ProjectResponse:
        """更新项目名称或说明；缺失返回 404，重名返回 409。"""
        project = await self.repository.get_project(project_id)
        if project is None:
            raise not_found("Project")
        if data.name is not None:
            project.name = data.name
        if data.description is not None:
            project.description = data.description
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise conflict("project_exists", "A project with this name already exists") from None
        await self.session.refresh(project)
        return ProjectResponse.model_validate(project)

    async def delete_project(self, project_id: str) -> None:
        """删除项目及其下属环境与环境版本（历史执行/控制台/长连接记录外键自动置空）。"""
        project = await self.repository.get_project(project_id)
        if project is None:
            raise not_found("Project")
        env_ids = await self.repository.list_project_environment_ids(project_id)
        if env_ids:
            await self.session.execute(
                delete(EnvironmentVersion).where(EnvironmentVersion.environment_id.in_(env_ids))
            )
            await self.session.execute(delete(Environment).where(Environment.id.in_(env_ids)))
        await self.session.delete(project)
        await self.session.commit()

    async def create_environment(self, project_id: str, data: EnvironmentCreate):
        """在指定项目下创建环境，并同时写入它的第 1 版配置。

        实现：先校验项目存在（不存在 404）；然后手动给环境和版本都预先赋 ``new_id()``，
        这样版本行能直接引用环境 ID，两条记录可以放在同一个 ``add_all`` 里一次提交；
        ``data.model_dump(exclude={"name"})`` 把 origin/allowed_methods 等配置字段
        整体铺进版本行，避免逐字段复制。重名时由唯一约束报错，翻译成 ``environment_exists``。
        """
        if await self.repository.get_project(project_id) is None:
            raise not_found("Project")
        environment = Environment(project_id=project_id, name=data.name)
        environment.id = new_id()
        version = EnvironmentVersion(
            id=new_id(),
            environment_id=environment.id,
            version=1,
            created_at=utc_now(),
            **data.model_dump(exclude={"name"}),
        )
        self.session.add_all([environment, version])
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise conflict(
                "environment_exists", "An environment with this name already exists in the project"
            ) from None
        return self.environment_response(environment, version)

    async def update_environment(self, environment_id: str, data: EnvironmentUpdate):
        """追加一个新版本，并用乐观锁保证并发安全。

        作用：环境配置是“只追加不修改”的，更新等于在 ``environment_versions``
        里插入 version+1，同时把指针 ``environments.current_version`` 前移。
        实现：三步——
        ① 读当前版本，与客户端给的 ``expected_version`` 比对，不一致直接 409；
        ② 用带 ``where current_version == expected_version`` 的 UPDATE 做原子推进，
           通过 ``rowcount`` 判定是否被其他请求抢先（并发下只有一方能成功）；
        ③ 插入新版本行并提交，再 ``refresh`` 环境对象拿到最新的 current_version。
        """
        environment = await self.repository.get_environment(environment_id)
        if environment is None:
            raise not_found("Environment")
        if environment.current_version != data.expected_version:
            raise conflict(
                "environment_version_conflict",
                f"Expected environment version {data.expected_version}, "
                f"current version is {environment.current_version}",
            )
        next_version = environment.current_version + 1
        result = await self.session.execute(
            update(Environment)
            .where(
                Environment.id == environment_id,
                Environment.current_version == data.expected_version,
            )
            .values(current_version=next_version)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            raise conflict(
                "environment_version_conflict", "Environment version changed concurrently"
            )
        version = EnvironmentVersion(
            environment_id=environment_id,
            version=next_version,
            created_at=utc_now(),
            **data.model_dump(exclude={"expected_version"}),
        )
        self.session.add(version)
        await self.session.commit()
        await self.session.refresh(environment)
        return self.environment_response(environment, version)

    async def rollback_environment(
        self, environment_id: str, data: EnvironmentRollback
    ) -> EnvironmentResponse:
        """将环境配置回滚到指定历史版本：以追加新版本的方式复制历史快照，保留审计历史。"""
        environment = await self.repository.get_environment(environment_id)
        if environment is None:
            raise not_found("Environment")
        if environment.current_version != data.expected_version:
            raise conflict(
                "environment_version_conflict",
                f"Expected environment version {data.expected_version}, "
                f"current version is {environment.current_version}",
            )
        target = await self.repository.get_version(environment_id, data.target_version)
        if target is None:
            raise not_found("Environment version")
        next_version = environment.current_version + 1
        result = await self.session.execute(
            update(Environment)
            .where(
                Environment.id == environment_id,
                Environment.current_version == data.expected_version,
            )
            .values(current_version=next_version)
        )
        if result.rowcount != 1:
            await self.session.rollback()
            raise conflict(
                "environment_version_conflict", "Environment version changed concurrently"
            )
        version = EnvironmentVersion(
            environment_id=environment_id,
            version=next_version,
            origin=target.origin,
            allowed_methods=list(target.allowed_methods),
            path_templates=list(target.path_templates),
            auth_ref=deepcopy(target.auth_ref),
            replay_policy=deepcopy(target.replay_policy),
            sensitive_field_rules=list(target.sensitive_field_rules),
            created_at=utc_now(),
        )
        self.session.add(version)
        await self.session.commit()
        await self.session.refresh(environment)
        return self.environment_response(environment, version)

    async def get_environment(self, environment_id: str, version: int | None = None):
        """取环境当前配置，或指定历史版本。

        实现：``version`` 为 None 时取 ``environment.current_version``，
        否则按传入版本号查；版本行不存在（含传入不存在的版本号）统一抛
        ``Environment version`` 的 404。
        """
        environment = await self.repository.get_environment(environment_id)
        if environment is None:
            raise not_found("Environment")
        config = await self.repository.get_version(
            environment_id, version or environment.current_version
        )
        if config is None:
            raise not_found("Environment version")
        return self.environment_response(environment, config)

    async def list_environments(self, project_id: str, limit: int, offset: int):
        """分页列出某项目的环境，并带上各自的当前版本配置。

        实现：先确认项目存在（否则 404，避免“空列表”与“项目不存在”混淆），
        再用仓储返回的 ``{环境ID: 当前版本行}`` 映射逐个组装响应，
        返回 ``(响应列表, 总数)`` 供路由层分页。
        """
        if await self.repository.get_project(project_id) is None:
            raise not_found("Project")
        environments, versions, total = await self.repository.list_environments(
            project_id, limit, offset
        )
        items = [self.environment_response(item, versions[item.id]) for item in environments]
        return items, total

    async def environment_history(self, environment_id: str) -> EnvironmentHistoryResponse:
        """返回环境信息及其全部版本历史。

        实现：取环境主记录判空后，用 ``list_versions`` 拿到升序版本列表，
        一次性组装成带 ``versions`` 数组的历史响应（版本行本身不做裁剪，
        由 ``EnvironmentVersionResponse`` 定义对外的字段白名单）。
        """
        environment = await self.repository.get_environment(environment_id)
        if environment is None:
            raise not_found("Environment")
        versions = await self.repository.list_versions(environment_id)
        return EnvironmentHistoryResponse(
            id=environment.id,
            project_id=environment.project_id,
            name=environment.name,
            current_version=environment.current_version,
            created_at=environment.created_at,
            versions=[EnvironmentVersionResponse.model_validate(item) for item in versions],
        )

    @staticmethod
    def environment_response(environment, version) -> EnvironmentResponse:
        """把“环境指针 + 某个版本配置”拼成对外响应对象。

        作用：HTTP 层的 EnvironmentResponse 是“环境 + config”二合一的扁平结构，
        而数据库里两者分表存放，这里做一次组装。
        实现：显式列出环境侧字段，``config`` 用 ``model_validate`` 从版本行转换；
        设为静态方法是因为它不依赖 session 或仓储，纯映射逻辑。
        """
        return EnvironmentResponse(
            id=environment.id,
            project_id=environment.project_id,
            name=environment.name,
            current_version=environment.current_version,
            created_at=environment.created_at,
            config=EnvironmentVersionResponse.model_validate(version),
        )
