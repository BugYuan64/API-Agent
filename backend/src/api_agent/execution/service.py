"""Local policy is independent of plan input; secrets are resolved only here."""

import json
import os
from typing import Any

from pydantic import ValidationError

from api_agent.execution.postman import PostmanRunner
from api_agent.execution.schemas import ExecutionPlan, validate_scope
from api_agent.infrastructure.config import Settings


class PlanError(ValueError):
    """执行计划被拒绝的统一异常，消息即稳定错误码。

    作用：把“解析失败”和“范围校验失败”都收敛成一种类型，
    调用方只需捕获它并把 ``str(error)`` 当作 ``error_kind`` 返回。
    实现：直接继承 ValueError，不自带额外字段。
    """


class ExecutionService:
    """执行门面：解析/校验计划、解析凭据、调用 CLI 运行器。

    作用：把本地策略（允许的 origin、允许的凭据变量）与外部输入彻底分开——
    计划只是“申请”，能不能执行由本类依据配置裁定。
    实现：优先使用配置里的 CLI 路径，缺省回落到项目缓存目录；
    临时工作区固定在 ``.cache/execution`` 下，便于整体清理。
    """

    def __init__(self, settings: Settings):
        """按配置定位 CLI 可执行文件并建立运行器。

        实现：若配置显式给了 ``postman_cli_path`` 就用它，
        否则回落到项目内的 ``.cache/tools/postman/postman.exe``——
        这条默认路径让“把 CLI 放到缓存目录”成为免配置的部署方式；
        临时工作区固定在 ``.cache/execution``，便于整体清理且不污染源码目录；
        ``omit_response_payload`` 透传给运行器，决定结果里是否带响应头与响应体。
        """
        self.settings = settings
        executable = settings.postman_cli_path or (
            settings.project_root / ".cache/tools/postman/postman.exe"
        )
        self.runner = PostmanRunner(
            executable,
            settings.project_root / ".cache/execution",
            omit_response_payload=settings.omit_response_payload,
        )

    def parse(self, payload: Any) -> ExecutionPlan:
        """把任意输入解析成受控的 ExecutionPlan，或抛出 PlanError。

        作用：这是不可信输入的收口点，所有结构限制与本地范围限制都在此生效。
        实现：三道闸门——
        ① 必须是 dict 且序列化后不超过 256KiB（先卡大小，避免解析大对象耗资源）；
        ② pydantic 严格校验（``strict=True`` 不允许隐式类型转换），
           任何 ValidationError/TypeError/ValueError/RecursionError 统一转 ``invalid_plan``；
        ③ ``validate_scope`` 用本地配置裁定 origin 与凭据变量名是否被授权，
           失败直接把错误码透出（如 ``origin_not_allowed``）便于定位。
        """
        try:
            if not isinstance(payload, dict) or len(json.dumps(payload).encode()) > 262_144:
                raise PlanError("invalid_plan")
            plan = ExecutionPlan.model_validate(payload)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise PlanError("invalid_plan") from None
        try:
            validate_scope(
                plan, self.settings.target_allowed_origins, self.settings.target_auth_env_vars
            )
        except ValueError as error:
            raise PlanError(str(error)) from None
        return plan

    def validate(self, payload: Any) -> dict:
        """只校验不执行：返回是否合法、请求数量和格式。

        作用：给 ``validate`` 命令和 MCP 的 ``validate_plan`` 工具用，
        让使用者能在真正发请求前先确认计划会被接受。
        实现：捕获 PlanError 转成 ``{"valid": False, "error_kind": ...}``；
        成功则回传请求条数与引擎格式，绝不触发任何网络访问。
        """
        try:
            plan = self.parse(payload)
        except PlanError as error:
            return {"valid": False, "error_kind": str(error)}
        return {"valid": True, "requests": len(plan.requests), "format": "v2.1-newman"}

    async def run(self, payload: Any) -> dict:
        """解析计划、解析凭据后真正执行一次。

        实现：先 ``parse``（失败返回 error 结构，不抛异常）；
        若计划引用了凭据，则从进程环境读取对应变量，并做四项校验——
        非空、长度≤4096、全部是可打印 ASCII（32..126，杜绝换行导致请求头注入）、
        不含 ``{{``（避免被 Postman 当成变量展开）。
        任一不满足即返回 ``auth_reference_unavailable``，绝不退化为无凭据执行。
        凭据只在本方法内以 dict 形式传给运行器，不写入数据库、不进入日志。
        """
        try:
            plan = self.parse(payload)
        except PlanError as error:
            return {"status": "error", "error_kind": str(error)}
        secrets = {}
        if plan.auth_ref:
            value = os.environ.get(plan.auth_ref.env_var, "")
            if (
                not value
                or len(value) > 4096
                or any(ord(c) < 32 or ord(c) > 126 for c in value)
                or "{{" in value
            ):
                return {"status": "error", "error_kind": "auth_reference_unavailable"}
            secrets[plan.auth_ref.env_var] = value
        return (await self.runner.run(plan, secrets)).model_dump(mode="json")
