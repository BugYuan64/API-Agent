/** 按业务域封装管理 API：依赖 request 和响应类型，接收页面参数并返回 Promise。除 Markdown 报告直接读取文本外，失败统一由 request 抛出；加载态、重试与提示由调用方负责。 */
/** 按领域分组的接口函数。
 *  每个函数只做「拼路径 + 声明类型」，不处理加载态与错误展示——
 *  那是页面与 store 的职责。 */

import { ApiError, readConsoleFrames, request } from './client'
import type {
  AgentDiagnosis,
  AgentRun,
  AgentRunInput,
  AssistantActiveRunsResponse,
  AssistantLiveFrame,
  AssistantRunStartRequest,
  AssistantRunStartResponse,
  AssistantThread,
  CaseLinkBlock,
  ConsoleActiveRunsResponse,
  ConsoleHistory,
  ConsoleHistoryDetail,
  ConsoleRequestInput,
  ConsoleResult,
  ConsoleRunLiveFrame,
  ConsoleRunSummary,
  ConsoleSseProgress,
  ConsoleSubCase,
  EngineInfo,
  EngineStatus,
  Environment,
  EnvironmentHistory,
  EnvironmentInput,
  Execution,
  Health,
  EmbeddingConfigUpdate,
  KnowledgeBuildResult,
  KnowledgeSearchResult,
  KnowledgeStatus,
  LLMConfig,
  LLMConfigUpdate,
  LLMModelListResult,
  LLMPingResult,
  ModelDiscoverResult,
  ModelProbeResult,
  ModelProbeStreamFrame,
  ModelProtocol,
  ModelTestActiveRunsResponse,
  ModelTestLiveFrame,
  ModelTestRunDetail,
  ModelTestRunListResponse,
  ModelTestRunStartRequest,
  ModelTestSubCaseSnapshot,
  Paged,
  PageScope,
  ProbeCheckName,
  Project,
  StreamActiveResponse,
  StreamEventPage,
  StreamHistory,
  StreamReplayInputs,
  StreamSession,
  StreamStartInput,
  StreamSubCaseDetail,
  StreamSubCaseListResponse,
  StreamSuiteLiveFrame,
  StreamSuiteStatus,
} from './types'

