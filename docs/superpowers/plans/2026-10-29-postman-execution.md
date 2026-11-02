# Postman 执行基座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 交付独立命令入口经真实 MCP 调用 Postman CLI 的受控 API 测试基座，并提供 CRM 阶段 2 样例。

**Architecture:** 保留阶段 1 后端；execution 管理输入校验、Collection 编译、子进程和归一化结果，tools 提供 stdio MCP 服务与独立客户端。模型、自适应、SSE、短链和前端不在本次代码范围。

**Tech Stack:** Python 3.12、Pydantic 2、官方 MCP Python SDK、Postman CLI、pytest、uv。

**Spec:** [项目最终架构.md 第 14 节](../../../项目最终架构.md)、[开发实施契约.md 第 14 节](../../../开发实施契约.md)。用户已确认设计及代码执行；采用当前会话直接实施。

## Global Constraints

- 首步仅受本机允许 origin 控制的回环 HTTP(S) 目标；禁止重定向、任意脚本、任意 CLI 参数和云端资源。
- Collection v2.1、单迭代、无隐藏发送；请求/集合/宿主超时及取消均有边界。
- 本机凭据只用环境变量引用，不进 MCP payload、命令行或归一化结果；原始报告临时保存并清理。
- 当前目录不是 Git 仓库，直接更新用户指定目录；不初始化 Git、不创建工作树、不提交/推送。
- 保留 simulation API；本次独立执行不伪造持久任务或模型调用。
- 全部异步/回环通信验证需在允许本机网络的执行环境运行，沙箱内 asyncio socketpair 已证实阻塞。

## Review Focus

- 编码后的路径、大小写/重复 Header 或 Host 覆盖不得改变实际目标。
- 断言预期和目标错误文本不得借 JavaScript/报告内容泄漏秘密或引入额外请求。
- 非零退出但报告有效应归断言失败；空报告、缺断言、缺请求不能报通过。
- MCP 取消/CLI 总超时必须回收进程，临时原始证据不能遗留。
- CRM 的 HTTP 200 业务错误、自定义 token 和软删除必须独立表达，SSE 不冒充 JSON 支持。

### Task 1: 计划、范围和 Collection 编译

**Files:** Create `backend/src/api_agent/execution/{__init__,schemas,collection}.py`; tests `backend/tests/execution/test_plan.py`。

**Interfaces:** `ExecutionPlan`、`RunResult`；`validate_scope(plan, allowed_origins)`；`compile_collection(plan, secrets)` 输出本地 Collection 字典。

- [x] 写失败测试：合法 JSON/查询参数与固定断言可编译；越界 origin、非回环、URL/路径穿越、Host、脚本字段、预算不足、无断言均拒绝。
- [x] 运行测试确认缺模块失败，再实现类型、范围检查和数据编码生成模板。
- [x] 验证测试通过，记录 Task 1 结果。

### Task 2: CLI 执行与证据

**Files:** Create `execution/{postman,report,service}.py`; Modify `infrastructure/config.py`、`.env.example`；tests `tests/execution/{test_report,test_postman}.py`。

**Interfaces:** `PostmanRunner.status()`、`PostmanRunner.run(plan, secrets)`；`ExecutionService.validate/run` 使用 Task 1 类型。

- [x] 写失败测试：Newman 结构通过/断言失败/网络错误/缺项/缺断言/无报告；脱敏输出无原文。
- [x] 实现受控 CLI 路径、临时工作区/最小环境、固定参数、整体超时/取消进程树和报告白名单归一化。
- [x] 用真实 CLI + 受控回环服务验证正负路径、超时/取消、重定向和秘密不外泄；静态 parser 测试与真实验证分别记录。

### Task 3: MCP 服务与独立命令入口

**Files:** Create `tools/{__init__,server,client}.py`、`cli.py`；Modify `backend/pyproject.toml`、`uv.lock`；tests `tests/tools/test_mcp.py`。

**Interfaces:** 三个固定工具 `postman_status/validate_plan/run_plan`；命令 `api-agent doctor/validate/run` 通过官方 SDK stdio 调用服务端。

- [x] 写失败测试：真实初始化及列工具、校验拒绝、run 调用返回结构、无凭据错误详情；命令行退出码区分通过/失败/工具错误。
- [x] 安装并锁定 SDK，接通类型化工具与命令入口，拒绝任意路径/CLI 参数工具输入。
- [x] 验证真实 MCP → CLI → 受控 HTTP 全链路和子进程退出。

### Task 4: CRM 样例、真实联调及文档

**Files:** Create `examples/crm/{README.md,smoke.json,authenticated.json}`；Update 根目录五份现有文档及本计划。

**Interfaces:** 样例符合 Task 1 计划类型；只包含非秘密固定预期和凭据引用。

- [x] 样例校验覆盖注册/登录参数校验、token 鉴权、路径/查询；JSON/SSE、账号残留和软删除限制明确。
- [x] 核实真实 CRM 服务与依赖；可运行时用样例实测，否则清楚记录未验收原因，不拿受控样例代替。
- [x] 完整 pytest、Ruff、格式与锁检查；独立代码审阅并处理重要问题。
- [x] 更新实际入口、依赖版本、命令和验证结果，明确本次与完整模型 Agent 的边界。


## 完成记录

2026-10-29：四项交付完成；完整后端 105 passed in 99.72s，Ruff/格式/锁检查通过。真实 CRM 正式命令复核 8 请求/16 断言通过（6.42 秒），测试 Java 进程停止。独立代码审阅发现的三项重要断言问题及唯一 Header 契约问题已完成失败先行修复和全量回归。有效身份业务、RAG/SSE 和模型闭环按已确认范围继续后续实施；本次不声明这些能力已完成。目录无 Git 元数据，未执行提交/推送。
