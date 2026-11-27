# API-Agent：API 自动测试与异常分析智能体

> 更新于 2026-11-27：全站四个测试工作台（`/request` 接口测试、`/long-connection` 长连接测试、`/testcases` 智能群测、`/model-testing` 大模型测试）统一接入四区 `TestWorkspace` 外壳与页面级隔离的 **小Y AI 工具测试助手**（`/api/assistant/*`），支持后台持久化多轮运行、冷却间隔（`cooldown_ms=500`）、单条 `AI 测试` 标注历史、可点击 `CaseLinkBlock` 子用例只读回放、切页不断连重连、原子对话分叉、向量知识库检索（`/api/knowledge/*`）及零明文密钥的大模型能力探测持久化（迁移 `20261127_0008`，共 12 张业务表）；同步完成 CRM 全量测试审核缺陷 A.1–A.7 修复（线性时间无 ReDoS 邮箱脱敏、Postman 单进程 `pm.execution.skipRequest()` 有状态依赖链与 `actual` 断言观测值双引擎一致、引擎 `id`/`engine` 对齐、WebSocket 子用例断言判定落库持久化、静态 Token 智能群测直接重测与 `runtime_headers`、四工作台窄屏防裁切与顶栏引擎版本响应式同步、SSE 观测时限取消隐式减半）。后端全量 **566 passed, 1 skipped**、前端 **157 passed**、`vue-tsc` + `vite build` 与 Ruff（177 文件）全部通过。

> 更新于 2026-11-22：大模型服务默认开启（`LLM_MODE=openai`、`enabled=true`，未配置 `base_url` 时仍保持本地安全未就绪、不发起外网请求）；支持按用途分离「小Y 对话模型」与可选「接口文档与 test 生成模型」（迁移 `20261120_0007`）；「接口测试」左侧历史栏仅保留单条删除；AI 对话（小T / 小Y）支持 `Enter` 直接发送、`Shift + Enter` 换行，以及底部锚点自动跟进输出；前端校验错误提示附带脱敏后的 `details` 字段原因。

> 智能群测支持在上传接口文档区域通过「生成接口文档」打开本机项目选择器。源码只在本次请求内脱敏分析，生成的 `openapi<时间>.json` 写入所选项目的 `./API/`，不保存项目快照。

> 2026-11-04（CRM Docker 全链路实测）：CRM 栈（8088 后端 / 8002 RAG）上完成双引擎冒烟（各 **6 请求 / 16 断言**）、鉴权只读（**3 请求 / 15 断言**）、AI 契约生成与按 `execution_id` 诊断、自适应补测、CRM SSE 与 WebSocket 双向收发。过程中修复一个真实缺陷：赋值式凭据正则会把证据 ID `r0:xxx-password:a0=500` 误判为凭据并整段脱敏，导致 AI 诊断恒返回 `invalid_model_output`。后端 **325 项测试通过**。未执行写操作，未写入 CRM 业务数据。

> 2026-11-03（结构与质量复核）：拆分长连接输入、状态、持久化和编排职责；模型客户端分离传输与供应方响应解析。修复启动恢复、异常资源释放、事件写入失败丢失、终态任务收尾、前端轮询重启/卸载及响应体超时等问题。Docker 8088 经正式 CLI→MCP→两种引擎实测，每个引擎均通过 **9 请求 / 32 断言**（登录、错误参数、鉴权拦截、客户分页/搜索/详情），unknown=0；本轮未写入 CRM 业务数据。完整证据和限制见[阶段记录](阶段步骤实现.md)最新章节。

> 2026-11-03（审核修复）：按第二轮审核修复 **R01–R13 共 13 项问题**（不可回退约束见[开发实施契约](开发实施契约.md)第 20 节），包括 JSON 断言类型严格性（`true` 不再等于 `1`）、跨源不复用已存密钥、历史记录统一脱敏（含 `token` 头、请求体密码、响应 JWT）、流式响应字节预算、执行计划冻结与按 ID 诊断、流事件共享预算与取消一致性。后端 **304 项测试通过**，Ruff 与前端构建通过。CRM 有效身份业务链、真实 SSE 问答与真实模型调用仍未验收。