/** 逐行消费 application/x-ndjson 响应流，统一处理 UTF-8 分片、尾部缓冲与 reader 回收。 */
async function readNdjsonStream<TFrame, TResult>(
  response: Response,
  incompleteMessage: string,
  handleFrame: (frame: TFrame) => { done: true; value: TResult } | null,
): Promise<TResult> {
  const incomplete = () => new ApiError(0, 'incomplete_stream', incompleteMessage)
  if (!response.body || !response.headers.get('content-type')?.startsWith('application/x-ndjson')) {
    throw incomplete()
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let pending = ''

  const consumeLine = (rawLine: string): { done: true; value: TResult } | null => {
    const trimmed = rawLine.trim()
    if (!trimmed) return null
    const frame = JSON.parse(trimmed) as TFrame
    return handleFrame(frame)
  }

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) {
        const trailing = pending + decoder.decode()
        if (trailing.trim()) {
          const outcome = consumeLine(trailing)
          if (outcome?.done) return outcome.value
        }
        throw incomplete()
      }
      const lines = (pending + decoder.decode(value, { stream: true })).split('\n')
      pending = lines.pop() ?? ''
      for (const line of lines) {
        const outcome = consumeLine(line)
        if (outcome?.done) return outcome.value
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

/** 逐段编码 FastAPI `:path` 路径参数，保留 `/` 分隔符同时安全转义 `:`, `?`, `#` 等字符。 */
function encodePathParam(value: string): string {
  return value.split('/').map(encodeURIComponent).join('/')
}

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
/** 发送一次目标请求，从同一管理响应逐条读取 SSE 事件及最终结果。 */
export const sendConsoleRequest = (body: ConsoleRequestInput, onProgress: (frame: ConsoleSseProgress) => void, signal?: AbortSignal) =>
  request<ConsoleResult>('/api/console/requests/live', {
    method: 'POST', body, timeoutMs: 70_000, signal,
    readResponse: response => readConsoleFrames(response, onProgress),
  })

/** 在后台启动持久化单次受控 HTTP/SSE 请求，立即返回 202 运行摘要，支持跨页不断连。 */
export const startConsoleRun = (body: ConsoleRequestInput) =>
  request<ConsoleRunSummary>('/api/console/runs', { method: 'POST', body, timeoutMs: 30_000 })

/** 列出当前仍在后台运行的控制台请求与 AI 多轮套件，供切页返回自动重连。 */
export const getActiveConsoleRuns = () =>
  request<ConsoleActiveRunsResponse>('/api/console/runs/active')

/** 以可重连 NDJSON 流订阅后台控制台运行进度；客户端 signal 中止不会取消后台任务。 */
export const followConsoleRun = (
  runId: string,
  signal: AbortSignal | undefined,
  onFrame: (frame: ConsoleRunLiveFrame) => void,
) =>
  request<ConsoleRunLiveFrame>(`/api/console/runs/${encodeURIComponent(runId)}/live`, {
    signal,
    timeoutMs: 3_660_000,
    readResponse: response =>
      readNdjsonStream<ConsoleRunLiveFrame, ConsoleRunLiveFrame>(
        response,
        '控制台运行进度流提前结束',
        frame => {
          onFrame(frame)
          if (frame.type === 'error') {
            throw new ApiError(
              frame.status ?? 502,
              frame.error?.code ?? frame.code ?? 'console_run_error',
              frame.error?.message ?? frame.message ?? '控制台运行流异常中断',
              [],
              frame.error?.request_id ?? '',
            )
          }
          if (frame.type === 'result' || frame.type === 'done') {
            return { done: true, value: frame }
          }
          return null
        },
      ),
  })

/** 显式取消指定后台控制台运行并返回其终态详情记录。 */
export const cancelConsoleRun = (runId: string) =>
  request<ConsoleHistoryDetail>(`/api/console/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
    timeoutMs: 30_000,
  })

/** 读取脱敏 HTTP 请求历史分页；正文为后端保留的预览，失败抛 ApiError。 */
export const listConsoleHistory = (limit = 50, offset = 0) =>
  request<Paged<ConsoleHistory>>(`/api/console/history?limit=${limit}&offset=${offset}`)

/** Fetch complete replay inputs for a single request. */
export const readConsoleHistory = (id: string, signal?: AbortSignal) =>
  request<ConsoleHistoryDetail>(`/api/console/history/${encodeURIComponent(id)}`, { signal })

/** 读取单条 AI 多轮控制台子用例的脱敏只读回放快照。 */
export const getConsoleSubCase = (recordId: string, caseId: string, signal?: AbortSignal) =>
  request<ConsoleSubCase>(
    `/api/console/history/${encodeURIComponent(recordId)}/cases/${encodeURIComponent(caseId)}`,
    { signal },
  )

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

/** 列出当前仍在后台运行的手动长连接与 AI 多轮测试套件，供跨页切回重连。 */
export const getActiveStreams = () =>
  request<StreamActiveResponse>('/api/streams/active')

/** 分页读取已保存长连接会话；返回历史摘要，不拉取事件，失败抛 ApiError。 */
export const listStreams = (limit = 50, offset = 0) =>
  request<Paged<StreamHistory>>(`/api/streams?limit=${limit}&offset=${offset}`)

/** Fetch saved startup inputs separately from the event timeline. */
export const readStreamInputs = (id: string, signal?: AbortSignal) =>
  request<StreamReplayInputs>(`/api/streams/${encodeURIComponent(id)}/inputs`, { signal })

/** 列出分组 AI WebSocket 历史记录下的全部子用例摘要。 */
export const listStreamSubCases = (sessionId: string, signal?: AbortSignal) =>
  request<StreamSubCaseListResponse>(
    `/api/streams/${encodeURIComponent(sessionId)}/cases`,
    { signal },
  )

/** 读取单条 WebSocket 子用例的完整只读回放快照（连接参数 + 会话状态 + 事件流）。 */
export const getStreamSubCase = (sessionId: string, caseId: string, signal?: AbortSignal) =>
  request<StreamSubCaseDetail>(
    `/api/streams/${encodeURIComponent(sessionId)}/cases/${encodeURIComponent(caseId)}`,
    { signal },
  )

/** 以可重连 NDJSON 流订阅多轮 WebSocket AI 测试套件进度；断线不取消后台任务。 */
export const followStreamSuite = (
  suiteId: string,
  signal: AbortSignal | undefined,
  onFrame: (frame: StreamSuiteLiveFrame) => void,
) =>
  request<Extract<StreamSuiteLiveFrame, { type: 'done' }>>(
    `/api/streams/suites/${encodeURIComponent(suiteId)}/live`,
    {
      signal,
      timeoutMs: 3_660_000,
      readResponse: response =>
        readNdjsonStream<StreamSuiteLiveFrame, Extract<StreamSuiteLiveFrame, { type: 'done' }>>(
          response,
          'WebSocket 套件进度流提前结束',
          frame => {
            onFrame(frame)
            if (frame.type === 'error') {
              throw new ApiError(
                502,
                frame.code ?? 'stream_suite_error',
                frame.message ?? 'WebSocket 套件进度流异常中断',
              )
            }
            if (frame.type === 'done') {
              return { done: true, value: frame }
            }
            return null
          },
        ),
    },
  )

/** 显式取消一个后台运行中的多轮 WebSocket AI 测试套件。 */
export const cancelStreamSuite = (suiteId: string) =>
  request<StreamSuiteStatus>(
    `/api/streams/suites/${encodeURIComponent(suiteId)}/cancel`,
    { method: 'POST', timeoutMs: 30_000 },
  )

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

/** Persist only the model name; chat and Settings read the same saved configuration. */
export const selectLLMModel = (model: string) =>
  request<LLMConfig>('/api/settings/llm/model', { method: 'PATCH', body: { model } })

/** 由后端按可选覆盖配置进行模型探测；管理链路上限 95 秒以对齐系统设置超时，业务失败在结果内，管理失败抛 ApiError。 */
export const pingLLM = (body?: { base_url?: string; api_key?: string; model?: string }) =>
  request<LLMPingResult>('/api/settings/llm/ping', { method: 'POST', body, timeoutMs: 95_000 })

/** 由后端按可选地址及密钥读取模型列表；管理链路上限 20 秒，业务失败在结果内，管理失败抛 ApiError。 */
export const getLLMModels = (body?: { base_url?: string; api_key?: string }) =>
  request<LLMModelListResult>('/api/settings/llm/models', { method: 'POST', body, timeoutMs: 20_000 })

/* ---------- AI 助手小Y 向量知识库 ---------- */
/** 查询小Y 向量知识库语料准备状态、Embedding 配置与当前检索模式。 */
export const getKnowledgeStatus = () => request<KnowledgeStatus>('/api/knowledge/status')

/** 保存小Y 向量模型（Embedding）配置，返回脱敏后的最新状态。 */
export const updateKnowledgeConfig = (body: EmbeddingConfigUpdate) =>
  request<KnowledgeStatus>('/api/knowledge/config', { method: 'PUT', body })

/** 使用已配置的向量模型为全部项目知识块生成并持久化向量索引。 */
export const buildKnowledgeIndex = () =>
  request<KnowledgeBuildResult>('/api/knowledge/build', { method: 'POST', timeoutMs: 120_000 })

/** 调试检索小Y 项目知识库，返回命中片段与当前检索模式。 */
export const searchKnowledge = (query: string, top_k?: number) =>
  request<KnowledgeSearchResult>('/api/knowledge/search', { method: 'POST', body: { query, top_k } })

/* ---------- 大模型测试 ---------- */
/** 测试指定 Base URL 与 API Key 是否可拉取模型，并返回初始类型与模态画像。 */
export const discoverProviderModels = (body: {
  base_url?: string
  api_key?: string
  use_saved_key?: boolean
  protocol?: ModelProtocol
  timeout_seconds?: number
}) => request<ModelDiscoverResult>('/api/model-testing/discover', { method: 'POST', body, timeoutMs: 35_000 })

/** 对选定模型执行可用性、工具调用、流式响应、结构化输出与输入/输出模态探测。 */
export const probeProviderModels = (body: {
  base_url?: string
  api_key?: string
  use_saved_key?: boolean
  protocol?: ModelProtocol
  models: string[]
  checks: ProbeCheckName[]
  timeout_seconds?: number
  concurrency?: number
}) => request<ModelProbeResult>('/api/model-testing/probe', { method: 'POST', body, timeoutMs: 120_000 })

/** 以异步消费队列并发探测选定模型，并通过 NDJSON 流逐条回调单个模型的开始与结果帧。 */
export const probeProviderModelsStream = (
  body: {
    base_url?: string
    api_key?: string
    use_saved_key?: boolean
    protocol?: ModelProtocol
    models: string[]
    checks: ProbeCheckName[]
    timeout_seconds?: number
    concurrency?: number
  },
  onFrame: (frame: ModelProbeStreamFrame) => void,
  signal?: AbortSignal,
) =>
  request<ModelProbeResult>('/api/model-testing/probe/stream', {
    method: 'POST',
    body,
    timeoutMs: 600_000,
    signal,
    readResponse: async response => {
      const incomplete = () => new ApiError(0, 'incomplete_stream', '模型探测结果流提前结束')
      if (!response.body || !response.headers.get('content-type')?.startsWith('application/x-ndjson')) {
        throw incomplete()
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let pending = ''
      let finalResult: ModelProbeResult | null = null

      const consumeLine = (rawLine: string): ModelProbeResult | null => {
        const trimmed = rawLine.trim()
        if (!trimmed) return null
        const frame = JSON.parse(trimmed) as ModelProbeStreamFrame
        onFrame(frame)
        if (frame.type === 'error') {
          throw new ApiError(502, frame.code ?? 'probe_stream_error', frame.message ?? '模型探测流异常中断')
        }
        if (frame.type === 'done') {
          finalResult = frame.result
          return frame.result
        }
        return null
      }

      try {
        while (true) {
          const { value, done } = await reader.read()
          if (done) {
            if (pending.trim()) {
              const trailing = consumeLine(pending)
              if (trailing) return trailing
            }
            if (finalResult) return finalResult
            throw incomplete()
          }
          const lines = (pending + decoder.decode(value, { stream: true })).split('\n')
          pending = lines.pop() ?? ''
          for (const line of lines) {
            const doneResult = consumeLine(line)
            if (doneResult) return doneResult
          }
        }
      } finally {
        await reader.cancel().catch(() => {})
        reader.releaseLock()
      }
    },
  })

/** 启动持久化后台模型探测任务，立即返回 202 初始详情，跨页切换不中断执行。 */
export const startModelTestRun = (body: ModelTestRunStartRequest) =>
  request<ModelTestRunDetail>('/api/model-testing/runs', {
    method: 'POST',
    body,
    timeoutMs: 30_000,
  })

/** 列出当前仍在后台运行的模型探测任务，供跨页切回自动重连观察流。 */
export const getActiveModelTestRuns = () =>
  request<ModelTestActiveRunsResponse>('/api/model-testing/runs/active')

/** 分页查询持久化模型探测历史列表（含手动与 AI 标注，不含明文密钥）。 */
export const listModelTestRuns = (limit = 50, offset = 0) =>
  request<ModelTestRunListResponse>(`/api/model-testing/runs?limit=${limit}&offset=${offset}`)

/** 读取单次模型探测任务的完整详情，包含模型画像快照、各轮汇总与子用例列表。 */
export const getModelTestRun = (runId: string, signal?: AbortSignal) =>
  request<ModelTestRunDetail>(`/api/model-testing/runs/${encodeURIComponent(runId)}`, { signal })

/** 读取指定模型探测任务中单个子用例的只读回放快照。 */
export const getModelTestSubCase = (runId: string, caseId: string, signal?: AbortSignal) =>
  request<ModelTestSubCaseSnapshot>(
    `/api/model-testing/runs/${encodeURIComponent(runId)}/cases/${encodePathParam(caseId)}`,
    { signal },
  )

/** 以可重连 NDJSON 流实时订阅后台模型探测进度；客户端断开不会取消后台任务。 */
export const followModelTestRun = (
  runId: string,
  signal: AbortSignal | undefined,
  onFrame: (frame: ModelTestLiveFrame) => void,
) =>
  request<Extract<ModelTestLiveFrame, { type: 'done' }>>(
    `/api/model-testing/runs/${encodeURIComponent(runId)}/live`,
    {
      signal,
      timeoutMs: 3_660_000,
      readResponse: response =>
        readNdjsonStream<ModelTestLiveFrame, Extract<ModelTestLiveFrame, { type: 'done' }>>(
          response,
          '模型探测进度流提前结束',
          frame => {
            onFrame(frame)
            if (frame.type === 'error') {
              throw new ApiError(
                502,
                frame.code ?? 'live_stream_error',
                frame.message ?? '模型探测进度流异常中断',
              )
            }
            if (frame.type === 'done') {
              return { done: true, value: frame }
            }
            return null
          },
        ),
    },
  )

/** 显式取消正在运行的模型探测任务，保留已完成子用例并返回最新终态。 */
export const cancelModelTestRun = (runId: string) =>
  request<ModelTestRunDetail>(`/api/model-testing/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
    timeoutMs: 30_000,
  })

/** 删除单条终态模型探测历史记录（运行中由服务端返回 409）。 */
export const deleteModelTestRun = (runId: string) =>
  request<null>(`/api/model-testing/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' })

/** 清空全部终态模型探测历史记录（不影响运行中任务）。 */
export const clearModelTestRuns = () =>
  request<null>('/api/model-testing/runs', { method: 'DELETE' })

/* ---------- AI 助手小Y 后台运行与持久化对话线程 ---------- */

/** 启动后台 AI 助手运行（202），支持普通问答及多轮 AI 自动化测试编排。 */
export const startAssistantRun = (body: AssistantRunStartRequest) =>
  request<AssistantRunStartResponse>('/api/assistant/runs', {
    method: 'POST',
    body,
    timeoutMs: 30_000,
  })

/** 查询指定页面作用域（或全部作用域）当前活跃的后台 AI 助手运行。 */
export const getActiveAssistantRuns = (scope?: PageScope) =>
  request<AssistantActiveRunsResponse>(
    `/api/assistant/runs/active${scope ? `?scope=${encodeURIComponent(scope)}` : ''}`,
  )

/** 以可重连 NDJSON 流订阅后台 AI 助手运行；客户端 AbortSignal 仅关闭观察流，绝不取消后台任务。 */
export const followAssistantRun = (
  runId: string,
  signal: AbortSignal | undefined,
  onFrame: (frame: AssistantLiveFrame) => void,
) =>
  request<Extract<AssistantLiveFrame, { type: 'done' }>>(
    `/api/assistant/runs/${encodeURIComponent(runId)}/live`,
    {
      signal,
      timeoutMs: 3_660_000,
      readResponse: response =>
        readNdjsonStream<AssistantLiveFrame, Extract<AssistantLiveFrame, { type: 'done' }>>(
          response,
          'AI 回复未完整返回',
          frame => {
            onFrame(frame)
            if (frame.type === 'error') {
              throw new ApiError(
                502,
                frame.code || 'assistant_run_failed',
                frame.message || `AI 回复失败（${frame.code || '未知错误'}）`,
              )
            }
            if (frame.type === 'done') {
              return { done: true, value: frame }
            }
            return null
          },
        ),
    },
  )

/** 显式取消正在运行的后台 AI 助手任务。 */
export const cancelAssistantRun = (runId: string) =>
  request<{ run_id: string; status: string }>(
    `/api/assistant/runs/${encodeURIComponent(runId)}/cancel`,
    { method: 'POST', timeoutMs: 30_000 },
  )

/** 读取绑定到指定 (scope, history_id) 的服务端持久化 AI 助手对话线程。 */
export const getAssistantThread = (scope: PageScope, historyId: string, signal?: AbortSignal) =>
  request<AssistantThread>(
    `/api/assistant/threads/${encodeURIComponent(scope)}/${encodeURIComponent(historyId)}`,
    { signal },
  )

/** 当服务端线程尚为空时，将旧版浏览器 localStorage 对话历史幂等迁移入库。 */
export const migrateAssistantThread = (
  scope: PageScope,
  historyId: string,
  body: {
    messages: Array<{ role: 'user' | 'assistant'; content: string; blocks?: CaseLinkBlock[] }>
    title?: string | null
  },
) =>
  request<AssistantThread>(
    `/api/assistant/threads/${encodeURIComponent(scope)}/${encodeURIComponent(historyId)}/migrate`,
    { method: 'PUT', body, timeoutMs: 30_000 },
  )

/** 删除指定历史记录关联的服务端 AI 助手对话线程。 */
export const deleteAssistantThread = (scope: PageScope, historyId: string) =>
  request<null>(
    `/api/assistant/threads/${encodeURIComponent(scope)}/${encodeURIComponent(historyId)}`,
    { method: 'DELETE' },
  )



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
