"""Database engine, migrations and short-lived async sessions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api_agent.infrastructure.config import Settings

BACKEND_ROOT = Path(__file__).resolve().parents[3]


class Database:
    """SQLite 异步引擎与迁移入口的封装。

    作用：把引擎创建、SQLite pragma 设置、Alembic 迁移和会话工厂集中在一处，
    供应用启动/测试夹具复用。
    实现：``create_async_engine`` 基于配置里的 aiosqlite URL 建引擎；
    用 SQLAlchemy 的 ``connect`` 事件在每个物理连接建立时执行 pragma；
    再用 ``async_sessionmaker`` 产出会话工厂。
    """

    def __init__(self, settings: Settings):
        """创建异步引擎、注册 SQLite pragma 并建立会话工厂。

        实现：``connect_args={"timeout": 5}`` 让 aiosqlite 在等锁时最多阻塞 5 秒；
        随后用 ``event.listens_for`` 钩住 ``connect`` 事件，在每条物理连接建立时
        设置外键与忙等待；最后 ``async_sessionmaker(expire_on_commit=False)``
        产出会话工厂，使提交后对象仍可安全读取。
        """
        self.settings = settings
        self.engine: AsyncEngine = create_async_engine(
            settings.async_database_url,
            connect_args={"timeout": 5},
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record):
            """为每个新建的 SQLite 连接打开外键约束并设置忙等待超时。

            实现：直接拿 DBAPI 游标执行两条 PRAGMA。
            ``foreign_keys=ON`` 让 ``ondelete=RESTRICT/CASCADE`` 真正生效
            （SQLite 默认不校验外键）；``busy_timeout=5000`` 让并发写锁冲突时
            最多等待 5 秒再报 "database is locked"。
            """
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        # expire_on_commit=False：提交后仍可读取已加载属性，
        # 避免异步会话在响应序列化阶段再触发隐式 IO。
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        """同步执行 Alembic 迁移到最新版本（head）。

        实现：先确保数据库文件所在目录存在；再用代码构造 Alembic ``Config``
        （不依赖 alembic.ini 路径），把 script_location 指向 ``backend/migrations``；
        连接串中的 ``%`` 需要转义成 ``%%``，否则 ConfigParser 会做插值解析而报错；
        最后通过 ``config.attributes`` 把 settings 传给迁移脚本，
        并 ``command.upgrade(config, "head")``。
        """
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        config = Config()
        config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        config.set_main_option("sqlalchemy.url", self.settings.sync_database_url.replace("%", "%%"))
        config.attributes["settings"] = self.settings
        command.upgrade(config, "head")

    async def migrate_async(self) -> None:
        """在事件循环里异步地跑迁移。

        实现：Alembic 是同步阻塞 API，所以用 ``asyncio.to_thread`` 丢到线程池执行，
        防止迁移耗时阻塞整个 ASGI 事件循环。
        """
        await asyncio.to_thread(self.migrate)

    def session(self) -> AsyncSession:
        """创建一个新的异步会话（调用方负责 ``async with`` 关闭）。

        实现：直接委托给 ``async_sessionmaker`` 实例，保证每次调用都是独立会话，
        满足“短生命周期会话”的设计约束。
        """
        return self.sessions()

    async def dispose(self) -> None:
        """释放连接池并关闭所有底层连接。

        实现：调用引擎的 ``dispose()``；在应用 lifespan 结束时执行，
        避免进程退出时残留 SQLite 文件句柄。
        """
        await self.engine.dispose()

    async def __aenter__(self) -> Database:
        """支持 ``async with Database(settings) as db`` 写法：先迁移再交回自身。

        实现：进入上下文时执行迁移，保证拿到 Database 时表结构已就绪。
        """
        await self.migrate_async()
        return self

    async def __aexit__(self, *_exc) -> None:
        """退出异步上下文时释放引擎资源。

        实现：忽略异常参数，无条件 ``dispose()``，确保异常路径也不会泄漏连接。
        """
        await self.dispose()


async def session_for(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：为每个请求提供一个自动关闭的数据库会话。

    实现：从 ``request.app.state.database``（lifespan 中挂上的单例）取会话工厂，
    用 ``async with`` 包住 ``yield``，请求处理结束后会话自动关闭；
    该会话不自动提交，事务边界由各 service 显式 ``commit`` 控制。
    """
    async with request.app.state.database.session() as session:
        yield session
