# AI 编排与智能测试使用说明

2026-11-27：全站四个测试工作台（`/request` 接口测试、`/long-connection` 长连接测试、`/testcases` 智能群测、`/model-testing` 大模型测试）均已接入页面级隔离的 **小Y 工具调用 AI 助手**（`/api/assistant/*`）与后台持久化多轮测试运行器，支持冷却间隔（`cooldown_ms`，默认 `500 ms`）、单条带 `AI 测试` 标注的历史聚合、可点击 `CaseLinkBlock` 子用例只读回放、切页后台保活与自动重连、原子对话分叉以及向量知识库检索（`/api/knowledge/*`）。既有 `/api/agent/runs` 多轮补测闭环与 `/testcases` 文档任务继续独立可用。大模型服务默认开启（`LLM_MODE=openai`、`enabled=true`），支持按用途分别配置「小Y 对话模型」与可选「接口文档与 test 生成模型」。

## 五条路径怎么选

| 场景 | 用哪个页面 / 接口 | 说明 |
| --- | --- | --- |
| 测单条 HTTP / SSE 接口或让小Y 发起多轮单接口测试 | `/request` 接口测试 | 支持实时 NDJSON SSE 观察、后台运行不断连、小Y 工具调用（`console` 作用域）与多轮子用例只读回放 |
| 调试 WebSocket 双向消息或让小Y 运行多轮 WS 套件 | `/long-connection` 长连接测试 | 仅 WebSocket：双向收发、关闭码、事件回放、小Y 工具调用（`websocket` 作用域）与持久化断言判定 |
| 从 API 文档生成并执行批量测试任务 | `/testcases` 智能群测 | 支持本机目录生成 OpenAPI 文档、上传识别、有状态变量提取与依赖链（`extract`/`depends_on`/`always_run`）、执行 HTTP/SSE/WebSocket、AI 总结、小Y 工具调用（`tasks` 作用域）与 Markdown 导出 |
| 探测外部大模型供应商接口能力与模态 | `/model-testing` 大模型测试 | 支持多协议探测（流式 TTFT、工具调用、结构化输出、输入/输出模态）、持久化历史（绝不落库明文 `api_key`）、小Y 工具调用（`model_testing` 作用域） |
| 使用原多轮 AI 补测闭环 | `/api/agent/runs` | 仍独立可用，适用于基于契约的生成 → 执行 → 诊断 → 单因素补测闭环 |

## 页面级小Y 工具测试架构（`/api/assistant/*`）

四个测试工作台底部的 `小Y` 助手统一接入 `AssistantRunManager` 与 `AssistantToolRegistry`，具备以下安全与交互约束：

1. **严格页面作用域隔离**：四个页面分别绑定 `scope = "console" | "websocket" | "tasks" | "model_testing"`，每个作用域只能调用自身对应的测试工具与只读知识库查询，禁止跨页调用其他工作台工具。
2. **后台多跳运行与无损重连**：`POST /api/assistant/runs` 返回 `202` 与 `Location`，前端通过 `GET /api/assistant/runs/{run_id}/live` 订阅 NDJSON 帧；切换页面或刷新时客户端断流**绝不取消**后台测试，返回页面后通过 `GET /api/assistant/runs/active` 与各工作台 `/active` 接口自动重连，仅显式点击停止才调用 `POST /api/assistant/runs/{run_id}/cancel`。
3. **冷却间隔与单条标注历史**：多轮 AI 测试统一遵守工作台设置的 `cooldown_ms`（默认 `500 ms`，范围 `0–60000 ms`），整场多轮测试仅生成**单条**带有 `AI 测试` 徽标与轮次标记的历史记录，并在内部持久化有序子用例。
4. **可点击 `CaseLinkBlock` 只读回放与原子分叉**：小Y 回复中内嵌可点击的用例卡片（`CaseLinkBlock`），点击即载入该子用例的完整脱敏快照并锁定工作台表单为只读；点击「再次测试」解锁表单并递增草稿版本开启新会话；当用户在已有历史线程里发起新测试并触发 `run_bound` 时，服务端原子分叉出独立的新线程绑定到新历史 ID，旧历史对话不受污染。
5. **零明文密钥与向量知识库**：大模型测试仅在请求内存中使用临时凭据（`ephemeral_api_key`），数据库与助手消息均自动脱敏、不落库明文 `api_key`；小Y 可通过 `/api/knowledge/*` 检索项目功能与使用指南知识库（已构建向量索引时走向量检索，未构建时自动词法回退）。

