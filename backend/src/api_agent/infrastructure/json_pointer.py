"""Single implementation of RFC 6901 JSON Pointer handling.

作用：指针语义此前在四处各写一遍（native 引擎求值、Collection 编译生成 JS、
agent 变异定位、隐私模块的敏感判定），任何一处的边界处理不一致都会让
“同一份计划在两个引擎下判定不同”。这里收敛为唯一实现。
实现：``parse`` 负责转义还原与合法性；``evaluate`` 返回“是否存在 + 值”；
``is_sensitive`` 判断路径上任一段是否命中敏感键。数组下标只接受规范十进制，
``01``、``-``、``length`` 与越界索引一律视为不存在——与 Postman ``atPointer`` 对齐。
"""

import re

# 合法的 RFC 6901 指针：空串或以 / 开头，~ 只能后接 0 或 1。
POINTER_VALID = re.compile(r"(?:/(?:[^~]|~[01])*)*")
# 数组下标：只接受 ASCII 十进制且无前导零（与 Postman atPointer 一致）。
_CANONICAL_INDEX = re.compile(r"0|[1-9][0-9]*")


def is_valid(pointer: str) -> bool:
    """判断字符串是否是语法合法的 JSON Pointer。"""
    return isinstance(pointer, str) and POINTER_VALID.fullmatch(pointer) is not None


def parse(pointer: str) -> list[str]:
    """把指针拆成未转义的路径段列表。

    实现：空指针无段；否则按 RFC 6901 **只剥掉首个** ``/`` 再切分——
    用 ``lstrip`` 会把 ``//a`` 的空段一起吃掉，使 ``["", "a"]`` 退化成 ``["a"]``，
    而 Postman 的 ``pointer.slice(1).split('/')`` 仍会得到空段，两个引擎判定随之分叉。
    最后按 ``~1`` → ``/``、``~0`` → ``~`` 还原转义。
    """
    if pointer == "":
        return []
    return [segment.replace("~1", "/").replace("~0", "~") for segment in pointer[1:].split("/")]


def _index(token: str, length: int) -> int | None:
    """把数组下标 token 转成索引；非规范或越界返回 None。

    实现：用 ASCII 十进制正则而不是 ``str.isdigit()``——后者对 ``'١'``、
    ``'²'`` 等字符也为真，``int()`` 要么得到与 Postman 不一致的索引，
    要么直接抛 ``ValueError`` 把断言求值变成 500。
    ``01`` 这类带前导零的写法在 Postman 侧也不算成员，因此一并拒绝。
    """
    if not _CANONICAL_INDEX.fullmatch(token):
        return None
    index = int(token)
    return index if index < length else None


def evaluate(document, segments: list[str]) -> tuple[bool, object]:
    """按路径段求值，返回 ``(是否存在, 值)``。

    实现：数组走规范下标，对象走键存在性；中途遇到非容器即视为不存在，
    不抛异常——调用方（断言、敏感值判定）都不希望指针错误变成 500。
    """
    current = document
    for token in segments:
        if isinstance(current, list):
            index = _index(token, len(current))
            if index is None:
                return False, None
            current = current[index]
        elif isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        else:
            return False, None
    return True, current


def resolve(document, pointer: str) -> tuple[bool, object]:
    """``parse`` + ``evaluate`` 的组合入口。"""
    return evaluate(document, parse(pointer))


def is_sensitive(pointer) -> bool:
    """判断指针路径上任一段是否为敏感字段（保护其后代属性）。

    实现：复用 ``is_sensitive_key`` 做段名判定——它与 ``redact_headers`` 用的是
    同一套规则（精确匹配 + 词尾匹配），因此 ``/data/x-auth-token`` 会被视为敏感，
    不会出现「头被脱敏、断言证据却泄露同名凭据」的不一致。
    任一段命中即整体敏感，``/data/token`` 与 ``/data/token/value`` 同等对待。
    """
    from api_agent.infrastructure.logging import is_sensitive_key

    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    return any(is_sensitive_key(segment) for segment in parse(pointer))
