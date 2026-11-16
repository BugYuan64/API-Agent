"""Validated management contracts. Secret values are never accepted or returned."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from api_agent.infrastructure.api import ApiInput, ApiModel, UtcDatetime

HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
PATH_TEMPLATE = re.compile(
    r"^/(?:[A-Za-z0-9._~-]+|\{[A-Za-z_][A-Za-z0-9_]*\})"
    r"(?:/(?:[A-Za-z0-9._~-]+|\{[A-Za-z_][A-Za-z0-9_]*\}))*/?$"
)
ENVIRONMENT_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_POINTER = re.compile(r"^/(?:[^/~]|~[01])+(?:/(?:[^/~]|~[01])*)*$")


class ProjectCreate(ApiInput):
    """创建项目的名称与说明；限制长度，沿用管理输入的未知字段拒绝规则。"""

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class ProjectResponse(ApiModel):
    """项目公开元信息，不包含环境凭据或运行证据。"""

    id: str
    name: str
    description: str
    created_at: UtcDatetime


class AuthReference(ApiInput):
    """指向凭据的“引用”，只存变量名，绝不存凭据值。

    作用：让环境配置能声明“用哪个环境变量做鉴权”，而不把 token 写进数据库或响应。
    实现：``kind`` 决定注入方式（bearer / api_key / header），``env_var`` 是变量名，
    ``header_name`` 是自定义头名；三者由下面三个校验函数约束成合法组合。
    """

    kind: Literal["bearer", "api_key", "header"]
    env_var: str
    header_name: str | None = None

    @field_validator("env_var")
    @classmethod
    def validate_env_var(cls, value: str) -> str:
        """校验 ``env_var`` 只能是环境变量名。

        实现：用 ``[A-Za-z_][A-Za-z0-9_]*`` 全匹配；
        这样可以挡住把 ``sk-xxx`` 这种真实密钥误填进来的情况（含连字符、长度过长都会失败）。
        """
        if not ENVIRONMENT_VARIABLE.fullmatch(value):
            raise ValueError("must name an environment variable, not contain a credential value")
        return value

    @field_validator("header_name")
    @classmethod
    def validate_header(cls, value: str | None) -> str | None:
        """校验自定义头名符合 RFC 7230 的 token 字符集。

        实现：用 ``[!#$%&'*+.^_`|~0-9A-Za-z-]+`` 全匹配；
        None 直接放过（bearer 场景不需要头名）。
        """
        if value is not None and not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
            raise ValueError("must be a valid HTTP header name")
        return value

    @model_validator(mode="after")
    def validate_kind(self):
        """校验 ``kind`` 与 ``header_name`` 的组合是否自洽。

        实现：在字段级校验之后统一检查三条规则——
        bearer 固定用 Authorization，传 header_name 视为矛盾；
        api_key/header 必须有 header_name；
        任何情况下都不允许覆盖 ``Host``（否则会改变请求目标）。
        """
        if self.kind == "bearer" and self.header_name is not None:
            raise ValueError("bearer authentication uses Authorization and takes no header_name")
        if self.kind != "bearer" and self.header_name is None:
            raise ValueError("api_key and header authentication require header_name")
        if self.header_name and self.header_name.lower() == "host":
            raise ValueError("host cannot be overridden by target authentication")
        return self


class ReplayPolicy(ApiInput):
    """失败后是否允许重放请求的策略开关。

    作用：真实请求可能产生副作用（如创建订单），默认应当禁止重放，
    需要显式开启才允许；非幂等方法还要额外再加一层开关（见 ``validate_replay``）。
    实现：两个布尔开关，默认都为 False，即最保守的配置。
    """

    allow_replay: bool = False
    allow_non_idempotent: bool = False


class EnvironmentInput(ApiInput):
    """环境配置的公共字段与校验（创建和更新都复用它）。

    作用：把“这个环境允许打哪些 origin / 方法 / 路径，用哪个凭据引用”这套
    白名单规则集中在一处，创建与更新走同一套约束。
    实现：字段级校验函数各自负责 origin 形态、方法集合、路径模板、敏感字段指针；
    模型级校验负责跨字段的依赖关系（如 allow_non_idempotent 依赖 allow_replay）。
    """

    origin: str
    allowed_methods: list[str] = Field(min_length=1, max_length=7)
    path_templates: list[str] = Field(min_length=1, max_length=100)
    auth_ref: AuthReference | None = None
    replay_policy: ReplayPolicy = Field(default_factory=ReplayPolicy)
    sensitive_field_rules: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        """校验 origin 是纯 HTTP(S) 源，并去掉末尾斜杠。

        实现：``urlsplit`` 解析后逐项排除——非 http/https、无主机名、
        含账号密码、带路径/查询串/片段、含空白或控制字符、端口为 0。
        最后 ``rstrip("/")`` 统一形态，避免 ``http://x`` 与 ``http://x/`` 被当成两个源。
        """
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("must contain a valid HTTP(S) origin and port") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
            or port == 0
        ):
            raise ValueError(
                "must be an HTTP(S) origin without credentials, path, query or fragment"
            )
        return value.rstrip("/")

    @field_validator("allowed_methods")
    @classmethod
    def validate_methods(cls, values: list[str]) -> list[str]:
        """把 HTTP 方法统一大写、去重校验并排序。

        实现：先 ``upper()`` 归一化（避免 get/GET 被当作两种），
        再检查全部属于 ``HTTP_METHODS`` 且无重复，最后排序——
        排序让同一份配置无论书写顺序如何都得到相同输出，便于比较与断言。
        """
        methods = [value.upper() for value in values]
        if any(value not in HTTP_METHODS for value in methods) or len(set(methods)) != len(methods):
            raise ValueError("must contain unique supported HTTP methods")
        return sorted(methods)

    @field_validator("path_templates")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        """校验路径模板：必须绝对、不含查询串和通配符、且不重复。

        实现：去重检查后，逐个用 ``PATH_TEMPLATE`` 全匹配；
        该正则只允许 ``/段/段`` 形式，段内可以是字面量或 ``{变量名}``，
        天然排除 ``*``、``?``、``..`` 等可能扩大访问面的写法；长度上限 500 防滥用。
        """
        if len(set(values)) != len(values):
            raise ValueError("must not contain duplicate path templates")
        for value in values:
            if not PATH_TEMPLATE.fullmatch(value) or len(value) > 500:
                raise ValueError("must be absolute path templates without query or wildcards")
        return values

    @field_validator("sensitive_field_rules")
    @classmethod
    def validate_sensitive_rules(cls, values: list[str]) -> list[str]:
        """校验敏感字段规则是合法的 JSON Pointer。

        实现：先去重，再用 ``SENSITIVE_POINTER`` 全匹配——
        只允许 ``/`` 分隔的段，段内 ``~`` 后只能跟 ``0``/``1``（JSON Pointer 的转义规则），
        这样规则能被严格地作用在具体字段上，不会退化成模糊匹配。
        """
        if len(set(values)) != len(values):
            raise ValueError("must not contain duplicate field rules")
        for value in values:
            if not SENSITIVE_POINTER.fullmatch(value) or len(value) > 500:
                raise ValueError("must be JSON Pointer paths to sensitive response/request fields")
        return values

    @model_validator(mode="after")
    def validate_replay(self):
        """保证“允许重放非幂等方法”不会脱离“允许重放”单独开启。

        实现：跨字段依赖检查，``allow_non_idempotent`` 必须伴随 ``allow_replay=True``；
        否则等于放开了最危险的一档权限却没有任何前置开关。
        """
        if self.replay_policy.allow_non_idempotent and not self.replay_policy.allow_replay:
            raise ValueError("allow_non_idempotent requires allow_replay")
        return self


class EnvironmentCreate(EnvironmentInput):
    """创建命名环境及第一版配置；目标、鉴权引用与重放边界沿用父类校验。"""

    name: str = Field(min_length=1, max_length=80)


class EnvironmentUpdate(EnvironmentInput):
    """追加配置版本的输入；expected_version 用于乐观锁，拒绝并发覆盖。"""

    expected_version: int = Field(ge=1)


class EnvironmentVersionResponse(ApiModel):
    """不可变环境版本快照，仅公开凭据引用而非 token。"""

    id: str
    environment_id: str
    version: int
    origin: str
    allowed_methods: list[str]
    path_templates: list[str]
    auth_ref: AuthReference | None
    replay_policy: ReplayPolicy
    sensitive_field_rules: list[str]
    created_at: UtcDatetime


class EnvironmentResponse(ApiModel):
    """环境身份、当前版本号及对应配置，用于编辑和版本冲突检查。"""

    id: str
    project_id: str
    name: str
    current_version: int
    created_at: UtcDatetime
    config: EnvironmentVersionResponse


class EnvironmentHistoryResponse(ApiModel):
    """按版本顺序返回只读历史，不代表历史目标请求可直接重放。"""

    id: str
    project_id: str
    name: str
    current_version: int
    created_at: UtcDatetime
    versions: list[EnvironmentVersionResponse]
