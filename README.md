# API-Agent：API 自动测试与异常分析智能体

> 2026-11-02：修复审核中的目标重定向越界、流读取预算、并发名额、任务取消竞争、执行前项目校验、响应省略模式与历史回放分页问题。后端 **176 项测试通过**（新增 30 个回归场景，含真实 MCP/CLI）；前端类型检查、构建与 650 条历史事件浏览器回放通过。详见[审核报告](审核报告-2026-11-02.md)。模型生成、诊断与自适应补测仍为后续阶段。

> 2026-11-01：新增 **Vue 3 + TypeScript 前端控制台**（magpie 浅色配色 / apifox 布局）与 **SSE / WebSocket 长连接测试**。前端由 FastAPI 同源托管，类型检查与生产构建通过；后端新增控制台请求、计划执行、长连接三组管理接口与对应持久化表，146 项测试通过（含真实 CLI 集成）。

> 2026-10-29：阶段 1 管理后端与 Postman 执行基座已实现。独立命令通过本地 MCP 调用真实 Postman CLI；CRM 阶段 2 的 8 项参数/鉴权场景、16 条断言已实测通过。模型生成、诊断、自适应补测、动态短链尚未实现。

本项目是专用于 API 测试的课程设计。目标流程为接口契约 → 用例生成 → 工具执行 → 异常事实与诊断 → 有预算的补测 → 报告。运行时由 API-Agent 自己编排，不依赖 Codex。

当前独立入口提供确定性测试执行，还不是完整大模型 Agent。“轻量”指工具与上下文只围绕 API 测试；尚未与 Codex 比较速度、token 或成本。设计见[项目最终架构](项目最终架构.md)第 14 节，跨模块约定见[开发实施契约](开发实施契约.md)，进度与证据见[阶段步骤实现](阶段步骤实现.md)。[方案审核记录](方案审核记录.md)保留原设计及此次调整依据。

## 实际能力

| 能力 | 当前状态 |
| --- | --- |
| 项目/环境追加版本、SQLite 迁移、模拟任务创建/取消/恢复 | 已实现；`/api` 管理 API |
| 独立测试命令、本地 stdio MCP | 已实现；`doctor`、`validate`、`run` |
| 结构化计划 → Postman Collection v2.1 → JSON reporter | 已实现；只接受本项目受控编译的集合 |
| 目标限制、JSON 断言、预算上限、超时/取消、脱敏结果 | 已实现；首步只支持明确授权的数字回环 origin |
| **Vue 3 + TS 控制台（接口调试 / 用例计划 / 长连接 / 项目环境 / 任务 / 历史）** | **已实现**；Vite 构建产物由 FastAPI 同源托管 |
| **HTTP 手动请求控制台 + 脱敏历史** | **已实现**；`/api/console/requests` + `/api/console/history` |
| **SSE 长连接测试** | **已实现**；逐事件记录、握手状态、字节/事件上限、主动停止、历史回放 |
| **WebSocket 长连接测试** | **已实现**；握手、子协议协商、逐帧收发、关闭码、预置消息调度、主动停止 |
| **计划执行的界面入口 + 逐断言历史** | **已实现**；`/api/executions` 复用既有 Postman 引擎 |
| CRM 阶段 2 参数校验与未登录拦截 | 已完成 8 请求/16 断言实测 |
| 有效身份下的业务 CRUD、数据库验证 | 未验收；已整理样例与语义边界 |
| OpenAPI 解析、规则/模型生成、异常分析、反馈 | 后续阶段 |

管理端的 `/api/runs` 仍只执行模拟动作。真实请求通过本次独立入口运行，没有将模拟任务或预算账本伪装为真实执行。

**2026-11-01 新增的界面层**：Vue 3 + TypeScript + Vite 控制台，配色取自 magpie 的克制浅色体系（近白底 + 墨黑主色 + 单一紫色强调），布局参考 apifox（左侧栏 + 顶栏 + 左历史/右工作台三段式）。包含七类页面：接口调试、用例计划、长连接测试、项目与环境、模拟任务、请求历史、执行结果。构建产物由 FastAPI 同源挂载（`/assets` 静态资源 + history 模式 SPA 回退），无需单独启动前端进程即可使用。

## 组成与数据流

```text
api-agent CLI / 后续编排器
    → 官方 MCP Python SDK Client
    → 本地 stdio MCP Server
    → ExecutionService（本机范围、凭据引用校验）
    → 受控 Collection 编译器
    → 原生 Postman CLI
    → 被测 HTTP API
    → JSON reporter → 结果归一化 → JSON 输出
```

