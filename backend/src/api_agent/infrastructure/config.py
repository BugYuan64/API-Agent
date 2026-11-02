"""Load local configuration without opening databases or making network calls."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class ConfigurationError(ValueError):
    """A configuration error safe to show in startup output."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    project_root: Path = PROJECT_ROOT
    app_name: str = Field(default="API-Agent", min_length=1, max_length=80)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: str = "sqlite:///data/api_agent.db"
    artifact_dir: Path = Path("data/artifacts")
    llm_mode: Literal["disabled", "mock"] = "disabled"
    llm_api_key: SecretStr | None = None
    target_allowed_origins: list[str] = Field(default_factory=list)
    target_auth_env_vars: list[str] = Field(default_factory=list)
    postman_cli_path: Path | None = None
    omit_response_payload: bool = False
    stream_max_sessions: int = Field(default=4, ge=1, le=16)
    stream_max_bytes: int = Field(default=8_388_608, ge=1024, le=268_435_456)
    frontend_dist: Path | None = Path("frontend/dist")

    @field_validator("postman_cli_path", mode="before")
    @classmethod
    def empty_cli_path(cls, value):
        """把空字符串的 POSTMAN_CLI_PATH 归一成 None。

        实现：mode="before" 表示在类型转换（str → Path）之前执行；
        因为 ``Path("")`` 会得到 ``Path(".")``，不拦截的话空值会被误当成“当前目录下的可执行文件”。
        """
        return None if value == "" else value

    @field_validator("target_auth_env_vars")
    @classmethod
    def explicit_auth_variables(cls, values):
        """校验目标鉴权变量名列表，并去除重复项（保持首次出现顺序）。

        实现：逐项用 ``[A-Za-z_][A-Za-z0-9_]*`` 全匹配，确保这里写的是“环境变量名”
        而不是已经泄露的凭据值；随后用 ``dict.fromkeys`` 去重（dict 保序 + 键唯一）。
        """
        import re

        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in values):
            raise ValueError("must contain environment variable names")
        return list(dict.fromkeys(values))

    @field_validator("llm_api_key", mode="before")
    @classmethod
    def empty_key_is_absent(cls, value):
        """空字符串的模型密钥视为“未配置”。

        实现：同样在 before 阶段拦截，避免 ``SecretStr("")`` 被当成已配置的密钥，
        导致日志脱敏和降级判定都得出错误结论。
        """
        return None if value == "" else value

    @field_validator("artifact_dir", mode="before")
    @classmethod
    def nonempty_artifact_path(cls, value):
        """拒绝空白字符串的产物目录。

        实现：非空但全是空格的字符串在 ``Path`` 解析时不会报错，因此在 before 阶段
        显式检查 ``strip()``，抛出的 ValueError 会被 load_settings 包装成可读错误。
        """
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be a nonempty directory path")
        return value

    @field_validator("frontend_dist", mode="before")
    @classmethod
    def empty_frontend_dist(cls, value):
        """把空的 FRONTEND_DIST 归一成 None，表示“不托管前端构建产物”。

        实现：与 ``postman_cli_path`` 同理，``Path("")`` 会变成当前目录，
        不拦截就会把项目根当成静态站点去扫描。
        """
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def local_sqlite_file(cls, value: str) -> str:
        """限制数据库只能是“本地 SQLite 文件”。

        实现：只接受 ``sqlite:///`` 前缀，且路径部分非空、不是 ``:memory:``、
        不含 ``?``/``#``（即不允许 URL 参数和片段）。这样既避免连到远程库，
        也避免内存库导致进程重启后数据全丢。
        """
        prefix = "sqlite:///"
        if not value.startswith(prefix):
            raise ValueError("must use a local sqlite:/// file URL")
        path = value[len(prefix) :]
        if not path.strip() or path == ":memory:" or "?" in path or "#" in path:
            raise ValueError("must name a persistent SQLite file without URL options")
        return value

    @field_validator("target_allowed_origins")
    @classmethod
    def http_origins_only(cls, values: list[str]) -> list[str]:
        """把允许访问的目标 origin 列表校验为“纯 HTTP(S) 源”。

        实现：对每个值用 ``urlsplit`` 拆解后逐项断言——协议必须是 http/https，
        必须有主机名，不能带用户名/密码、路径（除 ``/``）、查询串、片段、
        控制字符或空白；端口为 0 视为非法。最后统一去掉尾部的 ``/``，
        让后续比较（如 ``validate_scope`` 的集合匹配）能直接做字符串相等。
        """
        origins = []
        for value in values:
            try:
                parsed = urlsplit(value)
                hostname = parsed.hostname
                port = parsed.port
            except ValueError:
                raise ValueError("must contain valid HTTP(S) origins and ports") from None
            if (
                parsed.scheme not in {"http", "https"}
                or not hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or any(char.isspace() or ord(char) < 32 for char in value)
            ):
                raise ValueError("must contain HTTP(S) origins without credentials or paths")
            if port == 0:
                raise ValueError("origin port must be between 1 and 65535")
            origins.append(value.rstrip("/"))
        return origins

    @model_validator(mode="after")
    def resolve_paths(self):
        """把相对路径配置统一解析成绝对路径。

        实现：模型校验通过后执行（``mode="after"``，此时字段类型已是 Path），
        用 ``project_root.resolve()`` 先定基准，再让 artifact_dir 和 postman_cli_path
        相对它解析。这样进程在任意工作目录启动，路径都指向同一处。
        """
        self.project_root = self.project_root.resolve()
        self.artifact_dir = self.resolve_path(self.artifact_dir)
        if self.postman_cli_path is not None:
            self.postman_cli_path = self.resolve_path(self.postman_cli_path)
        if self.frontend_dist:
            self.frontend_dist = self.resolve_path(self.frontend_dist)
        return self

    @property
    def frontend_root(self) -> Path | None:
        """返回可直接托管的前端构建目录，未构建时返回 None。

        作用：把“路径存在但还没跑过 npm build”与“配置为不托管前端”区分开。
        实现：要求该目录下存在 ``index.html``——只有真正构建过才具备入口页，
        否则 ``StaticFiles`` 会以 404/500 的形式暴露一个空目录。
        """
        if self.frontend_dist is None:
            return None
        return self.frontend_dist if (self.frontend_dist / "index.html").is_file() else None

    def resolve_path(self, path: Path) -> Path:
        """把可能是相对路径的 Path 解析为基于 project_root 的绝对路径。

        实现：直接拼接 ``project_root / path`` 后 ``resolve()``；
        若 ``path`` 本身是绝对路径，``/`` 运算符会丢弃前缀，行为符合直觉。
        """
        return (self.project_root / path).resolve()

    @property
    def database_path(self) -> Path:
        """返回 SQLite 文件的绝对路径。

        实现：``removeprefix`` 去掉 ``sqlite:///`` 前缀得到相对路径，
        再交给 ``resolve_path`` 拼成绝对路径。供 Alembic 建库和父目录创建使用。
        """
        return self.resolve_path(Path(self.database_url.removeprefix("sqlite:///")))

    @property
    def async_database_url(self) -> str:
        """返回 SQLAlchemy 异步驱动（aiosqlite）使用的连接串。

        实现：先取绝对路径的 ``as_uri()``（得到 ``file:///...``，Windows 盘符也会被正确编码），
        再用字符串替换把协议换成 ``sqlite+aiosqlite:///``；只在第一次出现处替换，
        避免路径中的 ``file:///`` 字样被误改。
        """
        return self.database_path.as_uri().replace("file:///", "sqlite+aiosqlite:///", 1)

    @property
    def sync_database_url(self) -> str:
        """返回 Alembic 迁移使用的同步连接串（标准 sqlite 驱动）。

        实现：与异步版本同一套 ``as_uri()`` + 替换策略，只是协议换成 ``sqlite:///``，
        保证迁移和运行时指向同一个物理文件。
        """
        return self.database_path.as_uri().replace("file:///", "sqlite:///", 1)


