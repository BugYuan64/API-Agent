/** 按业务域封装管理 API：依赖 request 和响应类型，接收页面参数并返回 Promise。除 Markdown 报告直接读取文本外，失败统一由 request 抛出；加载态、重试与提示由调用方负责。 */
/** 按领域分组的接口函数。
 *  每个函数只做「拼路径 + 声明类型」，不处理加载态与错误展示——
 *  那是页面与 store 的职责。 */

import { request } from './client'
import type {
  AgentDiagnosis,
  AgentRun,
  AgentRunInput,
  ConsoleHistory,
  ConsoleRequestInput,
  ConsoleResult,
  EngineInfo,
  EngineStatus,
  Environment,
  EnvironmentHistory,
  EnvironmentInput,
  Execution,
  Health,
  LLMConfig,
  LLMConfigUpdate,
  LLMModelListResult,
  LLMPingResult,
  Paged,
  Project,
  StreamEventPage,
  StreamHistory,
  StreamSession,
  StreamStartInput,
} from './types'

/* ---------- 健康检查 ---------- */
/** 读取管理服务健康信息，返回 Health；失败向调用方传播 ApiError，不代表目标服务健康。 */
export const getHealth = () => request<Health>('/health')

/* ---------- 项目与环境 ---------- */
/** 按 limit/offset 读取项目分页；返回记录与分页信息，范围校验及读取错误由管理 API 处理。 */
export const listProjects = (limit = 50, offset = 0) =>
  request<Paged<Project>>(`/api/projects?limit=${limit}&offset=${offset}`)

/** 按给定名称和说明创建项目并返回新记录；重名或输入无效等失败以 ApiError 传播。 */
export const createProject = (body: { name: string; description: string }) =>
  request<Project>('/api/projects', { method: 'POST', body })

/** 读取指定项目的环境分页；不自动选择当前环境，项目不存在或管理失败由调用方处理。 */
export const listEnvironments = (projectId: string, limit = 50, offset = 0) =>
  request<Paged<Environment>>(
    `/api/projects/${projectId}/environments?limit=${limit}&offset=${offset}`,
  )

/** 在指定项目下创建环境初始版本；输入含授权范围和认证引用，返回环境记录，校验失败抛 ApiError。 */
export const createEnvironment = (projectId: string, body: EnvironmentInput) =>
  request<Environment>(`/api/projects/${projectId}/environments`, { method: 'POST', body })

/** 读取环境的当前或指定版本配置；未给版本时由后端选择当前版本，读取失败抛 ApiError。 */
export const getEnvironment = (environmentId: string, version?: number) =>
  request<Environment>(
    `/api/environments/${environmentId}${version ? `?version=${version}` : ''}`,
  )

/** 按环境 ID 读取全部版本快照；只读查询，不切换当前版本，失败抛 ApiError。 */
export const environmentHistory = (environmentId: string) =>
  request<EnvironmentHistory>(`/api/environments/${environmentId}/versions`)

/** 带 expected_version 追加环境配置版本并返回更新记录；过期版本冲突由后端拒绝并抛 ApiError。 */
export const updateEnvironment = (
  environmentId: string,
  body: Omit<EnvironmentInput, 'name'> & { expected_version: number },
) => request<Environment>(`/api/environments/${environmentId}`, { method: 'PUT', body })

/* ---------- 控制台请求 ---------- */
/** 提交目标 HTTP 请求并返回 ConsoleResult；管理链路等待上限 70 秒，目标传输错误在结果字段内，管理失败抛 ApiError。 */
export const sendConsoleRequest = (body: ConsoleRequestInput) =>
  request<ConsoleResult>('/api/console/requests', { method: 'POST', body, timeoutMs: 70_000, acceptResultErrors: true })

/** 读取脱敏 HTTP 请求历史分页；正文为后端保留的预览，失败抛 ApiError。 */
export const listConsoleHistory = (limit = 50, offset = 0) =>
  request<Paged<ConsoleHistory>>(`/api/console/history?limit=${limit}&offset=${offset}`)

/** 按记录 ID 删除一条 HTTP 历史；成功返回空结果，失败抛 ApiError，由调用方同步列表。 */
export const deleteConsoleHistory = (recordId: string) =>
  request<null>(`/api/console/history/${recordId}`, { method: 'DELETE' })

/** 清空 HTTP 请求历史；不影响长连接或计划执行记录，管理失败抛 ApiError。 */
export const clearConsoleHistory = () =>
  request<null>('/api/console/history', { method: 'DELETE' })

/* ---------- 长连接 ---------- */
/** 按协议、授权地址与限额启动后端长连接会话；返回会话摘要，不代表事件已读尽，失败抛 ApiError。 */
export const startStream = (body: StreamStartInput) =>
  request<StreamSession>('/api/streams', { method: 'POST', body })

/** 分页读取已保存长连接会话；返回历史摘要，不拉取事件，失败抛 ApiError。 */
export const listStreams = (limit = 50, offset = 0) =>
  request<Paged<StreamHistory>>(`/api/streams?limit=${limit}&offset=${offset}`)

/** 删除一条长连接会话及其事件时间线；在跑的会话会先收尾，不存在时抛 ApiError。 */
export const deleteStream = (sessionId: string) =>
  request<null>(`/api/streams/${sessionId}`, { method: 'DELETE' })

/** 从 after 游标增量读取会话事件，可使用 waitMs 长轮询并传 signal 取消；管理截止时间额外预留 15 秒，返回结束与是否仍有事件两个独立标记。 */
export const readStream = (sessionId: string, after: number, waitMs = 1000, signal?: AbortSignal) =>
  request<StreamEventPage>(
    `/api/streams/${sessionId}?after=${after}&wait_ms=${waitMs}`,
    { signal, timeoutMs: waitMs + 15_000 },
  )

