"""Run states and persisted transition rules."""

from api_agent.infrastructure.api import conflict

QUEUED = "queued"
RUNNING = "running"
CANCELLING = "cancelling"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
TERMINAL = {COMPLETED, FAILED, CANCELLED, INTERRUPTED}
# 状态迁移表：键是当前状态，值是允许进入的目标状态集合。
# 只有 queued 能进 running（由 worker 原子抢占），running 才能进取消中/终态，
# 终态（TERMINAL）不在键中出现，因此没有任何出边——迁移天然不可逆。
TRANSITIONS = {
    QUEUED: {RUNNING, CANCELLED},
    RUNNING: {CANCELLING, COMPLETED, FAILED},
    CANCELLING: {CANCELLED, FAILED},
}


def sources_for(target: str) -> set[str]:
    """反查：哪些状态允许迁移到 ``target``。

    作用：更新语句需要写成 ``WHERE status IN (...)`` 才能保持原子性，
    而迁移表是按“当前 → 目标”组织的，所以这里做一次反向索引。
    实现：遍历 ``TRANSITIONS``，把所有目标集合包含 ``target`` 的当前状态收进集合；
    返回空集表示 ``target`` 不是合法目标（调用方据此拒绝操作）。
    """
    return {current for current, targets in TRANSITIONS.items() if target in targets}


def check_transition(current: str, target: str) -> None:
    """校验一次状态迁移是否被允许，非法时抛出 409 冲突。

    作用：在内存中做前置判断，给出可读的报错；
    不能替代数据库层条件更新——真正的并发安全仍靠 ``WHERE status = ...``。
    实现：查表判断 ``target`` 是否在当前状态的目标集合里，不在就抛 ``invalid_run_transition``。
    """
    if target not in TRANSITIONS.get(current, set()):
        raise conflict("invalid_run_transition", f"Cannot change run from {current} to {target}")
