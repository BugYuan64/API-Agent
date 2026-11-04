"""Stable, redacted management API responses and exception handling."""

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from api_agent.infrastructure.api_utils import as_utc

UtcDatetime = Annotated[datetime, BeforeValidator(as_utc)]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, hide_input_in_errors=True)
    schema_version: str = "1"


class ApiInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, str_strip_whitespace=True)


class ErrorDetail(ApiModel):
    model_config = ConfigDict(extra="ignore")
    field: str
    reason: str


class ErrorBody(ApiModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str


class ErrorResponse(ApiModel):
    error: ErrorBody


class PageInfo(ApiModel):
    limit: int
    offset: int
    total: int


class Page[ResponseItem](ApiModel):
    items: list[ResponseItem]
    page: PageInfo


class ApiError(Exception):
    """携带 HTTP 状态码与稳定错误码的业务异常。

    作用：让业务层（service/repository）不必依赖 FastAPI 就能表达“404 未找到”“409 冲突”，
    由统一异常处理器转换成对外 JSON。
    实现：把 ``status_code``/``code``/``message``/``details`` 存成实例属性，
    同时把 message 传给 ``super().__init__`` 以便日志里能直接看到原因。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: list[dict] | None = None,
    ):
        """保存状态码、稳定错误码、可读消息与字段级细节。

        实现：四项参数分别对应 HTTP 层状态、调用方分支判断用的 code、
        展示给使用者的 message，以及可选的 ``details``（字段名 + 原因列表）；
        ``details or []`` 保证该属性永远是列表，读取方无需判空。
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []


def not_found(resource: str) -> ApiError:
    """构造 404 “资源不存在”异常。

    实现：固定状态码 404、错误码 ``not_found``，仅把资源名（如 ``Project``）拼进提示，
    不暴露内部 ID 或数据库细节。
    """
    return ApiError(404, "not_found", f"{resource} was not found")


# 分页参数类型：所有列表接口共用同一组边界，避免各自声明的上下限漂移。
LimitParam = Annotated[int, Query(ge=1, le=200)]
OffsetParam = Annotated[int, Query(ge=0)]


def paginate(model, rows, limit: int, offset: int, total: int) -> Page:
    """把已取回的行组装成统一分页响应。

    作用：六个列表接口此前各自手写 ``Page[X](items=..., page=PageInfo(...))``，
    分页元信息的组装方式一旦改口径就要改六处。这里收敛为一条语句。
    实现：``model`` 是响应模型类；``from_attributes`` 已在 ``ApiModel`` 开启，
    因此 ORM 行与已构造的 Pydantic 对象都能直接校验通过。
    """
    return Page[model](
        items=[item if isinstance(item, model) else model.model_validate(item) for item in rows],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


def conflict(code: str, message: str) -> ApiError:
    """构造 409 冲突异常（重名、版本号并发冲突等）。

    实现：错误码由调用方指定（如 ``environment_version_conflict``），
    便于调用端按 code 分支处理，而不是去解析自然语言消息。
    """
    return ApiError(409, code, message)


def error_response(request: Request, status: int, code: str, message: str, details=None):
    """把所有异常统一渲染成 ``{"error": {...}}`` 结构。

    实现：从中间件写入的 ``request.state.request_id`` 取请求 ID（取不到就临时生成），
    组装 ErrorResponse 后 ``model_dump(mode="json")``（把 datetime 等转成 JSON 友好类型），
    最后包成指定状态码的 JSONResponse。
    """
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            request_id=request_id,
        )
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def install_api_handlers(app: FastAPI) -> None:
    """在应用上安装请求 ID 中间件与四类异常处理器。

    作用：让所有错误响应结构一致、可追踪（request_id），且不把内部堆栈返回给调用方。
    实现：用装饰器就地注册四个处理函数——
    1. 中间件为每个请求生成 uuid 写入 ``request.state``；
    2. ``ApiError`` 直接按其自带状态码输出；
    3. ``RequestValidationError`` 把 pydantic 的 loc/msg 转成 ``details`` 并返回 422，
       定位信息去掉 body/query/path 这些固定前缀，只保留字段路径；
    4. 原生 ``HTTPException`` 与兜底 ``Exception``（记 error 日志 + 返回 500 通用文案）。
    """

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """为每个请求分配唯一 ID，供错误响应和日志关联。

        实现：在调用下游路由前写入 ``request.state.request_id``，
        这样即使是路由内部抛错，处理器也能取到同一个 ID。
        """
        request.state.request_id = str(uuid.uuid4())
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError):
        """把业务 ``ApiError`` 转成统一错误 JSON。

        实现：原样沿用异常携带的状态码、错误码与 details，不做任何加工，
        保证 service 层定义的语义在 HTTP 层不失真。
        """
        return error_response(request, error.status_code, error.code, error.message, error.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        """把 FastAPI/pydantic 校验失败转成 422 结构化错误。

        实现：遍历 ``error.errors()``，把 ``loc`` 里非 body/query/path 的部分用点号拼成
        ``field``；同时用 debug 级别把完整位置和原因记入日志（便于排查，
        但对外只返回精简信息）。原始输入值不会出现在响应与日志里。
        """
        details = []
        for item in error.errors():
            location = [str(part) for part in item["loc"] if part not in {"body", "query", "path"}]
            details.append({"field": ".".join(location), "reason": item["msg"]})
        logging.getLogger("api_agent").debug(
            "Management request validation failed",
            extra={
                "context": {
                    "locations": [item["loc"] for item in error.errors()],
                    "reasons": [item["msg"] for item in error.errors()],
                }
            },
        )
        return error_response(
            request, 422, "validation_error", "Request validation failed", details
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        """兜底处理框架抛出的原生 ``HTTPException``（如 404 路由、405 方法）。

        实现：注册在 **starlette** 的 ``HTTPException`` 上而不是 FastAPI 的子类上——
        路由未匹配时抛出的是 starlette 的那个基类实例，只注册子类会让这类 404
        绕过统一处理器，返回框架默认的 ``{"detail": ...}``，前端于是拿到两种错误结构。
        用查表把常见状态码映射成固定文案，未知状态码给通用提示。
        """
        messages = {404: "Resource was not found", 405: "Method is not allowed"}
        return error_response(
            request,
            error.status_code,
            "http_error",
            messages.get(error.status_code, "Request could not be completed"),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        """最终兜底：任何未预期异常都返回 500 通用错误并记录堆栈。

        实现：``logger.error(..., exc_info=error)`` 把异常链交给格式化器（含脱敏），
        响应体只写 ``internal_error``，防止堆栈或 SQL 语句等内部信息外泄。
        """
        logging.getLogger("api_agent").error("Unhandled management API error", exc_info=error)
        return error_response(request, 500, "internal_error", "Internal service error")
