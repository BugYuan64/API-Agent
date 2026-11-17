# API-Agent：API 自动测试与异常分析智能体

> 2026-11-17 新增项目源码导入与 AI 接口文档。`/projects` 可读取运行 API-Agent 电脑上的项目目录，保存源码快照，按全部纳入扫描的文件分批分析并核对静态接口清单。文档可下载为 Markdown 或 OpenAPI 3.1 JSON，包含文件处理进度、扫描遗漏与待确认事项。使用步骤见下方“项目导入与接口文档”。

> 2026-11-04（CRM Docker 全链路实测）：CRM 栈（8088 后端 / 8002 RAG）上完成双引擎冒烟（各 **6 请求 / 16 断言**）、鉴权只读（**3 请求 / 15 断言**）、AI 契约生成与按 `execution_id` 诊断、自适应补测、CRM SSE 与 WebSocket 双向收发。过程中修复一个真实缺陷：赋值式凭据正则会把证据 ID `r0:xxx-password:a0=500` 误判为凭据并整段脱敏，导致 AI 诊断恒返回 `invalid_model_output`。后端 **325 项测试通过**。未执行写操作，未写入 CRM 业务数据。

> 2026-11-03（结构与质量复核）：拆分长连接输入、状态、持久化和编排职责；模型客户端分离传输与供应方响应解析。修复启动恢复、异常资源释放、事件写入失败丢失、终态任务收尾、前端轮询重启/卸载及响应体超时等问题。Docker 8088 经正式 CLI→MCP→两种引擎实测，每个引擎均通过 **9 请求 / 32 断言**（登录、错误参数、鉴权拦截、客户分页/搜索/详情），unknown=0；本轮未写入 CRM 业务数据。完整证据和限制见[阶段记录](阶段步骤实现.md)最新章节。

> 2026-11-03（审核修复）：按第二轮审核修复 **R01–R13 共 13 项问题**（不可回退约束见[开发实施契约](开发实施契约.md)第 20 节），包括 JSON 断言类型严格性（`true` 不再等于 `1`）、跨源不复用已存密钥、历史记录统一脱敏（含 `token` 头、请求体密码、响应 JWT）、流式响应字节预算、执行计划冻结与按 ID 诊断、流事件共享预算与取消一致性。后端 **304 项测试通过**，Ruff 与前端构建通过。CRM 有效身份业务链、真实 SSE 问答与真实模型调用仍未验收。

> 2026-11-03 的双模式界面说明属于历史设计。2026-11-16 当前页面分为接口管理（HTTP/SSE 接口测试、WebSocket 长连接调试）与任务测试（文档驱动用例测试）。

> 2026-11-02（AI 编排）：新增独立 `agent` 业务模块，实现契约生成、证据诊断、预算内单因素补测和 Markdown 报告。通过 `POST /api/agent/runs` 后台运行，支持取消、重启中断恢复与部分报告。真实兼容模型已完成两轮 7 请求、5 次模型调用联调；测试目标是自建无状态接口，不代表实际业务系统验收。使用方法见 [AI 编排说明](docs/ai-agent.md)。

> 2026-11-02（第二轮）：**执行驱动适配器正式落地为可切换的双引擎**——新增纯 Python 原生 `native` 引擎（基于 `httpx`，零外部依赖、毫秒级），与既有 Postman CLI `postman` 沙箱引擎并列；前端「用例计划」页可直接选择引擎，后端 `/api/executions` 与 MCP `run_plan` 均接受 `engine` 参数。同时补全**大模型配置能力**：支持写入/覆盖/清空 API Key、从 `GET /models` 拉取可用模型清单并在界面下拉选择。全量后端测试通过，前端类型检查与生产构建通过。

> 2026-11-02（第一轮）：新增**执行驱动适配器解耦（Driver / Adapter Pattern）**与**系统设置页测试引擎选择器**。修复审核中的目标重定向越界、流读取预算、并发名额、任务取消竞争、执行前项目校验、响应省略模式与历史回放分页问题。详见[阶段记录](阶段步骤实现.md)。

