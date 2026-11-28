"""Application assembly for the local, single-process management backend."""

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.routing import Match

from api_agent import __version__
from api_agent.agent.manager import AgentManager
from api_agent.agent.routes import router as agent_router
from api_agent.agent.store import AgentStore
from api_agent.assistant.routes import router as assistant_router
from api_agent.assistant.runner import AssistantRunManager
from api_agent.assistant.thread_store import AssistantThreadRepository
from api_agent.console.routes import router as console_router
from api_agent.console.runner import ConsoleRunManager
from api_agent.execution.routes import router as executions_router
from api_agent.infrastructure.api import error_response, install_api_handlers
from api_agent.infrastructure.config import Settings, load_settings
from api_agent.infrastructure.database import Database
from api_agent.infrastructure.history import sanitize_history
from api_agent.infrastructure.logging import configure_logging
from api_agent.knowledge.routes import router as knowledge_router
from api_agent.knowledge.service import KnowledgeBaseService
from api_agent.llm.routes import router as llm_router
from api_agent.llm.testing_manager import ModelTestManager
from api_agent.projects.document_routes import router as documents_router
from api_agent.projects.document_store import DocumentStore
from api_agent.projects.documents import DocumentManager
from api_agent.projects.routes import router as projects_router
from api_agent.streams.routes import router as streams_router
from api_agent.streams.runner import StreamSuiteRunner
from api_agent.streams.service import StreamManager
from api_agent.tasks.routes import router as tasks_router
from api_agent.tasks.service import TaskManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


class _SpaRoute(APIRoute):
    """SPA history 回落路由：拒绝匹配 `/api` 与 `/api/*`，避免未知非 GET 接口被转为 405。"""

    def matches(self, scope):
        path = scope.get("path", "")
        if path == "/api" or path.startswith("/api/"):
            return Match.NONE, {}
        return super().matches(scope)