MCP 只暴露 `postman_status`、`validate_plan`、`run_plan`。没有任意 Shell、脚本、文件读取或云端发布工具。输入是结构化计划；可执行文件路径和允许目标来自本机配置，模型不能通过 payload 改写。默认结果包含响应头、正文和失败断言差异，可能包含目标返回的敏感值；需要只输出请求 ID、HTTP 状态、耗时与断言判定时，显式启用 `OMIT_RESPONSE_PAYLOAD=true`。CLI 原始输出不透传。

## 环境与依赖

Python 3.12.x、uv；Windows 辅助脚本使用 PowerShell 7.3+；锁文件包含 FastAPI、SQLAlchemy、Alembic、Pydantic 和官方 MCP SDK 2.3.0。Windows 的进程生命周期使用 pywin32。Postman CLI 本机验证版本为 1.82.0，实际程序及配套 `lib/` 位于 `.cache/tools/postman/`。

在根目录执行：

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.cache/uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD '.python'
uv sync --project backend --locked
```

如果缺少原生 CLI 或 npm 启动器缺少 Windows 二进制包，使用项目内安装方式：

```powershell
& './辅助脚本/install-postman.ps1' -Version 1.82.0
```

该辅助脚本下载官方 `@postman/pm-bin-windows-x64` 指定版本，校验 npm SHA-512 完整性，展开原生程序及其必要依赖，然后检查版本。只写 `.cache/tools/postman/`，不运行 npm postinstall，不改全局 Agent 指令、不登录或安装 Agent 插件。其他平台需自行提供原生 CLI 并设置 `POSTMAN_CLI_PATH`；目前完整生命周期实测在 Windows。

## 配置

可复制根目录 `.env.example` 为 `.env`。已有 `.env` 时自行合并新增字段，不覆盖秘密。显式配置优先于环境变量，其次根目录 `.env`、默认值；相对路径始终相对项目根目录。

| 配置 | 用途 |
| --- | --- |
| `APP_NAME`、`LOG_LEVEL` | 管理应用名称与日志级别 |
| `DATABASE_URL` | 默认 `sqlite:///data/api_agent.db`；管理端启动时迁移 |
| `ARTIFACT_DIR` | 默认 `data/artifacts`；独立命令只输出 JSON，可由操作者保存 |
| `LLM_MODE` | 当前 `disabled/mock` 配置标识，不会发起模型调用；`live` 仍未接入 |
| `LLM_API_KEY` | 预留秘密字段，本阶段不使用、不转发给 CLI |
| `TARGET_ALLOWED_ORIGINS` | JSON 数组，例如 `["http://127.0.0.1:8080"]`；真实执行只允许数字回环 origin |
| `TARGET_AUTH_ENV_VARS` | JSON 数组，仅列出允许计划引用的目标鉴权环境变量名称，默认空 |
| `POSTMAN_CLI_PATH` | 原生可执行文件的受信本机路径；Windows 空值默认项目内 `.cache/tools/postman/postman.exe` |
| `OMIT_RESPONSE_PAYLOAD` | `false`（默认）保留响应头与响应体；`true` 只保留状态/耗时/断言结果，省略失败差异原文；独立 CLI 会将实际设置传入 MCP 子进程 |
| `FRONTEND_DIST` | 前端构建目录，默认 `frontend/dist`；设为空字符串则不托管前端 |
| `STREAM_MAX_SESSIONS` | 长连接并发上限，默认 4 |
| `STREAM_MAX_BYTES` | 单条长连接会话累计接收字节上限，默认 8 MiB |

模型/计划不能任意引用系统中的其他秘密变量。实际 token 应保留在本机进程环境中，计划只包含 `auth_ref.env_var`。独立客户端只向 MCP 子进程传递许可名单中的鉴权值；CLI 在临时工作区、独立用户配置目录和最小环境中运行，不继承 Postman 登录或代理配置。集合、原始报告及执行上下文在退出时清理；默认关闭 CLI 运行事件上报，不执行 init/login/push/publish。

## 独立测试入口

以下在项目根目录执行；首次准备好锁定依赖后可添加 `--offline`：

```powershell
uv run --project backend --locked api-agent doctor
uv run --project backend --locked api-agent --allow-origin http://127.0.0.1:8080 validate examples/crm/smoke.json
uv run --project backend --locked api-agent --allow-origin http://127.0.0.1:8080 run examples/crm/smoke.json
```

`--allow-origin` 是本机操作者为本次调用明确设置的范围，也可使用 `.env` 中的 `TARGET_ALLOWED_ORIGINS`。计划自身声明 origin 不会获得授权。`validate` 不发目标请求，`doctor` 不登录或安装软件；`run` 才发送计划中的请求。