> 2026-11-01：新增 **Vue 3 + TypeScript 前端控制台**（magpie 浅色配色 / apifox 布局）与 **SSE / WebSocket 长连接测试**。前端由 FastAPI 同源托管，类型检查与生产构建通过；后端新增控制台请求、计划执行、长连接三组管理接口与对应持久化表。

> 2026-10-29：阶段 1 管理后端与 Postman 执行基座已实现。独立命令通过本地 MCP 调用真实 Postman CLI；CRM 阶段 2 的 8 项参数/鉴权场景、16 条断言已实测通过。模型生成、诊断、自适应补测、动态短链尚未实现。

本项目是专用于 API 测试的课程设计。目标流程为接口契约 → 用例生成 → 工具执行 → 异常事实与诊断 → 报告。运行时由 API-Agent 自己编排，不依赖 Codex。

当前界面将单接口验证放在 `/request`（普通 HTTP 与 SSE），WebSocket 双向调试放在 `/long-connection`，文档驱动的自动化任务放在 `/testcases`。侧边栏“接口管理”仅含接口测试和长连接测试，“任务测试”包含自动文档识别、用例生成与执行。标准 OpenAPI/Swagger JSON、YAML 可本地解析；非标准 UTF-8 文本使用已配置的大模型提取接口和文档用例。任务逐项复用 HTTP/SSE 与 WebSocket 执行类，默认跳过写请求与 WebSocket 主动发送，需用户显式授权；报告提供 Markdown 下载。内置生成器不集成 Schemathesis/RESTler，也不宣称具备完整 property-based 或状态化序列生成能力。

独立 CLI/MCP 继续提供确定性执行，新增 `/api/agent` 提供独立的大模型闭环编排，不依赖 Codex 运行。“轻量”指工具与上下文只围绕 API 测试；尚未与 Codex 比较速度、token 或成本。设计见[项目最终架构](项目最终架构.md)第 14～16 节（MCP 执行基座、控制台与长连接、AI 业务编排），跨模块约定见[开发实施契约](开发实施契约.md)，进度与证据见[阶段步骤实现](阶段步骤实现.md)，设计与调整依据见[项目最终架构](项目最终架构.md)。

## 实际能力

结果接口追加 `result_code`（稳定分类）与 `result_status`（本项目语义码）。目标实际 HTTP 仍在 `response.status` / `http_status`，不会用超时结果覆盖它。主要分类如下：

| 场景 | 结果分类 / 语义码 | 行为 |
| --- | --- | --- |
| 目标请求超时 / 整体执行超时 | `REQUEST_TIMEOUT` / `EXECUTION_TIMEOUT`，504 | 保留已完成证据；未取得完整响应的断言显示“未评估” |
| 模型超时 / 输出不可用 / 供应方鉴权失败 | `MODEL_TIMEOUT` 504 / `MODEL_ERROR` 502 / `MODEL_AUTH_FAILED` 502 | 与目标请求错误分开显示 |
| 断言失败 | `ASSERTION_FAILED`，422 | 展示实际响应与期望差异 |
| 连接 / TLS / 解码失败 | `CONNECTION_FAILED` / `TLS_ERROR` / `RESPONSE_ENCODING_ERROR`，502 | 使用固定安全说明，不回显传输异常原文 |
| 响应超过大小限制 | `RESPONSE_LIMIT_EXCEEDED`，413 | 中止读取，不将不完整响应判为通过 |
| 取消 / 未知 / 进程中断 | `CANCELLED` / `UNKNOWN_RESULT` / `INTERRUPTED`，无数字码 | 保留已有证据，不暗示目标操作已撤销 |
| 流程完成 | `COMPLETED`，200 | 仅表示流程结束，测试结论仍以逐项断言为准 |

`POST /api/console/requests` 的目标传输失败会在保存历史后返回 HTTP 504、502 或 413，并携带完整结果信封；确实收到目标 HTTP 4xx/5xx 时，管理接口返回 200，目标状态原样放在响应字段中。计划执行和运行查询的管理 HTTP 状态表示管理操作结果，测试分类看上述字段。页面等待 API-Agent 超时另显示 `BROWSER_TIMEOUT`，后端可能仍在运行，请先查询历史再决定是否重试。

文档任务页保持左侧任务历史和右侧工作区；输入/结果分栏独立保存尺寸偏好。

