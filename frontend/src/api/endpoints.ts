/** 按领域分组的接口函数。
 *  每个函数只做「拼路径 + 声明类型」，不处理加载态与错误展示——
 *  那是页面与 store 的职责。 */

import { request } from './client'
import type {
  BudgetEntry,
  ConsoleHistory,
  ConsoleRequestInput,
  ConsoleResult,
  DispatchAttempt,
  EngineStatus,
  Environment,
  EnvironmentHistory,
  EnvironmentInput,
  Execution,
  Health,
  Paged,
  Project,
  Run,
  RunEvent,
  StreamEventPage,
  StreamHistory,
  StreamKind,
  StreamSession,
  StreamStartInput,
} from './types'

/* ---------- 健康检查 ---------- */
export const getHealth = () => request<Health>('/health')

/* ---------- 项目与环境 ---------- */
export const listProjects = (limit = 50, offset = 0) =>
  request<Paged<Project>>(`/api/projects?limit=${limit}&offset=${offset}`)

export const createProject = (body: { name: string; description: string }) =>
  request<Project>('/api/projects', { method: 'POST', body })

export const listEnvironments = (projectId: string, limit = 50, offset = 0) =>
  request<Paged<Environment>>(
    `/api/projects/${projectId}/environments?limit=${limit}&offset=${offset}`,
  )

export const createEnvironment = (projectId: string, body: EnvironmentInput) =>
  request<Environment>(`/api/projects/${projectId}/environments`, { method: 'POST', body })

export const getEnvironment = (environmentId: string, version?: number) =>
  request<Environment>(
    `/api/environments/${environmentId}${version ? `?version=${version}` : ''}`,
  )

export const environmentHistory = (environmentId: string) =>
  request<EnvironmentHistory>(`/api/environments/${environmentId}/versions`)

export const updateEnvironment = (
  environmentId: string,
  body: Omit<EnvironmentInput, 'name'> & { expected_version: number },
) => request<Environment>(`/api/environments/${environmentId}`, { method: 'PUT', body })

/* ---------- 模拟运行 ---------- */
export const listRuns = (limit = 50, offset = 0) =>
  request<Paged<Run>>(`/api/runs?limit=${limit}&offset=${offset}`)

export const getRun = (runId: string) => request<Run>(`/api/runs/${runId}`)

export const createRun = (body: {
  project_id: string
  actions: { name: string; duration_ms: number; outcome: 'completed' | 'failed' }[]
  resource_limits: { max_actions: number; work_timeout_ms: number }
  random_seed: number
}) => request<Run>('/api/runs', { method: 'POST', body })

export const cancelRun = (runId: string) =>
  request<Run>(`/api/runs/${runId}/cancel`, { method: 'POST' })

export const listRunEvents = (runId: string, limit = 200, offset = 0) =>
  request<Paged<RunEvent>>(`/api/runs/${runId}/events?limit=${limit}&offset=${offset}`)

export const listRunBudget = (runId: string) =>
  request<BudgetEntry[]>(`/api/runs/${runId}/budget`)

export const listRunAttempts = (runId: string) =>
  request<DispatchAttempt[]>(`/api/runs/${runId}/attempts`)

/* ---------- 控制台请求 ---------- */
export const sendConsoleRequest = (body: ConsoleRequestInput) =>
  request<ConsoleResult>('/api/console/requests', { method: 'POST', body, timeoutMs: 70_000 })

export const listConsoleHistory = (limit = 50, offset = 0) =>
  request<Paged<ConsoleHistory>>(`/api/console/history?limit=${limit}&offset=${offset}`)

export const deleteConsoleHistory = (recordId: string) =>
  request<null>(`/api/console/history/${recordId}`, { method: 'DELETE' })

export const clearConsoleHistory = () =>
  request<null>('/api/console/history', { method: 'DELETE' })

/* ---------- 长连接 ---------- */
export const startStream = (body: StreamStartInput) =>
  request<StreamSession>('/api/streams', { method: 'POST', body })

export const listStreams = (limit = 50, offset = 0) =>
  request<Paged<StreamHistory>>(`/api/streams?limit=${limit}&offset=${offset}`)

/** 增量读取会话事件。``waitMs`` 用于长轮询，让界面接近实时而不必高频空转。 */
export const readStream = (sessionId: string, after: number, waitMs = 1000, signal?: AbortSignal) =>
  request<StreamEventPage>(
    `/api/streams/${sessionId}?after=${after}&wait_ms=${waitMs}`,
    { signal, timeoutMs: waitMs + 15_000 },
  )

export const sendStreamMessage = (sessionId: string, message: string) =>
  request<StreamSession>(`/api/streams/${sessionId}/send`, { method: 'POST', body: { message } })

export const stopStream = (sessionId: string) =>
  request<StreamSession>(`/api/streams/${sessionId}/stop`, { method: 'POST' })

export const streamKindLabel = (kind: StreamKind) => (kind === 'sse' ? 'SSE' : 'WebSocket')

/* ---------- 计划执行 ---------- */
export const engineStatus = () => request<EngineStatus>('/api/executions/engine/status')

export const runExecution = (body: { plan: unknown; label: string; project_id: string | null }) =>
  request<Execution>('/api/executions', { method: 'POST', body, timeoutMs: 180_000 })

export const listExecutions = (limit = 50, offset = 0) =>
  request<Paged<Execution>>(`/api/executions?limit=${limit}&offset=${offset}`)