class HealthResponse(BaseModel):
    """公开探活结果：给出服务身份、版本和模型模式，不回显凭据或本地配置。"""

    status: str
    service: str
    version: str
    llm_mode: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """组装 FastAPI 应用（本地单进程管理后端）。

    作用：这是应用的唯一组装点，把配置、日志、错误处理、路由、数据库迁移
    和后台 worker 串在一起；参数可注入 settings 以便测试隔离。
    实现：
    ① 未显式传 settings 时按标准优先级装载；随后配置应用专属 logger；
    ② 建立 FastAPI 实例，并把 settings 挂到 ``app.state`` 供依赖读取；
    ③ 安装统一错误处理（含 request_id 中间件）；
    ④ 用 ``lifespan`` 管理启动/关闭资源：迁移、恢复流会话与中断的编排运行，
       用退出栈保证启动失败或关闭异常时也释放所有已取得资源；
    ⑤ 注册各领域路由，最后声明 ``/health``；
    ⑥ 用本地静态资源提供 Swagger UI（``/docs``），不依赖外网 CDN。
    """
    settings = settings if settings is not None else load_settings()
    logger = configure_logging(settings)
    app = FastAPI(title=settings.app_name, version=__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings
    install_api_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui():
        """渲染离线版 Swagger UI 页面。

        作用：本地部署的应用不该依赖境外 CDN——断网或 CDN 不可达时默认页面会白屏，
        因此 js/css/favicon 全部由 ``/static`` 同源提供。
        实现：``get_swagger_ui_html`` 只负责生成页面骨架，
        三个资源 URL 都改成本地路径；``openapi_url`` 沿用 FastAPI 的默认 ``/openapi.json``。
        """
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
            swagger_favicon_url="/static/favicon-32x32.png",
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Register ownership as resources are acquired; unwind every acquired resource.

        Exit callbacks run in reverse order, even if startup or another callback
        fails. All background work therefore stops before the database is disposed.
        """
        async with AsyncExitStack() as resources:
            database = Database(settings)
            resources.push_async_callback(database.dispose)
            await database.migrate_async()
            await sanitize_history(database)
            app.state.database = database

            assistant_threads = AssistantThreadRepository(database)
            app.state.assistant_threads = assistant_threads

            streams = StreamManager(database, settings)
            app.state.streams = streams
            resources.push_async_callback(streams.shutdown)
            await streams.recover()

            agents = AgentManager(AgentStore(settings.artifact_dir / "agent"))
            app.state.agents = agents
            resources.push_async_callback(agents.shutdown)
            agents.store.recover()
            tasks = TaskManager(settings.artifact_dir / "tasks", settings, streams)
            app.state.tasks = tasks
            resources.push_async_callback(tasks.shutdown)
            tasks.recover()
            documents = DocumentManager(DocumentStore(settings.artifact_dir / "projects"))
            app.state.documents = documents
            resources.push_async_callback(documents.shutdown)
            documents.store.recover()
            knowledge = KnowledgeBaseService(settings)
            app.state.knowledge = knowledge
            resources.callback(knowledge.close)

            console_runs = ConsoleRunManager(database, settings)
            app.state.console_runs = console_runs
            resources.push_async_callback(console_runs.shutdown)
            await console_runs.recover()

            stream_suites = StreamSuiteRunner(streams)
            app.state.stream_suites = stream_suites
            resources.push_async_callback(stream_suites.shutdown)
            await stream_suites.recover()

            model_tests = ModelTestManager(database, settings)
            app.state.model_tests = model_tests
            resources.push_async_callback(model_tests.shutdown)
            await model_tests.recover()

            assistant_runs = AssistantRunManager(
                database=database,
                settings=settings,
                threads=assistant_threads,
                console_runs=console_runs,
                stream_suites=stream_suites,
                tasks=tasks,
                model_tests=model_tests,
                knowledge=knowledge,
            )
            app.state.assistant_runs = assistant_runs
            resources.push_async_callback(assistant_runs.shutdown)
            yield

    app.router.lifespan_context = lifespan
    app.include_router(projects_router)
    app.include_router(documents_router)
    app.include_router(console_router)
    app.include_router(streams_router)
    app.include_router(executions_router)
    app.include_router(llm_router)
    app.include_router(knowledge_router)
    app.include_router(agent_router)
    app.include_router(tasks_router)
    app.include_router(assistant_router)

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        """健康检查：返回服务名、版本和模型模式。

        实现：只读 ``app.state.settings`` 和模块版本号，不触碰数据库——
        这样即使数据库暂时不可用，进程也能被探活为“已启动”，
        便于区分“服务没起来”和“依赖有问题”。
        """
        return HealthResponse(
            status="ok",
            service=settings.app_name,
            version=__version__,
            llm_mode=settings.llm_mode,
        )

    frontend = settings.frontend_root
    if frontend is not None:
        # 前端构建产物按同源挂载：`/assets/*` 由 StaticFiles 直出，
        # 其余非 API 路径回落到 index.html，交给 vue-router 处理（history 模式）。
        assets = frontend / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        async def spa(path: str, request: Request):
            """为前端 history 路由提供入口页回落。

            实现：路由匹配层（``_SpaRoute``）直接排除 ``/api`` 与 ``/api/*``，
            使未知的 GET/POST/PUT/DELETE 管理接口统一返回 404 JSON，不再因仅注册 GET
            兜底路由而被 Starlette 误报 405；同时已注册的 API 路由仍保留准确 405。
            其余路径先尝试命中构建目录里的真实文件（favicon、robots.txt），
            命中就直出，否则返回 index.html。
            """
            if path == "api" or path.startswith("api/"):
                return error_response(request, 404, "http_error", "Resource was not found")
            candidate = (frontend / path).resolve()
            if path and candidate.is_file() and frontend in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(frontend / "index.html")

        app.router.add_api_route(
            "/{path:path}",
            spa,
            methods=["GET"],
            include_in_schema=False,
            route_class_override=_SpaRoute,
        )

    logger.info("Application configured", extra={"context": {"llm_mode": settings.llm_mode}})
    return app