## 开始使用

1. 按根 README 启动后端（端口 `8010`）。大模型服务默认开启；在「系统设置」页填写 OpenAI 兼容服务的 `Base URL`、模型名及所需密钥后保存即可生效。数据库配置优先于环境配置；只有未创建数据库配置记录时才回落到环境配置，已保存但显式停用的配置不会被环境变量覆盖。
   - **双模型分工**：「小Y 对话模型」（`model`）用于 AI 对话、工具调度与 AI 总结，并可在小Y 输入框右侧经 `PATCH /api/settings/llm/model` 快捷切换；「接口文档与 test 生成模型」（`generation_model`）默认跟随小Y，开启「自定义模型」后可独立指定。
2. 如需限制可测目标范围，可将目标 origin 加入 `TARGET_ALLOWED_ORIGINS`（留空 `[]` 表示允许任意合法目标地址，可按需配置显式白名单）；模型服务的远端 Base URL 与被测 origin 是两个不同概念。
3. 打开 `/testcases`，可上传 `.json`、`.yaml`、`.yml` 或 UTF-8 文本（上限 512 KiB），也可点击「生成接口文档」（`POST /api/tasks/generate-document`，仅限本机浏览器）通过本机目录选择器临时扫描源码并输出 `API/openapi<时间>.json`。标准 OpenAPI/Swagger 文档在本地识别，非标准文本由模型结构化识别。填写并确认目标 origin；文档服务器 URL 可预填但仍须人工确认后开始执行。写请求与 WebSocket 主动发送在后端 `StartTask` 中默认关闭、需显式传 `allow_writes=true`（前端工作台首次默认勾选开启，可在设置中关闭）。静态 Token 任务历史支持通过内存临时凭据缓存直接重测，也可在 `/api/tasks/{id}/rerun` 中传入 `runtime_headers` 覆盖请求头（落库快照始终保持 `[REDACTED]`）。任务历史、文档附件与逐用例证据持久化，可编辑重测、取消并下载 Markdown 总结。
4. 四个测试工作台底部均提供小Y 助手（持久化线程 `/api/assistant/threads/{scope}/{history_id}`，兼容保留 `POST /api/tasks/chat`），支持 `Enter` 直接发送、`Shift + Enter` 换行（输入法组合态不误触）；发送前若输出区停留在最后一行（或尚无输出），后续流式输出自动跟进到底部，若用户主动向上翻看则停止拉回。

有效模型配置可通过 `/api/llm/status` 核验，数据库配置优先于环境配置。文档识别的输出预算为 16,000 token，其他结构化调用从 6,000 token 开始。模型以 `length` 结束时，下次修复调用将输出预算翻倍，最高 32,000 token，仍计入原有调用次数和时间限制。推理模型的思考 token 也可能占用该预算。

SSE 用例支持 GET，以及携带 JSON 请求体的 POST 等 HTTP 请求方式。POST SSE 仍受任务的写请求许可开关控制。GET、HEAD、OPTIONS 用例不能携带请求体。多目标文档需要按 origin 分别创建任务，目标鉴权凭据由本地执行者补充。

仅允许数字回环 IP 和配置白名单。路径必须是文档中的相对路径；拒绝绝对 URL 和网络路径引用。标准文档生成器基于操作定义、参数样例与成功状态基线生成一条用例/操作，并保留 `x-test-cases` 与扩展测试用例；没有样例语义时不伪造业务预期。复杂数据生成、状态相关流程和外部 `$ref` 解析未实现，报告中明确提示限制。