> 2026-11-03 的双模式界面说明属于历史设计。现行入口为「单点测试」和独立的「智能群测」。

> 2026-11-02（AI 编排）：新增独立 `agent` 业务模块，实现契约生成、证据诊断、预算内单因素补测和 Markdown 报告。通过 `POST /api/agent/runs` 后台运行，支持取消、重启中断恢复与部分报告。真实兼容模型已完成两轮 7 请求、5 次模型调用联调；测试目标是自建无状态接口，不代表实际业务系统验收。使用方法见 [AI 编排说明](docs/ai-agent.md)。

> 2026-11-02（第二轮）：**执行驱动适配器正式落地为可切换的双引擎**——新增纯 Python 原生 `native` 引擎（基于 `httpx`，零外部依赖、毫秒级），与既有 Postman CLI `postman` 沙箱引擎并列；前端「用例计划」页可直接选择引擎，后端 `/api/executions` 与 MCP `run_plan` 均接受 `engine` 参数。同时补全**大模型配置能力**：支持写入/覆盖/清空 API Key、从 `GET /models` 拉取可用模型清单并在界面下拉选择。全量后端测试通过，前端类型检查与生产构建通过。

> 2026-11-02（第一轮）：新增**执行驱动适配器解耦（Driver / Adapter Pattern）**与**系统设置页测试引擎选择器**。修复审核中的目标重定向越界、流读取预算、并发名额、任务取消竞争、执行前项目校验、响应省略模式与历史回放分页问题。详见[阶段记录](阶段步骤实现.md)。

> 2026-11-01：新增 **Vue 3 + TypeScript 前端控制台**（magpie 浅色配色 / apifox 布局）与 **SSE / WebSocket 长连接测试**。前端由 FastAPI 同源托管，类型检查与生产构建通过；后端新增控制台请求、计划执行、长连接三组管理接口与对应持久化表。

> 2026-10-29：阶段 1 管理后端与 Postman 执行基座已实现。独立命令通过本地 MCP 调用真实 Postman CLI；CRM 阶段 2 的 8 项参数/鉴权场景、16 条断言已实测通过。模型生成、诊断、自适应补测、动态短链尚未实现。

本项目是专用于 API 测试的课程设计。目标流程为接口契约 → 用例生成 → 工具执行 → 异常事实与诊断 → 报告。运行时由 API-Agent 自己编排，不依赖 Codex。

当前界面将单接口验证放在「单点测试」下的 `/request`（普通 HTTP 与 SSE）与 `/long-connection`（仅 WebSocket 双向调试），将文档驱动的自动化任务放在独立分组「智能群测」（`/testcases`），将外部模型接口能力探测放在「大模型测试」（`/model-testing`），并将四类汇总历史与系统配置分别放在「记录 → 执行结果」（`/history`）和「设置 → 系统设置」（`/settings`）。四个测试工作台均集成页面级隔离的小Y 工具调用助手（`/api/assistant/*`）与后台持久化运行器，切换页面不取消后台测试，返回自动重连。标准 OpenAPI/Swagger JSON、YAML 可本地解析；非标准 UTF-8 文本使用已配置的大模型提取接口和文档用例。任务逐项复用 HTTP/SSE 与 WebSocket 执行类并支持受控多步骤变量提取与依赖链（`variables`/`extract`/`depends_on`/`always_run`），写请求与 WebSocket 主动发送由任务参数显式控制（前端工作台首次默认勾选开启，可在设置中关闭；后端 `StartTask` 接口默认跳过，需显式传入 `allow_writes=true`）；报告提供 Markdown 下载。内置生成器不集成 Schemathesis/RESTler，也不宣称具备完整 property-based 生成能力。

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

