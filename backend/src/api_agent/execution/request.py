"""Request construction shared by the two execution drivers.

作用：native 引擎与 Postman Collection 编译器都要“把计划里的一条用例
变成真实请求头与 URL”。这段逻辑此前在两处几乎逐行重复——包括
Accept/Content-Type 规范化、JSON 默认 Content-Type、Bearer 注入，
任何一处改动漏掉另一处，两个引擎就会发出不同请求，判定也随之分叉。
"""

from urllib.parse import urlencode

from api_agent.execution.schemas import ExecutionPlan, RequestCase

_CANONICAL = {"accept": "Accept", "content-type": "Content-Type"}


def build_request_headers(
    plan: ExecutionPlan, case: RequestCase, secrets: dict[str, str]
) -> dict[str, str]:
    """构造一条用例的最终请求头。

    实现：先用默认 ``Accept: application/json``，再把用例显式声明的
    Accept/Content-Type 按规范大小写覆盖回去；有请求体时补 JSON Content-Type；
    计划声明了 ``auth_ref`` 时按 kind 注入 ``Bearer`` 前缀或裸值。
    头名保持规范化写法，取值来自 ``secrets``（调用方保证已校验）。
    """
    headers = {"Accept": "application/json"}
    headers.update({_CANONICAL[name.lower()]: value for name, value in case.headers.items()})
    if case.body is not None:
        headers.setdefault("Content-Type", "application/json")
    if plan.auth_ref:
        # 直接索引而非 ``.get``：缺少凭据时必须显式失败，
        # 否则会静默发出 ``Authorization: Bearer ``（空凭据）这种半残请求。
        value = secrets[plan.auth_ref.env_var]
        name = plan.auth_ref.header_name or "Authorization"
        headers[name] = f"Bearer {value}" if plan.auth_ref.kind == "bearer" else value
    return headers


def build_request_url(plan: ExecutionPlan, case: RequestCase) -> str:
    """拼出 ``origin + path + query``；查询串用 ``urlencode`` 避免手工拼接注入。"""
    query = "?" + urlencode(case.query) if case.query else ""
    return f"{plan.origin}{case.path}{query}"
