"""Alembic migration environment using the application's validated settings."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from api_agent.console import models as _console_models  # noqa: F401
from api_agent.execution import models as _execution_models  # noqa: F401
from api_agent.infrastructure.orm import Base
from api_agent.llm import models as _llm_models  # noqa: F401
from api_agent.projects import models as _project_models  # noqa: F401
from api_agent.streams import models as _stream_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """生成不建立数据库连接的迁移 SQL；由 Alembic 提供配置和目标 metadata。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """以同步连接执行当前版本迁移；显式导入模型使 metadata 完整，退出时关闭引擎。"""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
