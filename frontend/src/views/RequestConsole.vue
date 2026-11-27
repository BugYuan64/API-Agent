<script setup lang="ts">
/** HTTP 调试页面：请求、历史和响应各有独立工作区。目标请求由后端执行；
 * 历史正文是预览，管理接口错误与目标响应分别展示。支持跨页不断连后台运行、
 * AI 多轮测试历史标注与点击子用例进入只读回放锁定。 */

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  ArrowDownLeft,
  ArrowUpRight,
  Braces,
  Clock3,
  FileCode2,
  Loader2,
  Lock,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  Trash2,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import ResponseDetails from '@/components/ResponseDetails.vue'
import TaskChat from '@/components/TaskChat.vue'
import HistoryList, { type HistoryBadge, type HistoryEntry } from '@/components/HistoryList.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import UrlInput from '@/components/UrlInput.vue'
import { useHeaderRows } from '@/composables/useHeaderRows'
import EmptyState from '@/components/EmptyState.vue'
import { ApiError, describeError } from '@/api/client'
import {
  cancelConsoleRun,
  deleteConsoleHistory,
  followConsoleRun,
  getActiveConsoleRuns,
  getConsoleSubCase,
  listConsoleHistory,
  readConsoleHistory,
  sendConsoleRequest,
  startConsoleRun,
} from '@/api/endpoints'
import type {
  AssistantRunBoundPayload,
  BodyMode,
  CaseLinkBlock,
  ConsoleHistory,
  ConsoleHistoryDetail,
  ConsoleRequestInput,
  ConsoleResult,
  ConsoleRunSummary,
  ConsoleSseEvent,
  ConsoleSubCase,
  HttpMethod,
  PageScope,
} from '@/api/types'
import {
  describeRedirect,
  formatBytes,
  formatDuration,
  formatRelative,
  isSseHistory,
  looksLikeJson,
  prettyJson,
  statusTone,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { useWorkbenchRunsStore, type ReadonlyReplayState } from '@/stores/workbenchRuns'
import { useConsoleWorkbenchStore } from '@/stores/consoleWorkbench'
import { presentManagementError, presentResult, resultCodeLabel, type ResultPresentation } from '@/lib/results'
import { consoleSettings } from '@/lib/testSettings'
import TestSettingsFields from '@/components/TestSettingsFields.vue'
import { joinTargetUrl, urlSuffix } from '@/lib/urlPrefixes'
import { COPY } from '@/lib/copy'
import { buildConsoleTestContext } from '@/lib/assistantContext'
import { removeTestChat } from '@/lib/chatHistory'

const METHODS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const BODY_MODES: { value: BodyMode; label: string }[] = [
  { value: 'none', label: 'none' },
  { value: 'json', label: 'JSON' },
  { value: 'text', label: 'Text' },
  { value: 'form', label: 'x-www-form-urlencoded' },
]

/** 一组示例值，让页面打开就能一键跑通，而不是面对空表单猜格式。 */
const SAMPLES = [
  { label: 'GET 健康检查', method: 'GET' as HttpMethod, url: 'http://127.0.0.1:8010/health', body: '' },
  { label: 'GET OpenAPI', method: 'GET' as HttpMethod, url: 'http://127.0.0.1:8010/openapi.json', body: '' },
  {
    label: 'POST 示例',
    method: 'POST' as HttpMethod,
    url: 'http://127.0.0.1:8080/api/user/login',
    body: '{\n  "username": "demo",\n  "password": "123"\n}',
  },
]

function resolveWorkbenchRuns() {
  if (typeof useWorkbenchRunsStore !== 'function') return null
  try {
    return useWorkbenchRunsStore()
  } catch {
    return null
  }
}

function resolveConsoleWorkbench() {
  if (typeof useConsoleWorkbenchStore !== 'function') return null
  try {
    return useConsoleWorkbenchStore()
  } catch {
    return null
  }
}

const workbenchRuns = resolveWorkbenchRuns()
const consoleStore = resolveConsoleWorkbench()
const hasPersistentConsoleApi =
  typeof startConsoleRun === 'function' && typeof followConsoleRun === 'function'

const form = reactive({
  method: 'GET' as HttpMethod,
  url: 'http://127.0.0.1:8010/health',
  bodyMode: 'none' as BodyMode,
  body: '',
})

/** 头以「行」的形式编辑（Postman/Apifox 的心智模型），折叠规则与长连接页共用一份。 */
const {
  rows: headerRows,
  headers: activeHeaders,
  count: headerCount,
  add: addHeaderRowRaw,
  remove: removeHeaderRowRaw,
} = useHeaderRows()

const tab = ref<'params' | 'headers' | 'body' | 'meta'>('body')
const sending = ref(false)
const cancellingRun = ref(false)
const urlPrefix = ref('')
const result = ref<ConsoleResult | null>(null)
const liveEvents = ref<ConsoleSseEvent[] | null>(null)
const responseCleared = ref(false)
const detailRequest = ref<
  ConsoleRequestInput | Pick<ConsoleRequestInput, 'url' | 'method' | 'headers' | 'body_mode' | 'body'> | null
>(null)

let activeRequest: AbortController | undefined
let observerAbort: AbortController | null = null
let followingRunId: string | null = null
let historyRead: AbortController | undefined
let subCaseRead: AbortController | undefined
let sendGeneration = 0
let unmounted = false

/** 页面管理链路失败仅保留本地提示，不写入目标结果或历史。 */
const localFailure = ref<ResultPresentation | null>(null)
const history = ref<ConsoleHistory[]>([])
const historyLoading = ref(false)
const restoringHistory = ref(false)
const activeHistoryId = ref<string | null>(null)
const activeRunId = ref<string | null>(null)
const activeRunProgress = ref<ConsoleRunSummary | null>(null)
const activeDetail = ref<ConsoleHistoryDetail | null>(null)
const liveSubCases = ref<ConsoleSubCase[]>([])
const readonlyReplay = ref<ReadonlyReplayState | null>(null)
const activeSubCase = ref<ConsoleSubCase | null>(null)
const draftVersion = ref(0)

const isReadOnlyReplay = computed(() => Boolean(readonlyReplay.value))

/** 根据所选方法判断是否展示正文能力；GET、HEAD 与 OPTIONS 不允许发送正文。 */
const hasBody = computed(() => !['GET', 'HEAD', 'OPTIONS'].includes(form.method))

/** GET/HEAD/OPTIONS 不允许携带请求体（后端会 422），所以切到这些方法时自动收敛。 */
watch(
  () => form.method,
  (method) => {
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && form.bodyMode === 'none') {
      form.bodyMode = 'json'
    }
    if (['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      form.bodyMode = 'none'
      tab.value = tab.value === 'body' ? 'headers' : tab.value
    }
  },
  { flush: 'sync' },
)

function normalizeHttpMethod(raw: string | null | undefined): HttpMethod {
  const upper = String(raw ?? 'GET').trim().toUpperCase()
  return (METHODS as readonly string[]).includes(upper) ? (upper as HttpMethod) : 'GET'
}

function normalizeBodyMode(raw: string | null | undefined, method: HttpMethod): BodyMode {
  if (['GET', 'HEAD', 'OPTIONS'].includes(method)) return 'none'
  const candidate = String(raw ?? 'none').trim().toLowerCase()
  if (candidate === 'json' || candidate === 'text' || candidate === 'form' || candidate === 'none') {
    return candidate
  }
  return 'none'
}

function normalizeResponseHeaders(
  headers: Record<string, unknown> | null | undefined,
): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  if (!headers || typeof headers !== 'object') return out
  for (const [key, value] of Object.entries(headers)) {
    if (Array.isArray(value)) {
      out[key] = value.map((item) => String(item))
    } else if (value != null) {
      out[key] = [String(value)]
    }
  }
  return out
}

function toStringRecord(headers: Record<string, unknown> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {}
  if (!headers || typeof headers !== 'object') return out
  for (const [key, value] of Object.entries(headers)) {
    out[key] = typeof value === 'string' ? value : JSON.stringify(value)
  }
  return out
}

