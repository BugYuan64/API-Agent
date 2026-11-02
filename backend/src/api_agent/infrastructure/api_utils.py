"""Small dependency-free helpers for management API serialization."""

from datetime import UTC, datetime


def as_utc(value: datetime | None) -> datetime | None:
    """把数据库读出的无时区时间标注成 UTC。

    作用：SQLite 的 ``DateTime`` 不保存时区，直接序列化会得到不带 ``+00:00`` 的字符串；
    本函数保证所有响应里的时间都显式带 UTC 时区。
    实现：仅当值存在且 ``tzinfo`` 为空时 ``replace(tzinfo=UTC)``，
    已有其他时区的值保持原样，不做换算以保留原始信息。
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