| 能力 | 当前状态 |
| --- | --- |
| 项目/环境追加版本、SQLite 迁移 | 已实现；`/api` 管理 API |
| **项目源码导入与 AI 接口文档** | **已实现**，`/projects` 支持本机目录导入、源码快照、分批生成、取消与历史回看，导出 Markdown 和 OpenAPI 3.1 JSON |
| 独立测试命令、本地 stdio MCP | 已实现；`doctor`、`validate`、`run`（均支持 `--engine`） |
| **执行驱动适配器（Driver / Adapter Pattern）** | **已实现可切换双引擎**；测试内容（`ExecutionPlan`）与执行技术解耦，`native` 与 `postman` 并列注册 |
| **原生 `native` 引擎（纯 Python / httpx）** | **已实现**；零外部依赖、毫秒级、跨平台，覆盖全部 5 类受控断言 |
| **Postman CLI `postman` 引擎（沙箱）** | **已实现**；受控 Collection v2.1 编译、隔离工作区、Windows Job Object 进程树管控、Newman 报告归一化 |
| 结构化计划 → Postman Collection v2.1 → JSON reporter | 已实现；只接受本项目受控编译的集合 |
| 目标限制、JSON 断言、预算上限、超时/取消、脱敏结果 | 已实现；只支持明确授权的数字回环 origin |
| **Vue 3 + TS 控制台（接口管理 / 任务测试 / 项目 / 历史 / 设置）** | **已实现**；Vite 构建产物由 FastAPI 同源托管 |
| **大模型配置：API Key 写入/覆盖/清空 + 在线拉取模型列表** | **已实现**；`PUT /api/settings/llm`、`POST /api/settings/llm/models` |
| **系统设置页测试引擎选择器（本地原生 / Postman CLI / 导入入口）** | **已实现**；卡片式选择、实时就绪状态、选择同步到用例计划页 |
| **MCP 服务详情（postmanmcp 工具清单与客户端接入配置）** | **已实现**；随引擎选择器一同展示 |
| **HTTP 手动请求控制台 + 脱敏历史** | **已实现**；`/api/console/requests` + `GET/DELETE /api/console/history` |
| **HTTP/SSE 接口测试** | **已实现**；接口调试页支持受限 SSE 事件观察，用例任务支持 SSE 断言 |
| **WebSocket 长连接调试** | **已实现**；长连接页仅调试 WebSocket，支持握手、子协议、逐帧双向收发、关闭码、预置消息、历史回放；SSE 在接口测试或用例测试中执行 |
| **计划执行 API + 引擎选择 + 逐断言历史** | **已实现**；`POST /api/executions` 接受 `engine`，供 CLI/MCP 与已有消费者使用 |
| **三类历史统一删除** | **已实现**；console / streams / executions / agent 各自提供单条删除，前端由共享 `HistoryList` 的可选回调启用 |
| CRM 阶段 2 参数校验与未登录拦截 | 已完成 8 请求/16 断言实测 |
| 有效身份下的业务 CRUD、数据库验证 | Docker 鉴权只读分页/搜索/详情已验证；写入、软删除及数据库后验仍未验收 |
| 契约/接口描述 → AI 用例生成 | 已实现；生成受控 `ExecutionPlan` 与期望依据，通过本地范围校验 |
| 断言结果 → AI 诊断与自适应补测 | 已实现；证据引用校验、预算/单因素/去重/写操作重放约束 |
| 后台 AI 闭环与 Markdown 诊断报告 | 已实现；`/api/agent/runs`，取消、重启中断、部分报告与脱敏 JSON 快照 |
| **文档驱动用例任务（`/testcases`）** | **已实现**；单一自动流程：上传并识别 OpenAPI/Swagger 或非标准文本，保留原有用例、由内置生成器补充用例，复用 HTTP/SSE 与 WebSocket 执行类，持久化结果并生成 AI 总结和 Markdown 导出；当前不集成 Schemathesis/RESTler |
| **执行计划溯源与断言证据结构** | **已实现**；`executions.plan` / `plan_sha256` 冻结脱敏计划，断言结果携带 `pointer`/`actual`/`actual_type` |
| **历史记录统一脱敏** | **已实现**；写入/启动迁移/读取三处共用 `infrastructure/history.py`，覆盖 `token` 头、请求体密码与响应 JWT |
| 完整确定性 OpenAPI IR、业务链/数据库验证、动态短链 | 后续阶段；当前直接将契约对象交模型理解，不请求外部 `$ref` |

