"""SQL persistence for projects and append-only environment versions."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api_agent.projects.models import Environment, EnvironmentVersion, Project


class ProjectRepository:
    """项目与环境配置的只存 SQL 读写层。

    作用：把所有查询语句集中在此，service 层只关心业务规则与事务边界。
    实现：持有请求级 ``AsyncSession``，每个方法只做一次查询、不发提交，
    提交/回滚由调用方（ProjectService）负责。
    """

    def __init__(self, session: AsyncSession):
        """持有请求级会话，供各查询方法复用。

        实现：只保存引用，不在此处开启事务或发查询；
        会话的生命周期由 FastAPI 依赖（或测试）管理。
        """
        self.session = session

    async def get_project(self, project_id: str) -> Project | None:
        """按主键取单个项目。

        实现：用 ``session.get(Project, id)`` 走身份映射缓存，比手写 select 更省一次查询；
        不存在时返回 None，由上层决定是否抛 404。
        """
        return await self.session.get(Project, project_id)

    async def list_projects(self, limit: int, offset: int) -> tuple[list[Project], int]:
        """分页列出项目，并返回总数。

        实现：两次查询——一次 ``select`` 带 limit/offset 取当页数据，
        一次 ``count(*)`` 取总量（供响应里的 ``page.total``）；
        排序用 ``(created_at, id)`` 保证同秒创建时顺序稳定，分页不会重复或漏行。
        """
        projects = (
            await self.session.scalars(
                select(Project).order_by(Project.created_at, Project.id).limit(limit).offset(offset)
            )
        ).all()
        total = await self.session.scalar(select(func.count()).select_from(Project))
        return list(projects), total

    async def get_environment(self, environment_id: str) -> Environment | None:
        """按主键取环境（只含指针信息，不含具体配置）。

        实现：``session.get`` 返回 ``environments`` 表整行；
        当前的配置内容需再通过 ``get_version`` 取对应的版本行。
        """
        return await self.session.get(Environment, environment_id)

    async def get_version(self, environment_id: str, version: int) -> EnvironmentVersion | None:
        """取某个环境的指定版本配置。

        实现：按 ``(environment_id, version)`` 联合条件查询，
        正好走 ``UniqueConstraint("environment_id", "version")`` 的唯一索引。
        """
        return await self.session.scalar(
            select(EnvironmentVersion).where(
                EnvironmentVersion.environment_id == environment_id,
                EnvironmentVersion.version == version,
            )
        )

    async def list_environments(self, project_id: str, limit: int, offset: int):
        """分页列出某项目的环境，并一次性取回各自当前版本配置。

        作用：避免 N+1 查询——先查当页环境，再用一条 ``IN`` 查询取回这些环境的全部版本行，
        在内存里配对出每个环境的当前版本。
        实现：第二次查询把 ``environment_id.in_([...])`` 作为条件；
        随后用生成器表达式匹配 ``version == current_version`` 的行，
        组成 ``{environment_id: 当前版本行}`` 映射返回；无环境时跳过第二次查询。
        """
        environments = (
            await self.session.scalars(
                select(Environment)
                .where(Environment.project_id == project_id)
                .order_by(Environment.created_at, Environment.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
        total = await self.session.scalar(
            select(func.count())
            .select_from(Environment)
            .where(Environment.project_id == project_id)
        )
        versions = (
            (
                await self.session.scalars(
                    select(EnvironmentVersion).where(
                        EnvironmentVersion.environment_id.in_([item.id for item in environments])
                    )
                )
            ).all()
            if environments
            else []
        )
        by_environment = {}
        for item in environments:
            by_environment[item.id] = next(
                version
                for version in versions
                if version.environment_id == item.id and version.version == item.current_version
            )
        return list(environments), by_environment, total

    async def list_project_environment_ids(self, project_id: str) -> list[str]:
        """列出某项目下的全部环境 ID，供级联删除前清理子版本使用。"""
        return list(
            (
                await self.session.scalars(
                    select(Environment.id).where(Environment.project_id == project_id)
                )
            ).all()
        )

    async def list_versions(self, environment_id: str) -> list[EnvironmentVersion]:
        """按版本号升序列出某环境的全部历史版本。

        实现：过滤 environment_id 后 ``order_by(version)``，
        让返回值天然成为“追加式版本历史”的时间线。
        """
        return list(
            (
                await self.session.scalars(
                    select(EnvironmentVersion)
                    .where(EnvironmentVersion.environment_id == environment_id)
                    .order_by(EnvironmentVersion.version)
                )
            ).all()
        )