退出码：0=通过/校验有效，1=断言失败，2=执行、配置、连接、报告或取消问题。真实目标未启动时应得到 error，而不是通过或“发现产品缺陷”。

计划使用 JSON：一个 origin、允许方法/路径模板、具体请求、固定断言与预算。参考[CRM 样例](examples/crm/README.md)。支持 `status`、`json_equals`、`json_exists`、`content_type`、`response_time`；JSON Pointer 支持转义，数组只接受规范且在范围内的十进制索引。等值断言拒绝绝对值超过 9007199254740991 的数字（包括嵌套值），避免 Postman JavaScript 精度造成错误通过；大整数若按接口契约以字符串传输则可比较。请求可带标量查询参数及 JSON 请求体，`body: null` 当前表示不发送请求体。每次最多 50 个请求、每请求最多 20 条断言，默认请求超时 3 秒、整体 30 秒；整体上限 120 秒。固定单次迭代、无重试或脚本额外发送，禁止自动重定向。结果区分 reserved、observed 与 unknown，不把未取得证据的请求算作已通过。

MCP 服务正式入口：

```powershell
uv run --project backend --locked api-agent-mcp
```

它等待 stdio 协议消息，不输出交互式提示，也不监听 HTTP 端口。外部 MCP 宿主可用同一入口；独立命令已包含启动/初始化/调用/关闭逻辑。取消和超时会回收 CLI，Windows 通过 Job Object 覆盖进程树。没有对外发布或更改其他宿主的 MCP 配置。

## 管理后端入口

```powershell
uv run --project backend --locked uvicorn api_agent.main:create_app --factory --host 127.0.0.1 --port 8010 --no-access-log
```

使用 8010 避免与 CRM RAG 默认的 8000 冲突。提供 `/health`、`/docs` 和 `/api` 下的项目、环境、模拟任务与事件/预算查询。单进程运行；启动会迁移 SQLite 并恢复任务状态。健康检查成功仅表示管理端可用。

## 前端控制台

前端位于 `frontend/`（Vue 3 + TypeScript + Vite + vue-router + Pinia，图标用 lucide-vue-next）。**不需要单独启动前端进程**：生产构建产物由 FastAPI 同源挂载，浏览器直接打开 `http://127.0.0.1:8010/` 即可使用。

### 启动步骤

```powershell
# 1) 安装依赖 + 构建（只需构建一次，或改了前端代码再构建）
cd frontend
npm install
npm run build

# 2) 回到根目录启动后端
cd ..
uv run --project backend --locked uvicorn api_agent.main:create_app --factory --host 127.0.0.1 --port 8010 --no-access-log

# 3) 浏览器打开
#    http://127.0.0.1:8010/                → 接口调试
#    http://127.0.0.1:8010/long-connection → 长连接测试
#    http://127.0.0.1:8010/plan            → 用例计划执行
```

### 开发模式（可选）

```powershell
cd frontend
npm run dev   # vite dev server，端口 5180，/api 与 /health 代理到 8010
```

`npm run build` 已包含 `vue-tsc --noEmit` 类型检查；`npm run typecheck` 可单独跑类型。

### 页面与对应后端能力

| 页面 | 路径 | 调用的管理接口 |
| --- | --- | --- |
| 接口调试 | `/request` | `POST /api/console/requests`、`GET/DELETE /api/console/history` |
| 用例计划 | `/plan` | `POST /api/executions`、`GET /api/executions/engine/status` |
| 长连接测试 | `/long-connection` | `POST/GET /api/streams`、`/{id}/send`、`/{id}/stop` |
| 项目与环境 | `/projects` | `/api/projects`、`/api/environments*` |
| 模拟任务 | `/runs`、`/runs/:id` | `/api/runs*` |
| 请求历史 | `/history` | console + streams + executions 三组历史 |
| 执行结果 | `/executions` | `GET /api/executions` |

### 长连接测试