接口测试、长连接测试与智能群测共用分栏布局。侧栏展开或折叠时，AI 对话和填写区从左侧伸缩，右边界保持不动；填写区达到尺寸上下限后，依次由结果区和历史栏分担。手动拖动分隔线只调整相邻两区，尺寸偏好按页保存。智能群测的执行结果按接口分组：展开接口可查看 API 格式及用例列表，再展开用例查看请求数据、断言与响应证据。旧任务按已保存的协议、方法和路径分组，不补造未保存的接口示例。

用例收到 SSE 响应后会展开对应的「测试结果」，按 `data:` 逐条追加，完成后保留已记录的事件。接口测试、长连接测试、智能测试的结果区和每条用例均提供「详情」，可查看并复制 JSON。历史详情使用已保存的脱敏记录，仍受正文和事件保留上限约束。

接口测试、长连接测试和智能测试的结果区都可用「清除」清空当前显示。原结果保留在历史中，可再次打开查看。运行中的请求、连接和任务继续执行，长连接之后收到的事件会继续显示在时间线上。

三个测试页共用当前浏览器保存的 URL 前缀。点击 URL 输入框左侧可切换或添加前缀，断开链接图标表示「不使用前缀」，每条已保存前缀右侧的铅笔和 × 按钮分别用于修改和移除。右侧可补主机、端口或路径，前缀已经是有效目标地址时可留空。智能测试会在拼接后的基址上追加文档里的各接口路径。未授权发送的用例显示为「待授权」，并计入接口和任务总统计。每个接口的用例在所属接口内从 1 开始编号。

各测试页历史支持回填后重测。接口测试保存请求地址、方法、头和请求体，长连接测试保存地址、请求头、子协议和自动发送的消息。智能测试保存目标地址、执行用例快照及一份原始接口文档附件。附件保存在本机任务记录内，选中历史后点击文件名可下载。选中用例历史后可修改契约示例的请求和各条用例，再用当前全局设置重测。每次执行创建新记录，不覆盖原历史或附件。

三个测试页的「设置」与系统设置中的「工作台设置」共用当前浏览器保存的配置。超出允许范围的数值会自动调整到最近的边界并提示。各历史栏的「新建测试」清空当前输入和结果，保留历史与配置。

智能测试设置包含观察时限（默认 30,000 ms）、任务总时长（默认 3,600 s）、每接口 test 生成数量（默认 3，可选 1–10）以及「允许写接口和 WebSocket 发送消息」（前端工作台默认开启，可在设置中关闭），单次草稿最多 200 条实际用例。契约示例和文档用例不计入 test 配额。生成会去重、保留已收集的不同输入，并多轮补齐缺额；模型调用达到预算或连续无新增时，仍有缺额则报 `test_quota_unmet`，不覆盖现有草稿。接口支持删除、重新解析，以及勾选接口重新生成 test；重新解析保留已有接口的 test，仅给新增接口生成。可从历史编辑或重测，每次执行另存新历史。

上传含测试基址的接口文档时，智能测试填入完整基址并取消当前前缀选择。历史以 `task: 文档标题` 显示，缺少标题时使用模型拟定的标题，模型不可用时用接口名称。执行结果页汇总接口请求、长连接和智能测试历史，SSE 请求显示 `SSE` 标签。智能测试和执行结果页均可拖动分隔条。