既有 AI 闭环通过 `/api/agent/runs`，确定性计划仍通过 `/api/executions` 或独立 CLI/MCP 执行。文档任务使用 `/api/tasks/recognize`、`POST /api/tasks`、详情/取消/报告接口；本地标准文档识别无需 LLM，非标准文档识别与任务总结需模型服务可用。单接口页面的 SSE 请求通过 HTTP 响应呈现，事件观察使用字节、事件数与时间上限。

**前端控制台**：Vue 3 + TypeScript + Vite，侧边栏分为接口管理、任务测试、项目、记录和设置；SSE 在接口测试或文档用例测试中执行，长连接页仅调试 WebSocket。Iconify 图标数据本地打包，不请求远端图标服务。

控制台提供暗色和亮色主题，首次打开采用暗色。暗色使用炭灰表面与柔和渐变，亮色使用白色表面与蓝绿渐变；按钮、表单、卡片、表格、弹窗和状态提示共用主题变量。侧边栏底部可切换主题和折叠导航，折叠后保留可点击图标与名称提示。主题和折叠偏好保存在当前浏览器，刷新后恢复。

## 组成与数据流

系统采用**驱动适配器模式（Driver / Adapter Pattern）**，将“测试内容（What to test）”与“底层驱动执行引擎（How to execute）”彻底解耦：

```text
┌─────────────────────────────────────────────────────────────────┐
│                    调度源与外部客户端                           │
│  - Vue 3 控制台界面 (/request, /testcases, /long-connection)    │
│  - 外部大模型宿主 (Claude Desktop / Cursor 等通过 MCP 连接)     │
│  - 独立命令行工具 (api-agent doctor / validate / run)           │
└───────────────────────────────┬─────────────────────────────────┘
                                │ JSON 数据调用
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│            协议适配层 (MCP Stdio Server / REST API)              │
│  - api-agent-mcp: 暴露 postman_status, validate_plan, run_plan  │
│    （三者均接受可选 engine 参数）                                │
│  - FastAPI Endpoints: /api/console, /api/executions, /api/streams│
│  - /api/executions/engine/list 返回可用驱动与就绪状态           │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│        核心编排与安全守护 (ExecutionService / Targets Gate)      │
│  - 本机数字回环 IP 强制校验 (validate_target，防 DNS 引流)      │
│  - 计划静态深度结构防御 (禁未绑定变量 {{}}、禁超大整数、禁路径穿越) │
│  - 跨源重定向过滤、敏感请求头脱敏 (redact_headers)              │
│  - 驱动注册表 engines = {native, postman}，按名分发             │
└───────────────────────────────┬─────────────────────────────────┘
                                │ 适配器分发（engine=native / postman）
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│  原生 HTTPX 驱动 │    │ Postman CLI 沙箱 │    │ 长连接流探测驱动 │
│   (native)       │    │   (postman)      │    │  (streams)       │
├──────────────────┤    ├──────────────────┤    ├──────────────────┤
│• 内存轻量极速发包 │    │• 受控 Collection │    │• SSE WHATWG 解析 │
│• 纯 Python 零依赖│    │• 隔离临时工作区  │    │• WS 逐帧双向调度 │
│• 毫秒级、跨平台  │    │• Win Job 树管控  │    │• 字节/事件/时长限│
│• 默认引擎        │    │• 工业报告背书    │    │• 增量长轮询回放  │
└────────┬─────────┘    └────────┬─────────┘    └────────┬─────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 ▼
                     ┌───────────────────────┐
                     │     被测目标服务      │
                     │ (127.0.0.1:8080/9100) │
                     └───────────────────────┘
```