示例见 `examples/agent/register-run.json`。示例假设自己在 9100 端口提供了 `/register`，不会启动或更改实际业务系统。

```powershell
$run = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8010/api/agent/runs' `
  -ContentType 'application/json' -InFile 'examples/agent/register-run.json'
Invoke-RestMethod -Uri "http://127.0.0.1:8010/api/agent/runs/$($run.id)"
Invoke-WebRequest -Uri "http://127.0.0.1:8010/api/agent/runs/$($run.id)/report" `
  -OutFile 'agent-report.md'
```

创建返回 `202` 和 `Location`；任务由应用持有，与创建它的 HTTP 请求无关。读取详情轮询 `status`；终态包括 `completed/error/timed_out/cancelled/interrupted`。`completed` 表示编排结束，可以包含失败断言，不表示目标没有问题。

## 四项能力的接口

| 操作 | 接口 | 结果 |
| --- | --- | --- |
| 契约生成 | `POST /api/agent/generate` | `{plan, intents, notes}`；`plan` 可直接传给现有 `validate_plan`、`run_plan` 或 `/api/executions` |
| 根因诊断 | `POST /api/agent/diagnose` | 输入 `{contract, plan, result, round_index?}`，输出确定性 `evidence` 与 AI `analysis` |
| 自适应决策 | `POST /api/agent/adapt` | 输入 `{contract, plan, diagnosis, remaining_requests, previous_requests?, allow_mutating_followups?, round_index?}`，输出继续标志、下一轮计划、变异说明及拒绝原因 |
| 完整闭环 | `POST /api/agent/runs` | 在同一预算内生成 → 执行 → 断言 → 诊断 → 补测 → 报告概述 |
| 详情与报告 | `GET /api/agent/runs/{id}`、`GET /api/agent/runs/{id}/report` | 完整脱敏证据 JSON 和 Markdown；运行中或异常结束也可读取部分报告 |
| 取消 | `POST /api/agent/runs/{id}/cancel` | 等待在途执行收尾并保存部分报告，不开始后续补测 |

生成接口和闭环中的 `max_cases` 默认 8，范围 1–50。每个生成/诊断/决策输出最多一次格式修复，修复计模型预算。独立能力接口每次最多两次模型调用，且不会执行目标请求；完整闭环共享全程预算。

## 模型与代码各自负责什么

模型负责理解字段含义、提出有价值的用例、解释差异及选择补测维度。模型输出是受限 JSON 数据，不执行模型给出的工具、代码或脚本。生成的每条用例都有 category、rationale 和 basis；`basis=contract` 需逐字引用契约，`inferred` 表示期望来自推断，需要人工核对。逐字引用存在只证明来源文本存在，不是语义证明。

代码固定执行 origin、方法、路径模板、凭据引用及预算；生成的请求还必须经过 `ExecutionService.parse`。诊断中 `r0:case-id:a0` 表示第 0 轮、指定用例、第 0 条断言；引用不存在的证据会被拒绝。根因始终作为假设展示，报告的“已证实”仅指实际 HTTP/断言观察，不把模型期望差异直接升级为已确认业务缺陷。

补测使用 `parent_id + hypothesis_id + location + pointer + value`，代码从父用例复制并修改一个 body/query 标量或一个路径段，模型另给出期望断言。禁止把一整个对象或数组替换作为“单因素”；支持删除单个标量必填字段。HTTP 请求语义相同而只有 ID/断言不同的候选也会去重。修改类操作默认不补测；`allow_mutating_followups=true` 表示显式允许本次运行自动再次发送 POST/PUT/PATCH/DELETE，操作者应只在可重置或无状态目标使用。当前不实现多步数据清理、事务回滚和数据库验收。

现有 `ExecutionPlan.auth_ref` 作用于整份计划，带 token 与不带 token 的用例需分开生成/执行；不会让模型在请求中直接填 Authorization。公开注册接口未声明鉴权时不会强制要求生成 401。

## 预算、持久化与退化

