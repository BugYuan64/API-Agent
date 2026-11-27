# API-Agent 自测样例（Dogfooding）

本目录提供针对 API-Agent 自身管理接口（默认 `http://127.0.0.1:8010`）的只读与校验自测计划，避免递归触发 `/api/tasks`、`/api/executions` 或 `/api/console/requests` 写入路径。

- `management-smoke.json`：13 请求 / 33 断言，覆盖健康检查、OpenAPI 契约、项目分页与 404 契约、执行引擎列表/状态、大模型设置与状态、HTTP 历史分页与 422 边界、长连接列表、任务列表及模型测试非法 URL 400 校验。

## 运行

先在另一个终端启动 API-Agent 管理服务：

```powershell
uv run --project backend --locked uvicorn api_agent.main:create_app --factory --host 127.0.0.1 --port 8010 --no-access-log
```

再执行自测计划（默认 `native` 引擎，可切换 `--engine postman` 复核）：

```powershell
uv run --project backend --locked api-agent --allow-origin http://127.0.0.1:8010 run examples/self/management-smoke.json
```