| 能力 | 当前状态 |
| --- | --- |
| 项目/环境追加版本、SQLite 迁移 | 已实现；`/api` 管理 API |
| **项目源码与接口文档** | **已迁移**，智能测试的“生成接口文档”通过本机目录选择器临时分析源码，并将 OpenAPI JSON 写入所选项目 `./API/`；不再提供项目快照页 |
| 独立测试命令、本地 stdio MCP | 已实现；`doctor`、`validate`、`run`（均支持 `--engine`） |
| **执行驱动适配器（Driver / Adapter Pattern）** | **已实现可切换双引擎**；测试内容（`ExecutionPlan`）与执行技术解耦，`native` 与 `postman` 并列注册 |
| **原生 `native` 引擎（纯 Python / httpx）** | **已实现**；零外部依赖、毫秒级、跨平台，覆盖全部 5 类受控断言 |
| **Postman CLI `postman` 引擎（沙箱）** | **已实现**；受控 Collection v2.1 编译、隔离工作区、Windows Job Object 进程树管控、Newman 报告归一化 |
| 结构化计划 → Postman Collection v2.1 → JSON reporter | 已实现；只接受本项目受控编译的集合 |
| 目标限制、JSON 断言、预算上限、超时/取消、脱敏结果 | 已实现；只支持明确授权的数字回环 origin |
| **Vue 3 + TS 控制台（四工作台 / 执行结果 / 系统设置）** | **已实现**；接口测试、长连接、智能群测、大模型测试统一复用四区 `TestWorkspace` + `HistoryList` + `TaskChat`（全员窄屏防裁切与跨页 Pinia 保活），执行结果汇总 4 类页签与 AI 徽标，由 FastAPI 同源托管 |
| **大模型配置：默认开启 + 密钥三态 + 在线模型清单 + 生成模型覆写** | **已实现**；`GET/PUT /api/settings/llm`、`PATCH /api/settings/llm/model`、`POST /api/settings/llm/ping`、`POST /api/settings/llm/models` |
| **大模型能力测试工作台（`/model-testing`）** | **已实现**；多协议模型发现、并发探测（流式 TTFT、工具调用、结构化输出、输入/输出模态）、持久化历史（`model_test_runs`，零明文密钥落盘）与小Y 工具调用 |
| **页面级小Y AI 工具测试助手（`/api/assistant/*`）** | **已实现**；四工作台严格按作用域隔离工具、支持多轮冷却间隔（`cooldown_ms=500`）、单条 `AI 测试` 历史聚合、可点击 `CaseLinkBlock` 子用例只读回放、原子对话分叉与无损重连 |
| **小Y 项目功能向量知识库（`/api/knowledge/*`）** | **已实现**；内置项目功能与指南分块、OpenAI 兼容 Embedding 索引构建、向量检索与未构建时自动词法回退 |
| **系统设置页测试引擎选择器（本地原生 / Postman CLI / 导入入口）** | **已实现**；卡片式选择、实时就绪状态、`id`/`engine` 一致且顶栏随切换即时同步版本 |
| **MCP 服务详情（postmanmcp 工具清单与客户端接入配置）** | **已实现**；点击引擎卡片配置按钮以弹窗展示 |
| **HTTP/SSE 接口测试 + 后台保活与 AI 子用例历史** | **已实现**；`POST /api/console/requests(/live)`、`/api/console/runs*`、`GET/DELETE /api/console/history` 与子用例只读回放；`sse_duration_ms` 与 `timeout_ms` 边界一致 |
| **WebSocket 长连接调试 + 多轮 AI 套件** | **已实现**；仅负责 WebSocket，支持握手、子协议、逐帧双向收发、关闭码、预置消息、后台多轮 AI 套件（`/api/streams/suites/*`）与持久化子用例断言判定 |
| **计划执行 API + 双引擎有状态变量链与逐断言历史** | **已实现**；`POST /api/executions` 接受 `engine`（`native` / `postman`），双引擎一致支持 `variables` / `extract` / `depends_on` / `always_run`、无回退跳过、断言 `actual` 观测值与非敏感变量提取回显 |
| **历史记录单条删除与管理** | **已实现**；console / streams / tasks / model-testing / executions / agent 均提供单条删除（运行中返回 409 或安全收尾）；四个工作台左侧 `HistoryList` 统一仅提供单条删除，汇总页 `/history` 保留四类管理入口 |
| CRM 阶段 2 参数校验、鉴权只读、有状态 CRUD 链与 SSE/WS | 已完成双引擎冒烟、鉴权只读、`docker-stateful-crud.json`、SSE 与 WS `/api/ws/chat` 实测（外部 CRM/RAG 目标自身缺陷见 `examples/crm/README.md`） |
| 契约/接口描述 → AI 用例生成 | 已实现；生成受控 `ExecutionPlan` 与期望依据，通过本地范围校验 |
| 断言结果 → AI 诊断与自适应补测 | 已实现；证据引用校验、预算/单因素/去重/写操作重放约束 |
| 后台 AI 闭环与 Markdown 诊断报告 | 已实现；`/api/agent/runs`，取消、重启中断、部分报告与脱敏 JSON 快照 |
| **文档驱动用例任务（`/testcases`）** | **已实现**；上传并识别 OpenAPI/Swagger 或非标准文本，在接口下审核和编辑 AI 生成用例，支持有状态变量链、内存临时敏感头直接重测与 `runtime_headers` 覆盖（落库始终 `[REDACTED]`），实时显示 SSE 和总结，支持历史文档下载与 Markdown 导出 |
| **执行计划溯源与断言证据结构** | **已实现**；`executions.plan` / `plan_sha256` 冻结脱敏计划，双引擎断言结果均携带 `pointer`/`actual`/`actual_present`/`actual_type` |
| **历史记录统一脱敏（线性时间无 ReDoS）** | **已实现**；写入/启动迁移/读取三处共用 `infrastructure/history.py` 与线性时间 `privacy.py`，覆盖 `token` 头、请求体密码、响应 JWT 与超大非邮箱载荷 |
| 完整确定性 OpenAPI IR、自动数据库断言 | 后续阶段；当前直接将契约对象交模型理解，不请求外部 `$ref` |