def load_settings(*, env_file: Path | str | None = PROJECT_ROOT / ".env", **overrides) -> Settings:
    """按“显式覆盖 > 进程环境变量 > 根目录 .env > 默认值”的优先级装载配置。

    实现要点：
    1. 把 ``_env_file`` 和 ``_env_file_encoding`` 传给 pydantic-settings，
       由它在上述优先级下合并来源；
    2. 捕获 ``ValidationError`` 后只用 ``loc``/``msg`` 重组报错文本，
       并显式传 ``include_input=False``，防止启动回溯把含密钥的原始配置值打印出来；
    3. ``SettingsError`` 单独处理成“TARGET_ALLOWED_ORIGINS 必须是 JSON 数组”的提示，
       ``OSError``/``ValueError`` 则归为 .env 或路径类问题；
    4. 所有分支都 ``raise ... from None``，抹掉底层异常链，避免细节外泄。
    """
    try:
        return Settings(_env_file=env_file, _env_file_encoding="utf-8", **overrides)
    except ValidationError as error:
        problems = []
        for item in error.errors(include_input=False, include_context=False, include_url=False):
            field = ".".join(str(part).upper() for part in item["loc"])
            problems.append(f"{field}: {item['msg']}")
        raise ConfigurationError("Invalid configuration: " + "; ".join(problems)) from None
    except SettingsError:
        raise ConfigurationError(
            "Invalid configuration: TARGET_ALLOWED_ORIGINS must be a JSON array of HTTP(S) origins"
        ) from None
    except (OSError, ValueError):
        raise ConfigurationError(
            "Invalid configuration: check the .env file and PROJECT_ROOT/ARTIFACT_DIR paths"
        ) from None
