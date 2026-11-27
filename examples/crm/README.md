# CRM 阶段 2 测试入口

目标源码：`<CRM_PROJECT_DIRECTORY>`。本目录只有非秘密计划，不复制 CRM 源码、配置、凭据和数据库。接口依据见根目录架构第 14.5 节。

- `smoke.json`：8 次请求、16 条断言；注册/登录参数验证以及 GET/POST/PUT/DELETE 的 token 鉴权前置检查。没有有效身份，不创建账号或客户，不执行成功的修改/删除。
- `authenticated.json`：自定义 `token` Header 引用 `CRM_TEST_TOKEN`，只读分页。需要用户提供可用的专用测试身份；不自动登录，不输出客户明细。
- `rag.json`：FastAPI 健康检查及空问题的 JSON 错误响应；不调用有效问题，不验证 SSE 事件流。RAG 本身初始化依赖需由目标环境准备。
- `docker-smoke.json`：WSL Docker 统一入口 `http://127.0.0.1:8088`，6 请求 / 17 断言，验证容器初始化样例身份登录、错误密码/参数与无 token 拦截。仅适用于该本地测试数据；正式凭据不得写入计划。
- `docker-authenticated.json`：同一入口，3 请求 / 15 断言，自定义 `token` 头通过 `CRM_TEST_TOKEN` 环境引用，验证分页、中文关键词搜索和客户详情；固定预期依赖容器初始化数据。
- `docker-register-validation.json`：同一入口，6 请求 / 15 断言，覆盖 `POST /api/user/register` 的缺必填、邮箱/手机号/密码格式校验、两次密码不一致与已注册邮箱冲突；不写入有效用户。
- `docker-rag.json`：RAG 直连入口 `http://127.0.0.1:8002`，6 请求 / 13 断言，覆盖 `/health` 与 `/ask`、`/ask/stream` 的 400/422 参数边界。
- `docker-stateful-crud.json`：同一入口 `http://127.0.0.1:8088`，11 请求 / 28 断言，基于有状态多步骤依赖链自动完成登录提取 `crm_token` → 新增测试客户 → 按邮箱反查提取 `customer_id` → 详情/更新/回读 → 复现 `PUT /api/customer/{id}` 缺 `status` 缺陷 → 商机外键拦截删除 → `always_run` 软删除清理、幂等重删与 `inactive` 状态核验。注意运行后会在 MySQL 留下 1 条 `apitester-chain@example.com` 软删除记录，请按文档规范清理。

## 运行

先按根目录 README 准备 uv 环境及 CLI。以下在 API-Agent 根目录执行：

```powershell
uv run --project backend --locked api-agent doctor
uv run --project backend --locked api-agent --allow-origin http://127.0.0.1:8080 validate examples/crm/smoke.json
uv run --project backend --locked api-agent --allow-origin http://127.0.0.1:8080 run examples/crm/smoke.json
```

`--allow-origin` 是本机操作者给予本次进程的授权；MCP payload 内的 origin 不能替代此授权。也可用根目录 `.env` 的 `TARGET_ALLOWED_ORIGINS` 配置。仅支持数字回环地址，`localhost` 不会自动解析放行。

Docker 实测入口：`uv run --project backend --locked --offline api-agent --allow-origin http://127.0.0.1:8088 --engine native run examples/crm/docker-smoke.json`。将 `--engine` 改为 `postman` 可用同一计划复核；两种引擎于 2026-11-03 经正式 CLI→MCP 路径分别通过上述 9 请求 / 32 断言。鉴权计划需安全设置环境 token，结果只汇总判定，未输出客户明细。

使用鉴权样例时，另将 `TARGET_AUTH_ENV_VARS` 配为 `["CRM_TEST_TOKEN"]`，并在本机进程环境安全设置实际 token；不要写入样例、命令行参数或提交文件。服务端只解析许可列表中的变量名。token 必须是 CRM 签发的测试身份，不是模型或 Postman API key。

CLI 退出码：0=通过/校验有效，1=断言失败，2=执行、配置、连接或证据错误。默认结果包含响应头、正文与失败断言差异；对鉴权或业务数据测试，应按数据处理要求设置 `OMIT_RESPONSE_PAYLOAD=true`，以仅保留状态、耗时与断言判定，并省略差异原文。需要查看实际业务数据时应在 CRM 本身检查。

## 可选：隔离构建 Spring 目标

无需修改原 CRM。`辅助脚本/prepare-crm.ps1` 从原 POM 生成临时构建描述，源码和资源仍从原目录读取，依赖和产物放 API-Agent 的 `.cache/`。构建包含目标原有配置，仅用于本机私有测试，不可上传。

```powershell
& './辅助脚本/prepare-crm.ps1'
& '<MAVEN_HOME>/bin/mvn.cmd' -s .cache/crm-smoke/settings.xml -gs .cache/crm-smoke/settings.xml '-Dmaven.repo.local=.cache/maven' -f .cache/crm-smoke/pom.xml -DskipTests package -q
java -jar .cache/crm-smoke/target/crm-backend-1.0.0.jar --server.address=127.0.0.1 --server.port=8080 --logging.level.root=ERROR
```

停止该前台 Java 进程使用 Ctrl+C。原目标配置/数据库是否可用仍影响业务接口；通过参数验证/鉴权测试不代表数据库业务或完整 CRM 验收完成。FastAPI RAG 默认 8000，API-Agent 管理服务默认使用 8010（以避免端口冲突）；如同时运行其他服务，联调时请显式区分端口。

## 当前边界与实测说明（2026-11-27）

- Spring 部分业务异常返回 HTTP 200 + JSON `code="400"/"500"`，鉴权拦截器返回 HTTP 401 + `code="401"`；计划分别断言 HTTP 状态码与业务码。客户 `DELETE` 实际为软删除（置 `status="inactive"`）。
- Docker 栈（`8088` 统一入口 / `8002` RAG / `ws://127.0.0.1:8088/api/ws/chat`）已通过双引擎（`native` + `postman`）冒烟、鉴权只读、注册边界、RAG 参数边界、有状态 CRUD 依赖链（`docker-stateful-crud.json`）、SSE 流式问答与 WebSocket 双向对话验证。
- **已知外部 CRM / RAG 被测目标自身缺陷**（位于外部目标仓库，不属于 API-Agent 代码范围）：
  1. `PUT /api/customer/{id}` 未传 `status` 时会触发 `Column 'status' cannot be null` 并将完整 SQL 异常堆栈泄露到响应 `msg` 中；
  2. `GET /api/customer/list` 按 `updated_at DESC` 排序且不过滤软删除记录，执行软删除测试后 `inactive` 记录会排在第一页首位，从而影响依赖初始数据顺序的 `docker-authenticated.json`（需按 `docs/CRM接口测试清单.md` 第八节清理测试数据）；
  3. RAG 直连 `http://127.0.0.1:8002/ask/stream` 的流式分片中直接拼接了未转义的原始 `\n\n`，会破坏 WHATWG SSE 帧边界解析。

详细接口实测记录见 `docs/CRM接口测试清单.md` 与根目录 `阶段步骤实现.md`。