默认 `limits`：`target_requests=16`、`model_calls=8`、`max_rounds=3`、`wall_time_seconds=120`；上限分别为 100、30、10、600。应用最多同时运行 4 个 AI 任务，超过返回 429。

每次模型调用、每轮 HTTP 派发前均把预留写入本地 JSON，再发送请求。HTTP 预算保守按整轮计划占用，实际观察到的尝试数与未知数另外报告；不能将消耗上界说成真实发包数。模型失败也占用一次调用，不隐式重试网络请求。报告概述本身消耗模型预算；余额不足时仍能生成确定性 Markdown 报告。

达到请求/轮次/时间上限、没有有效补测、没有假设或连续两轮未发现新断言/状态异常特征时停止。模型超时、鉴权失败、输出拒绝和预算不足明确记录，已有 HTTP/断言证据保留；模型失败不会把未知结果变成通过。

记录保存在 `ARTIFACT_DIR/agent/{UUID}.json`，以临时文件 + 原子替换保存。输入快照先脱敏再计算 SHA-256，之后不再修改；每轮执行计划追加保存。应用重启将 queued/running 记录标记为 interrupted，在途额度保守计消耗/未知，不自动重发。

模型上下文与记录按已知凭据、敏感字段、手机号/邮箱规则脱敏；密码/token 属性的 schema 约束保留，秘密 example/default/enum 等值遮蔽。目标响应正文/头不发给模型、不写入 AI 证据文件；诊断使用状态、错误类别和已脱敏断言差异。脱敏不是任意个人数据识别器，不应提交含无关私人数据的契约。快照含脱敏值，不是可直接重放的原始请求副本。

## 验证入口与当前边界

```powershell
# 不使用外部供应商的回归测试
Set-Location backend
& .venv/Scripts/python.exe -m pytest tests/agent -q

# 从项目根目录明确选择真实模型联调（使用现有设置，最多 8 次调用）
& backend/.venv/Scripts/python.exe 辅助脚本/verify-ai-agent.py --live
```

辅助脚本创建自有无状态注册校验接口，刻意让 12 位手机号返回 500；不会创建用户、接触 CRM 或修改目标授权配置。2026-11-02 实测 `deepseek-v4.1-flash`：生成/诊断/补测/诊断/报告五次调用均接受，两轮共 7 个请求，初轮状态 `[200,500,400,400,400]`，补测 `[400,400]`；按两轮上限停止。token 用量与实际模型名称保存在本地证据，未配置可靠价格，费用未知。供应商嵌套 token details 已兼容，归一化保留整数计数。

侧边栏将入口分为「单点测试」（接口测试、长连接测试）、「智能群测」（`/testcases`）、「大模型测试」（`/model-testing`）、「记录」（执行结果，含四类页签）和「设置」（系统设置）；原项目快照页已迁出并重定向至 `/testcases`。长连接测试仅负责 WebSocket 调试；SSE 由接口测试或智能群测任务执行。

`/testcases` 文档任务与 `/api/agent/runs` 多轮 AI 闭环相互独立：上传 API 文档后，标准 OpenAPI/Swagger JSON/YAML 由本地识别，非标准 UTF-8 文本交由已配置模型结构化识别；识别结果保留原有用例，由内置生成器补充操作基线及部分参数边界用例，并支持受控多步骤变量提取与依赖链（`variables` / `extract` / `depends_on` / `always_run`），再按序复用 HTTP/SSE 与 WebSocket 执行类，最后由 LLM 汇总证据。任务结果持久化并可导出 Markdown。当前不集成 Schemathesis/RESTler；完整确定性 OpenAPI IR 与自动数据库断言仍未实现。图标资源使用本地打包的 Iconify 图标数据，不依赖运行时远端服务。

模型客户端的 HTTP 传输和响应解析分别由 `llm/client.py`、`llm/response.py` 负责。畸形结构、非法 Unicode 或工具参数中的非有限数字会转为 `LLMResponseError`，`/api/llm/chat` 返回 502；错误文本不包含供应方原始响应。填写完整 `/chat/completions` 地址也可正确查询同一服务的 `/models`。
