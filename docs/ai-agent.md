# AI 编排使用说明

2026-11-16：文档驱动任务入口位于 `/testcases`，任务 API 位于 `backend/src/api_agent/tasks/`；既有 `/api/agent/runs` 闭环仍独立可用。

## 四条路径怎么选

| 场景 | 用哪个页面 | 说明 |
| --- | --- | --- |
| 测单条 HTTP 或 SSE 接口 | `/request` 接口测试 | SSE 有界观察在响应中展示事件 |
| 调试 WebSocket 双向消息 | `/long-connection` 长连接测试 | 仅 WebSocket：连接、收发帧、关闭码与历史 |
| 从 API 文档生成任务用例 | `/testcases` 任务测试 | 单一自动流程：上传、识别、保留原有用例、生成补充用例、执行 HTTP/SSE/WebSocket、AI 总结与导出 |
| 使用原多轮 AI 补测闭环 | `/api/agent/runs` | 仍独立可用，不是 `/testcases` 当前生成器 |

单接口不并入 AI 闭环的原因：没有多轮补测语义，且通常没有契约依据，交由模型推断会使全部期望降级为 `inferred`。

## 开始使用

1. 按根 README 启动后端，在系统设置页配置并启用 OpenAI 兼容服务。优先使用数据库设置；只有未创建数据库配置记录时才回落到环境配置。已保存但禁用的配置不会被环境变量悄悄覆盖。
2. 将被测服务的数字回环 origin 加入 `TARGET_ALLOWED_ORIGINS`，例如 `["http://127.0.0.1:8088"]`。模型服务的远端 Base URL 与被测 origin 是两个不同概念。
3. 打开 `/testcases`，上传 `.json`、`.yaml`、`.yml` 或 UTF-8 文本（上限 512 KiB）。标准 OpenAPI/Swagger 文档在本地识别，非标准文本由模型结构化识别。填写并确认目标 origin；文档服务器 URL 不自动作为目标。写请求与 WebSocket 主动发送默认跳过，显式勾选后执行。任务历史与逐用例证据持久化，可取消并下载 Markdown 总结。

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

侧边栏将入口分为“接口管理”（接口测试、长连接测试）和“任务测试”（用例测试），其后为项目区域。长连接测试仅负责 WebSocket 调试；SSE 由接口测试或文档任务用例测试。

`/testcases` 文档任务与 `/api/agent/runs` 多轮 AI 闭环相互独立：上传 API 文档后，标准 OpenAPI/Swagger JSON/YAML 由本地识别，非标准 UTF-8 文本交由已配置模型结构化识别；识别结果保留原有用例，由内置生成器补充操作基线及部分参数边界用例，再按序复用 HTTP/SSE 与 WebSocket 执行类，最后由 LLM 汇总证据。任务结果持久化并可导出 Markdown。当前不集成 Schemathesis/RESTler；完整 schema mutation、状态序列和跨接口依赖仍未实现。图标资源使用本地打包的 Iconify 图标数据，不依赖运行时远端服务。

模型客户端的 HTTP 传输和响应解析分别由 `llm/client.py`、`llm/response.py` 负责。畸形结构、非法 Unicode 或工具参数中的非有限数字会转为 `LLMResponseError`，`/api/llm/chat` 返回 502；错误文本不包含供应方原始响应。填写完整 `/chat/completions` 地址也可正确查询同一服务的 `/models`。