- **SSE**：逐行解析 WHATWG 的 SSE 语法（`data:`/`event:`/`id:`/`retry:`/`:` 注释，多行 `data` 拼接）。握手状态与 `Content-Type` 被记录；目标主动关闭记为 `target_closed`。
- **WebSocket**：记录握手结果、协商到的子协议、逐帧文本/二进制（二进制按 base64 保存）、关闭码与原因。支持预置消息调度（连接后按间隔自动发送）与连接后手动发送。
- **上限**：事件数、累计字节数、总时长三项任一达到即由本服务主动断开，结束原因记为 `limits_exceeded` 或 `deadline_exceeded`，与目标主动关闭区分开。
- **范围**：仅允许数字回环 IP 地址；域名（如 `example.com`）返回 `target_not_loopback`。若配置了 `TARGET_ALLOWED_ORIGINS`，目标还必须出现在该列表中（`ws://` 自动映射到 `http://` 比对）。
- **重定向与代理**：SSE 与 WebSocket 均不跟随目标重定向；WebSocket 不读取系统或环境代理。HTTP 手动请求默认不跟随重定向，启用后逐跳校验目标；跨源跳转移除原请求的自定义头、认证头与 Cookie。
- **接收预算**：SSE 在原始流块进入时计字节，包含尚未成行或派发的事件；WebSocket 在消息分片进入时计应用数据字节，并使用有界消息缓存。生命周期提示文字不计入接收字节。SSE 请求 `Accept-Encoding: identity`，目标仍发送压缩正文时明确返回 `unsupported_content_encoding`，避免先自动解压再检查上限。
- **观察方式**：前端通过 1.2 秒长轮询增量拉取事件（`GET /api/streams/{id}?after=<cursor>&wait_ms=1200`），不在浏览器里再开第二条长连接；历史会话可从 SQLite 完整回放。
- **历史分页**：数据库回放每页最多 500 条；`finished` 表示会话终止，`has_more` 表示仍有下一页事件，前端按游标读取至 `has_more=false`。
- **测试夹具**：`examples/stream_target.py` 提供一个本地 SSE/WebSocket 目标（`uv run --project backend --locked --offline python examples/stream_target.py`，监听 9100），可用于手动验证界面。

## 实际目录

```text
backend/
  pyproject.toml, uv.lock
  migrations/                    # 阶段1迁移 + 20261101 控制台/执行/长连接表
  src/api_agent/
    main.py, cli.py
    infrastructure/              # 配置、日志、ORM、API公共契约、目标策略
    projects/                    # 项目和环境版本
    runs/                        # 持久化模拟任务、预算、事件、worker
    execution/                   # 计划、Collection、CLI、报告、执行服务 + 界面执行路由
    console/                     # 手动 HTTP 请求 + 脱敏历史
    streams/                     # SSE / WebSocket 长连接会话与事件
    tools/                       # MCP服务与独立客户端
  tests/                         # 模块测试和真实MCP/CLI集成验证
frontend/
  package.json, vite.config.ts, tsconfig.json
  src/
    api/                         # 类型化的 fetch 封装 + 接口函数
    components/                  # 侧边栏、顶栏、提示、复制按钮、空状态
    views/                       # 接口调试/用例计划/长连接/项目/任务/历史/执行
    styles/                      # magpie 配色设计令牌 + apifox 布局基础样式
    stores/                      # Pinia（toast 队列）
  dist/                          # 生产构建产物（由 FastAPI 挂载，不入库）
examples/crm/                    # 源码映射的非秘密测试计划与说明
examples/stream_target.py        # 本地 SSE/WebSocket 测试目标（9100）
辅助脚本/
  install-postman.ps1             # 项目内官方CLI安装与完整性验证
  prepare-crm.ps1                 # 原CRM只读，生成隔离构建描述
```

`.cache/`、`.python/`、`.env`、虚拟环境、临时原始证据和 `data/` 均为本机运行资料。CRM 源码不迁入本项目；隔离测试包可能包含目标原配置，只留在私有 `.cache/`，不发布。

## 验证与限制

在 backend 目录运行：

```powershell
uv lock --check --offline
uv run --locked --offline pytest -q
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
```

真实 CLI 集成用例当前查找项目内 `.cache/tools/postman/postman.exe`，并需要允许本机回环通信的执行环境；该文件缺失时用例会明确跳过。正式独立入口另支持 `POSTMAN_CLI_PATH` 配置；集成用例尚不自动读取该替代路径。受限沙箱内 Windows asyncio 的 socketpair 已复现阻塞，不能据此断言业务代码失败。

2026-10-29 已实际运行独立入口，对隔离构建的真实 CRM 完成 8 请求/16 断言，结果为 passed；报告保存在本机 `data/artifacts/crm-smoke-2026-10-29.json`。本次 Java 测试实例已停止，未修改原 CRM 源码或全局服务，本次请求未执行数据库业务写入。后端完整验证的最新计数见阶段记录。

真实 CRM 验证覆盖参数错误及未登录拦截，未覆盖有效 token、数据库业务 CRUD、软删除业务规则或 RAG/SSE。没有创建账号或客户，也未发起真实模型调用。完整大模型 Agent、动态变量链和自适应效果仍需后续实现及验收。
