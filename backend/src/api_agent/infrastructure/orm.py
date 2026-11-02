"""Shared ORM primitives; business tables stay in their owning modules."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 进程内最后发出的时间戳，用于保证 ``utc_now`` 严格递增（见其文档）。
_last_timestamp: datetime | None = None
_timestamp_lock = threading.Lock()


class Base(DeclarativeBase):
    """所有 ORM 模型的公共声明基类。

    作用：给 SQLAlchemy 2.0 的 Declarative 体系提供一个统一基类，
    使 ``metadata`` 和映射注册表被所有业务表共用。
    实现：只用类型注解声明，自身不定义任何表或列。
    """


def new_id() -> str:
    """生成 36 位 UUIDv4 字符串作为主键默认值。

    实现：``str(uuid.uuid4())`` 直接转成标准带连字符格式，
    与列定义 ``String(36)`` 的长度一致，且不依赖数据库自增。
    """
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """返回带 UTC 时区、且在进程内严格递增的当前时间。

    作用：多个接口靠 ``created_at`` 排序表达写入先后（预算账本尤其如此，
    ``reserved`` 必须先于 ``consumed``）。Windows 的系统时钟粒度约 15.6ms，
    同一批次写入的多行会拿到完全相同的 ``created_at``，这时排序只能退化到
    随机 UUID 兜底，账本顺序就会随机颠倒——这是实测到的偶发失败原因。
    实现：与上一次返回值比较，若没有前进则加 1 微秒，
    在保持 UTC 语义的前提下给出严格递增序列；用锁保证多线程安全
    （迁移会在线程池里执行）。系统时间大幅前进时直接采用真实值，不做阻挡。
    """
    global _last_timestamp
    now = datetime.now(UTC)
    with _timestamp_lock:
        if _last_timestamp is not None and now <= _last_timestamp:
            now = _last_timestamp + timedelta(microseconds=1)
        _last_timestamp = now
        return now


def as_utc(value: datetime | None) -> datetime | None:
    """把“无时区”的时间补成 UTC 时区，其余原样返回。

    实现：SQLite 不保存时区信息，读回来的 datetime 是 naive 的；
    这里在其 ``tzinfo`` 为空时 ``replace(tzinfo=UTC)``，
    让所有对外时间戳都带明确时区。
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class IdMixin:
    """为主表提供 36 位字符串主键 ``id`` 的混入类。

    实现：``mapped_column`` 声明主键并用 ``default=new_id`` 在 Python 侧赋值，
    这样对象在 ``flush`` 前就已有 ID，便于先构建关联行再统一提交。
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class CreatedAtMixin:
    """为主表提供创建时间 ``created_at`` 的混入类。

    实现：同时设置 Python 侧默认值 ``utc_now``（带时区、便于测试断言）
    和数据库侧默认值 ``server_default=func.now()``（绕过 ORM 直接写 SQL 时也能落到时间）。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
