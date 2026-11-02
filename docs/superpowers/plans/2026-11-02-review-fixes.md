# Review Fixes Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task, with a final independent read-only review.

**Goal:** 修复 `审核报告-2026-11-02.md` 的八项已确认缺陷，并保持已有控制台与执行工作流。

**Architecture:** HTTP 重定向逐跳校验并限制跳数；WebSocket 禁止跳转与环境代理。流读取在解码/完整事件形成前执行预算；任务取消使用条件更新；回放增加 `has_more`，与会话 `finished` 分离。

**Tech Stack:** 现有 Python 3.12/FastAPI/SQLAlchemy/httpx/websockets 15、Vue 3/TypeScript，不新增依赖。

**Spec:** [审核报告](../../../审核报告-2026-11-02.md)，用户已明确要求修复。

## Global Constraints

- 无 Git 元数据，直接修复当前工作区，不创建 worktree、不提交或推送。
- 仅用隔离测试数据库和本机随机端口，不调用实际 CRM 或模型，不修改用户配置。
- HTTP 同源重定向保持可用，跨源即使获许可也不转发原源凭据。
- 保持会话 `finished` 的既有含义，新增 `has_more` 控制翻页。

## Review Focus

- 相对跳转、域名/非回环跳转、未许可端口、跨源鉴权头。
- SSE 巨型无换行输入、多行无空行事件、压缩响应、UTF-8/CRLF 分块。
- WebSocket 巨型/分片消息、累计预算、二进制与正常关闭。
- 取消时旧身份映射、queued→running 与 running→completed 的交错。
- 会话名额在持久化失败或启动取消后应归还；历史恰好 500 条和多页完整读取。

## Tasks and ledger

- [x] Task 1: 新增回归测试，修复前观察到 18 failed、2 passed；其余边界在各次修改前独立复现。
- [x] Task 2: 目标、预算与名额修复；首轮聚焦 77 passed，追加复核回归场景后 30 passed。
- [x] Task 3: 取消、项目校验与省略模式修复；聚焦测试包含真实 MCP→Postman 验证，均通过。
- [x] Task 4: `has_more` 与前端翻页实施完成；500/650/1100 条 API 测试、650 条浏览器回放与前端构建通过。
- [x] Task 5: 独立只读复核完成并处理两项候选回归；最终完整 pytest 176 passed in 144.56s，Ruff/格式/锁检查与前端构建通过，README 和审核报告已更新。

验证命令：backend 目录运行 `uv run --locked --offline pytest -q`、`ruff check src tests migrations`、`ruff format --check src tests migrations`、`uv lock --check --offline`；frontend 目录运行 `npm run build`。

Ruling: 用户“请你修复”已授权报告中的具体修复与必要测试，本计划直接执行，不新增设计确认流程。

Ruling: SSE 不使用自动解压；请求 identity，拒绝非 identity 正文并返回明确错误，以阻断预算校验前的解压分配。此行为已记录 README。

Ruling: 最终复核发现 WebSocket 空分片元数据增长和恰好预算消息丢失；按同一资源边界补测并修复为 bytearray 累积和完整消息保存后结束。
