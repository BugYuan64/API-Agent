"""Local policy is independent of plan input; secrets are resolved only here."""

import json
import os
from typing import Any, Literal

from pydantic import ValidationError

from api_agent.execution.engine import TestEngine
from api_agent.execution.native import NativeHttpEngine
from api_agent.execution.postman import PostmanRunner
from api_agent.execution.schemas import ExecutionPlan, validate_scope
from api_agent.infrastructure.config import Settings

EngineName = Literal["native", "postman"]
DEFAULT_ENGINE: EngineName = "native"


class PlanError(ValueError):
    """执行计划被拒绝的统一异常，消息即稳定错误码。

    作用：把“解析失败”和“范围校验失败”都收敛成一种类型，
    调用方只需捕获它并把 ``str(error)`` 当作 ``error_kind`` 返回。
    实现：直接继承 ValueError，不自带额外字段。
    """


class ExecutionService:
    """执行门面：解析/校验计划、解析凭据、调度执行驱动。

    作用：把本地策略（允许的 origin、允许的凭据变量）与外部输入彻底分开——
    计划只是“申请”，能不能执行由本类依据配置裁定。
    实现：遵循驱动适配器模式（Driver / Adapter Pattern），
    把测试内容（ExecutionPlan）与执行技术（原生 HTTPX 引擎 / Postman CLI 沙箱）
    解耦：``engines`` 注册表按名称分发，默认使用零依赖的原生引擎，
    需要工业级判定与沙箱隔离时显式切到 ``postman``。
    """

    def __init__(self, settings: Settings):
        """装配执行驱动注册表。

        实现：原生引擎始终可用（纯 Python，无外部依赖）；
        Postman CLI 驱动按配置定位可执行文件——显式 ``postman_cli_path`` 优先，
        否则回落到项目内 ``.cache/tools/postman/postman.exe``，
        临时工作区固定在 ``.cache/execution`` 下，便于整体清理且不污染源码目录。
        """
        self.settings = settings
        executable = settings.postman_cli_path or (
            settings.project_root / ".cache/tools/postman/postman.exe"
        )
        self.engines: dict[str, TestEngine] = {
            "native": NativeHttpEngine(
                omit_response_payload=settings.omit_response_payload,
                max_response_bytes=settings.response_max_bytes,
                trust_env=settings.trust_system_proxy,
            ),
            "postman": PostmanRunner(
                executable,
                settings.project_root / ".cache/execution",
                omit_response_payload=settings.omit_response_payload,
                trust_env=settings.trust_system_proxy,
            ),
        }

    def resolve_engine(self, name: str | None) -> TestEngine:
        """按名称取出执行驱动，未知名称抛 ``PlanError``（稳定错误码）。"""
        key = (name or DEFAULT_ENGINE).strip().lower()
        engine = self.engines.get(key)
        if engine is None:
            raise PlanError("unknown_engine")
        return engine

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

    def validate(self, payload: Any, engine: str | None = None) -> dict:
        """只校验不执行：返回是否合法、请求数量和驱动格式。

        作用：给 ``validate`` 命令和 MCP 的 ``validate_plan`` 工具用，
        让使用者能在真正发请求前先确认计划会被接受。
        实现：捕获 PlanError 转成 ``{"valid": False, "error_kind": ...}``；
        成功则回传请求条数与所选驱动格式，绝不触发任何网络访问。
        """
        try:
            plan = self.parse(payload)
            driver = self.resolve_engine(engine)
        except PlanError as error:
            return {"valid": False, "error_kind": str(error)}
        return {
            "valid": True,
            "requests": len(plan.requests),
            "engine": driver.name,
            "format": "native-httpx" if driver.name == "native" else "v2.1-newman",
        }

    async def run(self, payload: Any, engine: str | None = None) -> dict:
        """解析计划、解析凭据后交给选定驱动真正执行一次（原始载荷入口）。

        实现：先 ``parse``（失败返回 error 结构，不抛异常），再按 ``engine`` 取驱动。
        """
        try:
            plan = self.parse(payload)
            driver = self.resolve_engine(engine)
        except PlanError as error:
            return {"status": "error", "error_kind": str(error)}
        return await self.run_plan(plan, driver)

    async def run_plan(self, plan: ExecutionPlan, driver: TestEngine) -> dict:
        """用已解析的计划与已解析的驱动执行一次。

        作用：路由层为了区分“计划被拒”与“计划跑了但没通过”，必须先
        ``parse`` 一次再执行；若这里再走 ``run`` 就会把同一份载荷重复
        序列化、重复做 256 KiB 体积检查与全量字符串扫描。因此拆出本方法，
        让“已校验的计划”只被使用、不被重新校验。

        实现：凭据只在本方法内从进程环境读取并做四项校验——
        非空、长度≤4096、全部可打印 ASCII（32..126，杜绝换行导致请求头注入）、
        不含 ``{{``（避免被当成变量展开）。任一不满足即返回
        ``auth_reference_unavailable``，绝不退化为无凭据执行。
        凭据只以 dict 形式传给驱动，不写入数据库、不进入日志。
        """
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
        return (await driver.run(plan, secrets)).model_dump(mode="json")

    async def engine_status(self, name: str | None = None) -> dict[str, Any]:
        """查询指定（默认原生）执行驱动的就绪状态。"""
        key = (name or DEFAULT_ENGINE).strip().lower()
        try:
            driver = self.resolve_engine(name)
        except PlanError as error:
            return {"available": False, "error_kind": str(error)}
        status = await driver.status()
        status.setdefault("id", key)
        status.setdefault("engine", driver.name)
        return status
