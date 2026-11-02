"""Projects and append-only target environment versions."""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from api_agent.infrastructure.orm import Base, CreatedAtMixin, IdMixin


class Project(IdMixin, CreatedAtMixin, Base):
    """项目：配置的最高一层归属。

    实现：``name`` 加唯一索引，用作创建时的去重约束；
    ``description`` 默认为空串而非 NULL，避免读取方到处判空。
    """

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")


class Environment(IdMixin, CreatedAtMixin, Base):
    """环境：项目下的一套目标系统命名空间（如 dev/test）。

    作用：自身只保存“指针”信息，具体配置以版本行的形式存放在
    ``environment_versions``，``current_version`` 指向当前生效的那一版。
    实现：``(project_id, name)`` 联合唯一，保证同一项目内环境不重名；
    外键 ``ondelete="RESTRICT"`` 防止误删仍被引用的项目。
    """

    __tablename__ = "environments"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    current_version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (UniqueConstraint("project_id", "name"),)


class EnvironmentVersion(IdMixin, CreatedAtMixin, Base):
    """环境配置的历史版本，只追加不修改。

    作用：每次更新都插入一条新行，从而天然保留完整审计轨迹，
    并让乐观锁只需比较版本号。
    实现：origin、方法白名单、路径模板、鉴权引用、重放策略、敏感字段规则
    都以 JSON 列存储（这些结构简单且无需跨行查询）；
    ``(environment_id, version)`` 唯一约束保证同一版本号不会写两次。
    """

    __tablename__ = "environment_versions"

    environment_id: Mapped[str] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    origin: Mapped[str] = mapped_column(String(255))
    allowed_methods: Mapped[list[str]] = mapped_column(JSON)
    path_templates: Mapped[list[str]] = mapped_column(JSON)
    auth_ref: Mapped[dict | None] = mapped_column(JSON)
    replay_policy: Mapped[dict] = mapped_column(JSON)
    sensitive_field_rules: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    __table_args__ = (UniqueConstraint("environment_id", "version"),)