既有 AI 闭环通过 `/api/agent/runs`，确定性计划仍通过 `/api/executions` 或独立 CLI/MCP 执行。文档任务复用 HTTP/SSE 与 WebSocket 执行引擎，通过 `GET /api/tasks/{id}/live` 逐步把执行进度、SSE 事件和 AI 总结传给工作台。对应接口和总统计显示等待响应或等待流结束，断言完成后更新结果计数。AI 总结通过已配置模型的 Chat Completions SSE 逐段生成，在工作台下方独立分区展示，可拖动分隔线调整高度。

**前端控制台**：Vue 3 + TypeScript + Vite；「单点测试」包含接口测试和长连接测试，「智能群测」是独立分组。SSE 在接口测试或智能群测中执行，长连接页仅调试 WebSocket。Iconify 图标数据本地打包，不请求远端图标服务。

控制台提供石墨暗色与雾白亮色，首次打开采用暗色，并可在「系统设置 → 外观与配色」按主题自定义各颜色令牌或恢复默认。两套主题均以靛蓝标记主要操作和当前交互，HTTP 方法与执行结果各有专用语义色。侧边栏底部可切换主题和折叠导航，偏好保存在当前浏览器，刷新后恢复。窄屏的 HTTP 工作台优先保留请求编辑器，仍可从侧栏进入执行结果历史。

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
| `LLM_MODE` | 环境模式 `disabled/mock/openai`（默认 `openai`，即大模型服务默认开启；未配置 `base_url` 时仍保持本地未就绪不联网）；AI API 优先使用设置页保存的配置，无数据库配置时回落到环境配置；不将 mock 冒充真实调用 |
| `LLM_API_KEY` | 环境模式下的模型密钥；设置页配置使用既有 SQLite 设置表。均不转发给目标执行器 |
| `TARGET_ALLOWED_ORIGINS` | JSON 数组，默认 `[]`（允许任意合法 HTTP/HTTPS/WS 目标；填入如 `["http://127.0.0.1:8088"]` 时启用显式白名单限制） |
| `TARGET_AUTH_ENV_VARS` | JSON 数组，仅列出允许计划引用的目标鉴权环境变量名称，默认空 |
| `TRUST_SYSTEM_PROXY` | 布尔值，默认 `false`（不读取系统/环境代理；需要走本地代理访问外部目标或模型时显式设为 `true`） |
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
#    http://127.0.0.1:8010/long-connection → 长连接测试（WebSocket）
#    http://127.0.0.1:8010/testcases       → 智能群测
#    http://127.0.0.1:8010/model-testing   → 大模型测试
#    http://127.0.0.1:8010/history         → 执行结果（四类汇总）
#    http://127.0.0.1:8010/settings        → 系统设置（模型 / 引擎 / 知识库 / 外观）
```

### 开发模式（可选）

```powershell
cd frontend
npm run dev   # vite dev server，端口 5180，/api 与 /health 代理到 8010
```

若当前版本后端运行在其他端口，在启动 Vite 前设置 `$env:API_AGENT_DEV_BACKEND = 'http://127.0.0.1:<PORT>'`（替换 `<PORT>`）。同一份前端构建会被已运行的 FastAPI 服务直接提供；如果对话接口返回 405，请确认访问的是新版后端，不只是新版前端。

`npm run build` 已包含 `vue-tsc --noEmit` 类型检查；`npm run typecheck` 可单独跑类型。

在「系统设置 → 工作台设置」中可重置当前站点的本地偏好和应用缓存；此操作保留大模型配置、执行历史和小Y对话。

### 页面与对应后端能力

| 页面 | 路径 | 调用的管理接口 |
| --- | --- | --- |
| 接口测试 | `/request` | `POST /api/console/requests(/live)`、`/api/console/runs*`、`GET/DELETE /api/console/history(/{id}(/cases/{case_id}))`、`/api/assistant/*`、`PATCH /api/settings/llm/model` |
| 长连接测试 | `/long-connection` | `POST/GET/DELETE /api/streams/*`、`GET /api/streams/active`、`/api/streams/suites/{id}/{live,cancel}`、`/{id}/inputs`、`/{id}/cases*`、`/api/assistant/*` |
| 智能群测 | `/testcases` | `POST /api/tasks/generate-document`（本机目录选择器）、`/api/tasks/recognize`、`/api/tasks/regenerate`、`POST/GET /api/tasks`、`GET/DELETE /api/tasks/{id}`、`/{id}/{live,document,rerun,cancel,report}`、`/api/assistant/*` |
| 大模型测试 | `/model-testing` | `POST /api/model-testing/{discover,probe,probe/stream}`、`POST/GET/DELETE /api/model-testing/runs*`、`/api/assistant/*` |
| 执行结果 | `/history` | 接口请求、长连接会话、智能群测与大模型测试四类历史汇总、筛选、AI 徽标展示与删除（旧入口 `/executions` 重定向至此页） |
| 系统设置 | `/settings` | `GET/PUT /api/settings/llm`、`PATCH /api/settings/llm/model`、`POST /api/settings/llm/{ping,models}`、`GET /api/executions/engine/{list,status}`、`/api/knowledge/*` |

四个测试页左侧历史栏共用同一个 `HistoryList` 组件：条目以 `HistoryEntry[]` 传入，单条删除由 `onDelete` 启用；当前四个测试页均仅保留单条删除按钮（不再在测试工作台历史栏顶部展示总删除按钮），样式与 `@click.stop` 冒泡防护收敛在组件内。删除按钮**常驻**并以低不透明度弱化，悬浮整行时提亮；不能用 `display: none` 切换——按钮高 18px 高于文字行高约 15.3px，显隐会让条目在悬浮时跳动 2.7px。

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

- **默认开启**：新部署或空库环境下大模型服务默认开启（`enabled=true`、`LLM_MODE=openai`），仅在填写有效 `Base URL` 后进入就绪状态；可在设置页或通过 `LLM_MODE=disabled` 显式停用。
- **API Key**：可写入新密钥覆盖保存，也可点「清空密钥」显式清除（后端收到空字符串才置空；`None` 仅对同源地址保留原密钥，跨源变更自动清除旧密钥）。
- **模型列表**：点「获取模型列表」调用 `POST /api/settings/llm/models`，后端向 `{base_url}/models` 发起 `GET` 并解析 `data[].id`，界面在单控件 `ModelPicker` 中切换为下拉框供点选。
- **按用途选模型**：「小Y 对话模型」用于 AI 对话和 AI 总结，在小Y对话框通过 `PATCH /api/settings/llm/model` 切换时也会同步更新。接口文档识别与生成、接口 test 生成默认跟随小Y；只在系统设置中开启「自定义模型」后，才可以单独选择生成模型（对应数据库 `generation_model` 列）。两者共用服务地址与密钥，点击「保存模型配置」后生效；前端会在提交前校验超时时间（`1~600` 秒）并在管理接口返回 422 时直接展示脱敏字段详情。
- **预览态**：`base_url` 与 `api_key` 可在未保存时先用于拉取模型与连通性测试，避免“先存错再改”。

### 智能测试接口文档

1. 在系统设置中保存模型服务地址、模型名及所需凭据。
2. 打开 `/testcases`，在接口文档区域点击“生成接口文档”，后端会打开本机目录选择器。
3. 选择项目目录后，源码只在内存中脱敏、分批分析；生成结果保存到所选项目根目录的 `API/openapi<时间>.json`，不会写入项目快照或项目数据库。
4. 不超过 512 KiB 的生成文档会自动导入。支持重新解析、勾选接口生成 test、编辑请求并开始测试。远程访问或非本机浏览器会被拒绝。

旧项目源码快照不再写入，存量工件与只读下载接口保留。AI 总结在执行结果下方流式显示。三种测试页底部的小T / 小Y 助手独立滚动，正在测试或生成总结时也可提问；输入框支持 **`Enter` 直接发送、`Shift + Enter` 换行**（中文输入法候选上屏期间不误触），且具备**底部锚点跟进**——发送前输出区停留在最后一行（或尚无输出）时，每行流式输出自动跟进到底部，用户主动上滚离开末行或发送前未停在末行时不强制拉回。输入框右侧切换的模型与系统设置同步。只有实际执行的测试才生成历史。未关联测试的普通对话仅保留在当前页面，不保存，也不带入后续测试；已有测试下的对话随对应记录保存在当前浏览器。旧「仅 AI 对话」记录不再显示，不自动删除本地数据。回看历史时未提交的表单和文档修改不会覆盖原始快照。

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
  migrations/                    # Alembic 迁移（0001 初始表 ～ 0008 AI 工具测试与历史，共 12 张业务表）
  src/api_agent/
    main.py, cli.py
    infrastructure/              # 配置、日志脱敏、API 公共契约、ORM、数据库、结果分类、线性时间隐私脱敏、目标策略、有界 HTTP
    projects/                    # 项目/环境版本、只读历史文档接口、可复用的源码扫描与 OpenAPI/Markdown 渲染
    execution/                   # 计划、engine(协议)、native(原生驱动)、postman(CLI驱动)、checks、variables、Collection、报告、路由
    console/                     # 手动 HTTP/SSE 请求、后台运行器（runner）、仓库、小Y 工具与子用例只读回放
    streams/                     # WebSocket 调试与多轮 AI 套件：schemas、state、repository、service、runner、tools、transport、路由
    tasks/                       # 智能群测任务：文档识别、test 生成、有状态依赖链规划与执行、本机目录文档生成、小Y 工具、报告导出
    llm/                         # 动态设置（含 generation_model）、客户端传输、多协议模型能力探测与持久化（testing*）、小Y 工具、路由
    assistant/                   # 页面级隔离小Y 工具注册表、后台多跳运行器（runner）、持久化线程存储与 /api/assistant 路由
    knowledge/                   # 小Y 项目功能语料、Embedding 配置、本地向量索引存储与检索路由
    agent/                       # AI 闭环：契约生成、证据诊断、自适应补测、Markdown 报告
    tools/                       # stdio MCP 服务（api-agent-mcp）与独立客户端
    static/                      # 离线 Swagger UI 资源（js/css/图标）与上游 LICENSE/NOTICE
  tests/                         # 单元测试、四工作台 AI 工具回归、CRM 审核回归、agent/tasks 闭环与真实 MCP/CLI 集成验证
frontend/
  package.json, vite.config.ts, tsconfig.json
  src/
    api/                         # 类型化 fetch 封装（client.ts）与各领域接口函数（endpoints.ts / tasks.ts / projectDocuments.ts）
    components/                  # TestWorkspace、HistoryList、TaskChat、TaskDraft、TaskResultTree、KnowledgeBaseSettings、ModelPicker、UrlPrefixPicker、ThemePaletteSettings、ResizableSplit 等
    composables/                 # 共享轮询生命周期（usePolling）与请求头表单（useHeaderRows）
    lib/                         # 外观主题、配色令牌、测试设置（含 cooldownMs）、工作台布局与重置、URL 前缀、助手上下文、结果分类、本地打包图标
    views/                       # 接口测试 / 长连接测试 / 智能群测 / 大模型测试 / 执行结果 / 系统设置 / 404（含旧入口兼容重定向）
    styles/                      # 暗/亮双主题设计令牌（tokens.css）与基础样式（base.css）
    stores/                      # Pinia（consoleWorkbench / streamWorkbench / tasksWorkbench / modelTestingWorkbench / workbenchRuns / toasts）
  tests/                         # Node 原生行为与回归测试（157 项）
  dist/                          # 生产构建产物（由 FastAPI 同源挂载，不入库）
examples/crm/                    # 源码映射的非秘密测试计划与说明（含 docker-stateful-crud.json）
examples/agent/                  # AI 闭环请求样例（register-run.json）
examples/stream_target.py        # 本地 SSE/WebSocket 测试目标（9100）
辅助脚本/
  install-postman.ps1             # 项目内官方 CLI 安装与完整性验证
  prepare-crm.ps1                 # 原 CRM 只读，生成隔离构建描述
  verify-ai-agent.py              # AI 闭环回归与可选真实模型联调
```

`.cache/`、`.python/`、`.env`、虚拟环境、临时原始证据和 `data/` 均为本机运行资料。CRM 源码不迁入本项目；隔离测试包可能包含目标原配置，只留在私有 `.cache/`，不发布。

## 验证与限制

最近复核基线（2026-11-27）：
- 前端 `frontend/`：`npm test`（`node --test tests/*.test.cjs`）**157 项全部通过**，`npm run typecheck`（`vue-tsc --noEmit`）0 错误，`npm run build`（`vue-tsc --noEmit && vite build`）构建成功；
- 后端 `backend/`：`pytest -q` **566 passed, 1 skipped**（1 项 `tests/projects/test_source.py:97` 因 Windows 符号链接 OS 权限跳过），`ruff check src tests migrations` 全部通过、`ruff format --check src tests migrations` **177 个文件**全部格式化就绪，`uv lock --check --offline`（109 个锁定包）通过；
- 本轮已通过自动化回归覆盖 CRM 全量测试审核缺陷 A.1–A.7（`tests/test_crm_audit_regressions.py` + `frontend/tests/layout-topbar-regressions.test.cjs`）以及四工作台页面级小Y 工具测试与历史回放（含真实本地回环服务与真实 Postman CLI 1.82.0 单进程有状态依赖链执行）。

历史端到端实测基线（2026-11-22）：本地 `examples/stream_target.py` + 独立临时服务 + CLI/MCP 覆盖 `/health`、`/docs`、SPA 与 `/api` 404 JSON、HTTP/SSE、WebSocket、项目环境乐观锁、双引擎（`native` + `postman`）、大模型切换与智能群测，**20/20 项全部通过**。

常用验证命令（在 `backend/` 目录运行）：

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