function toHeaderRows(headers: Record<string, unknown> | null | undefined) {
  const entries = Object.entries(headers ?? {}).map(([name, value]) => ({
    name,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
  return entries.length ? entries : [{ name: '', value: '' }]
}

function isApiStatus(failure: unknown, statuses: number[]): boolean {
  if (!failure || typeof failure !== 'object') return false
  if (failure instanceof ApiError) return statuses.includes(failure.status)
  const candidate = failure as { name?: unknown; status?: unknown }
  return (
    candidate.name === 'ApiError' &&
    typeof candidate.status === 'number' &&
    statuses.includes(candidate.status)
  )
}

/** 将工作台草稿、运行句柄、响应和回放状态同步到 Pinia 内存仓库，确保切页返回不丢失。 */
function syncStoreState() {
  if (!consoleStore) return
  consoleStore.saveSnapshot({
    method: form.method,
    url: form.url,
    prefix: urlPrefix.value,
    bodyMode: form.bodyMode,
    body: form.body,
    headers: headerRows.value.map((row) => ({ name: row.name, value: row.value })),
    tab: tab.value,
    activeHistoryId: activeHistoryId.value,
    activeRunId: activeRunId.value,
    activeRunProgress: activeRunProgress.value,
    result: result.value,
    liveEvents: liveEvents.value,
    localFailure: localFailure.value,
    responseCleared: responseCleared.value,
    detailRequest: detailRequest.value,
    activeDetail: activeDetail.value,
    liveSubCases: liveSubCases.value,
    readonlyReplay: readonlyReplay.value,
    activeSubCase: activeSubCase.value,
    draftVersion: draftVersion.value,
  })
}

/** 从 Pinia 内存仓库恢复上次离开本页时的表单、响应与只读回放上下文。 */
function restoreFromStore(): boolean {
  if (!consoleStore?.hydrated) return false
  form.method = consoleStore.form.method
  urlPrefix.value = consoleStore.form.prefix
  form.url = consoleStore.form.url
  form.bodyMode = consoleStore.form.bodyMode
  form.body = consoleStore.form.body
  headerRows.value = consoleStore.headers.length
    ? consoleStore.headers.map((row) => ({ name: row.name, value: row.value }))
    : [{ name: '', value: '' }]
  tab.value = consoleStore.tab
  activeHistoryId.value = consoleStore.activeHistoryId ?? workbenchRuns?.activeHistoryId.console ?? null
  activeRunId.value = consoleStore.activeRunId ?? workbenchRuns?.activeRunId.console ?? null
  activeRunProgress.value = consoleStore.activeRunProgress ? { ...consoleStore.activeRunProgress } : null
  result.value = consoleStore.result
  liveEvents.value = consoleStore.liveEvents ? consoleStore.liveEvents.map((evt) => ({ ...evt })) : null
  localFailure.value = consoleStore.localFailure ? { ...consoleStore.localFailure } : null
  responseCleared.value = consoleStore.responseCleared
  detailRequest.value = consoleStore.detailRequest ? { ...consoleStore.detailRequest } : null
  activeDetail.value = consoleStore.activeDetail
  liveSubCases.value = consoleStore.liveSubCases.slice()
  readonlyReplay.value = consoleStore.readonlyReplay
    ? { ...consoleStore.readonlyReplay }
    : workbenchRuns?.readonlyReplay.console
      ? { ...workbenchRuns.readonlyReplay.console }
      : null
  activeSubCase.value = consoleStore.activeSubCase
  draftVersion.value = consoleStore.draftVersion
  return true
}

// 在 setup 阶段若已有跨页保留的状态，立即同步回填以避免 TaskChat 首次渲染抖动。
restoreFromStore()

/** 响应体展示：JSON 自动美化，其它类型原样展示。 */
const responseBodyText = computed(() => {
  const body = result.value?.response?.body
  if (body == null) return ''
  return looksLikeJson(body) ? prettyJson(body) : body
})

/** 实时事件和最终结果共用同一列表，流结束时保留正文滚动容器。 */
const responseEvents = computed(() => result.value?.response?.sse?.events ?? liveEvents.value)
const responseHeaders = computed(() => Object.entries(result.value?.response?.headers ?? {}))
const responseDetails = computed(() => {
  if (responseCleared.value || !detailRequest.value) return null
  if (result.value) return { ...result.value, request: { ...result.value.request, ...detailRequest.value } }
  if (localFailure.value) return { request: detailRequest.value, response: null, error: localFailure.value }
  if (liveEvents.value) return { request: detailRequest.value, response: { sse: { events: liveEvents.value } }, pending: sending.value }
  return null
})

/** 仅按已返回目标响应的 HTTP 状态计算色调；没有响应时为 neutral。 */
const responseTone = computed(() => statusTone(result.value?.response?.status))
/** 即时与历史结果共用分类，不把本地 504 写进目标响应。 */
const resultInfo = computed(() => (result.value ? presentResult(result.value, result.value.response?.status) : null))

/** 传给 AI 助手与后端工具的当前接口测试设置（含冷却时间）。 */
const consoleSettingsPayload = computed(() => ({
  timeoutMs: consoleSettings.timeoutMs,
  sseDurationMs: consoleSettings.sseDurationMs,
  sseMaxEvents: consoleSettings.sseMaxEvents,
  followRedirects: consoleSettings.followRedirects,
  cooldownMs: consoleSettings.cooldownMs,
}))

/** 供 AI 助手小Y 当前测试结果读取工具使用的脱敏实时快照。 */
const assistantContext = computed(() =>
  buildConsoleTestContext({
    method: form.method,
    url: joinTargetUrl(urlPrefix.value, form.url) || form.url,
    headers: activeHeaders.value,
    bodyMode: form.bodyMode,
    body: form.body,
    result: result.value,
    liveEvents: liveEvents.value,
    localFailure: localFailure.value,
    responseCleared: responseCleared.value,
    settings: consoleSettingsPayload.value,
  }),
)

/** 当前 AI 多轮套件的子用例集合（优先展示已落库详情，运行中实时合并 case_done 帧）。 */
const suiteCases = computed<ConsoleSubCase[]>(() => {
  if (liveSubCases.value.length > 0) return liveSubCases.value
  return activeDetail.value?.sub_cases ?? []
})

const showSuiteOverview = computed(
  () =>
    suiteCases.value.length > 0 ||
    activeDetail.value?.source === 'ai' ||
    activeRunProgress.value?.source === 'ai',
)

const suiteTitle = computed(
  () => activeDetail.value?.title || activeRunProgress.value?.title || 'AI 接口测试套件',
)

const suiteBadge = computed(
  () => activeDetail.value?.badge || activeRunProgress.value?.badge || 'AI 测试',
)

const suiteSummary = computed(
  () => activeDetail.value?.summary || activeRunProgress.value?.summary || null,
)

function subCaseBadgeMeta(subCase: ConsoleSubCase): { label: string; tone: string } {
  if (!subCase.ok) {
    return { label: subCase.error_kind || '传输失败', tone: 'danger' }
  }
  if (subCase.assertion_summary) {
    return subCase.assertion_summary.passed
      ? { label: subCase.status_code ? `HTTP ${subCase.status_code} · 通过` : '通过', tone: 'success' }
      : { label: subCase.status_code ? `HTTP ${subCase.status_code} · 断言未通过` : '断言未通过', tone: 'danger' }
  }
  if (subCase.status_code != null) {
    const tone =
      subCase.status_code >= 500
        ? 'danger'
        : subCase.status_code >= 400
          ? 'warning'
          : subCase.status_code >= 300
            ? 'info'
            : subCase.status_code >= 200
              ? 'success'
              : 'neutral'
    return { label: `HTTP ${subCase.status_code}`, tone }
  }
  return { label: '完成', tone: 'neutral' }
}

const readonlyStatusBadge = computed(() => {
  if (activeSubCase.value) return subCaseBadgeMeta(activeSubCase.value)
  if (result.value?.response) {
    return { label: `HTTP ${result.value.response.status}`, tone: responseTone.value }
  }
  if (resultInfo.value) {
    return { label: resultInfo.value.title, tone: resultInfo.value.tone }
  }
  return null
})

/**
 * 目标返回 3xx 且没有跟随时，提取它的 Location 供界面提示。
 *
 * 作用：默认不跟随跳转是安全设计（避免被带到授权范围外），但用户只看到一个
 * 302 会以为请求失败或没生效。这里明确告诉他“跳转被刻意拦下了、指向哪里”。
 * 实现：只在响应状态是 3xx 且 ``redirects`` 为空（即未跟随）时返回；否则为 null。
 */
const blockedRedirect = computed(() => {
  const response = result.value?.response
  if (!response || response.status < 300 || response.status >= 400) return null
  if (response.redirects.length) return null
  const location = response.headers['location']?.[0] ?? null
  return { status: response.status, reason: response.reason, location }
})

function addHeaderRow() {
  if (isReadOnlyReplay.value) return
  addHeaderRowRaw()
}

function removeHeaderRow(index: number) {
  if (isReadOnlyReplay.value) return
  removeHeaderRowRaw(index)
}

/** 将示例方法和地址填入表单；仅示例包含正文时切换为 JSON 并覆盖正文，不自动发送。 */
function applySample(sample: (typeof SAMPLES)[number]) {
  if (isReadOnlyReplay.value) return
  form.method = sample.method
  urlPrefix.value = ''
  form.url = sample.url
  if (sample.body) {
    form.bodyMode = 'json'
    form.body = sample.body
    tab.value = 'body'
  }
}

/** Keep the path when switching prefixes and restore the full address on deselection. */
function selectUrlPrefix(prefix: string) {
  if (isReadOnlyReplay.value) return
  form.url = prefix ? urlSuffix(form.url, prefix, urlPrefix.value) : joinTargetUrl(urlPrefix.value, form.url) || form.url
  urlPrefix.value = prefix
}

/** 对合法 JSON 正文进行美化；非法文本保留原状并给出提示。 */
function formatBody() {
  if (isReadOnlyReplay.value) return
  if (!looksLikeJson(form.body)) {
    useToastStore().push('warning', '当前内容不是合法 JSON', '格式化只在内容是 JSON 时可用')
    return
  }
  form.body = prettyJson(form.body)
}

/** 仅中止本地后台运行观察流，绝不取消后端正在执行的任务。 */
function detachBackgroundObserver() {
  if (observerAbort) {
    observerAbort.abort()
    observerAbort = null
  }
  followingRunId = null
  cancellingRun.value = false
  if (sending.value && !activeRequest) {
    sending.value = false
  }
}

/** 清除只读回放锁定状态并同步共享工作台状态。 */
function exitReadonlyReplayState() {
  readonlyReplay.value = null
  activeSubCase.value = null
  workbenchRuns?.exitReadonlyReplay('console')
  consoleStore?.exitReadonlyReplay()
}

/** 用户点击「退出只读」：解除请求输入锁定并保留当前回填的参数供查看或修改。 */
function exitReadonlyReplay() {
  exitReadonlyReplayState()
  syncStoreState()
}

function upsertLiveSubCase(subCase: ConsoleSubCase) {
  const existingIndex = liveSubCases.value.findIndex((item) => item.id === subCase.id)
  if (existingIndex >= 0) {
    const next = liveSubCases.value.slice()
    next[existingIndex] = subCase
    liveSubCases.value = next
  } else {
    liveSubCases.value = [...liveSubCases.value, subCase]
  }
}

function subCaseToConsoleResult(subCase: ConsoleSubCase): ConsoleResult {
  const hasHttpResponse = subCase.ok || subCase.status_code != null
  return hasHttpResponse
    ? {
        ok: subCase.ok,
        history_id: subCase.parent_id,
        request: { method: subCase.method, url: subCase.url },
        response: {
          status: subCase.status_code ?? 0,
          reason: subCase.response_reason ?? null,
          http_version: subCase.http_version ?? null,
          headers: normalizeResponseHeaders(subCase.response_headers),
          body: subCase.response_preview,
          body_bytes: subCase.response_bytes ?? 0,
          body_truncated: (subCase.response_preview?.length ?? 0) >= 8192,
          body_encoding: null,
          elapsed_ms: subCase.elapsed_ms ?? 0,
          redirects: Array.isArray(subCase.redirects) ? subCase.redirects : [],
          sse: subCase.sse ?? null,
        },
        error_kind: subCase.error_kind,
        error_message: subCase.error_message,
        elapsed_ms: subCase.elapsed_ms ?? 0,
        result_code: subCase.result_code,
        result_status: subCase.result_status,
      }
    : {
        ok: false,
        history_id: subCase.parent_id,
        request: { method: subCase.method, url: subCase.url },
        response: null,
        error_kind: subCase.error_kind,
        error_message: subCase.error_message,
        elapsed_ms: subCase.elapsed_ms ?? 0,
        result_code: subCase.result_code,
        result_status: subCase.result_status,
      }
}

function historyDetailToConsoleResult(record: ConsoleHistoryDetail): ConsoleResult {
  const res: ConsoleResult = record.ok
    ? {
        ok: true,
        history_id: record.id,
        request: { method: record.method, url: record.url },
        response: {
          status: record.status_code ?? 0,
          reason: record.response_reason ?? null,
          http_version: record.http_version ?? null,
          headers: normalizeResponseHeaders(record.response_headers),
          body: record.response_preview,
          body_bytes: record.response_bytes,
          body_truncated: (record.response_preview?.length ?? 0) >= 8192,
          body_encoding: null,
          elapsed_ms: record.elapsed_ms,
          redirects: Array.isArray(record.redirects) ? record.redirects : [],
          sse: record.sse ?? null,
        },
        error_kind: null,
        error_message: null,
        elapsed_ms: record.elapsed_ms,
      }
    : {
        ok: false,
        history_id: record.id,
        request: { method: record.method, url: record.url },
        response: null,
        error_kind: record.error_kind,
        error_message: record.error_message,
        elapsed_ms: record.elapsed_ms,
      }
  res.result_code = record.result_code
  res.result_status = record.result_status
  return res
}

/** 回填一条子用例快照并进入只读回放锁定模式，禁止误改 AI 用例参数。 */
function applySubCaseSnapshot(subCase: ConsoleSubCase, fallbackHistoryId?: string, fallbackLabel?: string) {
  const historyId = subCase.parent_id || fallbackHistoryId || activeHistoryId.value || ''
  activeHistoryId.value = historyId || null
  if (historyId) {
    workbenchRuns?.setActiveHistory('console', historyId)
  }

  const nextMethod = normalizeHttpMethod(subCase.method)
  const nextBodyMode = normalizeBodyMode(subCase.body_mode, nextMethod)
  form.method = nextMethod
  urlPrefix.value = ''
  form.url = subCase.url
  form.bodyMode = nextBodyMode
  form.body = subCase.request_body ?? ''
  headerRows.value = toHeaderRows(subCase.request_headers)

  responseCleared.value = false
  localFailure.value = null
  liveEvents.value = null
  detailRequest.value = {
    method: nextMethod,
    url: subCase.url,
    headers: toStringRecord(subCase.request_headers),
    body_mode: nextBodyMode,
    body: subCase.request_body,
  }
  result.value = subCaseToConsoleResult(subCase)

  const replay: ReadonlyReplayState = {
    historyId,
    caseId: subCase.id,
    round: subCase.round_index ?? 1,
    label: subCase.label || fallbackLabel || `${subCase.method} ${subCase.url}`,
  }
  readonlyReplay.value = replay
  activeSubCase.value = subCase
  workbenchRuns?.enterReadonlyReplay('console', replay)
  consoleStore?.enterReadonlyReplay(replay, subCase)
  syncStoreState()
}

/** 从 AI 多轮测试概览面板点击一条子用例，立即进入只读回放锁定视图。 */
function selectSubCase(subCase: ConsoleSubCase) {
  applySubCaseSnapshot(subCase, activeHistoryId.value ?? undefined)
}

/** 订阅后台运行的实时进度流（支持手动持久化请求、AI 多轮套件及跨页切回重连）。 */
async function attachToBackgroundRun(
  runId: string,
  hint?: Partial<ConsoleRunSummary> | null,
  options: { isReconnect?: boolean; controller?: AbortController } = {},
) {
  if (typeof followConsoleRun !== 'function' || !runId || unmounted) return
  if (followingRunId === runId && observerAbort && !observerAbort.signal.aborted) return

  if (observerAbort) observerAbort.abort()
  const controller = options.controller ?? new AbortController()
  observerAbort = controller
  followingRunId = runId
  activeRunId.value = runId
  sending.value = true
  cancellingRun.value = false
  workbenchRuns?.setActiveRun('console', runId)

  if (hint) {
    activeRunProgress.value = {
      id: runId,
      source: hint.source ?? activeRunProgress.value?.source ?? 'manual',
      title: hint.title ?? activeRunProgress.value?.title ?? null,
      badge: hint.badge ?? activeRunProgress.value?.badge ?? null,
      status: hint.status ?? 'running',
      stage: hint.stage ?? 'executing',
      method: hint.method ?? form.method,
      url: hint.url ?? form.url,
      ok: hint.ok ?? false,
      status_code: hint.status_code ?? null,
      elapsed_ms: hint.elapsed_ms ?? 0,
      error_kind: hint.error_kind ?? null,
      error_message: hint.error_message ?? null,
      total_rounds: hint.total_rounds ?? 1,
      current_round: hint.current_round ?? 0,
      total_cases: hint.total_cases ?? 1,
      completed_cases: hint.completed_cases ?? 0,
      cooldown_ms: hint.cooldown_ms ?? consoleSettings.cooldownMs,
      cooldown_remaining_ms: hint.cooldown_remaining_ms ?? null,
      summary: hint.summary ?? null,
      created_at: hint.created_at ?? null,
    }
  }
  consoleStore?.setActiveRun(runId, activeRunProgress.value)

  try {
    const terminal = await followConsoleRun(runId, controller.signal, (frame) => {
      if (controller.signal.aborted || unmounted || observerAbort !== controller) return
      if (frame.type === 'sse_start') {
        if (!responseCleared.value && !isReadOnlyReplay.value) {
          liveEvents.value ??= []
        }
        return
      }
      if (frame.type === 'sse_event') {
        if (!responseCleared.value && !isReadOnlyReplay.value) {
          const events = (liveEvents.value ??= [])
          events.push(frame.event)
        }
        return
      }
      if (frame.type === 'progress') {
        activeRunProgress.value = {
          id: frame.id,
          source: frame.source,
          title: frame.title,
          badge: frame.badge,
          status: frame.status,
          stage: frame.stage,
          method: frame.method,
          url: frame.url,
          ok: frame.ok,
          status_code: frame.status_code,
          elapsed_ms: frame.elapsed_ms,
          error_kind: frame.error_kind,
          error_message: frame.error_message,
          total_rounds: frame.total_rounds,
          current_round: frame.current_round,
          total_cases: frame.total_cases,
          completed_cases: frame.completed_cases,
          cooldown_ms: frame.cooldown_ms,
          cooldown_remaining_ms: frame.cooldown_remaining_ms,
          summary: frame.summary,
          created_at: frame.created_at,
        }
        return
      }
      if (frame.type === 'cooling_down') {
        activeRunProgress.value = {
          ...(activeRunProgress.value ?? {
            id: runId,
            source: 'ai',
            title: null,
            badge: 'AI 测试',
            status: 'running',
            stage: 'cooling_down',
            method: form.method,
            url: form.url,
            ok: false,
            status_code: null,
            elapsed_ms: 0,
            error_kind: null,
            error_message: null,
            total_rounds: frame.round,
            current_round: frame.round,
            total_cases: 1,
            completed_cases: 0,
            cooldown_ms: frame.cooldown_ms,
            cooldown_remaining_ms: frame.cooldown_remaining_ms,
            summary: null,
            created_at: null,
          }),
          stage: 'cooling_down',
          current_round: frame.round,
          cooldown_ms: frame.cooldown_ms,
          cooldown_remaining_ms: frame.cooldown_remaining_ms,
        }
        return
      }
      if (frame.type === 'case_done') {
        upsertLiveSubCase(frame.case)
        activeRunProgress.value = {
          ...(activeRunProgress.value ?? {
            id: runId,
            source: 'ai',
            title: null,
            badge: 'AI 测试',
            status: 'running',
            stage: 'executing',
            method: frame.case.method,
            url: frame.case.url,
            ok: frame.case.ok,
            status_code: frame.case.status_code,
            elapsed_ms: frame.case.elapsed_ms ?? 0,
            error_kind: frame.case.error_kind,
            error_message: frame.case.error_message,
            total_rounds: frame.round,
            current_round: frame.round,
            total_cases: frame.total_cases,
            completed_cases: frame.completed_cases,
            cooldown_ms: consoleSettings.cooldownMs,
            cooldown_remaining_ms: null,
            summary: null,
            created_at: null,
          }),
          stage: 'executing',
          current_round: frame.round,
          completed_cases: frame.completed_cases,
          total_cases: frame.total_cases,
          cooldown_remaining_ms: null,
        }
      }
    })

    if (controller.signal.aborted || unmounted || observerAbort !== controller) return

    activeRunId.value = null
    activeRunProgress.value = null
    workbenchRuns?.setActiveRun('console', null)
    consoleStore?.setActiveRun(null)

    if (terminal.type === 'result') {
      const completed = terminal.result
      if (completed.history_id) {
        activeHistoryId.value = completed.history_id
        workbenchRuns?.setActiveHistory('console', completed.history_id)
      }
      if (!responseCleared.value && !isReadOnlyReplay.value) {
        result.value = completed
        if (completed.ok) {
          liveEvents.value = null
          if (!completed.response?.sse) tab.value = 'body'
        } else {
          const feedback = presentResult(completed)
          useToastStore().push(feedback.tone === 'warning' ? 'warning' : 'danger', feedback.title, feedback.message)
        }
      }
    } else if (terminal.type === 'done') {
      const detail =
        terminal.record ??
        (typeof readConsoleHistory === 'function' ? await readConsoleHistory(runId).catch(() => null) : null)
      if (detail) {
        activeDetail.value = detail
        if (detail.sub_cases?.length) {
          liveSubCases.value = detail.sub_cases.slice()
        }
        if (!responseCleared.value && !isReadOnlyReplay.value && (!activeHistoryId.value || activeHistoryId.value === runId)) {
          activeHistoryId.value = detail.id
          workbenchRuns?.setActiveHistory('console', detail.id)
          applyHistoryDetail(detail, { silent: true, preserveForm: detail.source !== 'ai' && !options.isReconnect })
        }
      }
    }

    await loadHistory()
    syncStoreState()
  } catch (error) {
    if (controller.signal.aborted || unmounted || observerAbort !== controller) return
    activeRunId.value = null
    activeRunProgress.value = null
    workbenchRuns?.setActiveRun('console', null)
    consoleStore?.setActiveRun(null)
    if (options.isReconnect && isApiStatus(error, [404])) {
      await loadHistory()
      return
    }
    if (!responseCleared.value) {
      localFailure.value = presentManagementError(
        error instanceof ApiError ? error.code : '',
        error instanceof ApiError ? error.status : 0,
      )
      useToastStore().push(
        localFailure.value.tone === 'neutral' ? 'info' : localFailure.value.tone,
        localFailure.value.title,
        describeError(error),
      )
    }
  } finally {
    if (observerAbort === controller) {
      observerAbort = null
      followingRunId = null
      sending.value = false
      cancellingRun.value = false
      syncStoreState()
    }
  }
}

/** 兼容旧版或单测桩的即时 NDJSON 发送路径（服务端同样由 ConsoleRunManager 承载）。 */
async function sendViaLiveRequest(
  payload: ConsoleRequestInput,
  controller: AbortController,
  generation: number,
) {
  try {
    const completed = await sendConsoleRequest(
      payload,
      (frame) => {
        if (controller.signal.aborted || activeRequest !== controller || responseCleared.value) return
        const events = (liveEvents.value ??= [])
        if (frame.type === 'sse_event') events.push(frame.event)
      },
      controller.signal,
    )
    if (controller.signal.aborted || generation !== sendGeneration) return
    if (activeRequest === controller && completed.history_id) {
      activeHistoryId.value = completed.history_id
      workbenchRuns?.setActiveHistory('console', completed.history_id)
    }
    if (activeRequest === controller && !responseCleared.value) {
      result.value = completed
      if (completed.ok) {
        liveEvents.value = null
        if (!completed.response?.sse) tab.value = 'body'
      } else {
        const feedback = presentResult(completed)
        useToastStore().push(feedback.tone === 'warning' ? 'warning' : 'danger', feedback.title, feedback.message)
      }
    }
    await loadHistory()
    syncStoreState()
  } catch (error) {
    if (controller.signal.aborted || activeRequest !== controller || responseCleared.value) return
    localFailure.value = presentManagementError(
      error instanceof ApiError ? error.code : '',
      error instanceof ApiError ? error.status : 0,
    )
    useToastStore().push(
      localFailure.value.tone === 'neutral' ? 'info' : localFailure.value.tone,
      localFailure.value.title,
      describeError(error),
    )
  } finally {
    if (activeRequest === controller) {
      activeRequest = undefined
      sending.value = false
      syncStoreState()
    }
  }
}

/** 用当前全局设置发送表单请求，显示响应并刷新历史；空地址拦截，目标传输失败与管理接口拒绝分别提示，始终解除 sending。 */
async function send() {
  if (isReadOnlyReplay.value || sending.value || restoringHistory.value) return
  const url = joinTargetUrl(urlPrefix.value, form.url)
  if (!url) {
    useToastStore().push('warning', urlPrefix.value ? '请填写有效的 URL 后缀' : '请先填写请求地址')
    return
  }
  const generation = ++sendGeneration
  sending.value = true
  responseCleared.value = false
  const controller = new AbortController()
  activeRequest = controller
  result.value = null
  liveEvents.value = null
  localFailure.value = null
  activeDetail.value = null
  liveSubCases.value = []

  const payload: ConsoleRequestInput = {
    method: form.method,
    url,
    headers: { ...activeHeaders.value },
    body_mode: form.bodyMode,
    body: form.bodyMode === 'none' ? null : form.body,
    timeout_ms: consoleSettings.timeoutMs,
    sse_max_events: consoleSettings.sseMaxEvents,
    sse_duration_ms: consoleSettings.sseDurationMs,
    follow_redirects: consoleSettings.followRedirects,
  }
  detailRequest.value = payload

  if (!hasPersistentConsoleApi) {
    await sendViaLiveRequest(payload, controller, generation)
    return
  }

  try {
    const summary = await startConsoleRun(payload)
    if (controller.signal.aborted || unmounted || generation !== sendGeneration) return
    activeRequest = undefined
    activeRunId.value = summary.id
    activeRunProgress.value = summary
    activeHistoryId.value = summary.id
    workbenchRuns?.setActiveRun('console', summary.id)
    workbenchRuns?.setActiveHistory('console', summary.id)
    syncStoreState()
    void loadHistory()
    await attachToBackgroundRun(summary.id, summary, { isReconnect: false, controller })
  } catch (error) {
    if (isApiStatus(error, [404, 405])) {
      await sendViaLiveRequest(payload, controller, generation)
      return
    }
    if (controller.signal.aborted || activeRequest !== controller || responseCleared.value) return
    localFailure.value = presentManagementError(
      error instanceof ApiError ? error.code : '',
      error instanceof ApiError ? error.status : 0,
    )
    useToastStore().push(
      localFailure.value.tone === 'neutral' ? 'info' : localFailure.value.tone,
      localFailure.value.title,
      describeError(error),
    )
    if (activeRequest === controller) {
      activeRequest = undefined
      sending.value = false
      syncStoreState()
    }
  }
}

/** 显式取消当前后台控制台运行，保留已完成子用例并更新历史状态。 */
async function stopActiveRun() {
  const runId = activeRunId.value
  if (!runId || cancellingRun.value || typeof cancelConsoleRun !== 'function') return
  cancellingRun.value = true
  try {
    const detail = await cancelConsoleRun(runId)
    if (unmounted) return
    detachBackgroundObserver()
    activeRunId.value = null
    activeRunProgress.value = null
    workbenchRuns?.setActiveRun('console', null)
    consoleStore?.setActiveRun(null)
    if (detail) {
      activeDetail.value = detail
      if (detail.sub_cases?.length) liveSubCases.value = detail.sub_cases.slice()
      if (!isReadOnlyReplay.value) {
        applyHistoryDetail(detail, { silent: true, preserveForm: true })
      }
    }
    await loadHistory()
    useToastStore().push('info', '已停止后台测试', '已完成的结果已保留到历史记录。')
  } catch (error) {
    useToastStore().push('danger', '停止测试失败', describeError(error))
  } finally {
    cancellingRun.value = false
    syncStoreState()
  }
}

/** 切页卸载时仅保存内存快照并中止本地流观察器，绝不取消后端正在运行的测试任务。 */
onBeforeUnmount(() => {
  unmounted = true
  syncStoreState()
  activeRequest?.abort()
  activeRequest = undefined
  if (observerAbort) {
    observerAbort.abort()
    observerAbort = null
  }
  followingRunId = null
  historyRead?.abort()
  historyRead = undefined
  subCaseRead?.abort()
  subCaseRead = undefined
})

/** Clear the current display while allowing an in-flight request to finish and save its history. */
function clearResponse() {
  responseCleared.value = true
  detailRequest.value = null
  result.value = null
  liveEvents.value = null
  localFailure.value = null
  consoleStore?.clearResponse()
}

/** 再次测试：解除只读锁定、清除历史与响应绑定并自增 draftVersion 开启全新空白 AI 对话，保留当前可编辑请求参数。 */
function retestCurrent() {
  sendGeneration++
  detachBackgroundObserver()
  activeRequest = undefined
  sending.value = false
  historyRead?.abort()
  historyRead = undefined
  subCaseRead?.abort()
  subCaseRead = undefined
  restoringHistory.value = false
  exitReadonlyReplayState()
  activeDetail.value = null
  liveSubCases.value = []
  activeRunId.value = null
  activeRunProgress.value = null
  activeHistoryId.value = null
  workbenchRuns?.setActiveRun('console', null)
  workbenchRuns?.setActiveHistory('console', null)
  clearResponse()
  responseCleared.value = false
  draftVersion.value++
  consoleStore?.resetForRetest(draftVersion.value)
  syncStoreState()
}

/** Open a blank request while any previous response finishes saving to history. */
function newTest() {
  sendGeneration++
  detachBackgroundObserver()
  activeRequest = undefined
  sending.value = false
  historyRead?.abort()
  historyRead = undefined
  subCaseRead?.abort()
  subCaseRead = undefined
  restoringHistory.value = false
  exitReadonlyReplayState()
  activeDetail.value = null
  liveSubCases.value = []
  activeRunId.value = null
  activeRunProgress.value = null
  workbenchRuns?.setActiveRun('console', null)
  workbenchRuns?.setActiveHistory('console', null)
  clearResponse()
  activeHistoryId.value = null
  draftVersion.value++
  form.method = 'GET'
  form.url = ''
  form.bodyMode = 'none'
  form.body = ''
  headerRows.value = [{ name: '', value: '' }]
  urlPrefix.value = ''
  tab.value = 'body'
  consoleStore?.resetForNewTest(draftVersion.value)
}

/** 读取最近 HTTP 历史并更新加载态；失败清空列表但不影响主请求流程。 */
async function loadHistory() {
  if (typeof listConsoleHistory !== 'function') return
  historyLoading.value = true
  try {
    const page = await listConsoleHistory(30)
    history.value = page.items
  } catch {
    // 历史加载失败不影响主流程，界面在历史区显示空态即可。
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

/** 回填历史详情的请求参数、响应证据与子用例列表；silent=true 时不弹出回填提示。 */
function applyHistoryDetail(
  record: ConsoleHistoryDetail,
  options: { silent?: boolean; preserveForm?: boolean } = {},
) {
  sendGeneration++
  activeRequest?.abort()
  activeRequest = undefined
  if (followingRunId && followingRunId !== record.id) {
    detachBackgroundObserver()
  }
  if (record.status !== 'running' && record.status !== 'queued') {
    sending.value = false
  }
  exitReadonlyReplayState()
  responseCleared.value = false
  liveEvents.value = null
  localFailure.value = null
  activeHistoryId.value = record.id
  workbenchRuns?.setActiveHistory('console', record.id)
  activeDetail.value = record
  liveSubCases.value = Array.isArray(record.sub_cases) ? record.sub_cases.slice() : []

  const nextMethod = normalizeHttpMethod(record.method)
  const nextBodyMode = normalizeBodyMode(record.body_mode, nextMethod)
  if (!options.preserveForm) {
    form.method = nextMethod
    urlPrefix.value = ''
    form.url = record.url
    form.bodyMode = nextBodyMode
    form.body = record.request_body ?? record.body_preview ?? ''
    headerRows.value = toHeaderRows(record.request_headers)
  }

  detailRequest.value = {
    method: nextMethod,
    url: record.url,
    headers: toStringRecord(record.request_headers),
    body_mode: nextBodyMode,
    body: record.request_body ?? record.body_preview,
  }

  if (record.status === 'running' || record.status === 'queued') {
    result.value = null
    if (typeof followConsoleRun === 'function') {
      void attachToBackgroundRun(record.id, {
        id: record.id,
        source: record.source ?? 'manual',
        title: record.title ?? null,
        badge: record.badge ?? null,
        status: record.status,
        method: record.method,
        url: record.url,
        total_rounds: record.total_rounds ?? 1,
        cooldown_ms: record.cooldown_ms ?? consoleSettings.cooldownMs,
      }, { isReconnect: true })
    }
    syncStoreState()
    return
  }

  result.value = historyDetailToConsoleResult(record)
  syncStoreState()

  if (!options.silent) {
    const legacyBody =
      record.body_mode !== 'none' &&
      record.request_body == null &&
      (record.body_preview?.length ?? 0) >= 8192
    useToastStore().push(
      legacyBody ? 'warning' : 'info',
      '已回填历史请求',
      legacyBody
        ? '这条旧记录只有请求体预览，请补全正文后再发送。'
        : '请确认参数后发送，已脱敏的凭据需重新填写。',
    )
  }
}

/** 回填历史的请求参数和脱敏后的头，不覆盖全局设置。 */
function restore(record: ConsoleHistory) {
  applyHistoryDetail(record, { silent: false })
}

/** 删除指定 HTTP 历史，成功后移除本地条目并清理相关选中项；失败提示并保留列表。 */
async function removeHistory(record: ConsoleHistory) {
  try {
    await deleteConsoleHistory(record.id)
    history.value = history.value.filter((item) => item.id !== record.id)
    removeTestChat('console', record.id)
    if (activeHistoryId.value === record.id) {
      activeHistoryId.value = null
      activeDetail.value = null
      liveSubCases.value = []
      workbenchRuns?.setActiveHistory('console', null)
    }
    if (readonlyReplay.value?.historyId === record.id) {
      exitReadonlyReplayState()
    }
    syncStoreState()
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 把请求记录映射成共享历史列的条目（含 AI 标注徽标与后台运行脉冲态）。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  history.value.map((record) => {
    const running =
      record.status === 'running' ||
      record.status === 'queued' ||
      record.id === activeRunId.value
    const aiBadgeLabel = record.badge?.trim() || (record.source === 'ai' ? 'AI 测试' : '')
    const badges: HistoryBadge[] = []
    if (aiBadgeLabel) {
      badges.push({ label: aiBadgeLabel, tone: 'info' })
    }
    if ((record.total_rounds ?? 1) > 1) {
      badges.push({ label: `${record.total_rounds} 轮`, tone: 'neutral' })
    }

    let statusText: string
    let tone: string
    if (running) {
      const cooling =
        activeRunProgress.value?.id === record.id && activeRunProgress.value.stage === 'cooling_down'
      statusText = cooling ? '冷却中' : '运行中'
      tone = 'info'
    } else if (record.status === 'cancelled') {
      statusText = '已取消'
      tone = 'neutral'
    } else if (record.status === 'interrupted') {
      statusText = '已中断'
      tone = 'warning'
    } else if (record.source === 'ai' && (record.sub_case_count ?? 0) > 0) {
      if (record.ok) {
        statusText = `通过 · ${record.sub_case_count} 条`
        tone = 'success'
      } else if (record.error_kind) {
        const info = presentResult(record)
        statusText = info.title
        tone = info.tone
      } else {
        statusText = `异常 · ${record.sub_case_count} 条`
        tone = 'warning'
      }
    } else {
      statusText = record.ok ? String(record.status_code ?? '完成') : presentResult(record).title
      tone = record.ok ? statusTone(record.status_code) : presentResult(record).tone
    }

    return {
      id: record.id,
      label: record.title?.trim() || `${record.method} ${record.url}`,
      status: statusText,
      tone,
      meta: running
        ? formatRelative(record.created_at)
        : `${formatRelative(record.created_at)} · ${formatDuration(record.elapsed_ms)}`,
      ...(badges.length ? { badges } : {}),
      running,
      source: record.source,
      raw: record,
      at: record.created_at,
    }
  }),
)

/** Fetch one complete input. A late detail response must not overwrite a newer selection. */
async function restoreById(id: string) {
  if (sending.value && !activeRunId.value) {
    useToastStore().push('info', '请等待当前请求结束后再切换历史')
    return
  }
  historyRead?.abort()
  const controller = new AbortController()
  historyRead = controller
  restoringHistory.value = true
  try {
    const record = await readConsoleHistory(id, controller.signal)
    if (!controller.signal.aborted) restore(record)
  } catch (error) {
    if (!controller.signal.aborted) useToastStore().push('danger', '读取历史失败', describeError(error))
  } finally {
    if (historyRead === controller) {
      historyRead = undefined
      restoringHistory.value = false
    }
  }
}

/** 按共享历史列表提供的 ID 发起删除；记录未加载时忽略，异步失败由 removeHistory 提示。 */
function removeHistoryById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) removeHistory(target)
}

/** 点击 AI 回复中的子用例链接块：拉取该条子用例快照并进入只读回放锁定模式。 */
async function handleSelectCase(block: CaseLinkBlock) {
  subCaseRead?.abort()
  const controller = new AbortController()
  subCaseRead = controller

  const findLocalCase = (): ConsoleSubCase | undefined =>
    liveSubCases.value.find((item) => item.id === block.case_id && (!item.parent_id || item.parent_id === block.history_id)) ??
    activeDetail.value?.sub_cases?.find((item) => item.id === block.case_id)

  try {
    let subCase: ConsoleSubCase | null = null
    if (typeof getConsoleSubCase === 'function') {
      try {
        subCase = await getConsoleSubCase(block.history_id, block.case_id, controller.signal)
      } catch (error) {
        if (controller.signal.aborted) return
        subCase = findLocalCase() ?? null
        if (!subCase) throw error
      }
    } else {
      subCase = findLocalCase() ?? null
    }

    if (controller.signal.aborted || unmounted) return
    if (!subCase) {
      useToastStore().push('warning', '未找到子用例快照', '该测试记录可能已被删除。')
      return
    }

    applySubCaseSnapshot(subCase, block.history_id, block.label)

    // 后台补齐所属父历史详情（用于展示完整子用例列表），绝不覆盖已锁定的子用例回放。
    if (activeDetail.value?.id !== block.history_id && typeof readConsoleHistory === 'function') {
      void readConsoleHistory(block.history_id)
        .then((detail) => {
          if (!unmounted && activeHistoryId.value === block.history_id) {
            activeDetail.value = detail
            if (detail.sub_cases?.length) liveSubCases.value = detail.sub_cases.slice()
            syncStoreState()
          }
        })
        .catch(() => {})
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      useToastStore().push('danger', '读取子用例失败', describeError(error))
    }
  } finally {
    if (subCaseRead === controller) {
      subCaseRead = undefined
    }
  }
}

/** AI 助手触发新的接口测试套件时立即绑定历史 ID、刷新列表并订阅实时进度流。 */
async function handleRunBound(payload: AssistantRunBoundPayload & { historyId?: string | null }) {
  const historyId = payload.history_id || payload.historyId || null
  if (!historyId) return
  exitReadonlyReplayState()
  activeHistoryId.value = historyId
  activeRunId.value = historyId
  activeDetail.value = null
  liveSubCases.value = []
  workbenchRuns?.setActiveHistory('console', historyId)
  workbenchRuns?.setActiveRun('console', historyId)
  syncStoreState()
  void loadHistory()
  if (typeof followConsoleRun === 'function') {
    void attachToBackgroundRun(
      historyId,
      {
        id: historyId,
        source: 'ai',
        title: payload.title ?? null,
        badge: payload.badge ?? 'AI 测试',
        status: 'running',
        stage: 'executing',
      },
      { isReconnect: false },
    )
  }
}

/** AI 助手运行完成后刷新历史列表，并在当前选中该记录且未处于只读子用例回放时更新详情。 */
async function handleRunCompleted(payload: { scope: PageScope; historyId: string | null }) {
  await loadHistory()
  const targetId = payload.historyId ?? activeHistoryId.value
  if (!targetId || activeHistoryId.value !== targetId || typeof readConsoleHistory !== 'function') return
  try {
    const detail = await readConsoleHistory(targetId)
    if (unmounted || activeHistoryId.value !== targetId) return
    activeDetail.value = detail
    if (detail.sub_cases?.length) liveSubCases.value = detail.sub_cases.slice()
    if (!isReadOnlyReplay.value) {
      applyHistoryDetail(detail, { silent: true, preserveForm: detail.source !== 'ai' })
    } else {
      syncStoreState()
    }
  } catch {
    // 列表已刷新，忽略瞬时详情拉取失败。
  }
}

/** 切回本页时查询后台活跃控制台运行并自动续接实时流，若离开期间已结束则回填终态结果。 */
async function reconnectActiveConsoleRun() {
  if (unmounted || typeof getActiveConsoleRuns !== 'function') return
  const preferredId = workbenchRuns?.activeRunId.console ?? consoleStore?.activeRunId ?? activeRunId.value
  let items: ConsoleRunSummary[] = []
  try {
    const active = await getActiveConsoleRuns()
    items = active.items ?? []
  } catch {
    return
  }
  if (unmounted) return

  const matched =
    (preferredId ? items.find((item) => item.id === preferredId) : undefined) ?? items[0]
  if (matched) {
    activeRunId.value = matched.id
    activeRunProgress.value = matched
    if (!activeHistoryId.value || activeHistoryId.value === matched.id) {
      activeHistoryId.value = matched.id
      workbenchRuns?.setActiveHistory('console', matched.id)
    }
    if (typeof followConsoleRun === 'function') {
      void attachToBackgroundRun(matched.id, matched, { isReconnect: true })
    }
    return
  }

  if (preferredId) {
    activeRunId.value = null
    activeRunProgress.value = null
    workbenchRuns?.setActiveRun('console', null)
    consoleStore?.setActiveRun(null)
    await loadHistory()
    if (!isReadOnlyReplay.value && typeof readConsoleHistory === 'function') {
      try {
        const detail = await readConsoleHistory(preferredId)
        if (!unmounted && !isReadOnlyReplay.value) {
          applyHistoryDetail(detail, { silent: true, preserveForm: detail.source !== 'ai' })
        }
      } catch {
        // 已完成记录若已被清理则静默跳过。
      }
    }
  }
}

onMounted(() => {
  restoreFromStore()
  if (!readonlyReplay.value && workbenchRuns?.readonlyReplay.console) {
    readonlyReplay.value = { ...workbenchRuns.readonlyReplay.console }
  }
  if (readonlyReplay.value && !activeSubCase.value && typeof getConsoleSubCase === 'function') {
    const replay = readonlyReplay.value
    void handleSelectCase({
      type: 'case_link',
      scope: 'console',
      history_id: replay.historyId,
      case_id: replay.caseId,
      round: replay.round ?? 1,
      label: replay.label ?? replay.caseId,
      status: 'completed',
      tone: 'info',
    })
  }
  void loadHistory()
  void reconnectActiveConsoleRun()
})
</script>

<template>
  <TestWorkspace
    history-key="api-agent.layout.console.history"
    editor-key="api-agent.layout.console.request-width"
    assistant-key="api-agent.layout.console.chat"
    :editor-width="460"
    hide-history-on-narrow
  >
    <!-- 左栏：历史请求，与 apifox 的「快捷请求」列表同位 -->
    <template #history>
      <HistoryList
        title="请求历史"
        :items="historyEntries"
        :active-id="activeHistoryId"
        empty-text="还没有请求记录。发送一次请求后，这里会保留脱敏摘要。"
        :on-delete="removeHistoryById"
        :on-new-test="newTest"
        @select="restoreById"
      >
        <template #prefix="{ item }">
          <span v-if="isSseHistory(item.raw as ConsoleHistory | undefined)" class="tag tag-accent">SSE</span>
        </template>
      </HistoryList>
    </template>

    <template #editor>
      <section class="workbench scroll-y">
        <header class="request-head">
          <h1 class="panel-title"><ArrowUpRight :size="15" aria-hidden="true" /> 请求</h1>
          <div class="request-head-actions">
            <button
              v-if="activeHistoryId && !sending && !isReadOnlyReplay"
              type="button"
              class="btn btn-sm retest-btn"
              title="以当前请求参数开启全新测试与全新 AI 对话"
              @click="retestCurrent"
            >
              <RotateCcw :size="13" aria-hidden="true" />
              <span>再次测试</span>
            </button>
            <span class="protocol-label">HTTP</span>
          </div>
        </header>

        <div v-if="isReadOnlyReplay" class="readonly-banner" role="status" aria-label="只读回放模式">
          <div class="readonly-banner-main">
            <Lock :size="14" class="readonly-icon" aria-hidden="true" />
            <div class="readonly-banner-text">
              <div class="readonly-banner-title">
                <span>只读回放</span>
                <span class="tag tag-info">第 {{ readonlyReplay?.round ?? 1 }} 轮</span>
                <span class="readonly-case-label">{{ readonlyReplay?.label || activeSubCase?.label }}</span>
                <span v-if="readonlyStatusBadge" class="tag" :class="`tag-${readonlyStatusBadge.tone}`">
                  {{ readonlyStatusBadge.label }}
                </span>
              </div>
              <p class="hint readonly-banner-hint">当前正在查看 AI 测试子用例快照，请求参数已锁定。</p>
            </div>
          </div>
          <div class="readonly-banner-actions">
            <button type="button" class="btn btn-accent btn-sm" @click="retestCurrent">
              <RotateCcw :size="13" aria-hidden="true" />
              <span>再次测试</span>
            </button>
            <button type="button" class="btn btn-sm" @click="newTest">新建测试</button>
            <button type="button" class="btn btn-ghost btn-sm" @click="exitReadonlyReplay">退出只读</button>
          </div>
        </div>

        <div class="url-bar">
          <UrlInput
            v-model:address="form.url"
            :model-value="urlPrefix"
            label="请求地址"
            placeholder="http://127.0.0.1:8080/api/..."
            :disabled="isReadOnlyReplay"
            @update:model-value="selectUrlPrefix"
            @submit="send"
          >
            <template #leading>
              <select
                v-model="form.method"
                class="method-select method"
                :class="`method-${form.method.toLowerCase()}`"
                aria-label="HTTP 方法"
                :disabled="isReadOnlyReplay"
              >
                <option v-for="method in METHODS" :key="method" :value="method">{{ method }}</option>
              </select>
            </template>
          </UrlInput>
          <button
            class="btn btn-accent"
            :disabled="isReadOnlyReplay || sending || restoringHistory"
            @click="send"
          >
            <Loader2 v-if="sending" :size="14" class="spinning" />
            <Send v-else :size="14" />
            <span>{{ sending ? '发送中' : '发送' }}</span>
          </button>
          <button
            v-if="activeRunId"
            type="button"
            class="btn stop-run-btn"
            :disabled="cancellingRun"
            title="显式停止当前后台运行的测试"
            @click="stopActiveRun"
          >
            <Square :size="12" aria-hidden="true" />
            <span>{{ cancellingRun ? '停止中' : '停止测试' }}</span>
          </button>
        </div>

        <div class="samples">
          <span class="samples-label">示例</span>
          <button
            v-for="sample in SAMPLES"
            :key="sample.label"
            class="sample-chip"
            :disabled="isReadOnlyReplay"
            @click="applySample(sample)"
          >
            {{ sample.label }}
          </button>
        </div>

        <div class="tabs">
          <button
            v-for="item in [
              { key: 'body', label: '请求体' },
              { key: 'headers', label: `请求头 ${headerCount || ''}`.trim() },
              { key: 'meta', label: '设置' },
            ]"
            :key="item.key"
            class="tab"
            :class="{ 'tab-active': tab === item.key }"
            @click="tab = item.key as typeof tab"
          >
            {{ item.label }}
          </button>
          <div class="tabs-fill" />
          <span class="send-hint hint"><kbd>Enter</kbd> 发送</span>
        </div>

        <div v-if="tab === 'body'" class="tab-body">
          <div class="body-modes">
            <button
              v-for="mode in BODY_MODES"
              :key="mode.value"
              class="mode-chip"
              :class="{ 'mode-chip-active': form.bodyMode === mode.value }"
              :disabled="isReadOnlyReplay || (!hasBody && mode.value !== 'none')"
              @click="form.bodyMode = mode.value"
            >
              {{ mode.label }}
            </button>
            <div class="tabs-fill" />
            <button
              v-if="looksLikeJson(form.body)"
              class="btn btn-ghost btn-sm"
              :disabled="isReadOnlyReplay"
              @click="formatBody"
            >
              <Braces :size="13" />
              格式化
            </button>
          </div>
          <p v-if="!hasBody" class="hint body-hint">
            {{ form.method }} 不支持请求体，后端会拒绝携带 body 的该类请求。
          </p>
          <textarea
            v-model="form.body"
            class="textarea mono body-editor"
            spellcheck="false"
            :disabled="isReadOnlyReplay || form.bodyMode === 'none'"
            :readonly="isReadOnlyReplay"
            :placeholder="form.bodyMode === 'none' ? '选择一种请求体类型后开始编辑' : '在此粘贴请求体'"
          />
        </div>

        <div v-else-if="tab === 'headers'" class="tab-body">
          <div class="header-table">
            <div class="header-row header-row-head">
              <span>名称</span>
              <span>值</span>
              <span />
            </div>
            <div v-for="(row, index) in headerRows" :key="index" class="header-row">
              <input
                v-model="row.name"
                class="input mono"
                placeholder="Content-Type"
                spellcheck="false"
                :disabled="isReadOnlyReplay"
                :readonly="isReadOnlyReplay"
              />
              <input
                v-model="row.value"
                class="input mono"
                placeholder="application/json"
                spellcheck="false"
                :disabled="isReadOnlyReplay"
                :readonly="isReadOnlyReplay"
              />
              <button
                class="btn btn-ghost btn-icon btn-sm"
                title="删除这一行"
                :disabled="isReadOnlyReplay"
                @click="removeHeaderRow(index)"
              >
                <Trash2 :size="13" />
              </button>
            </div>
          </div>
          <button class="btn btn-sm add-header" :disabled="isReadOnlyReplay" @click="addHeaderRow">+ 添加请求头</button>
          <p class="hint">
            Host、Content-Length、Connection 等由传输层管理的头不允许覆盖；
            凭据类头会以
            <code>[redacted]</code> 的形式保存到历史。
          </p>
        </div>

        <div v-else class="tab-body meta-grid">
          <p class="hint settings-hint">适用于本浏览器的所有接口测试请求，不随历史回填而改变。</p>
          <TestSettingsFields class="settings-fields" group="console" />
          <div class="scope-note">
            <Clock3 :size="14" />
            <div>
              <strong>目标策略与护栏</strong>
              <p>
                默认支持任意合法 HTTP/HTTPS 域名、局域网 IP、回环与公网地址；
                若服务端配置了 <code>TARGET_ALLOWED_ORIGINS</code>，仅放行白名单源。
                跨源重定向自动剥离认证头，禁止 URL 内嵌账号密码与覆盖传输保留头。
              </p>
            </div>
          </div>
        </div>
      </section>
    </template>

    <template #results>
      <div class="response">
        <div class="response-head">
          <h2 class="panel-title"><ArrowDownLeft :size="15" aria-hidden="true" /> 响应</h2>
          <div class="response-stats">
            <span v-if="sending && !responseCleared" class="tag tag-info" role="status">
              <Loader2 :size="14" class="spinning" aria-hidden="true" />
              {{
                activeRunProgress?.stage === 'cooling_down'
                  ? `轮间冷却中 · ${activeRunProgress.cooldown_remaining_ms ?? activeRunProgress.cooldown_ms}ms`
                  : activeRunProgress?.source === 'ai'
                    ? `AI 测试中 · 第 ${Math.max(1, activeRunProgress.current_round)}/${activeRunProgress.total_rounds} 轮 (${activeRunProgress.completed_cases}/${activeRunProgress.total_cases})`
                    : responseEvents
                      ? `正在接收 SSE · ${responseEvents.length} 个事件`
                      : '正在等待响应…'
              }}
            </span>
            <template v-else-if="result">
              <span v-if="result.response" class="tag status-dot" :class="`tag-${responseTone}`">
                {{ result.response.status }} {{ result.response.reason ?? '' }}
              </span>
              <span v-else class="tag status-dot" :class="`tag-${resultInfo?.tone}`">
                {{ resultInfo?.title }}<template v-if="resultCodeLabel(resultInfo?.resultStatus)"> · {{ resultCodeLabel(resultInfo?.resultStatus) }}</template>
              </span>
              <span class="tag">{{ formatDuration(result.elapsed_ms) }}</span>
              <span v-if="result.response" class="tag">{{ formatBytes(result.response.body_bytes) }}</span>
              <span v-if="result.response?.sse" class="tag tag-accent">SSE · {{ result.response.sse.events.length }} 事件 · {{ result.response.sse.stop_reason }}</span>
              <CopyButton v-if="responseBodyText" :text="responseBodyText" label="复制响应" />
            </template>
            <ResponseDetails
              :key="detailRequest ? 'response' : 'empty'"
              :data="responseDetails"
              :note="activeHistoryId ? '历史响应显示当时保存的脱敏数据和正文预览。' : undefined"
            />
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              title="清除当前响应显示"
              :disabled="!result && !localFailure && (!sending || responseCleared)"
              @click="clearResponse"
            >
              清除
            </button>
          </div>
        </div>

        <section v-if="showSuiteOverview" class="suite-overview" aria-label="AI 测试子用例概览">
          <div class="suite-overview-head">
            <span class="tag tag-info"><Sparkles :size="11" aria-hidden="true" /> {{ suiteBadge }}</span>
            <strong class="suite-title truncate" :title="suiteTitle">{{ suiteTitle }}</strong>
            <span v-if="(activeDetail?.total_rounds ?? activeRunProgress?.total_rounds ?? 1) > 1" class="tag">
              共 {{ activeDetail?.total_rounds ?? activeRunProgress?.total_rounds }} 轮
            </span>
            <span v-if="suiteCases.length" class="hint suite-count">{{ suiteCases.length }} 条子用例（点击进入只读回放）</span>
          </div>
          <p v-if="suiteSummary" class="suite-summary">{{ suiteSummary }}</p>
          <div v-if="suiteCases.length" class="suite-case-list">
            <button
              v-for="subCase in suiteCases"
              :key="subCase.id"
              type="button"
              class="suite-case-chip"
              :class="{ 'suite-case-chip-active': activeSubCase?.id === subCase.id }"
              :aria-current="activeSubCase?.id === subCase.id ? 'true' : undefined"
              @click="selectSubCase(subCase)"
            >
              <span class="suite-case-round">R{{ subCase.round_index }}</span>
              <span class="suite-case-method mono">{{ subCase.method }}</span>
              <span class="suite-case-label truncate">{{ subCase.label }}</span>
              <span class="tag" :class="`tag-${subCaseBadgeMeta(subCase).tone}`">
                {{ subCaseBadgeMeta(subCase).label }}
              </span>
            </button>
          </div>
        </section>

        <div class="response-split">
          <div class="response-content code-block scroll-y">
            <div v-if="activeSubCase?.assertion_summary?.checks?.length" class="assertion-summary">
              <span class="mini-label">子用例断言</span>
              <div class="assertion-checks">
                <span
                  v-for="(check, idx) in activeSubCase.assertion_summary.checks"
                  :key="idx"
                  class="tag"
                  :class="check.passed ? 'tag-success' : 'tag-danger'"
                >
                  {{ check.kind }}: 期望 {{ String(check.expected) }} / 实际 {{ String(check.actual) }}
                </span>
              </div>
            </div>

            <div v-if="responseEvents !== null" class="sse-events" aria-label="已接收的 SSE 事件">
              <p v-if="!responseEvents.length" class="hint">{{ sending ? '等待首个事件…' : '未收到事件。' }}</p>
              <div v-for="(event, index) in responseEvents" :key="index" class="sse-event">
                <span class="mini-label">{{ index + 1 }} · {{ event.event }}</span>
                <pre>{{ event.data }}</pre>
              </div>
            </div>

            <div v-if="localFailure" class="response-error">
              <div class="error-title">{{ localFailure.title }}</div>
              <p class="error-detail">{{ localFailure.message }}</p>
              <p class="hint">结果码：<code>{{ localFailure.code }}</code><template v-if="localFailure.resultStatus != null">；管理接口 HTTP {{ localFailure.resultStatus }}</template></p>
            </div>
            <div v-else-if="!result && (!sending || responseCleared) && responseEvents === null" class="response-empty">
              <EmptyState
                :icon="FileCode2"
                title="还没有响应"
                :description="COPY.empty.consoleResponse"
              />
            </div>

            <div v-else-if="result && !result.response" class="response-error">
              <div class="error-title">{{ resultInfo?.title }}</div>
              <p class="error-detail">{{ resultInfo?.message }}</p>
              <p class="hint">
                结果码：<code>{{ resultInfo?.code }}</code>；错误类别：<code>{{ result.error_kind }}</code>。
                这表示本服务没能取得完整、可评估的 HTTP 响应，
                不代表目标返回了某个错误状态码。
              </p>
            </div>

            <template v-if="result?.response">
              <div v-if="result.response.body_truncated" class="truncated-note">
                {{ result.response.sse ? '仅显示已观察到的 SSE 内容。' : '响应体已截断展示（显示前 1,000,000 字符）。' }}
              </div>
              <div v-if="blockedRedirect" class="redirect-blocked">
                <div class="redirect-head">
                  <ArrowUpRight :size="12" />
                  目标返回 <strong>{{ describeRedirect(blockedRedirect.status, blockedRedirect.reason) }}</strong>，默认不跟随跳转
                </div>
                <div v-if="blockedRedirect.location" class="redirect-hop">
                  <span class="tag tag-info">{{ blockedRedirect.status }}</span>
                  <span class="hop-arrow">→</span>
                  <span class="hop-url">{{ blockedRedirect.location }}</span>
                </div>
                <div class="redirect-hint">如需跟随，在「设置」中开启「跟随重定向」。跳转仍会逐跳校验目标范围。</div>
              </div>
              <div v-if="result.response.redirects.length" class="redirect-note">
                <div class="redirect-head">已跟随跳转：</div>
                <div v-for="(hop, index) in result.response.redirects" :key="index" class="redirect-hop">
                  <span class="tag" :class="`tag-${statusTone(hop.status)}`">
                    {{ describeRedirect(hop.status, hop.reason) }}
                  </span>
                  <span class="hop-arrow">→</span>
                  <span class="hop-url">{{ hop.url }}</span>
                  <span v-if="hop.cross_origin" class="tag tag-warning">跨源 · 已剥离凭据</span>
                </div>
                <div class="redirect-hop">
                  <span class="tag" :class="`tag-${statusTone(result.response.status)}`">
                    {{ describeRedirect(result.response.status, result.response.reason) }}
                  </span>
                  <span class="hop-final">最终响应</span>
                </div>
              </div>
              <pre v-if="responseEvents === null" class="raw-response">{{ responseBodyText || '（空响应体）' }}</pre>
            </template>
          </div>
          <section class="response-headers" aria-label="响应头">
            <div class="mini-label">响应头</div>
            <div class="response-header-list scroll-y">
              <p v-if="!responseHeaders.length" class="hint">{{ sending && !responseCleared ? '等待响应头…' : '暂无响应头。' }}</p>
              <div v-for="([name, values]) in responseHeaders" :key="name" class="header-line">
                <span class="header-name">{{ name }}</span>
                <span class="header-value">{{ values.join(', ') }}</span>
              </div>
            </div>
          </section>
        </div>
      </div>
    </template>

    <template #assistant>
      <TaskChat
        scope="console"
        :execution-id="activeHistoryId"
        :draft-version="draftVersion"
        :current-test="assistantContext"
        :settings="consoleSettingsPayload"
        :active-case-id="readonlyReplay?.caseId ?? null"
        @select-case="handleSelectCase"
        @run-bound="handleRunBound"
        @run-completed="handleRunCompleted"
      />
    </template>
  </TestWorkspace>
</template>

<style scoped>

/* ---------- 左：历史 ---------- */
.tone-success { color: var(--success); }
.tone-info { color: var(--info); }
.tone-warning { color: var(--warning); }
.tone-danger { color: var(--danger); }
.tone-neutral { color: var(--text-tertiary); }

/* ---------- 右：工作台 ---------- */
.workbench {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
  /* 请求编辑区使用统一主题表面，保持分栏边缘连续。 */
  background: var(--bg-surface);
}

.request-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex: none;
  padding: 14px 20px 0;
}

.request-head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}

.panel-title svg { color: var(--text-tertiary); }

.protocol-label {
  padding: 2px 6px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  font: 10px/1.3 var(--font-mono);
  letter-spacing: 0.06em;
}

.readonly-banner {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 10px 20px 0;
  padding: 10px 12px;
  border: 1px solid var(--info-border);
  border-radius: var(--radius-md);
  background: var(--info-soft);
  flex: none;
}

.readonly-banner-main {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
}

.readonly-icon {
  color: var(--info);
  flex: none;
  margin-top: 2px;
}

.readonly-banner-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.readonly-banner-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.readonly-case-label {
  color: var(--text-secondary);
  font-weight: 500;
  word-break: break-word;
}

.readonly-banner-hint {
  margin: 0;
  font-size: 11px;
}

.readonly-banner-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.url-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px 10px;
  flex: none;
}