/** 向指定会话提交一条消息并返回会话摘要；协议或会话状态不允许时由服务端拒绝并抛 ApiError。 */
export const sendStreamMessage = (sessionId: string, message: string) =>
  request<StreamSession>(`/api/streams/${sessionId}/send`, { method: 'POST', body: { message } })

/** 请求结束后端会话并返回更新摘要；不负责停止浏览器轮询，管理失败抛 ApiError。 */
export const stopStream = (sessionId: string) =>
  request<StreamSession>(`/api/streams/${sessionId}/stop`, { method: 'POST' })


/* ---------- 计划执行 ---------- */
/** 探测指定或服务端默认执行引擎并返回可用性；可用性失败可在结果内，管理链路失败抛 ApiError。 */
export const engineStatus = (engine?: string) =>
  request<EngineStatus>(
    `/api/executions/engine/status${engine ? `?engine=${encodeURIComponent(engine)}` : ''}`,
  )

/** 读取执行引擎列表及服务端默认 ID；不更新共享选择状态，管理失败抛 ApiError。 */
export const engineList = () =>
  request<{ items: EngineInfo[]; default: string }>('/api/executions/engine/list')

/** 提交计划、标签、项目及可选引擎，等待并返回确定性执行结果；管理链路上限 180 秒，业务状态保留于 Execution，管理失败抛 ApiError。 */
export const runExecution = (body: {
  plan: unknown
  label: string
  project_id: string | null
  engine?: string | null
}) => request<Execution>('/api/executions', { method: 'POST', body, timeoutMs: 180_000 })

/** 读取已保存执行结果分页；不重新执行或重新裁定断言，读取失败抛 ApiError。 */
export const listExecutions = (limit = 50, offset = 0) =>
  request<Paged<Execution>>(`/api/executions?limit=${limit}&offset=${offset}`)

/** 删除一条执行历史；只删回看记录，不影响目标状态或 AI 编排证据，不存在时抛 ApiError。 */
export const deleteExecution = (executionId: string) =>
  request<null>(`/api/executions/${executionId}`, { method: 'DELETE' })

/* ---------- 大模型动态设置 ---------- */
/** 读取模型设置与密钥存在标志；响应不包含已保存密钥明文，管理失败抛 ApiError。 */
export const getLLMConfig = () => request<LLMConfig>('/api/settings/llm')

/** 保存模型配置并返回脱敏配置；api_key 省略保留旧值、空串清空，校验或保存失败抛 ApiError。 */
export const updateLLMConfig = (body: LLMConfigUpdate) =>
  request<LLMConfig>('/api/settings/llm', { method: 'PUT', body })

/** 由后端按可选覆盖配置进行模型探测；管理链路上限 20 秒，业务失败在结果内，管理失败抛 ApiError。 */
export const pingLLM = (body?: { base_url?: string; api_key?: string; model?: string }) =>
  request<LLMPingResult>('/api/settings/llm/ping', { method: 'POST', body, timeoutMs: 20_000 })

/** 由后端按可选地址及密钥读取模型列表；管理链路上限 20 秒，业务失败在结果内，管理失败抛 ApiError。 */
export const getLLMModels = (body?: { base_url?: string; api_key?: string }) =>
  request<LLMModelListResult>('/api/settings/llm/models', { method: 'POST', body, timeoutMs: 20_000 })



/* ---------- AI 编排 ---------- */
/** 提交契约、人工授权范围和预算，返回已受理编排任务；30 秒限制仅覆盖管理请求，后续进度由调用方轮询。 */
export const startAgentRun = (body: AgentRunInput) =>
  request<AgentRun>('/api/agent/runs', { method: 'POST', body, timeoutMs: 30_000 })

/** 按编排运行 ID 读取完整快照；不自动轮询或过滤迟到响应，管理失败抛 ApiError。 */
export const getAgentRun = (runId: string) => request<AgentRun>(`/api/agent/runs/${runId}`)

/** 提交编排运行取消并返回最新状态；管理请求上限 30 秒，实际终态由后端裁定，失败抛 ApiError。 */
export const cancelAgentRun = (runId: string) =>
  request<AgentRun>(`/api/agent/runs/${runId}/cancel`, { method: 'POST', timeoutMs: 30_000 })

/** 删除一次编排运行的证据文件；运行中会以 409 拒绝，须先取消，失败抛 ApiError。 */
export const deleteAgentRun = (runId: string) =>
  request<null>(`/api/agent/runs/${runId}`, { method: 'DELETE' })

/** 按编排 ID 读取 Markdown 报告原文；使用直接 fetch，不采用 JSON request 的超时与 ApiError 包装，非成功状态抛 Error。 */
export const getAgentReport = async (runId: string): Promise<string> => {
  const response = await fetch(`/api/agent/runs/${runId}/report`)
  if (!response.ok) {
    throw new Error(`report request failed: ${response.status}`)
  }
  return response.text()
}

/** 提交已有执行 ID 与契约进行显式 AI 诊断，返回证据和假设；管理链路上限 120 秒，不修改已保存执行结果，失败抛 ApiError。 */
export const diagnoseExecution = (
  executionId: string,
  body: { contract: string | Record<string, unknown> },
) => request<AgentDiagnosis>(`/api/agent/executions/${encodeURIComponent(executionId)}/diagnose`, {
  method: 'POST', body, timeoutMs: 120_000,
})