MCP 只暴露 `postman_status`、`validate_plan`、`run_plan`。没有任意 Shell、脚本、文件读取或云端发布工具。输入是结构化计划；可执行文件路径和允许目标来自本机配置，模型不能通过 payload 改写。三个工具都接受可选的 `engine` 参数（`native` / `postman`），缺省使用 `native`；未知引擎名在解析阶段即返回 `unknown_engine`。默认结果包含响应头、正文和失败断言差异，可能包含目标返回的敏感值；需要只输出请求 ID、HTTP 状态、耗时与断言判定时，显式启用 `OMIT_RESPONSE_PAYLOAD=true`。CLI 原始输出不透传。

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
| `LLM_MODE` | 环境模式 `disabled/mock/openai`；AI API 优先使用设置页保存的配置，无数据库配置时使用环境中的兼容客户端配置；不将 mock 冒充真实调用 |
| `LLM_API_KEY` | 环境模式下的模型密钥；设置页配置使用既有 SQLite 设置表。均不转发给目标执行器 |
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
# 显式选择执行驱动（缺省 native；传 postman 走 CLI 沙箱）
uv run --project backend --locked api-agent --engine postman --allow-origin http://127.0.0.1:8080 run examples/crm/smoke.json
```

`--allow-origin` 是本机操作者为本次调用明确设置的范围，也可使用 `.env` 中的 `TARGET_ALLOWED_ORIGINS`。计划自身声明 origin 不会获得授权。`validate` 不发目标请求，`doctor` 不登录或安装软件；`run` 才发送计划中的请求。

`--engine` 选择执行驱动：`native`（纯 Python / httpx，零外部依赖、毫秒级，默认）或 `postman`（Postman CLI 隔离沙箱，需本机安装 CLI）。不传时使用服务端默认驱动。

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

使用 8010 避免与 CRM RAG 默认的 8000 冲突。提供 `/health`、`/docs` 和 `/api` 下的项目、环境、控制台请求、计划执行、长连接与大模型编排接口。单进程运行；启动会迁移 SQLite 并恢复长连接会话与中断的编排运行。健康检查成功仅表示管理端可用。

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
#    http://127.0.0.1:8010/                → 接口测试
#    http://127.0.0.1:8010/long-connection → WebSocket 调试
#    http://127.0.0.1:8010/testcases       → 文档驱动用例测试
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
| 文档任务测试 | `/testcases` | `/api/tasks/recognize`、`POST/GET/DELETE /api/tasks/{id}`、`/{id}/cancel`、`/{id}/report` |
| 长连接测试 | `/long-connection` | `POST/GET /api/streams`、`/{id}/send`、`/{id}/stop`、`DELETE /api/streams/{id}` |
| 项目与环境 | `/projects` | `/api/projects`、`/api/projects/import`、`/api/projects/{id}/source`、`/api/projects/{id}/documents*`、`/api/environments*` |
| 请求历史 | `/history` | console + streams + executions 三组历史 |
| 执行结果 | `/executions` | `GET /api/executions` |
| 系统设置 | `/settings` | `GET/PUT /api/settings/llm`、`POST /api/settings/llm/ping`、`POST /api/settings/llm/models`、`GET /api/executions/engine/list`（引擎选择器） |

三类历史列共用同一个 `HistoryList` 组件：条目以 `HistoryEntry[]` 传入，删除与清空由**可选回调**（`onDelete` / `onClear`）启用——不传则完全不渲染。样式与行为绑定在组件内，`@click.stop` 由组件保证，调用方不必各自复制按钮或记得阻止冒泡。删除按钮**常驻**并以低不透明度弱化，悬浮整行时提亮；不能用 `display: none` 切换——按钮高 18px 高于文字行高约 15.3px，显隐会让条目在悬浮时跳动 2.7px。

### 执行驱动选择

执行引擎在系统设置页选择，供计划执行 API 使用：

- **系统设置页**（`/settings`）——「测试执行引擎」卡片式选择器，逐个列出引擎的名称、类型、传输方式、版本与就绪状态（绿点/红点 + 错误码）；末尾提供「导入引擎」入口（当前仅界面占位，导入逻辑尚未实现）。
| 驱动 | 标识 | 特点 | 适用场景 |
| --- | --- | --- | --- |
| 本地原生引擎 | `native` | 基于 `httpx` 内存发包，零外部依赖、毫秒级、跨平台 | 日常高频调试、CI、无 Postman 环境 |
| Postman CLI 引擎 | `postman` | 受控 Collection v2.1 + 隔离工作区 + Job Object + Newman 报告，经 stdio MCP 驱动 | 需要工业级独立裁决与沙箱隔离 |
| 导入引擎（预览） | — | 预留给自定义 MCP 测试引擎 | 尚未实现 |

两个驱动都执行同一份 `ExecutionPlan`、使用同一套 5 类断言语义、输出同一结构的 `RunResult`（`executions.engine` 字段记录实际驱动），因此切换驱动不需要改计划。

### 大模型配置

「系统设置」页的大模型卡片支持完整生命周期：

- **API Key**：可写入新密钥覆盖保存，也可点「清空密钥」显式清除（后端收到空字符串才置空；`None` 表示保留）。
- **模型列表**：点「获取模型列表」调用 `POST /api/settings/llm/models`，后端向 `{base_url}/models` 发起 `GET` 并解析 `data[].id`，界面以下拉框列出，可直接点选写回模型名。
- **预览态**：`base_url` 与 `api_key` 可在未保存时先用于拉取模型与连通性测试，避免“先存错再改”。

### 项目导入与接口文档

1. 在系统设置中保存模型服务地址、模型名及所需凭据。
2. 打开 `/projects`，点击项目栏的“导入”，填写项目名称与 `<PROJECT_DIRECTORY>`。目录必须是运行 API-Agent 电脑上的绝对本地路径。已有项目可在“接口文档”页签中点击“导入源码”或“重新导入源码”。
3. 查看扫描统计、排除项和待核验项，再点击“生成接口文档”。导入的脱敏源码会分批发送给系统设置中已配置的模型。生成进度按文件与批次展示，刷新页面后可继续查看，运行期间可取消。
4. 生成结束后，从当前文档或生成历史下载 Markdown、OpenAPI JSON。部分完成、取消或中断的记录也可导出已有内容，文档会保留完成度和遗漏说明。

导入会递归检查目录，排除凭据文件、版本控制目录、依赖与构建产物，并拒绝跟随符号链接。当前扫描上限为 5000 个源码文件、每个文件 512 KiB、合计 20 MiB。超限、无法读取或不支持编码的源码进入扫描问题清单，并将扫描标为不完整。有可读取源码时仍可生成部分文档，界面和导出均保留 `partial` 状态及原因。

后台保存不可变源码快照，将纳入范围的完整文本逐批交给模型分析。大文件按片段处理，并保留片段相邻上下文。静态提取的接口清单用于核对模型输出，未补全的已知接口保留待确认标记。文档包含方法、路径、参数、请求体、响应、鉴权、源码依据及未知信息。源码批次处理完成后，动态注册、运行时前缀、网关重写和外部组件路由仍需结合实际服务核验。

OpenAPI 导出使用 3.1 JSON，覆盖信息位于 `x-generation`，WebSocket 条目位于 `x-websocket-operations`。未知结构与鉴权信息通过说明和扩展字段保留。源码与文档保存在 `ARTIFACT_DIR/projects/<project-id>/sources/` 和 `documents/`，导入新源码会创建新版本，历史文档继续引用原快照。

2026-11-17 使用 `deepseek-v4.1-flash` 实际调用 1 次，完成非秘密夹具的 2 个源码文件和 4 个接口文档，其中 HTTP 3 个、WebSocket 1 个，跨文件 `Product` 参数的 JSON Schema 与源码一致。浏览器已通过项目导入、文档生成、Markdown/OpenAPI 下载及 900px 宽窗口检查。前端 51 项测试、类型检查与生产构建通过，相关后端专项 125 项通过、1 项跳过，所涉 20 个文件的 Ruff 检查与格式检查通过。后端本轮全量结果为 412 项通过、18 项失败、1 项跳过，失败集中于旧 SSE 测试与当前仅接受 WebSocket 的长连接输入契约不匹配，详细范围见阶段步骤实现第 21 节。

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
  migrations/                    # 阶段1迁移 + 20261101 控制台/执行/长连接表 + 20261105 删模拟任务表
  src/api_agent/
    main.py, cli.py
    infrastructure/              # 配置、日志、ORM、API公共契约、目标策略
    projects/                    # 项目/环境版本、源码扫描与快照、AI 文档任务和导出
    execution/                   # 计划、engine(协议)、native(原生驱动)、postman(CLI驱动)、Collection、报告、路由
    console/                     # 手动 HTTP 请求 + 脱敏历史
    streams/                     # schemas 输入输出、state 内存、repository 写库、service 编排、transport 读取预算
    llm/                         # 设置、客户端传输、response 供应方解析、errors 统一异常
    agent/                       # AI 闭环：契约生成、证据诊断、自适应补测、Markdown 报告
    tools/                       # MCP服务与独立客户端
    static/                      # 离线 Swagger UI 资源（js/css/图标）与上游 LICENSE/NOTICE
  tests/                         # 模块测试、agent 闭环与真实 MCP/CLI 集成验证
frontend/
  package.json, vite.config.ts, tsconfig.json
  src/
    api/                         # 类型化的 fetch 封装 + 接口函数
    components/                  # 侧边栏、顶栏、提示、复制按钮、空状态
    composables/                 # 共享轮询生命周期与请求头表单
    views/                       # 接口调试/用例测试(双模式)/长连接/项目/历史/执行/设置
    styles/                      # magpie 配色设计令牌 + apifox 布局基础样式
    stores/                      # Pinia（toast 队列）
  dist/                          # 生产构建产物（由 FastAPI 挂载，不入库）
examples/crm/                    # 源码映射的非秘密测试计划与说明
examples/agent/                  # AI 闭环请求样例（register-run.json）
examples/stream_target.py        # 本地 SSE/WebSocket 测试目标（9100）
辅助脚本/
  install-postman.ps1             # 项目内官方CLI安装与完整性验证
  prepare-crm.ps1                 # 原CRM只读，生成隔离构建描述
  verify-ai-agent.py              # AI 闭环回归与可选真实模型联调
```