.method-select {
  width: 91px;
  flex: none;
  border: 0;
  border-right: 1px solid var(--border-subtle);
  border-radius: calc(var(--radius-md) - 1px) 0 0 calc(var(--radius-md) - 1px);
  background: var(--bg-subtle);
  font-size: 12px;
  padding: 0 9px;
}

.method-select:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.method-select:focus-visible { outline-offset: -2px; }

.url-bar .btn {
  height: 38px;
  padding: 0 16px;
}

.stop-run-btn {
  border: 1px solid var(--danger-border);
  background: var(--danger-soft);
  color: var(--danger);
  white-space: nowrap;
}

.stop-run-btn:hover:not(:disabled) {
  border-color: var(--danger);
}

.samples {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 20px 10px;
  flex: none;
  flex-wrap: wrap;
}

.samples-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.sample-chip {
  height: 24px;
  padding: 0 7px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.sample-chip:hover:not(:disabled) {
  border-color: var(--accent-border);
  color: var(--accent);
  background: var(--accent-soft);
}

.sample-chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.tabs {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.tab-active {
  font-weight: 600;
}

.send-hint { white-space: nowrap; }
.send-hint kbd {
  padding: 1px 4px;
  border: 1px solid var(--border-default);
  border-radius: 4px;
  font: 10px var(--font-mono);
}

.tabs-fill {
  flex: 1;
}

.tab-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 12px 20px;
}

.body-modes {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 9px;
}

.mode-chip {
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.mode-chip:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.mode-chip-active {
  background: var(--accent-soft);
  border-color: var(--accent-border);
  color: var(--accent);
}

.mode-chip:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.body-editor {
  flex: 1;
  min-height: 100px;
  background: var(--bg-sunken);
}

.body-editor:disabled {
  opacity: 0.72;
  cursor: not-allowed;
}

.body-hint {
  margin: 0 0 8px;
}

.header-table {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.header-row {
  display: grid;
  grid-template-columns: 1fr 1.4fr 28px;
  gap: 6px;
  align-items: center;
}

.header-row-head {
  font-size: 11px;
  color: var(--text-tertiary);
  padding: 0 2px;
}

.header-row .input {
  height: 30px;
}

.header-row .input:disabled {
  opacity: 0.72;
  cursor: not-allowed;
}

.add-header {
  margin-top: 9px;
}

.meta-grid {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 14px 20px;
  align-items: start;
}

.settings-hint {
  grid-column: 1 / -1;
  margin: 0;
}

.settings-fields { grid-column: 1 / -1; }

.switch-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.switch {
  width: 15px;
  height: 15px;
  margin-top: 2px;
  accent-color: var(--accent);
}

.scope-note {
  grid-column: 1 / -1;
  display: flex;
  gap: 9px;
  padding: 11px 13px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.scope-note p {
  margin: 3px 0 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-tertiary);
}

.scope-note strong {
  font-size: 12px;
}

/* ---------- 响应 ---------- */
.response {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  /* 不再画上边框：与请求区同底色，靠分隔条的手柄提示可调，避免双层浅灰线。 */
  background: var(--bg-surface);
}

.response-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-rail);
  flex: none;
}

.response-head .panel-title { flex: none; }

.response-stats {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
  min-height: 28px;
}

.response-stats > .btn,
.response-stats > .tag {
  flex: none;
  white-space: nowrap;
}

.response-stats > :deep(*) {
  flex: none;
  white-space: nowrap;
}

.suite-overview {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  flex: none;
}

.suite-overview-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.suite-title {
  font-size: 12.5px;
  color: var(--text-primary);
  min-width: 0;
}

.suite-count {
  margin-left: auto;
  font-size: 11px;
}

.suite-summary {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.suite-case-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 104px;
  overflow-y: auto;
}

.suite-case-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 4px 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 11.5px;
}

