import asyncio

import httpx

from api_agent.infrastructure.config import load_settings
from api_agent.main import create_app


def build_frontend(tmp_path):
    """在临时目录里伪造一份已构建的前端产物。

    实现：只造出后端判定“可托管”所需的最小结构——
    ``index.html``、一个带内容指纹的 ``assets`` 文件和一个额外的静态文件，
    足以验证挂载、回退与安全边界，无需真的跑一次 npm build。
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return dist


def test_unbuilt_frontend_does_not_break_api(tmp_path):
    """未构建前端时，后端必须仍然只提供 API。

    实现：默认配置指向 ``frontend/dist``，而测试根目录下没有该目录，
    因此 ``frontend_root`` 应为 None，且未知路径仍是 JSON 404。
    """
    settings = load_settings(env_file=None, project_root=tmp_path)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as client:
            response = await client.get("/console")
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "http_error"

    asyncio.run(check())


def test_built_frontend_is_served_with_history_fallback(tmp_path):
    """构建后应由同源托管，并支持前端 history 路由回退。

    实现：``/`` 与深层路由都返回 index.html；
    ``/assets/*`` 走静态挂载；未知的 ``/api/*`` 仍是 JSON 404——
    这条最关键：接口路径写错时不能被 HTML 顶替，否则前端会收到一堆无法解析的响应。
    """
    dist = build_frontend(tmp_path)
    settings = load_settings(env_file=None, project_root=tmp_path, frontend_dist=dist)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://test"
        ) as client:
            root = await client.get("/")
            assert root.status_code == 200
            assert "spa" in root.text

            deep = await client.get("/streams/abc-123")
            assert deep.status_code == 200
            assert "spa" in deep.text

            asset = await client.get("/assets/app-abc123.js")
            assert asset.status_code == 200
            assert "console.log" in asset.text

            icon = await client.get("/favicon.svg")
            assert icon.status_code == 200
            assert icon.headers["content-type"].startswith("image/svg")

            missing_api = await client.get("/api/does-not-exist")
            assert missing_api.status_code == 404
            assert missing_api.json()["error"]["code"] == "http_error"

            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["service"] == "API-Agent"

    asyncio.run(check())


def test_frontend_dist_can_be_disabled_explicitly(tmp_path):
    """``FRONTEND_DIST`` 设为空串时应完全不托管前端。

    实现：即使目录存在也不挂载，适合把前端交给其他静态服务器托管的部署方式。
    """
    dist = build_frontend(tmp_path)
    settings = load_settings(env_file=None, project_root=tmp_path, frontend_dist="")
    assert settings.frontend_root is None
    assert settings.frontend_dist is None
    assert dist.is_dir()  # 目录确实存在，只是被显式关闭


def test_empty_frontend_dist_string_is_normalized(tmp_path):
    """空字符串必须归一成 None，不能变成“项目根目录”。

    实现：``Path("")`` 是 ``Path(".")``，直接拿去托管会把整个项目根暴露成静态站点。
    """
    settings = load_settings(env_file=None, project_root=tmp_path, frontend_dist="")
    assert settings.frontend_dist is None