`.cache/`、`.python/`、`.env`、虚拟环境、临时原始证据和 `data/` 均为本机运行资料。CRM 源码不迁入本项目；隔离测试包可能包含目标原配置，只留在私有 `.cache/`，不发布。

## 验证与限制

在 backend 目录运行（2026-11-16 删除模拟任务模块后复核：**371 passed**，Ruff check/format 与锁文件检查通过；前端 **62 项行为测试**、类型检查和生产构建通过）：

```powershell
uv lock --check --offline
uv run --locked --offline pytest -q
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
```

也可从根目录运行 `backend/.venv/Scripts/python.exe -m pytest -c backend/pyproject.toml -q`；`uv run --project backend` 选择虚拟环境，但不自动切换工作目录，根目录运行 pytest 时应显式提供 `-c`。前端在 `frontend/` 中运行 `npm test` 和 `npm run build`。

AI 闭环部分的回归可单独运行 `uv run --locked --offline pytest tests/agent -q`；需要真实模型联调时，从项目根目录执行 `辅助脚本/verify-ai-agent.py --live`（使用已保存设置，最多 8 次调用）。

真实 CLI 集成用例当前查找项目内 `.cache/tools/postman/postman.exe`，并需要允许本机回环通信的执行环境；该文件缺失时用例会明确跳过。正式独立入口另支持 `POSTMAN_CLI_PATH` 配置；集成用例尚不自动读取该替代路径。受限沙箱内 Windows asyncio 的 socketpair 已复现阻塞，不能据此断言业务代码失败。

**原生引擎无需任何外部依赖**：`native` 驱动的测试不依赖 Postman CLI，即使未安装 CLI，也能完整执行计划、产生断言结果并落库；仅 `postman` 驱动相关的用例会在 CLI 缺失时跳过。

2026-10-29 已实际运行独立入口，对隔离构建的真实 CRM 完成 8 请求/16 断言，结果为 passed；报告保存在本机 `data/artifacts/crm-smoke-2026-10-29.json`。本次 Java 测试实例已停止，未修改原 CRM 源码或全局服务，本次请求未执行数据库业务写入。后端完整验证的最新计数见阶段记录。

真实 CRM 验证覆盖参数错误及未登录拦截，未覆盖有效 token、数据库业务 CRUD、软删除业务规则或 RAG/SSE。没有创建账号或客户，也未发起真实模型调用。完整大模型 Agent、动态变量链和自适应效果仍需后续实现及验收。