.suite-case-chip:hover {
  border-color: var(--accent-border);
  background: var(--bg-hover);
}

.suite-case-chip-active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.suite-case-round {
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg-subtle);
  color: var(--text-tertiary);
  font: 10px/1.2 var(--font-mono);
}

.suite-case-method {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-secondary);
}

.suite-case-label {
  max-width: 220px;
}

.assertion-summary {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}

.assertion-checks {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.response-empty {
  display: grid;
  align-items: center;
  flex: 1;
}

.response-empty :deep(.empty-icon) {
  margin-bottom: 5px;
  box-shadow: 0 0 0 6px var(--bg-rail), 0 0 0 7px var(--border-subtle);
}

.response-error {
  padding: 20px 16px;
}

.error-title {
  font-size: 13px;
  font-weight: 650;
  color: var(--danger);
}

.error-detail {
  margin: 6px 0 8px;
  font-size: 12px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  word-break: break-word;
}

.response-content {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sse-event + .sse-event { border-top: 1px solid var(--border-subtle); }
.sse-event pre, .raw-response { font-family: var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.sse-event pre { margin: 4px 0 8px; }
.raw-response { margin: 0; }

.truncated-note,
.redirect-note {
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 11.5px;
}

.redirect-note {
  background: var(--info-soft);
  color: var(--info);
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 4px;
}

/* 未跟随的 3xx：独立提示，说明跳转被刻意拦下而不是失败。 */
.redirect-blocked {
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--info-soft);
  color: var(--info);
  font-size: 11.5px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  border-left: 2px solid var(--info);
}

.redirect-head {
  display: flex;
  align-items: center;
  gap: 5px;
  font-weight: 600;
}

.redirect-hint {
  color: var(--text-tertiary);
  font-size: 11px;
}

.redirect-hop {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 11.5px;
}

.hop-arrow {
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}

.hop-url {
  font-family: var(--font-mono);
  color: var(--text-secondary);
  word-break: break-all;
}

.hop-final {
  color: var(--text-tertiary);
  font-size: 11px;
}

.hop {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-family: var(--font-mono);
}

.response-split {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 290px;
  grid-template-rows: minmax(0, 1fr);
  padding: 12px 20px 16px;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.code-block {
  margin: 0;
  padding: 11px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  overflow: auto;
}

.response-headers {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  padding: 2px 0;
}

.response-header-list { min-height: 0; overflow: auto; }
.response-header-list .hint { margin: 0; }

.mini-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
  margin-bottom: 6px;
}

.header-line {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 5px 0;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
}

.header-name {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text-secondary);
}

.header-value {
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  word-break: break-all;
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1180px) {
  .response-split {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: minmax(0, 1fr) minmax(54px, 30%);
  }
}

@media (max-width: 680px) {
  .request-head, .url-bar, .samples, .tabs, .tab-body, .response-head, .suite-overview, .response-split {
    padding-inline: 12px;
  }
  .readonly-banner {
    margin-inline: 12px;
  }
  .url-bar .btn { padding: 0 10px; }
  .send-hint { display: none; }
  .panel-title { white-space: nowrap; }
}
</style>
