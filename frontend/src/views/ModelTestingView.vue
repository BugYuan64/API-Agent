<script setup lang="ts">
/**
 * 大模型测试工作台（scope="model_testing"）：
 * 1) 接入共享 TestWorkspace（results-max=1400），提供左侧持久化探测历史、中间输入配置、右侧能力矩阵与底部 AI 助手小Y；
 * 2) 探测由后端 ModelTestManager 持久化后台执行，切页仅断开本地观察流、绝不取消在跑任务；
 * 3) 临时 API Key 仅驻留前端内存与 Pinia 工作台仓库，绝不写入 localStorage 或后端数据库；
 * 4) 支持 AI 助手多轮编排进度同步、子用例链接只读回放锁定及显式「再次测试」重置为全新空白对话。
 */

import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  CheckCircle2,
  Cpu,
  Eye,
  EyeOff,
  Filter,
  KeyRound,
  Lock,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  Square,
  Trash2,
  Wrench,
  XCircle,
} from 'lucide-vue-next'
import AppModal from '@/components/AppModal.vue'
import EmptyState from '@/components/EmptyState.vue'
import HistoryList, { type HistoryBadge, type HistoryEntry } from '@/components/HistoryList.vue'
import TaskChat from '@/components/TaskChat.vue'
import TestSettingsFields from '@/components/TestSettingsFields.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import UrlInput from '@/components/UrlInput.vue'
import { ApiError, describeError } from '@/api/client'
import {
  cancelModelTestRun,
  deleteModelTestRun,
  discoverProviderModels,
  followModelTestRun,
  getActiveModelTestRuns,
  getLLMConfig,
  getModelTestRun,
  getModelTestSubCase,
  listModelTestRuns,
  probeProviderModels,
  probeProviderModelsStream,
  startModelTestRun,
} from '@/api/endpoints'
import type {
  AssistantRunBoundPayload,
  CapabilityResult,
  CaseLinkBlock,
  LLMConfig,
  ModalityItem,
  ModelCategory,
  ModelDiscoverResult,
  ModelProbeStreamFrame,
  ModelProfile,
  ModelProtocol,
  ModelTestActiveRunItem,
  ModelTestLiveFrame,
  ModelTestRunDetail,
  ModelTestRunListItem,
  ModelTestRunStatus,
  ModelTestSubCaseSnapshot,
  ProbeCheckName,
  ResolvedModelProtocol,
} from '@/api/types'
import { buildModelTestingContext } from '@/lib/assistantContext'
import { removeTestChat } from '@/lib/chatHistory'
import { formatRelative } from '@/lib/format'
import { modelTestingSettings } from '@/lib/testSettings'
import { joinTargetUrl, urlSuffix } from '@/lib/urlPrefixes'
import {
  type AvailabilityFilter,
  type InputModalityFilter,
  type ModelTestingReadonlyReplay,
  type OutputModalityFilter,
  type ToolFilter,
  useModelTestingWorkbenchStore,
} from '@/stores/modelTestingWorkbench'
import { useToastStore } from '@/stores/toasts'
import { useWorkbenchRunsStore } from '@/stores/workbenchRuns'

type ModelProbeQueueState = 'queued' | 'probing'

interface ProbeQueueJob {
  modelId: string
  payload: {
    base_url: string
    api_key?: string
    use_saved_key: boolean
    protocol: ModelProtocol
    checks: ProbeCheckName[]
    timeout_seconds: number
  }
  resolve: () => void
}

/** 仅在旧版测试环境未提供持久化 runs API 时启用的前端回退队列并发上限。 */
const QUEUE_CONCURRENCY = 4

function resolveRoute() {
  if (typeof useRoute !== 'function') return null
  try {
    return useRoute()
  } catch {
    return null
  }
}

function resolveRouter() {
  if (typeof useRouter !== 'function') return null
  try {
    return useRouter()
  } catch {
    return null
  }
}

function resolveWorkbenchRuns() {
  if (typeof useWorkbenchRunsStore !== 'function') return null
  try {
    return useWorkbenchRunsStore()
  } catch {
    return null
  }
}

function resolveWorkbenchStore() {
  if (typeof useModelTestingWorkbenchStore !== 'function') return null
  try {
    return useModelTestingWorkbenchStore()
  } catch {
    return null
  }
}

const route = resolveRoute()
const router = resolveRouter()
const workbenchRuns = resolveWorkbenchRuns()
const workbenchStore = resolveWorkbenchStore()
const toast = useToastStore()

const hasPersistentRunApi =
  typeof startModelTestRun === 'function' && typeof followModelTestRun === 'function'

/** 接入表单状态：API Key 仅保留在内存与 Pinia 仓库中，绝不写入 localStorage。 */
const form = reactive({
  baseUrl: workbenchStore?.baseUrl ?? '',
  apiKey: workbenchStore?.apiKey ?? '',
  useSavedKey: workbenchStore?.useSavedKey ?? false,
  protocol: (workbenchStore?.protocol ?? 'auto') as ModelProtocol,
  timeoutSeconds: workbenchStore?.timeoutSeconds ?? 25,
})

const UNTESTED_CAP: CapabilityResult = {
  status: 'untested',
  source: 'untested',
  latency_ms: null,
  ttft_ms: null,
  detail: null,
}

function capOf(cap?: CapabilityResult | null): CapabilityResult {
  return cap ?? UNTESTED_CAP
}

const PROTOCOL_OPTIONS: { value: ModelProtocol; label: string }[] = [
  { value: 'auto', label: '自动识别（推荐）' },
  { value: 'openai', label: 'OpenAI 兼容' },
  { value: 'anthropic', label: 'Anthropic Messages' },
  { value: 'gemini', label: 'Gemini generateContent' },
]

const PROTOCOL_LABELS: Record<ResolvedModelProtocol, string> = {
  openai: 'OpenAI 兼容',
  anthropic: 'Anthropic',
  gemini: 'Gemini',
}

const VALID_PROTOCOLS: ReadonlySet<string> = new Set<ModelProtocol>(['auto', 'openai', 'anthropic', 'gemini'])
const ALL_CHECK_NAMES: readonly ProbeCheckName[] = [
  'availability',
  'tool_calling',
  'input_modalities',
  'output_modalities',
  'streaming',
  'structured_output',
]

const showKey = ref(false)
const urlPrefix = ref(workbenchStore?.urlPrefix ?? '')
const savedConfig = ref<LLMConfig | null>(null)
const discovering = ref(false)

/** 后台持久化探测与历史状态。 */
const history = ref<ModelTestRunListItem[]>(workbenchStore ? [...workbenchStore.history] : [])
const activeRunId = ref<string | null>(workbenchStore?.activeRunId ?? null)
const activeHistoryId = ref<string | null>(workbenchStore?.activeHistoryId ?? null)
const currentRun = ref<ModelTestRunDetail | null>(workbenchStore?.currentRun ?? null)
const draftVersion = ref(workbenchStore?.draftVersion ?? 0)
const readonlyReplay = ref<ModelTestingReadonlyReplay | null>(
  workbenchStore?.readonlyReplay ? { ...workbenchStore.readonlyReplay } : null,
)
const isReadOnlyReplay = computed(() => readonlyReplay.value !== null)
const liveRunning = ref(false)
const liveStage = ref<'idle' | 'executing' | 'cooling_down' | 'finished'>(
  workbenchStore?.liveProgress.stage ?? 'idle',
)
const currentRound = ref(workbenchStore?.liveProgress.currentRound ?? 0)
const totalRounds = ref(workbenchStore?.liveProgress.totalRounds ?? 1)
const completedRounds = ref(workbenchStore?.liveProgress.completedRounds ?? 0)
const cooldownMs = ref(workbenchStore?.liveProgress.cooldownMs ?? 500)
const cooldownRemainingMs = ref<number | null>(workbenchStore?.liveProgress.cooldownRemainingMs ?? null)
const cancellingRun = ref(false)
const chatBusy = ref(false)

/** 模型级排队/探测进度映射（同时服务持久化后台流与旧版兼容回退队列）。 */
const probingStates = ref<Record<string, ModelProbeQueueState>>({})
const probingChecks = ref<Record<string, ProbeCheckName[]>>({})
const probingCompleted = ref(workbenchStore?.liveProgress.completedCases ?? 0)
const probingTotal = ref(workbenchStore?.liveProgress.totalCases ?? 0)
const probing = computed(() => liveRunning.value || Object.keys(probingStates.value).length > 0)
const activeProbingCount = computed(
  () => Object.values(probingStates.value).filter((state) => state === 'probing').length,
)
const queuedProbingCount = computed(
  () => Object.values(probingStates.value).filter((state) => state === 'queued').length,
)
const probeProgressPercent = computed(() =>
  probingTotal.value > 0 ? Math.min(100, Math.round((probingCompleted.value / probingTotal.value) * 100)) : 0,
)

/** 旧版非持久化探测回退队列状态（仅在单元测试未注入 startModelTestRun 时启用）。 */
const pendingQueue: ProbeQueueJob[] = []
const activeControllers = new Map<string, AbortController>()
let activeWorkerCount = 0
let stopRequested = false
let batchAvailableCount = 0
let batchToolSupportedCount = 0
let batchErrorCount = 0
let lastSingleProbeMessage = ''

/** 本地流观察器句柄：卸载时仅中止本地观察连接，绝不取消后台探测任务。 */
let observerAbort: AbortController | null = null
let observerGeneration = 0
let historyReadAbort: AbortController | null = null
let historyReadGeneration = 0
let unmounted = false

const discoverMeta = ref<Omit<ModelDiscoverResult, 'models'> | null>(
  workbenchStore?.discoverMeta ? { ...workbenchStore.discoverMeta } : null,
)
const models = ref<ModelProfile[]>(workbenchStore ? [...workbenchStore.models] : [])
const selectedIds = ref<string[]>(workbenchStore ? [...workbenchStore.selectedIds] : [])
const selectableProbeIds = computed(() => selectedIds.value.filter((id) => !probingStates.value[id]))
const activeModelId = ref<string | null>(workbenchStore?.activeModelId ?? null)
const clearConfirmOpen = ref(false)

/** 右侧模型列表多维筛选条件。 */
const searchQuery = ref(workbenchStore?.searchQuery ?? '')
const categoryFilter = ref<'all' | ModelCategory>(workbenchStore?.categoryFilter ?? 'all')
const availabilityFilter = ref<AvailabilityFilter>(workbenchStore?.availabilityFilter ?? 'all')
const toolFilter = ref<ToolFilter>(workbenchStore?.toolFilter ?? 'all')
const inputModalityFilter = ref<InputModalityFilter>(workbenchStore?.inputModalityFilter ?? 'all')
const outputModalityFilter = ref<OutputModalityFilter>(workbenchStore?.outputModalityFilter ?? 'all')

/** 可勾选的探测能力模块，后续扩展新探测项时只需在此追加。 */
const checks = reactive<Record<ProbeCheckName, boolean>>({
  availability: workbenchStore ? workbenchStore.checks.availability : true,
  tool_calling: workbenchStore ? workbenchStore.checks.tool_calling : true,
  input_modalities: workbenchStore ? workbenchStore.checks.input_modalities : true,
  output_modalities: workbenchStore ? workbenchStore.checks.output_modalities : false,
  streaming: workbenchStore ? workbenchStore.checks.streaming : false,
  structured_output: workbenchStore ? workbenchStore.checks.structured_output : false,
})

const CATEGORY_LABELS: Record<ModelCategory, string> = {
  chat: '对话',
  reasoning: '推理',
  embedding: '向量嵌入',
  rerank: '重排',
  image: '图像',
  audio: '语音',
  video: '视频',
  moderation: '内容审核',
  unknown: '未知',
}

const MODALITY_LABELS: Record<ModalityItem['modality'], string> = {
  text: '文本',
  image: '图像',
  audio: '音频',
  video: '视频',
  file: '文件',
  reasoning: '推理链',
  embedding: '向量',
  json: 'JSON',
}

const CHECK_OPTIONS: { key: ProbeCheckName; label: string; hint: string }[] = [
  { key: 'availability', label: '模型可用性', hint: '最小开销验证对话、向量、重排、审核、图像或语音端点是否可用' },
  { key: 'tool_calling', label: '工具调用', hint: '声明函数工具并检测是否返回工具调用' },
  { key: 'input_modalities', label: '输入模态', hint: '包含文本与轻量 1×1 PNG 视觉输入探测' },
  { key: 'output_modalities', label: '输出模态', hint: '观测文本、推理链、图像、音频与向量等输出能力' },
  { key: 'streaming', label: '流式 SSE', hint: '验证流式事件输出并测量首字延迟（TTFT）' },
  { key: 'structured_output', label: '结构化输出', hint: '探测 json_schema / responseSchema 与 json_object 约束输出' },
]

const AVAILABILITY_OPTIONS: { value: AvailabilityFilter; label: string }[] = [
  { value: 'all', label: '全部可用性' },
  { value: 'available', label: '实测可用' },
  { value: 'unavailable', label: '不可用 / 错误' },
  { value: 'unsupported', label: '暂不支持探测' },
  { value: 'untested', label: '尚未探测' },
]

const TOOL_OPTIONS: { value: ToolFilter; label: string }[] = [
  { value: 'all', label: '全部工具能力' },
  { value: 'supported', label: '支持工具调用' },
  { value: 'partial', label: '仅接受参数' },
  { value: 'unsupported', label: '不支持工具' },
  { value: 'untested', label: '未测 / 仅元数据' },
]

const INPUT_MODALITY_OPTIONS: { value: InputModalityFilter; label: string }[] = [
  { value: 'all', label: '全部输入模态' },
  { value: 'multimodal', label: '含多模态输入' },
  { value: 'image', label: '支持图像输入' },
  { value: 'image_probed', label: '实测支持图像' },
  { value: 'audio', label: '含音频输入' },
  { value: 'video', label: '含视频输入' },
  { value: 'text_only', label: '仅文本输入' },
]

const OUTPUT_MODALITY_OPTIONS: { value: OutputModalityFilter; label: string }[] = [
  { value: 'all', label: '全部输出模态' },
  { value: 'reasoning', label: '支持推理链' },
  { value: 'text', label: '文本输出' },
  { value: 'image', label: '图像输出' },
  { value: 'audio', label: '音频输出' },
  { value: 'embedding', label: '向量输出' },
]

const effectiveTimeoutSeconds = computed(() => {
  const configured = typeof modelTestingSettings === 'object' && modelTestingSettings
    ? modelTestingSettings.timeoutSeconds
    : undefined
  return typeof configured === 'number' && Number.isFinite(configured) ? configured : form.timeoutSeconds
})

const effectiveConcurrency = computed(() => {
  const configured = typeof modelTestingSettings === 'object' && modelTestingSettings
    ? modelTestingSettings.concurrency
    : undefined
  return typeof configured === 'number' && Number.isFinite(configured) ? configured : 4
})

const effectiveCooldownMs = computed(() => {
  const configured = typeof modelTestingSettings === 'object' && modelTestingSettings
    ? modelTestingSettings.cooldownMs
    : undefined
  return typeof configured === 'number' && Number.isFinite(configured) ? configured : 500
})

if (typeof modelTestingSettings === 'object' && modelTestingSettings) {
  watch(
    () => modelTestingSettings.timeoutSeconds,
    (value) => {
      if (typeof value === 'number' && Number.isFinite(value)) {
        form.timeoutSeconds = value
      }
    },
  )
}

function normalizeOrigin(url: string | null | undefined): string {
  if (!url) return ''
  const trimmed = url.trim()
  if (!trimmed) return ''
  const withScheme = trimmed.includes('://')
    ? trimmed
    : `${/^(localhost|127\.|\[::1\])/i.test(trimmed) ? 'http' : 'https'}://${trimmed}`
  try {
    const parsed = new URL(withScheme)
    return `${parsed.protocol}//${parsed.host}`.toLowerCase()
  } catch {
    return ''
  }
}

/** 合并选中的 URL 前缀与可编辑地址后缀，得到实际用于请求的 Base URL。 */
const effectiveBaseUrl = computed(() => joinTargetUrl(urlPrefix.value, form.baseUrl))

/** 切换 URL 前缀时按新旧前缀同步更新可编辑后缀，行为与其余工作台保持一致。 */
function selectUrlPrefix(prefix: string) {
  if (isReadOnlyReplay.value) return
  const previous = urlPrefix.value
  urlPrefix.value = prefix
  form.baseUrl = prefix ? urlSuffix(form.baseUrl, prefix, previous) : (joinTargetUrl(previous, form.baseUrl) || form.baseUrl)
}

/** 仅当填写地址与系统设置同源且系统已保存密钥时，才允许复用同源密钥。 */
const canReuseSavedKey = computed(() => {
  if (!savedConfig.value?.has_api_key || !savedConfig.value.base_url) return false
  if (!effectiveBaseUrl.value) return true
  return normalizeOrigin(effectiveBaseUrl.value) === normalizeOrigin(savedConfig.value.base_url)
})

const reuseSavedKeyActive = computed(
  () => form.useSavedKey && canReuseSavedKey.value && !form.apiKey.trim(),
)

const enabledChecks = computed<ProbeCheckName[]>(() =>
  (Object.keys(checks) as ProbeCheckName[]).filter((key) => checks[key]),
)

const categoryTabs = computed(() => {
  const counts = new Map<ModelCategory, number>()
  for (const item of models.value) {
    counts.set(item.model_type, (counts.get(item.model_type) ?? 0) + 1)
  }
  const tabs: { key: 'all' | ModelCategory; label: string; count: number }[] = [
    { key: 'all', label: '全部', count: models.value.length },
  ]
  for (const key of Object.keys(CATEGORY_LABELS) as ModelCategory[]) {
    const count = counts.get(key) ?? 0
    if (count > 0) {
      tabs.push({ key, label: CATEGORY_LABELS[key], count })
    }
  }
  return tabs
})

const hasActiveFilters = computed(() => Boolean(
  categoryFilter.value !== 'all'
  || availabilityFilter.value !== 'all'
  || toolFilter.value !== 'all'
  || inputModalityFilter.value !== 'all'
  || outputModalityFilter.value !== 'all'
  || searchQuery.value.trim(),
))

/** 判断模态项是否具备可用迹象（实测支持、元数据声明或命名推断，排除实测不支持/错误）。 */
function isModalityPositive(mod: ModalityItem): boolean {
  return mod.status === 'supported' || mod.status === 'inferred'
}

function matchesAvailability(item: ModelProfile, filter: AvailabilityFilter): boolean {
  if (filter === 'all') return true
  if (filter === 'available') return item.availability.status === 'available'
  if (filter === 'unavailable') return ['unavailable', 'error'].includes(item.availability.status)
  if (filter === 'unsupported') return item.availability.status === 'unsupported'
  return item.availability.status === 'untested'
}

function matchesTool(item: ModelProfile, filter: ToolFilter): boolean {
  if (filter === 'all') return true
  if (filter === 'supported') return item.tool_calling.status === 'supported'
  if (filter === 'partial') return item.tool_calling.status === 'partial'
  if (filter === 'unsupported') return ['unsupported', 'unavailable'].includes(item.tool_calling.status)
  return ['untested', 'inferred', 'unknown'].includes(item.tool_calling.status)
}

function matchesInputModality(item: ModelProfile, filter: InputModalityFilter): boolean {
  if (filter === 'all') return true
  const positive = item.input_modalities.filter(isModalityPositive)
  if (filter === 'multimodal') return positive.some((mod) => mod.modality !== 'text')
  if (filter === 'image') return positive.some((mod) => mod.modality === 'image')
  if (filter === 'image_probed') {
    return item.input_modalities.some(
      (mod) => mod.modality === 'image' && mod.status === 'supported' && mod.source === 'probed',
    )
  }
  if (filter === 'audio') return positive.some((mod) => mod.modality === 'audio')
  if (filter === 'video') return positive.some((mod) => mod.modality === 'video')
  return positive.length > 0 && positive.every((mod) => mod.modality === 'text')
}

function matchesOutputModality(item: ModelProfile, filter: OutputModalityFilter): boolean {
  if (filter === 'all') return true
  return item.output_modalities.some((mod) => mod.modality === filter && isModalityPositive(mod))
}

const filteredModels = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase()
  return models.value.filter((item) => {
    if (categoryFilter.value !== 'all' && item.model_type !== categoryFilter.value) return false
    if (!matchesAvailability(item, availabilityFilter.value)) return false
    if (!matchesTool(item, toolFilter.value)) return false
    if (!matchesInputModality(item, inputModalityFilter.value)) return false
    if (!matchesOutputModality(item, outputModalityFilter.value)) return false
    if (keyword && !item.id.toLowerCase().includes(keyword) && !(item.owned_by ?? '').toLowerCase().includes(keyword)) {
      return false
    }
    return true
  })
})

const activeModel = computed(() => {
  if (readonlyReplay.value?.subCase?.profile) {
    return readonlyReplay.value.subCase.profile
  }
  return models.value.find((item) => item.id === activeModelId.value) ?? null
})

const stats = computed(() => {
  const available = models.value.filter((m) => m.availability.status === 'available').length
  const unavailable = models.value.filter((m) => ['unavailable', 'error'].includes(m.availability.status)).length
  const toolSupported = models.value.filter((m) => m.tool_calling.status === 'supported').length
  const multimodal = models.value.filter((m) =>
    m.input_modalities.some((mod) => mod.modality !== 'text' && isModalityPositive(mod)),
  ).length
  return { total: models.value.length, available, unavailable, toolSupported, multimodal }
})

const allFilteredSelected = computed(
  () => filteredModels.value.length > 0 && filteredModels.value.every((item) => selectedIds.value.includes(item.id)),
)

/** 供底部 AI 助手小Y使用的实时脱敏上下文（绝不包含明文 apiKey）。 */
const assistantContext = computed(() =>
  buildModelTestingContext({
    baseUrl: effectiveBaseUrl.value || form.baseUrl,
    protocol: form.protocol,
    useSavedKey: reuseSavedKeyActive.value,
    apiKey: form.apiKey,
    timeoutSeconds: effectiveTimeoutSeconds.value,
    checks,
    cooldownMs: effectiveCooldownMs.value,
    models: models.value,
    activeModelId: activeModelId.value,
    activeModel: activeModel.value,
    hasResult: models.value.length > 0 || currentRun.value !== null,
  }),
)

/** 供底部 AI 助手小Y工具读取的当前页面配置（不含任何明文密钥）。 */
const assistantSettings = computed(() => ({
  base_url: effectiveBaseUrl.value || form.baseUrl || undefined,
  protocol: form.protocol,
  use_saved_key: reuseSavedKeyActive.value,
  has_custom_api_key: Boolean(form.apiKey.trim()),
  timeout_seconds: Math.min(120, Math.max(3, effectiveTimeoutSeconds.value)),
  concurrency: Math.min(10, Math.max(1, effectiveConcurrency.value)),
  cooldown_ms: effectiveCooldownMs.value,
  checks: [...enabledChecks.value],
  selected_models: [...selectedIds.value],
}))

/** 仅当未启用系统同源密钥复用且存在临时输入密钥时，向小Y透传仅内存使用的 ephemeralApiKey。 */
const ephemeralApiKeyForAssistant = computed<string | null>(() => {
  const raw = form.apiKey.trim()
  if (!raw) return null
  if (reuseSavedKeyActive.value) return null
  return raw
})

function runStatusMeta(status: ModelTestRunStatus | string): { label: string; tone: string } {
  switch (status) {
    case 'queued':
      return { label: '排队中', tone: 'info' }
    case 'running':
      return { label: '探测中', tone: 'info' }
    case 'completed':
      return { label: '已完成', tone: 'success' }
    case 'cancelled':
      return { label: '已停止', tone: 'warning' }
    case 'interrupted':
      return { label: '已中断', tone: 'warning' }
    case 'error':
      return { label: '异常', tone: 'danger' }
    default:
      return { label: status, tone: 'neutral' }
  }
}

/** 映射持久化探测历史到共享 HistoryList 条目（含 AI 徽标与后台运行脉动指示）。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  history.value.map((item) => {
    const status = runStatusMeta(item.status)
    const badges: HistoryBadge[] = []
    if (item.badge || item.source === 'ai') {
      badges.push({
        label: item.badge || 'AI 测试',
        tone: 'info',
        title: '由 AI 助手小Y编排执行',
      })
    }
    if (item.total_rounds > 1) {
      badges.push({
        label: `${item.total_rounds} 轮`,
        tone: 'neutral',
        title: `共 ${item.total_rounds} 轮探测`,
      })
    }
    const modelCount = item.summary?.tested_count ?? item.target_models.length
    return {
      id: item.id,
      label: item.title || item.base_url,
      tone: status.tone,
      status: status.label,
      meta: `${formatRelative(item.created_at)} · ${modelCount} 模型`,
      badges: badges.length ? badges : undefined,
      running:
        item.status === 'queued'
        || item.status === 'running'
        || (item.id === activeRunId.value && liveRunning.value),
      raw: item,
      at: item.created_at,
      source: item.source,
    }
  }),
)

/** 将当前页面状态同步到内存 Pinia 仓库，保障路由切换后状态完整无损且绝不触碰 localStorage。 */
function syncToStore() {
  if (!workbenchStore) return
  workbenchStore.baseUrl = form.baseUrl
  workbenchStore.urlPrefix = urlPrefix.value
  workbenchStore.apiKey = form.apiKey
  workbenchStore.useSavedKey = form.useSavedKey
  workbenchStore.protocol = form.protocol
  workbenchStore.timeoutSeconds = form.timeoutSeconds
  for (const key of ALL_CHECK_NAMES) {
    workbenchStore.checks[key] = checks[key]
  }
  workbenchStore.discoverMeta = discoverMeta.value ? { ...discoverMeta.value } : null
  workbenchStore.models = [...models.value]
  workbenchStore.selectedIds = [...selectedIds.value]
  workbenchStore.activeModelId = activeModelId.value
  workbenchStore.searchQuery = searchQuery.value
  workbenchStore.categoryFilter = categoryFilter.value
  workbenchStore.availabilityFilter = availabilityFilter.value
  workbenchStore.toolFilter = toolFilter.value
  workbenchStore.inputModalityFilter = inputModalityFilter.value
  workbenchStore.outputModalityFilter = outputModalityFilter.value
  workbenchStore.history = [...history.value]
  workbenchStore.activeRunId = activeRunId.value
  workbenchStore.activeHistoryId = activeHistoryId.value
  workbenchStore.currentRun = currentRun.value
  workbenchStore.readonlyReplay = readonlyReplay.value ? { ...readonlyReplay.value } : null
  workbenchStore.draftVersion = draftVersion.value
  workbenchStore.liveProgress.stage = liveStage.value
  workbenchStore.liveProgress.currentRound = currentRound.value
  workbenchStore.liveProgress.totalRounds = totalRounds.value
  workbenchStore.liveProgress.completedRounds = completedRounds.value
  workbenchStore.liveProgress.cooldownMs = cooldownMs.value
  workbenchStore.liveProgress.cooldownRemainingMs = cooldownRemainingMs.value
  workbenchStore.liveProgress.completedCases = probingCompleted.value
  workbenchStore.liveProgress.totalCases = probingTotal.value
}

function exitReadonlyReplayState() {
  readonlyReplay.value = null
  workbenchStore?.exitReadonlyReplay()
  workbenchRuns?.exitReadonlyReplay('model_testing')
}

function resetLiveViewState() {
  liveRunning.value = false
  liveStage.value = 'idle'
  currentRound.value = 0
  totalRounds.value = 1
  completedRounds.value = 0
  cooldownRemainingMs.value = null
  probingStates.value = {}
  probingChecks.value = {}
  probingCompleted.value = 0
  probingTotal.value = 0
}

function detachLocalObserver() {
  observerGeneration += 1
  if (observerAbort) {
    observerAbort.abort()
    observerAbort = null
  }
  liveRunning.value = false
}

function applyChecksList(list: readonly ProbeCheckName[] | readonly string[] | null | undefined) {
  if (!list || !list.length) return
  const activeSet = new Set(list)
  for (const key of ALL_CHECK_NAMES) {
    checks[key] = activeSet.has(key)
  }
}

async function loadSavedDefaults() {
  try {
    savedConfig.value = await getLLMConfig()
  } catch {
    savedConfig.value = null
  }
}

async function loadHistory() {
  if (typeof listModelTestRuns !== 'function') return
  try {
    const page = await listModelTestRuns(50)
    if (!unmounted) {
      history.value = page.items ?? []
      syncToStore()
    }
  } catch {
    // 历史加载失败不阻断当前配置与探测交互。
  }
}

/** 重置右侧响应结果的全部筛选条件，不影响模型列表与左侧输入。 */
function resetFilters() {
  categoryFilter.value = 'all'
  availabilityFilter.value = 'all'
  toolFilter.value = 'all'
  inputModalityFilter.value = 'all'
  outputModalityFilter.value = 'all'
  searchQuery.value = ''
}

/** 一键填入系统设置中的 Base URL 与超时时间（密钥不回显，同源自动启用密钥复用）。 */
function fillFromSystemSettings() {
  if (isReadOnlyReplay.value) return
  if (!savedConfig.value?.base_url) {
    toast.push('warning', '系统设置尚未配置 Base URL')
    return
  }
  urlPrefix.value = ''
  form.baseUrl = savedConfig.value.base_url
  form.apiKey = ''
  form.useSavedKey = savedConfig.value.has_api_key
  if (typeof savedConfig.value.timeout_seconds === 'number' && Number.isFinite(savedConfig.value.timeout_seconds)) {
    const clamped = Math.min(120, Math.max(3, Math.round(savedConfig.value.timeout_seconds)))
    form.timeoutSeconds = clamped
    if (typeof modelTestingSettings === 'object' && modelTestingSettings) {
      modelTestingSettings.timeoutSeconds = clamped
    }
  }
  const hints: string[] = []
  if (savedConfig.value.has_api_key) hints.push('已开启同源复用系统密钥')
  hints.push(`超时已同步为 ${form.timeoutSeconds}s`)
  toast.push('info', '已填入系统设置地址', hints.join('，'))
}

/** 切换同源复用系统设置密钥状态，供与「填入系统设置地址」紧挨的快捷按钮调用。 */
function toggleUseSavedKey() {
  if (isReadOnlyReplay.value) return
  if (!canReuseSavedKey.value || form.apiKey.trim()) return
  form.useSavedKey = !form.useSavedKey
}

/** 打开清空二次确认弹窗，未确认前不改动任何输入或输出。 */
function requestClearAll() {
  if (discovering.value || probing.value || isReadOnlyReplay.value) return
  clearConfirmOpen.value = true
}

function cancelClearAll() {
  clearConfirmOpen.value = false
}

/** 确认清空当前页面的全部测试输入、筛选条件与响应结果，不影响系统设置。 */
function confirmClearAll() {
  clearConfirmOpen.value = false
  exitReadonlyReplayState()
  detachLocalObserver()
  activeRunId.value = null
  activeHistoryId.value = null
  currentRun.value = null
  workbenchRuns?.setActiveRun('model_testing', null)
  workbenchRuns?.setActiveHistory('model_testing', null)
  resetLiveViewState()
  urlPrefix.value = ''
  form.baseUrl = ''
  form.apiKey = ''
  form.useSavedKey = false
  form.protocol = 'auto'
  form.timeoutSeconds = 25
  showKey.value = false
  checks.availability = true
  checks.tool_calling = true
  checks.input_modalities = true
  checks.output_modalities = false
  checks.streaming = false
  checks.structured_output = false
  discoverMeta.value = null
  models.value = []
  selectedIds.value = []
  activeModelId.value = null
  resetFilters()
  draftVersion.value += 1
  syncToStore()
  toast.push('info', '已清空全部输入与测试结果')
}

/**
 * 显式「再次测试」统一语义：
 * 退出只读回放锁、清空历史/运行绑定与展示结果、递增 draftVersion 开启全新空白 AI 对话，
 * 并保留当前 Base URL/协议/探测项/仅内存密钥供用户直接修改后重新探测。
 */
function retestCurrent() {
  historyReadAbort?.abort()
  historyReadAbort = null
  detachLocalObserver()
  exitReadonlyReplayState()
  activeRunId.value = null
  activeHistoryId.value = null
  currentRun.value = null
  workbenchRuns?.setActiveRun('model_testing', null)
  workbenchRuns?.setActiveHistory('model_testing', null)
  resetLiveViewState()
  discoverMeta.value = null
  models.value = []
  selectedIds.value = []
  activeModelId.value = null
  resetFilters()
  draftVersion.value += 1
  syncToStore()
}

/** 左侧历史栏「新建测试」：重置输入与结果并开启全新空白 AI 对话。 */
function newTest() {
  retestCurrent()
  urlPrefix.value = ''
  form.baseUrl = ''
  form.apiKey = ''
  form.useSavedKey = false
  form.protocol = 'auto'
  form.timeoutSeconds = 25
  showKey.value = false
  checks.availability = true
  checks.tool_calling = true
  checks.input_modalities = true
  checks.output_modalities = false
  checks.streaming = false
  checks.structured_output = false
  syncToStore()
}

async function discover() {
  if (discovering.value || isReadOnlyReplay.value) return
  if (!effectiveBaseUrl.value) {
    toast.push('warning', '请先填写 Base URL')
    return
  }
  discovering.value = true
  try {
    const result = await discoverProviderModels({
      base_url: effectiveBaseUrl.value,
      api_key: form.apiKey.trim() || undefined,
      use_saved_key: reuseSavedKeyActive.value,
      protocol: form.protocol,
      timeout_seconds: form.timeoutSeconds,
    })
    const { models: list, ...meta } = result
    if (!hasPersistentRunApi) {
      stopProbing(true)
    }
    discoverMeta.value = meta
    models.value = list
    selectedIds.value = []
    resetFilters()
    activeModelId.value = list[0]?.id ?? null
    if (result.ok) {
      if (result.base_url && result.base_url !== effectiveBaseUrl.value) {
        form.baseUrl = urlPrefix.value ? urlSuffix(result.base_url, urlPrefix.value) : result.base_url
      }
      toast.push('success', result.message)
    } else {
      toast.push('danger', '拉取模型失败', result.message)
    }
    syncToStore()
  } catch (error) {
    toast.push('danger', '模型拉取请求失败', describeError(error))
  } finally {
    discovering.value = false
  }
}

function mergeProbeResults(updatedList: ModelProfile[], options: { appendMissing?: boolean } = {}) {
  if (!updatedList.length) return
  const byId = new Map(updatedList.map((item) => [item.id, item]))
  const existingIds = new Set<string>()
  const merged = models.value.map((existing) => {
    existingIds.add(existing.id)
    const fresh = byId.get(existing.id)
    if (!fresh) return existing
    const freshStream = capOf(fresh.streaming)
    const freshStruct = capOf(fresh.structured_output)
    return {
      ...existing,
      model_type: fresh.type_source === 'probed' ? fresh.model_type : existing.model_type,
      type_source: fresh.type_source === 'probed' ? fresh.type_source : existing.type_source,
      availability: fresh.availability.source !== 'untested' ? fresh.availability : existing.availability,
      tool_calling: fresh.tool_calling.source !== 'untested' ? fresh.tool_calling : existing.tool_calling,
      streaming: freshStream.source !== 'untested' ? freshStream : existing.streaming,
      structured_output: freshStruct.source !== 'untested' ? freshStruct : existing.structured_output,
      input_modalities: fresh.input_modalities.length ? fresh.input_modalities : existing.input_modalities,
      output_modalities: fresh.output_modalities.length ? fresh.output_modalities : existing.output_modalities,
    }
  })
  if (options.appendMissing) {
    for (const fresh of updatedList) {
      if (!existingIds.has(fresh.id)) {
        merged.push(fresh)
      }
    }
  }
  models.value = merged
}

function applyRunDetail(detail: ModelTestRunDetail, options: { preserveDiscoveredList?: boolean; syncConfig?: boolean } = {}) {
  currentRun.value = detail
  activeHistoryId.value = detail.id
  workbenchRuns?.setActiveHistory('model_testing', detail.id)
  if (options.syncConfig) {
    urlPrefix.value = ''
    form.baseUrl = detail.base_url
    form.protocol = VALID_PROTOCOLS.has(detail.protocol) ? detail.protocol : 'auto'
    form.useSavedKey = Boolean(detail.used_saved_key)
    applyChecksList(detail.checks)
    selectedIds.value = [...(detail.target_models ?? [])]
  }
  if (detail.discover_meta && typeof detail.discover_meta === 'object') {
    const rawMeta = detail.discover_meta as Partial<Omit<ModelDiscoverResult, 'models'>>
    if (typeof rawMeta.ok === 'boolean' && typeof rawMeta.message === 'string') {
      discoverMeta.value = {
        ok: rawMeta.ok,
        protocol: rawMeta.protocol,
        base_url: rawMeta.base_url ?? detail.base_url,
        http_status: rawMeta.http_status ?? null,
        latency_ms: rawMeta.latency_ms ?? 0,
        count: rawMeta.count ?? detail.models_snapshot.length,
        message: rawMeta.message,
      }
    }
  }
  if (options.preserveDiscoveredList && models.value.length > 0) {
    mergeProbeResults(detail.models_snapshot ?? [], { appendMissing: true })
  } else {
    models.value = [...(detail.models_snapshot ?? [])]
  }
  if (!activeModelId.value || !models.value.some((m) => m.id === activeModelId.value)) {
    activeModelId.value = models.value[0]?.id ?? null
  }
  totalRounds.value = detail.total_rounds || 1
  completedRounds.value = detail.completed_rounds || 0
  cooldownMs.value = detail.cooldown_ms ?? 500
  if (detail.summary) {
    probingCompleted.value = detail.summary.completed_cases
    probingTotal.value = detail.summary.total_cases
  }
  syncToStore()
}

function appendUniqueSubCases(
  existing: ModelTestSubCaseSnapshot[],
  incoming: ModelTestSubCaseSnapshot[] | undefined,
): ModelTestSubCaseSnapshot[] {
  if (!incoming?.length) return existing
  const map = new Map<string, ModelTestSubCaseSnapshot>()
  for (const item of existing) map.set(item.case_id, item)
  for (const item of incoming) map.set(item.case_id, item)
  return [...map.values()]
}

function updateLiveProbingStates(
  currentModels: string[],
  snapshotModels: ModelProfile[],
  completedCases: number,
  totalCases: number,
  checkList: ProbeCheckName[],
) {
  const nextStates: Record<string, ModelProbeQueueState> = {}
  const nextChecks: Record<string, ProbeCheckName[]> = {}
  const activeSet = new Set(currentModels)
  for (const modelId of currentModels) {
    nextStates[modelId] = 'probing'
    nextChecks[modelId] = checkList
  }
  if (completedCases < totalCases) {
    for (const prof of snapshotModels) {
      if (!activeSet.has(prof.id) && prof.availability.source === 'untested') {
        nextStates[prof.id] = 'queued'
        nextChecks[prof.id] = checkList
      }
    }
  }
  probingStates.value = nextStates
  probingChecks.value = nextChecks
}

/** 消费后端 ModelTestManager 推送的 progress / cooling_down / done 实时帧。 */
function applyLiveFrame(runId: string, frame: ModelTestLiveFrame) {
  if (frame.type === 'heartbeat' || frame.type === 'error') return
  liveStage.value =
    frame.type === 'cooling_down'
      ? 'cooling_down'
      : frame.type === 'done'
        ? 'finished'
        : frame.stage === 'cooling_down'
          ? 'cooling_down'
          : 'executing'
  currentRound.value = frame.current_round ?? currentRound.value
  totalRounds.value = frame.total_rounds || 1
  completedRounds.value = frame.completed_rounds ?? 0
  cooldownMs.value = frame.cooldown_ms ?? 500
  cooldownRemainingMs.value = frame.cooldown_remaining_ms ?? null
  probingCompleted.value = frame.completed_cases ?? 0
  probingTotal.value = frame.total_cases ?? 0

  if (frame.models_snapshot?.length) {
    if (models.value.length > 0 && currentRun.value?.id === runId) {
      mergeProbeResults(frame.models_snapshot, { appendMissing: true })
    } else if (models.value.length > 0 && normalizeOrigin(frame.base_url) === normalizeOrigin(effectiveBaseUrl.value)) {
      mergeProbeResults(frame.models_snapshot, { appendMissing: true })
    } else {
      models.value = [...frame.models_snapshot]
    }
    if (!activeModelId.value) {
      activeModelId.value = models.value[0]?.id ?? null
    }
  }

  const runChecks = currentRun.value?.checks?.length ? currentRun.value.checks : enabledChecks.value
  if (frame.type === 'done') {
    probingStates.value = {}
    probingChecks.value = {}
    if (frame.run) {
      applyRunDetail(frame.run, { preserveDiscoveredList: true })
    } else if (currentRun.value && currentRun.value.id === runId) {
      currentRun.value = {
        ...currentRun.value,
        status: frame.status,
        completed_rounds: frame.completed_rounds,
        summary: frame.summary,
        summary_text: frame.summary_text,
        error_message: frame.error_message,
        models_snapshot: frame.models_snapshot ?? currentRun.value.models_snapshot,
        sub_cases: appendUniqueSubCases(currentRun.value.sub_cases ?? [], frame.new_sub_cases),
      }
    }
  } else {
    updateLiveProbingStates(
      frame.current_models ?? [],
      frame.models_snapshot ?? [],
      frame.completed_cases ?? 0,
      frame.total_cases ?? 0,
      runChecks,
    )
    if (currentRun.value && currentRun.value.id === runId) {
      currentRun.value = {
        ...currentRun.value,
        title: frame.title ?? currentRun.value.title,
        status: frame.status,
        resolved_protocol: frame.resolved_protocol ?? currentRun.value.resolved_protocol,
        total_rounds: frame.total_rounds || currentRun.value.total_rounds,
        completed_rounds: frame.completed_rounds ?? currentRun.value.completed_rounds,
        cooldown_ms: frame.cooldown_ms ?? currentRun.value.cooldown_ms,
        summary: frame.summary ?? currentRun.value.summary,
        summary_text: frame.summary_text ?? currentRun.value.summary_text,
        error_message: frame.error_message ?? currentRun.value.error_message,
        models_snapshot: frame.models_snapshot ?? currentRun.value.models_snapshot,
        sub_cases: appendUniqueSubCases(currentRun.value.sub_cases ?? [], frame.new_sub_cases),
      }
    } else {
      const nowIso = new Date().toISOString()
      currentRun.value = {
        id: runId,
        title: frame.title,
        source: frame.source,
        badge: frame.badge,
        status: frame.status,
        error_message: frame.error_message,
        base_url: frame.base_url,
        protocol: frame.protocol,
        resolved_protocol: frame.resolved_protocol,
        used_saved_key: reuseSavedKeyActive.value,
        has_custom_api_key: Boolean(form.apiKey.trim()),
        timeout_seconds: effectiveTimeoutSeconds.value,
        concurrency: effectiveConcurrency.value,
        checks: [...runChecks],
        target_models: (frame.models_snapshot ?? []).map((item) => item.id),
        cooldown_ms: frame.cooldown_ms ?? 500,
        total_rounds: frame.total_rounds || 1,
        completed_rounds: frame.completed_rounds ?? 0,
        sub_case_count: frame.new_sub_cases?.length ?? 0,
        summary: frame.summary,
        summary_text: frame.summary_text,
        started_at: nowIso,
        finished_at: null,
        created_at: nowIso,
        updated_at: nowIso,
        discover_meta: null,
        models_snapshot: [...(frame.models_snapshot ?? [])],
        rounds: [],
        sub_cases: [...(frame.new_sub_cases ?? [])],
      }
    }
  }
  syncToStore()
}

/** 订阅后台持久化模型探测流；客户端断开仅关闭观察连接，绝不取消后台任务。 */
async function attachRunObserver(runId: string, options: { notifyCompletion?: boolean } = {}) {
  if (typeof followModelTestRun !== 'function' || !runId) return
  detachLocalObserver()
  const generation = ++observerGeneration
  const controller = typeof AbortController === 'function' ? new AbortController() : undefined
  observerAbort = controller ?? null

  liveRunning.value = true
  activeRunId.value = runId
  activeHistoryId.value = runId
  workbenchRuns?.setActiveRun('model_testing', runId)
  workbenchRuns?.setActiveHistory('model_testing', runId)
  syncToStore()

  try {
    const doneFrame = await followModelTestRun(runId, controller?.signal, (frame) => {
      if (unmounted || generation !== observerGeneration || controller?.signal.aborted) return
      applyLiveFrame(runId, frame)
    })
    if (unmounted || generation !== observerGeneration || controller?.signal.aborted) return

    liveRunning.value = false
    liveStage.value = 'finished'
    probingStates.value = {}
    probingChecks.value = {}
    if (activeRunId.value === runId) {
      activeRunId.value = null
      workbenchRuns?.setActiveRun('model_testing', null)
    }
    if (doneFrame?.run) {
      applyRunDetail(doneFrame.run, { preserveDiscoveredList: true })
    } else if (typeof getModelTestRun === 'function') {
      try {
        const fresh = await getModelTestRun(runId)
        if (!unmounted && generation === observerGeneration) {
          applyRunDetail(fresh, { preserveDiscoveredList: true })
        }
      } catch {
        // 保留已收到的最终帧快照。
      }
    }
    await loadHistory()
    if (options.notifyCompletion && !unmounted) {
      const status = doneFrame?.status ?? currentRun.value?.status
      const message = doneFrame?.summary_text ?? currentRun.value?.summary_text ?? ''
      if (status === 'cancelled') {
        toast.push('info', '已停止模型探测', message || undefined)
      } else if (status === 'error') {
        toast.push('danger', '模型探测异常结束', doneFrame?.error_message ?? message)
      } else if (message) {
        toast.push('success', message)
      }
    }
  } catch (error) {
    if (unmounted || generation !== observerGeneration || controller?.signal.aborted) return
    liveRunning.value = false
    probingStates.value = {}
    probingChecks.value = {}
    if (activeRunId.value === runId) {
      activeRunId.value = null
      workbenchRuns?.setActiveRun('model_testing', null)
    }
    toast.push('danger', '模型探测进度流中断', describeError(error))
    await loadHistory()
  } finally {
    if (observerAbort === controller) {
      observerAbort = null
    }
    syncToStore()
  }
}

function probingStateOf(modelId: string): ModelProbeQueueState | null {
  return probingStates.value[modelId] ?? null
}

function isModelWaiting(modelId: string): boolean {
  return probingStateOf(modelId) !== null
}

function hasQueuedCheck(modelId: string, check: ProbeCheckName): boolean {
  const list = probingChecks.value[modelId]
  return list ? list.includes(check) : checks[check]
}

function waitLabelFor(modelId: string): string {
  return probingStateOf(modelId) === 'queued' ? '排队中' : '探测中'
}

function clearModelWaiting(modelId: string) {
  if (modelId in probingStates.value) {
    const nextStates = { ...probingStates.value }
    delete nextStates[modelId]
    probingStates.value = nextStates
  }
  if (modelId in probingChecks.value) {
    const nextChecks = { ...probingChecks.value }
    delete nextChecks[modelId]
    probingChecks.value = nextChecks
  }
}

function finalizeQueueSessionIfDrained(silent = false) {
  if (activeWorkerCount > 0 || pendingQueue.length > 0) return
  const wasStopped = stopRequested
  const total = probingTotal.value
  const completed = probingCompleted.value
  const available = batchAvailableCount
  const toolSupported = batchToolSupportedCount
  const errors = batchErrorCount
  const singleMessage = lastSingleProbeMessage
  stopRequested = false

  if (silent || total <= 0) return
  if (wasStopped) {
    toast.push('info', '已停止模型探测', `保留已完成 ${completed} / ${total} 个模型的结果`)
    return
  }
  if (completed > 0) {
    if (total === 1 && singleMessage) {
      toast.push('success', singleMessage)
    } else {
      const errorHint = errors > 0 ? `，失败 ${errors} 个` : ''
      toast.push(
        'success',
        `已完成 ${completed} 个模型探测：可用 ${available} 个，支持工具调用 ${toolSupported} 个${errorHint}`,
      )
    }
  }
}

/** 显式停止正在进行的模型探测：持久化模式下调用后端 cancelModelTestRun，绝不在切页卸载时隐式调用。 */
async function stopProbing(silent = false) {
  if (hasPersistentRunApi && activeRunId.value) {
    if (cancellingRun.value) return
    const targetId = activeRunId.value
    cancellingRun.value = true
    try {
      const cancelled = typeof cancelModelTestRun === 'function' ? await cancelModelTestRun(targetId) : null
      detachLocalObserver()
      probingStates.value = {}
      probingChecks.value = {}
      activeRunId.value = null
      liveStage.value = 'finished'
      workbenchRuns?.setActiveRun('model_testing', null)
      if (cancelled) {
        applyRunDetail(cancelled, { preserveDiscoveredList: true })
      }
      await loadHistory()
      if (!silent) {
        toast.push('info', '已停止模型探测', cancelled?.summary_text ?? undefined)
      }
    } catch (error) {
      if (!silent) {
        toast.push('danger', '停止探测失败', describeError(error))
      }
    } finally {
      cancellingRun.value = false
      syncToStore()
    }
    return
  }

  if (!probing.value && !pendingQueue.length && activeWorkerCount === 0) return
  stopRequested = true
  while (pendingQueue.length > 0) {
    const queued = pendingQueue.shift()
    if (!queued) continue
    clearModelWaiting(queued.modelId)
    queued.resolve()
  }
  for (const controller of activeControllers.values()) {
    controller.abort()
  }
  if (activeWorkerCount === 0) {
    probingStates.value = {}
    probingChecks.value = {}
    finalizeQueueSessionIfDrained(silent)
  }
}

async function consumeProbeJob(job: ProbeQueueJob) {
  const controller = typeof AbortController === 'function' ? new AbortController() : undefined
  if (controller) {
    activeControllers.set(job.modelId, controller)
  }
  const requestBody = {
    ...job.payload,
    models: [job.modelId],
    concurrency: 1,
  }
  let appliedModel: ModelProfile | null = null
  const applyModelResult = (profile: ModelProfile) => {
    appliedModel = profile
    mergeProbeResults([profile])
    if (!activeModelId.value) {
      activeModelId.value = profile.id
    }
  }
  try {
    const streamProbe = typeof probeProviderModelsStream === 'function' ? probeProviderModelsStream : null
    if (streamProbe) {
      const result = await streamProbe(
        requestBody,
        (frame: ModelProbeStreamFrame) => {
          if (frame.type === 'model_start') {
            if (probingStates.value[frame.model_id]) {
              probingStates.value = { ...probingStates.value, [frame.model_id]: 'probing' }
            }
          } else if (frame.type === 'model_result') {
            applyModelResult(frame.model)
          }
        },
        controller?.signal,
      )
      if (!appliedModel && result.results.length) {
        applyModelResult(result.results[0])
      }
      if (result.message) {
        lastSingleProbeMessage = result.message
      }
    } else {
      const result = await probeProviderModels(requestBody)
      if (result.results.length) {
        applyModelResult(result.results[0])
      }
      if (result.message) {
        lastSingleProbeMessage = result.message
      }
    }
    probingCompleted.value += 1
    if (appliedModel) {
      if ((appliedModel as ModelProfile).availability.status === 'available') {
        batchAvailableCount += 1
      }
      if ((appliedModel as ModelProfile).tool_calling.status === 'supported') {
        batchToolSupportedCount += 1
      }
    }
  } catch (error) {
    if (!controller?.signal.aborted && !stopRequested) {
      batchErrorCount += 1
      toast.push('danger', `模型 ${job.modelId} 探测失败`, describeError(error))
    }
  } finally {
    if (controller && activeControllers.get(job.modelId) === controller) {
      activeControllers.delete(job.modelId)
    }
    clearModelWaiting(job.modelId)
    activeWorkerCount = Math.max(0, activeWorkerCount - 1)
    job.resolve()
    pumpProbeQueue()
    finalizeQueueSessionIfDrained()
  }
}

function pumpProbeQueue() {
  if (stopRequested) return
  while (activeWorkerCount < QUEUE_CONCURRENCY && pendingQueue.length > 0) {
    const nextJob = pendingQueue.shift()
    if (!nextJob) break
    probingStates.value = { ...probingStates.value, [nextJob.modelId]: 'probing' }
    activeWorkerCount += 1
    void consumeProbeJob(nextJob)
  }
}

/** 启动持久化后台模型探测任务（或兼容旧单测环境回退到流式队列）。 */
async function runProbe(targetIds: string[]) {
  if (isReadOnlyReplay.value) return
  if (!targetIds.length) return
  if (!enabledChecks.value.length) {
    toast.push('warning', '请至少勾选一项探测能力')
    return
  }

  if (hasPersistentRunApi) {
    if (liveRunning.value) {
      toast.push('info', '当前已有探测任务正在后台执行', '可等待完成或点击「停止」后再发起新探测')
      return
    }
    const freshIds = Array.from(new Set(targetIds.map((id) => id.trim()).filter(Boolean))).slice(0, 100)
    if (!freshIds.length) return

    const snapshotChecks = [...enabledChecks.value]
    const profileMap = new Map(models.value.map((item) => [item.id, item]))
    const discoveredProfiles = freshIds
      .map((id) => profileMap.get(id))
      .filter((item): item is ModelProfile => Boolean(item))

    exitReadonlyReplayState()
    liveRunning.value = true
    liveStage.value = 'executing'
    currentRound.value = 1
    totalRounds.value = 1
    completedRounds.value = 0
    cooldownMs.value = effectiveCooldownMs.value
    cooldownRemainingMs.value = null
    probingCompleted.value = 0
    probingTotal.value = freshIds.length
    const initialStates: Record<string, ModelProbeQueueState> = {}
    const initialChecks: Record<string, ProbeCheckName[]> = {}
    for (const id of freshIds) {
      initialStates[id] = 'queued'
      initialChecks[id] = snapshotChecks
    }
    probingStates.value = initialStates
    probingChecks.value = initialChecks

    try {
      const created = await startModelTestRun({
        base_url: effectiveBaseUrl.value || undefined,
        api_key: form.apiKey.trim() || undefined,
        use_saved_key: reuseSavedKeyActive.value,
        protocol: form.protocol,
        models: freshIds,
        checks: snapshotChecks,
        timeout_seconds: Math.min(120, Math.max(3, effectiveTimeoutSeconds.value)),
        concurrency: Math.min(10, Math.max(1, effectiveConcurrency.value)),
        cooldown_ms: effectiveCooldownMs.value,
        rounds: 1,
        discover_meta: discoverMeta.value ? { ...discoverMeta.value } : null,
        discovered_profiles: discoveredProfiles.length ? discoveredProfiles : null,
      })
      activeRunId.value = created.id
      activeHistoryId.value = created.id
      currentRun.value = created
      workbenchRuns?.setActiveRun('model_testing', created.id)
      workbenchRuns?.setActiveHistory('model_testing', created.id)
      syncToStore()
      void loadHistory()
      await attachRunObserver(created.id, { notifyCompletion: true })
    } catch (error) {
      liveRunning.value = false
      liveStage.value = 'idle'
      probingStates.value = {}
      probingChecks.value = {}
      toast.push('danger', '启动模型探测失败', describeError(error))
      syncToStore()
    }
    return
  }

  const freshIds = Array.from(new Set(targetIds.map((id) => id.trim()).filter(Boolean)))
    .filter((id) => !probingStates.value[id])
    .slice(0, 30)
  if (!freshIds.length) return

  if (activeWorkerCount === 0 && pendingQueue.length === 0) {
    stopRequested = false
    probingCompleted.value = 0
    probingTotal.value = 0
    batchAvailableCount = 0
    batchToolSupportedCount = 0
    batchErrorCount = 0
    lastSingleProbeMessage = ''
  }

  const snapshotChecks = [...enabledChecks.value]
  const payload = {
    base_url: effectiveBaseUrl.value,
    api_key: form.apiKey.trim() || undefined,
    use_saved_key: reuseSavedKeyActive.value,
    protocol: form.protocol,
    checks: snapshotChecks,
    timeout_seconds: form.timeoutSeconds,
  }

  const nextStates = { ...probingStates.value }
  const nextChecks = { ...probingChecks.value }
  const jobPromises: Promise<void>[] = []

  for (const modelId of freshIds) {
    nextStates[modelId] = 'queued'
    nextChecks[modelId] = snapshotChecks
    jobPromises.push(
      new Promise<void>((resolve) => {
        pendingQueue.push({ modelId, payload, resolve })
      }),
    )
  }

  probingStates.value = nextStates
  probingChecks.value = nextChecks
  probingTotal.value += freshIds.length
  pumpProbeQueue()
  await Promise.all(jobPromises)
}

/** 选中左侧历史记录，加载完整能力矩阵并在任务仍运行时自动续接实时流。 */
async function selectHistory(id: string, options: { preserveReplay?: boolean } = {}) {
  if (typeof getModelTestRun !== 'function' || !id) return
  historyReadAbort?.abort()
  const controller = typeof AbortController === 'function' ? new AbortController() : undefined
  historyReadAbort = controller ?? null
  const generation = ++historyReadGeneration

  if (!options.preserveReplay) {
    exitReadonlyReplayState()
  }
  try {
    const detail = await getModelTestRun(id, controller?.signal)
    if (unmounted || generation !== historyReadGeneration || controller?.signal.aborted) return
    if (activeRunId.value && activeRunId.value !== detail.id) {
      detachLocalObserver()
    }
    applyRunDetail(detail, { preserveDiscoveredList: false, syncConfig: true })
    if (detail.status === 'queued' || detail.status === 'running') {
      void attachRunObserver(detail.id, { notifyCompletion: false })
    } else {
      liveRunning.value = false
      liveStage.value = 'finished'
      probingStates.value = {}
      probingChecks.value = {}
    }
  } catch (error) {
    if (!controller?.signal.aborted && !unmounted) {
      toast.push('danger', '读取探测历史失败', describeError(error))
    }
  } finally {
    if (historyReadAbort === controller) {
      historyReadAbort = null
    }
  }
}

/** 删除单条终态探测历史；运行中记录由后端 409 拒绝并优雅提示。 */
async function handleDeleteHistory(id: string) {
  if (typeof deleteModelTestRun !== 'function') return
  try {
    await deleteModelTestRun(id)
    history.value = history.value.filter((item) => item.id !== id)
    if (typeof removeTestChat === 'function') {
      removeTestChat('model_testing', id)
    }
    if (activeHistoryId.value === id) {
      exitReadonlyReplayState()
      activeHistoryId.value = null
      currentRun.value = null
      workbenchRuns?.setActiveHistory('model_testing', null)
      draftVersion.value += 1
    }
    syncToStore()
    toast.push('success', '探测历史已删除')
  } catch (error) {
    const isActiveConflict =
      typeof ApiError === 'function' && error instanceof ApiError && error.status === 409
    toast.push(
      isActiveConflict ? 'warning' : 'danger',
      isActiveConflict ? '无法删除运行中的任务' : '删除失败',
      isActiveConflict ? '该探测任务仍在后台运行中，请先停止后再删除。' : describeError(error),
    )
  }
}

/** 处理 AI 助手小Y创建/分叉新测试历史事件：立即刷新历史并订阅多轮实时探测流。 */
async function handleRunBound(payload: AssistantRunBoundPayload) {
  const historyId =
    payload.history_id
    ?? (payload as unknown as { historyId?: string }).historyId
    ?? null
  if (!historyId) return
  exitReadonlyReplayState()
  activeHistoryId.value = historyId
  activeRunId.value = historyId
  workbenchRuns?.setActiveHistory('model_testing', historyId)
  workbenchRuns?.setActiveRun('model_testing', historyId)
  syncToStore()
  void loadHistory()
  void attachRunObserver(historyId, { notifyCompletion: false })
}

/** 处理 AI 助手运行结束事件：刷新历史并加载完整能力矩阵与摘要。 */
async function handleRunCompleted(payload: { scope: string; historyId: string | null }) {
  const historyId =
    payload.historyId
    ?? (payload as unknown as { history_id?: string | null }).history_id
    ?? activeHistoryId.value
  await loadHistory()
  if (historyId && typeof getModelTestRun === 'function' && !isReadOnlyReplay.value) {
    try {
      const detail = await getModelTestRun(historyId)
      if (!unmounted && (!activeHistoryId.value || activeHistoryId.value === historyId)) {
        applyRunDetail(detail, { preserveDiscoveredList: true })
        if (!['queued', 'running'].includes(detail.status)) {
          liveRunning.value = false
          liveStage.value = 'finished'
          activeRunId.value = null
          workbenchRuns?.setActiveRun('model_testing', null)
          probingStates.value = {}
          probingChecks.value = {}
        }
      }
    } catch {
      // 忽略瞬态读取失败，保持已渲染快照。
    }
  }
}

/** 展示单个子用例的只读回放快照，锁定全部输入与触发控件直至显式点击「再次测试」。 */
function enterSubCaseReadonlyReplay(historyId: string, subCase: ModelTestSubCaseSnapshot, fallbackLabel?: string) {
  const reqSnapshot = subCase.request_snapshot ?? {}
  if (typeof reqSnapshot.base_url === 'string' && reqSnapshot.base_url.trim()) {
    urlPrefix.value = ''
    form.baseUrl = reqSnapshot.base_url
  } else if (currentRun.value?.base_url) {
    urlPrefix.value = ''
    form.baseUrl = currentRun.value.base_url
  }
  if (typeof reqSnapshot.protocol === 'string' && VALID_PROTOCOLS.has(reqSnapshot.protocol)) {
    form.protocol = reqSnapshot.protocol as ModelProtocol
  }
  if (typeof reqSnapshot.timeout_seconds === 'number' && Number.isFinite(reqSnapshot.timeout_seconds)) {
    form.timeoutSeconds = reqSnapshot.timeout_seconds
  }
  if (subCase.checks?.length) {
    applyChecksList(subCase.checks)
  }

  activeHistoryId.value = historyId
  workbenchRuns?.setActiveHistory('model_testing', historyId)
  models.value = [subCase.profile]
  selectedIds.value = [subCase.model_id]
  activeModelId.value = subCase.model_id
  resetFilters()

  const replay: ModelTestingReadonlyReplay = {
    historyId,
    caseId: subCase.case_id,
    round: subCase.round_index || 1,
    label: subCase.label || fallbackLabel || subCase.model_id,
    modelId: subCase.model_id,
    status: subCase.status,
    subCase,
  }
  readonlyReplay.value = replay
  workbenchStore?.enterReadonlyReplay(replay)
  workbenchRuns?.enterReadonlyReplay('model_testing', {
    historyId,
    caseId: replay.caseId,
    round: replay.round,
    label: replay.label,
  })
  syncToStore()
}

/** 处理 AI 回复中子用例回放链接点击事件（@select-case）。 */
async function handleSelectCase(block: CaseLinkBlock) {
  const historyId = block.history_id
  const caseId = block.case_id
  if (!historyId || !caseId) return

  let matched: ModelTestSubCaseSnapshot | null = null
  if (currentRun.value?.id === historyId) {
    matched = currentRun.value.sub_cases?.find((item) => item.case_id === caseId) ?? null
  }
  if (!matched && typeof getModelTestSubCase === 'function') {
    try {
      matched = await getModelTestSubCase(historyId, caseId)
    } catch (error) {
      toast.push('danger', '读取子用例快照失败', describeError(error))
      return
    }
  }
  if (currentRun.value?.id !== historyId && typeof getModelTestRun === 'function') {
    try {
      const parentRun = await getModelTestRun(historyId)
      if (!unmounted) {
        currentRun.value = parentRun
        if (!matched) {
          matched = parentRun.sub_cases?.find((item) => item.case_id === caseId) ?? null
        }
      }
    } catch {
      // 子用例本身已取回时仍可独立进入只读回放。
    }
  }
  if (!matched || unmounted) return
  enterSubCaseReadonlyReplay(historyId, matched, block.label)
}

/** 切回本页或从「执行结果」跳转打开时，自动恢复历史上下文并重连后台在跑任务。 */
async function restoreAndReconnectOnMount() {
  void loadSavedDefaults()
  await loadHistory()
  if (unmounted) return

  const rawQueryId = route?.query?.historyId
  const queryHistoryId =
    typeof rawQueryId === 'string' && rawQueryId.trim()
      ? rawQueryId.trim()
      : Array.isArray(rawQueryId) && typeof rawQueryId[0] === 'string' && rawQueryId[0].trim()
        ? rawQueryId[0].trim()
        : null

  const storeRequestedHistoryId = workbenchRuns?.activeHistoryId.model_testing ?? null
  const pendingReplay = workbenchRuns?.readonlyReplay.model_testing ?? null

  if (queryHistoryId) {
    await selectHistory(queryHistoryId, { preserveReplay: false })
    if (router && typeof router.replace === 'function') {
      void router.replace({ path: '/model-testing', query: {} }).catch(() => {})
    }
  } else if (
    storeRequestedHistoryId
    && storeRequestedHistoryId !== currentRun.value?.id
    && !readonlyReplay.value
  ) {
    await selectHistory(storeRequestedHistoryId, { preserveReplay: Boolean(pendingReplay) })
  }

  if (typeof getActiveModelTestRuns === 'function') {
    try {
      const response = await getActiveModelTestRuns()
      if (unmounted) return
      const activeItems: ModelTestActiveRunItem[] = response.items ?? []
      const preferredRunId =
        activeRunId.value
        ?? workbenchRuns?.activeRunId.model_testing
        ?? activeHistoryId.value
      const matched = preferredRunId
        ? activeItems.find((item) => item.id === preferredRunId)
        : activeItems[0]

      if (matched) {
        liveStage.value = matched.stage === 'cooling_down' ? 'cooling_down' : 'executing'
        currentRound.value = matched.current_round
        totalRounds.value = matched.total_rounds || 1
        completedRounds.value = matched.completed_rounds
        cooldownMs.value = matched.cooldown_ms
        cooldownRemainingMs.value = matched.cooldown_remaining_ms
        probingCompleted.value = matched.completed_cases
        probingTotal.value = matched.total_cases
        void attachRunObserver(matched.id, { notifyCompletion: false })
        return
      }

      // 若此前记录的后台任务在离页期间已结束，清除运行句柄并拉取终态快照。
      const staleRunId = activeRunId.value ?? workbenchRuns?.activeRunId.model_testing ?? null
      if (staleRunId) {
        activeRunId.value = null
        liveRunning.value = false
        workbenchRuns?.setActiveRun('model_testing', null)
        if (typeof getModelTestRun === 'function' && (!activeHistoryId.value || activeHistoryId.value === staleRunId)) {
          try {
            const finishedDetail = await getModelTestRun(staleRunId)
            if (!unmounted && !isReadOnlyReplay.value) {
              applyRunDetail(finishedDetail, { preserveDiscoveredList: true })
            }
          } catch {
            // 忽略已被删除的任务。
          }
        }
      }
    } catch {
      // 活跃探测查询失败不影响页面正常操作。
    }
  }

  if (!readonlyReplay.value && pendingReplay) {
    await handleSelectCase({
      type: 'case_link',
      scope: 'model_testing',
      history_id: pendingReplay.historyId,
      case_id: pendingReplay.caseId,
      round: pendingReplay.round ?? 1,
      label: pendingReplay.label ?? pendingReplay.caseId,
      status: 'completed',
      tone: 'neutral',
    })
  }
}

function toggleSelection(id: string) {
  if (isReadOnlyReplay.value) return
  if (selectedIds.value.includes(id)) {
    selectedIds.value = selectedIds.value.filter((item) => item !== id)
  } else {
    selectedIds.value = [...selectedIds.value, id]
  }
}

function toggleSelectAllFiltered() {
  if (isReadOnlyReplay.value) return
  const ids = filteredModels.value.map((item) => item.id)
  if (!ids.length) return
  if (allFilteredSelected.value) {
    const set = new Set(ids)
    selectedIds.value = selectedIds.value.filter((id) => !set.has(id))
  } else {
    selectedIds.value = Array.from(new Set([...selectedIds.value, ...ids]))
  }
}

function capabilityLabel(
  cap: CapabilityResult | undefined,
  mode: 'availability' | 'tool' | 'streaming' | 'structured',
): string {
  const safe = capOf(cap)
  if (mode === 'availability') {
    if (safe.status === 'available') return safe.latency_ms != null ? `可用 · ${Math.round(safe.latency_ms)}ms` : '可用'
    if (safe.status === 'unavailable') return '不可用'
    if (safe.status === 'unsupported') return '暂不支持'
    if (safe.status === 'error') return '错误'
    return '未测'
  }
  if (mode === 'streaming') {
    if (safe.status === 'supported') {
      return safe.ttft_ms != null ? `TTFT ${Math.round(safe.ttft_ms)}ms` : '支持流式'
    }
    if (safe.status === 'partial') return '非 SSE'
    if (safe.status === 'unsupported') return '不支持'
    if (safe.status === 'error') return '错误'
    return '未测'
  }
  if (mode === 'structured') {
    if (safe.status === 'supported') return 'Schema'
    if (safe.status === 'partial') return 'JSON Object'
    if (safe.status === 'unsupported') return '不支持'
    if (safe.status === 'error') return '错误'
    return '未测'
  }
  if (safe.status === 'supported') return '支持'
  if (safe.status === 'partial') return '接受参数'
  if (safe.status === 'unsupported') return '不支持'
  if (safe.status === 'inferred') return '元数据声明'
  if (safe.status === 'error') return '错误'
  return '未测'
}

function capabilityTone(status: CapabilityResult['status'] | string): string {
  if (status === 'available' || status === 'supported') return 'success'
  if (status === 'partial' || status === 'inferred') return 'info'
  if (status === 'unsupported') return 'warning'
  if (status === 'unavailable' || status === 'error') return 'danger'
  return 'neutral'
}

function categoryTone(category: ModelCategory): string {
  if (category === 'chat') return 'accent'
  if (category === 'reasoning') return 'info'
  if (category === 'embedding' || category === 'rerank') return 'success'
  if (category === 'image' || category === 'audio' || category === 'video') return 'warning'
  return 'neutral'
}

function typeSourceLabel(source: ModelProfile['type_source']): string {
  if (source === 'probed') return '实测'
  if (source === 'metadata') return '元数据'
  if (source === 'inferred') return '推断'
  return ''
}

function modalityClass(mod: ModalityItem): string {
  if (mod.status === 'supported' && mod.source === 'probed') return 'mod-probed'
  if (mod.status === 'supported' && mod.source === 'metadata') return 'mod-meta'
  if (mod.status === 'inferred') return 'mod-inferred'
  if (mod.status === 'unsupported') return 'mod-unsupported'
  return 'mod-neutral'
}

onMounted(() => {
  void restoreAndReconnectOnMount()
})

/** 切页卸载时仅中止本地流观察器并保存内存态，绝不取消后台正在执行的模型探测任务。 */
onBeforeUnmount(() => {
  unmounted = true
  if (observerAbort) {
    observerAbort.abort()
    observerAbort = null
  }
  if (historyReadAbort) {
    historyReadAbort.abort()
    historyReadAbort = null
  }
  syncToStore()
})
</script>

<template>
  <div class="model-testing">
    <TestWorkspace
      class="testing-workspace"
      history-key="api-agent.layout.model-testing.history"
      editor-key="api-agent.layout.model-testing.config"
      assistant-key="api-agent.layout.model-testing.chat"
      :editor-width="360"
      :results-max="1400"
      hide-history-on-narrow
    >
      <!-- 历史栏：持久化大模型探测记录 -->
      <template #history>
        <HistoryList
          title="探测历史"
          :items="historyEntries"
          :active-id="activeHistoryId"
          empty-text="还没有探测记录。选择模型并开始探测后，这里会保留脱敏的能力画像摘要。"
          :on-delete="handleDeleteHistory"
          :on-new-test="newTest"
          @select="selectHistory"
        />
      </template>

      <!-- 左栏：测试输入模块 -->
      <template #editor>
        <aside class="input-pane scroll-y">
          <div class="pane-head">
            <div class="title-wrap">
              <div class="title-icon"><Cpu :size="18" /></div>
              <div class="title-copy">
                <h1>大模型测试</h1>
                <p class="subtitle">拉取模型清单并探测可用性、工具调用与多模态支持</p>
              </div>
            </div>
            <button
              type="button"
              class="btn btn-ghost btn-sm clear-btn"
              :disabled="discovering || probing || isReadOnlyReplay"
              title="清空全部测试输入与响应结果"
              @click="requestClearAll"
            >
              <Trash2 :size="13" />
              清空
            </button>
          </div>

          <!-- 系统设置两个快捷按钮固定等宽紧挨排列 -->
          <div class="system-actions" role="group" aria-label="系统设置快捷填充">
            <button
              type="button"
              class="btn btn-sm system-btn"
              :disabled="!savedConfig?.base_url || isReadOnlyReplay"
              :title="savedConfig?.base_url ? `填入系统设置地址：${savedConfig.base_url}` : '系统设置尚未配置 Base URL'"
              @click="fillFromSystemSettings"
            >
              <Sparkles :size="13" />
              <span class="system-btn-label">填入系统设置地址</span>
            </button>
            <button
              type="button"
              class="btn btn-sm system-btn"
              :class="{ 'system-btn-active': reuseSavedKeyActive }"
              :aria-pressed="reuseSavedKeyActive"
              :disabled="!canReuseSavedKey || !!form.apiKey.trim() || isReadOnlyReplay"
              :title="form.apiKey.trim()
                ? '已填写临时 API Key，将优先使用上方密钥'
                : canReuseSavedKey
                  ? '切换是否同源复用系统设置已保存的密钥'
                  : '仅当地址与系统设置同源且系统已保存密钥时可复用'"
              @click="toggleUseSavedKey"
            >
              <KeyRound :size="13" />
              <span class="system-btn-label">{{ reuseSavedKeyActive ? '已复用系统同源密钥' : '复用系统同源密钥' }}</span>
            </button>
          </div>

          <div class="input-form">
            <div class="field">
              <span class="field-label">Base URL</span>
              <UrlInput
                v-model:address="form.baseUrl"
                :model-value="urlPrefix"
                label="Base URL"
                placeholder="https://api.openai.com/v1"
                :disabled="isReadOnlyReplay"
                @update:model-value="selectUrlPrefix"
                @submit="discover"
              />
            </div>

            <label class="field">
              <span class="field-label">API Key（仅本次探测内存使用，不持久化）</span>
              <div class="key-input-wrap">
                <input
                  v-model="form.apiKey"
                  :type="showKey ? 'text' : 'password'"
                  class="input mono"
                  :disabled="isReadOnlyReplay"
                  :placeholder="reuseSavedKeyActive ? '将复用系统设置同源密钥（可在此覆盖）' : 'sk-...'"
                  autocomplete="off"
                  spellcheck="false"
                  @keydown.enter="discover"
                />
                <button
                  type="button"
                  class="btn btn-ghost btn-icon key-eye"
                  :disabled="isReadOnlyReplay"
                  :title="showKey ? '隐藏密钥' : '显示密钥'"
                  @click="showKey = !showKey"
                >
                  <EyeOff v-if="showKey" :size="14" />
                  <Eye v-else :size="14" />
                </button>
              </div>
            </label>

            <label class="field">
              <span class="field-label">接口协议</span>
              <select v-model="form.protocol" class="select" aria-label="接口协议" :disabled="isReadOnlyReplay">
                <option v-for="opt in PROTOCOL_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
            </label>

            <div class="check-section" role="group" aria-label="模型能力探测项">
              <span class="field-label">探测项</span>
              <div class="check-grid">
                <label
                  v-for="item in CHECK_OPTIONS"
                  :key="item.key"
                  class="check-pill"
                  :class="{ 'check-pill-disabled': isReadOnlyReplay }"
                  :title="item.hint"
                >
                  <input v-model="checks[item.key]" type="checkbox" :disabled="isReadOnlyReplay" />
                  <span>{{ item.label }}</span>
                </label>
              </div>
            </div>

            <details class="settings-panel">
              <summary>设置</summary>
              <div class="settings-body">
                <TestSettingsFields group="model_testing" :disabled="isReadOnlyReplay" />
              </div>
            </details>
          </div>

          <div class="config-actions">
            <button
              type="button"
              class="btn btn-accent action-btn"
              :class="{ 'btn-loading': discovering }"
              :disabled="discovering || !effectiveBaseUrl || isReadOnlyReplay"
              @click="discover"
            >
              <svg v-if="discovering" class="probe-spinner" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
              </svg>
              <RefreshCw v-else :size="14" />
              <span>{{ discovering ? '拉取模型列表中…' : '拉取模型列表' }}</span>
            </button>
            <button
              type="button"
              class="btn action-btn"
              :class="{ 'btn-loading': probing }"
              :disabled="isReadOnlyReplay || !selectableProbeIds.length || !enabledChecks.length || (hasPersistentRunApi && liveRunning)"
              @click="runProbe(selectableProbeIds)"
            >
              <svg v-if="probing" class="probe-spinner" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
              </svg>
              <Play v-else :size="14" />
              <span>
                {{
                  probing
                    ? (hasPersistentRunApi
                      ? (liveStage === 'cooling_down'
                        ? `轮间冷却中（第 ${currentRound}/${totalRounds} 轮）`
                        : `探测中（${probingCompleted}/${probingTotal}）`)
                      : (selectableProbeIds.length
                        ? `加入探测队列（+${selectableProbeIds.length}）`
                        : `并发探测中（${probingCompleted}/${probingTotal}）`))
                    : `开始探测（${selectedIds.length}）`
                }}
              </span>
            </button>
            <button
              v-if="probing"
              type="button"
              class="btn btn-danger action-btn"
              :disabled="cancellingRun"
              @click="stopProbing()"
            >
              <Square :size="13" />
              <span>{{ cancellingRun ? '正在停止…' : '停止探测' }}</span>
            </button>
          </div>
        </aside>
      </template>

      <!-- 右栏：响应结果模块 -->
      <template #results>
        <section class="result-pane scroll-y">
          <!-- 子用例只读回放锁定横幅 -->
          <div v-if="isReadOnlyReplay && readonlyReplay" class="replay-banner" role="status">
            <div class="replay-banner-main">
              <span class="badge badge-info">
                <Lock :size="11" aria-hidden="true" />
                只读回放
              </span>
              <strong class="replay-title">{{ readonlyReplay.label }}</strong>
              <span class="badge badge-neutral mono">R{{ readonlyReplay.round }}</span>
              <span class="badge badge-neutral mono">{{ readonlyReplay.modelId }}</span>
              <span class="badge" :class="`badge-${capabilityTone(readonlyReplay.status)}`">
                {{ readonlyReplay.status }}
              </span>
            </div>
            <button type="button" class="btn btn-sm btn-accent" @click="retestCurrent">
              <RotateCcw :size="12" aria-hidden="true" />
              再次测试
            </button>
          </div>

          <!-- 已结束历史运行摘要横幅 -->
          <div v-else-if="currentRun && !liveRunning" class="run-summary-banner">
            <div class="replay-banner-main">
              <span class="badge" :class="`badge-${runStatusMeta(currentRun.status).tone}`">
                {{ runStatusMeta(currentRun.status).label }}
              </span>
              <span v-if="currentRun.badge || currentRun.source === 'ai'" class="badge badge-info">
                {{ currentRun.badge || 'AI 测试' }}
              </span>
              <strong class="replay-title">{{ currentRun.title || currentRun.base_url }}</strong>
              <span v-if="currentRun.total_rounds > 1" class="badge badge-neutral">
                {{ currentRun.completed_rounds }} / {{ currentRun.total_rounds }} 轮
              </span>
              <span v-if="currentRun.summary_text" class="hint">{{ currentRun.summary_text }}</span>
            </div>
            <button type="button" class="btn btn-sm" @click="retestCurrent">
              <RotateCcw :size="12" aria-hidden="true" />
              再次测试
            </button>
          </div>

          <div
            v-if="discoverMeta"
            class="discover-banner"
            :class="discoverMeta.ok ? 'banner-ok' : 'banner-err'"
          >
            <CheckCircle2 v-if="discoverMeta.ok" :size="15" />
            <XCircle v-else :size="15" />
            <span class="banner-msg">{{ discoverMeta.message }}</span>
            <span v-if="discoverMeta.protocol" class="banner-meta">{{ PROTOCOL_LABELS[discoverMeta.protocol] }}</span>
            <span v-if="discoverMeta.http_status" class="banner-meta">HTTP {{ discoverMeta.http_status }}</span>
            <span class="banner-meta">{{ Math.round(discoverMeta.latency_ms) }} ms</span>
          </div>

          <!-- 概览统计条 -->
          <section class="stats-row">
            <div class="stat-card">
              <span class="stat-label">已拉取模型</span>
              <strong class="stat-value">{{ stats.total }}</strong>
            </div>
            <div class="stat-card stat-card-success">
              <span class="stat-label">实测可用</span>
              <strong class="stat-value">{{ stats.available }}</strong>
            </div>
            <div class="stat-card stat-card-danger">
              <span class="stat-label">不可用 / 错误</span>
              <strong class="stat-value">{{ stats.unavailable }}</strong>
            </div>
            <div class="stat-card stat-card-accent">
              <span class="stat-label">支持工具调用</span>
              <strong class="stat-value">{{ stats.toolSupported }}</strong>
            </div>
            <div class="stat-card stat-card-info">
              <span class="stat-label">含多模态输入</span>
              <strong class="stat-value">{{ stats.multimodal }}</strong>
            </div>
          </section>

          <!-- 模型列表与能力矩阵 -->
          <section class="panel matrix-card">
            <div class="matrix-toolbar">
              <div class="category-tabs">
                <button
                  v-for="tab in categoryTabs"
                  :key="tab.key"
                  type="button"
                  class="cat-chip"
                  :class="{ 'cat-chip-active': categoryFilter === tab.key }"
                  @click="categoryFilter = tab.key"
                >
                  <span>{{ tab.label }}</span>
                  <span class="cat-count">{{ tab.count }}</span>
                </button>
              </div>
              <div class="toolbar-right">
                <div class="search-box">
                  <Search :size="13" />
                  <input
                    v-model="searchQuery"
                    class="input search-input"
                    placeholder="筛选模型 ID 或提供方…"
                    spellcheck="false"
                  />
                </div>
                <button
                  v-if="filteredModels.length"
                  type="button"
                  class="btn btn-sm"
                  :disabled="isReadOnlyReplay"
                  @click="toggleSelectAllFiltered"
                >
                  {{ allFilteredSelected ? '取消全选' : `全选当前（${filteredModels.length}）` }}
                </button>
              </div>
            </div>

            <!-- 多维筛选条 -->
            <div class="filter-row" role="group" aria-label="模型多维筛选条件">
              <span class="filter-label">
                <Filter :size="12" aria-hidden="true" />
                筛选
              </span>
              <select v-model="availabilityFilter" class="select filter-select" aria-label="按可用性筛选">
                <option v-for="opt in AVAILABILITY_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
              <select v-model="toolFilter" class="select filter-select" aria-label="按工具调用筛选">
                <option v-for="opt in TOOL_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
              <select v-model="inputModalityFilter" class="select filter-select" aria-label="按输入模态筛选">
                <option v-for="opt in INPUT_MODALITY_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
              <select v-model="outputModalityFilter" class="select filter-select" aria-label="按输出模态筛选">
                <option v-for="opt in OUTPUT_MODALITY_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
              <span v-if="models.length && hasActiveFilters" class="filter-count mono">
                命中 {{ filteredModels.length }} / {{ models.length }}
              </span>
              <button
                v-if="hasActiveFilters"
                type="button"
                class="btn btn-ghost btn-sm reset-filter-btn"
                @click="resetFilters"
              >
                <RotateCcw :size="12" />
                重置筛选
              </button>
            </div>

            <!-- 实时探测进度条（含多轮轮次与冷却状态） -->
            <div v-if="probing" class="probe-queue-bar" role="status" aria-live="polite">
              <div class="probe-queue-main">
                <svg class="probe-spinner" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                  <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                </svg>
                <span v-if="liveStage === 'cooling_down'" class="probe-queue-text">
                  轮间冷却中（第 <strong class="mono">{{ currentRound }} / {{ totalRounds }}</strong> 轮，剩余
                  <strong class="mono">{{ cooldownRemainingMs ?? cooldownMs }}ms</strong>）
                </span>
                <span v-else-if="totalRounds > 1" class="probe-queue-text">
                  第 <strong class="mono">{{ currentRound || 1 }} / {{ totalRounds }}</strong> 轮探测中：已完成
                  <strong class="mono">{{ probingCompleted }} / {{ probingTotal }}</strong>
                </span>
                <span v-else class="probe-queue-text">
                  消费队列并发探测中：已完成 <strong class="mono">{{ probingCompleted }} / {{ probingTotal }}</strong>
                </span>
                <span v-if="activeProbingCount > 0" class="probe-queue-chip probe-queue-chip-active mono">探测中 {{ activeProbingCount }}</span>
                <span v-if="queuedProbingCount > 0" class="probe-queue-chip mono">排队中 {{ queuedProbingCount }}</span>
              </div>
              <div class="probe-queue-right">
                <div class="probe-progress-track" aria-hidden="true">
                  <div class="probe-progress-fill" :style="{ width: `${probeProgressPercent}%` }" />
                </div>
                <button type="button" class="btn btn-ghost btn-sm stop-probe-btn" :disabled="cancellingRun" @click="stopProbing()">
                  <Square :size="11" />
                  停止
                </button>
              </div>
            </div>

            <div v-if="!models.length" class="matrix-empty">
              <EmptyState
                :icon="Cpu"
                title="尚未拉取模型"
                description="在左侧填写 Base URL 与 API Key 后点击「拉取模型列表」，即可查看模型分类、可用性、工具调用与输入/输出模态。"
              />
            </div>

            <div v-else-if="!filteredModels.length" class="matrix-empty">
              <EmptyState
                :icon="Filter"
                title="没有符合筛选条件的模型"
                description="可放宽可用性、工具调用或模态筛选条件，或点击下方按钮重置筛选。"
              />
              <div class="empty-actions">
                <button type="button" class="btn btn-sm" @click="resetFilters">
                  <RotateCcw :size="13" />
                  重置筛选
                </button>
              </div>
            </div>

            <div v-else class="table-wrap">
              <table class="model-table">
                <thead>
                  <tr>
                    <th class="col-check">
                      <input
                        type="checkbox"
                        :checked="allFilteredSelected"
                        :disabled="isReadOnlyReplay"
                        aria-label="全选当前模型"
                        @change="toggleSelectAllFiltered"
                      />
                    </th>
                    <th>模型 ID</th>
                    <th>模型类型</th>
                    <th>可用性</th>
                    <th>工具调用</th>
                    <th>流式 / 结构化</th>
                    <th>输入模态</th>
                    <th>输出模态</th>
                    <th class="col-actions">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="item in filteredModels"
                    :key="item.id"
                    :class="{
                      'row-active': activeModelId === item.id,
                      'row-probing': probingStateOf(item.id) === 'probing',
                      'row-queued': probingStateOf(item.id) === 'queued',
                    }"
                    @click="activeModelId = item.id"
                  >
                    <td class="col-check" @click.stop>
                      <input
                        type="checkbox"
                        :checked="selectedIds.includes(item.id)"
                        :disabled="isReadOnlyReplay"
                        :aria-label="`选择 ${item.id}`"
                        @change="toggleSelection(item.id)"
                      />
                    </td>
                    <td>
                      <div class="model-id mono">{{ item.id }}</div>
                      <div v-if="item.owned_by" class="model-owner">{{ item.owned_by }}</div>
                    </td>
                    <td>
                      <span class="badge" :class="`badge-${categoryTone(item.model_type)}`">
                        {{ CATEGORY_LABELS[item.model_type] }}
                      </span>
                      <span v-if="typeSourceLabel(item.type_source)" class="source-hint">
                        {{ typeSourceLabel(item.type_source) }}
                      </span>
                    </td>
                    <td>
                      <span
                        v-if="isModelWaiting(item.id) && (hasQueuedCheck(item.id, 'availability') || hasQueuedCheck(item.id, 'output_modalities'))"
                        class="badge badge-waiting"
                        :class="probingStateOf(item.id) === 'queued' ? 'badge-queued' : 'badge-probing'"
                      >
                        <svg
                          class="probe-spinner probe-spinner-sm"
                          :class="{ 'probe-spinner-queued': probingStateOf(item.id) === 'queued' }"
                          viewBox="0 0 20 20"
                          fill="none"
                          aria-hidden="true"
                        >
                          <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                          <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                        </svg>
                        <span>{{ waitLabelFor(item.id) }}</span>
                      </span>
                      <span
                        v-else
                        class="badge"
                        :class="`badge-${capabilityTone(item.availability.status)}`"
                        :title="item.availability.detail || ''"
                      >
                        {{ capabilityLabel(item.availability, 'availability') }}
                      </span>
                    </td>
                    <td>
                      <span
                        v-if="isModelWaiting(item.id) && hasQueuedCheck(item.id, 'tool_calling')"
                        class="badge badge-waiting"
                        :class="probingStateOf(item.id) === 'queued' ? 'badge-queued' : 'badge-probing'"
                      >
                        <svg
                          class="probe-spinner probe-spinner-sm"
                          :class="{ 'probe-spinner-queued': probingStateOf(item.id) === 'queued' }"
                          viewBox="0 0 20 20"
                          fill="none"
                          aria-hidden="true"
                        >
                          <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                          <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                        </svg>
                        <span>{{ waitLabelFor(item.id) }}</span>
                      </span>
                      <span
                        v-else
                        class="badge"
                        :class="`badge-${capabilityTone(item.tool_calling.status)}`"
                        :title="item.tool_calling.detail || ''"
                      >
                        <Wrench v-if="item.tool_calling.status === 'supported'" :size="11" />
                        {{ capabilityLabel(item.tool_calling, 'tool') }}
                      </span>
                    </td>
                    <td>
                      <div class="modality-group">
                        <span
                          v-if="isModelWaiting(item.id) && hasQueuedCheck(item.id, 'streaming')"
                          class="badge badge-waiting"
                          :class="probingStateOf(item.id) === 'queued' ? 'badge-queued' : 'badge-probing'"
                        >
                          <svg
                            class="probe-spinner probe-spinner-sm"
                            :class="{ 'probe-spinner-queued': probingStateOf(item.id) === 'queued' }"
                            viewBox="0 0 20 20"
                            fill="none"
                            aria-hidden="true"
                          >
                            <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                            <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                          </svg>
                          <span>{{ probingStateOf(item.id) === 'queued' ? '排队中' : '流式探测' }}</span>
                        </span>
                        <span
                          v-else
                          class="badge"
                          :class="`badge-${capabilityTone(capOf(item.streaming).status)}`"
                          :title="capOf(item.streaming).detail || '流式 SSE 尚未探测'"
                        >
                          {{ capabilityLabel(item.streaming, 'streaming') }}
                        </span>
                        <span
                          v-if="isModelWaiting(item.id) && hasQueuedCheck(item.id, 'structured_output')"
                          class="badge badge-waiting"
                          :class="probingStateOf(item.id) === 'queued' ? 'badge-queued' : 'badge-probing'"
                        >
                          <svg
                            class="probe-spinner probe-spinner-sm"
                            :class="{ 'probe-spinner-queued': probingStateOf(item.id) === 'queued' }"
                            viewBox="0 0 20 20"
                            fill="none"
                            aria-hidden="true"
                          >
                            <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                            <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                          </svg>
                          <span>{{ probingStateOf(item.id) === 'queued' ? '排队中' : '结构探测' }}</span>
                        </span>
                        <span
                          v-else
                          class="badge"
                          :class="`badge-${capabilityTone(capOf(item.structured_output).status)}`"
                          :title="capOf(item.structured_output).detail || '结构化输出尚未探测'"
                        >
                          {{ capabilityLabel(item.structured_output, 'structured') }}
                        </span>
                      </div>
                    </td>
                    <td>
                      <div class="modality-group">
                        <span
                          v-for="mod in item.input_modalities"
                          :key="`in-${mod.modality}`"
                          class="mod-chip"
                          :class="modalityClass(mod)"
                          :title="mod.detail || mod.source"
                        >
                          {{ MODALITY_LABELS[mod.modality] }}
                        </span>
                        <span v-if="!item.input_modalities.length" class="muted-dash">—</span>
                      </div>
                    </td>
                    <td>
                      <div class="modality-group">
                        <span
                          v-for="mod in item.output_modalities"
                          :key="`out-${mod.modality}`"
                          class="mod-chip"
                          :class="modalityClass(mod)"
                          :title="mod.detail || mod.source"
                        >
                          {{ MODALITY_LABELS[mod.modality] }}
                        </span>
                        <span v-if="!item.output_modalities.length" class="muted-dash">—</span>
                      </div>
                    </td>
                    <td class="col-actions" @click.stop>
                      <button
                        type="button"
                        class="btn btn-sm row-probe-btn"
                        :class="{ 'btn-loading': isModelWaiting(item.id) }"
                        :disabled="isReadOnlyReplay || isModelWaiting(item.id) || !enabledChecks.length || (hasPersistentRunApi && liveRunning)"
                        @click="runProbe([item.id])"
                      >
                        <svg
                          v-if="isModelWaiting(item.id)"
                          class="probe-spinner probe-spinner-sm"
                          :class="{ 'probe-spinner-queued': probingStateOf(item.id) === 'queued' }"
                          viewBox="0 0 20 20"
                          fill="none"
                          aria-hidden="true"
                        >
                          <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                          <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                        </svg>
                        <span>{{ isModelWaiting(item.id) ? waitLabelFor(item.id) : '探测' }}</span>
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <!-- 子用例只读回放请求快照卡片 -->
          <section v-if="isReadOnlyReplay && readonlyReplay?.subCase" class="panel detail-card replay-snapshot-card">
            <div class="detail-head">
              <div>
                <h2 class="mono">子用例回放快照 · {{ readonlyReplay.subCase.case_id }}</h2>
                <span class="hint">
                  第 {{ readonlyReplay.subCase.round_index }} 轮 · 模型 {{ readonlyReplay.subCase.model_id }}
                  <template v-if="readonlyReplay.subCase.latency_ms != null"> · 耗时 {{ Math.round(readonlyReplay.subCase.latency_ms) }}ms</template>
                  <template v-if="readonlyReplay.subCase.ttft_ms != null"> · TTFT {{ Math.round(readonlyReplay.subCase.ttft_ms) }}ms</template>
                </span>
              </div>
              <span class="badge" :class="`badge-${capabilityTone(readonlyReplay.subCase.status)}`">
                {{ readonlyReplay.subCase.status }}
              </span>
            </div>
            <div class="detail-grid">
              <div class="detail-box">
                <h3>请求参数快照（已脱敏）</h3>
                <dl class="kv-list">
                  <dt>Base URL</dt>
                  <dd class="mono">{{ readonlyReplay.subCase.request_snapshot?.base_url ?? '—' }}</dd>
                  <dt>协议 / 端点</dt>
                  <dd class="mono">
                    {{ readonlyReplay.subCase.request_snapshot?.protocol ?? '—' }}
                    <template v-if="readonlyReplay.subCase.request_snapshot?.endpoint_path">
                      · {{ readonlyReplay.subCase.request_snapshot.endpoint_path }}
                    </template>
                  </dd>
                  <dt>探测项</dt>
                  <dd>{{ (readonlyReplay.subCase.checks ?? []).join(', ') || '—' }}</dd>
                  <dt>单次超时</dt>
                  <dd class="mono">{{ readonlyReplay.subCase.request_snapshot?.timeout_seconds ?? '—' }} s</dd>
                </dl>
              </div>
              <div class="detail-box detail-raw">
                <h3>观测快照</h3>
                <pre class="mono raw-json">{{ JSON.stringify(readonlyReplay.subCase.observation ?? {}, null, 2) }}</pre>
              </div>
            </div>
          </section>

          <!-- 选中模型详情面板 -->
          <section v-if="activeModel" class="panel detail-card">
            <div class="detail-head">
              <div>
                <div class="detail-title-row">
                  <h2 class="mono">{{ activeModel.id }}</h2>
                  <span
                    v-if="isModelWaiting(activeModel.id)"
                    class="badge badge-waiting"
                    :class="probingStateOf(activeModel.id) === 'queued' ? 'badge-queued' : 'badge-probing'"
                  >
                    <svg
                      class="probe-spinner probe-spinner-sm"
                      :class="{ 'probe-spinner-queued': probingStateOf(activeModel.id) === 'queued' }"
                      viewBox="0 0 20 20"
                      fill="none"
                      aria-hidden="true"
                    >
                      <circle class="probe-spinner-track" cx="10" cy="10" r="7.2" />
                      <circle class="probe-spinner-arc" cx="10" cy="10" r="7.2" />
                    </svg>
                    <span>{{ probingStateOf(activeModel.id) === 'queued' ? '队列等待中…' : '正在探测中…' }}</span>
                  </span>
                </div>
                <span class="hint">
                  类型：{{ CATEGORY_LABELS[activeModel.model_type] }}
                  <template v-if="activeModel.owned_by"> · 提供方：{{ activeModel.owned_by }}</template>
                </span>
              </div>
              <div class="legend">
                <span class="mod-chip mod-probed">实测支持</span>
                <span class="mod-chip mod-meta">元数据声明</span>
                <span class="mod-chip mod-inferred">规则推断</span>
                <span class="mod-chip mod-unsupported">实测不支持</span>
              </div>
            </div>

            <div class="detail-grid">
              <div class="detail-box">
                <h3>可用性与进阶能力</h3>
                <dl class="kv-list">
                  <dt>可用性</dt>
                  <dd>{{ capabilityLabel(activeModel.availability, 'availability') }} — {{ activeModel.availability.detail || '尚未执行探测' }}</dd>
                  <dt>工具调用</dt>
                  <dd>{{ capabilityLabel(activeModel.tool_calling, 'tool') }} — {{ activeModel.tool_calling.detail || '尚未执行探测' }}</dd>
                  <dt>流式 SSE</dt>
                  <dd>{{ capabilityLabel(activeModel.streaming, 'streaming') }} — {{ capOf(activeModel.streaming).detail || '尚未执行探测' }}</dd>
                  <dt>结构化输出</dt>
                  <dd>{{ capabilityLabel(activeModel.structured_output, 'structured') }} — {{ capOf(activeModel.structured_output).detail || '尚未执行探测' }}</dd>
                </dl>
              </div>

              <div class="detail-box">
                <h3>模态明细</h3>
                <dl class="kv-list">
                  <dt>输入模态</dt>
                  <dd>
                    <span v-for="m in activeModel.input_modalities" :key="`d-in-${m.modality}`" class="detail-mod">
                      {{ MODALITY_LABELS[m.modality] }}（{{ m.detail || m.status }}）
                    </span>
                    <span v-if="!activeModel.input_modalities.length">暂无记录</span>
                  </dd>
                  <dt>输出模态</dt>
                  <dd>
                    <span v-for="m in activeModel.output_modalities" :key="`d-out-${m.modality}`" class="detail-mod">
                      {{ MODALITY_LABELS[m.modality] }}（{{ m.detail || m.status }}）
                    </span>
                    <span v-if="!activeModel.output_modalities.length">暂无记录</span>
                  </dd>
                </dl>
              </div>

              <div class="detail-box detail-raw">
                <h3>/models 原始元数据</h3>
                <pre class="mono raw-json">{{ JSON.stringify(activeModel.raw_metadata, null, 2) || '{}' }}</pre>
              </div>
            </div>
          </section>

          <!-- 当前运行的多轮子用例快照快速切换列表 -->
          <section v-if="currentRun?.sub_cases?.length" class="panel detail-card subcases-card">
            <div class="detail-head">
              <h2>探测子用例明细（{{ currentRun.sub_cases.length }}）</h2>
              <span class="hint">点击任意子用例进入只读回放</span>
            </div>
            <div class="subcase-chips">
              <button
                v-for="sub in currentRun.sub_cases"
                :key="sub.case_id"
                type="button"
                class="subcase-chip"
                :class="{ 'subcase-chip-active': readonlyReplay?.caseId === sub.case_id }"
                @click="enterSubCaseReadonlyReplay(currentRun.id, sub)"
              >
                <span class="mono">R{{ sub.round_index }}</span>
                <span class="mono">{{ sub.model_id }}</span>
                <span class="badge" :class="`badge-${capabilityTone(sub.status)}`">{{ sub.status }}</span>
                <span v-if="sub.latency_ms != null" class="hint mono">{{ Math.round(sub.latency_ms) }}ms</span>
              </button>
            </div>
          </section>
        </section>
      </template>

      <!-- 底部栏：AI 助手小Y（作用域 model_testing） -->
      <template #assistant>
        <TaskChat
          scope="model_testing"
          :execution-id="activeHistoryId"
          :draft-version="draftVersion"
          :current-test="assistantContext"
          :settings="assistantSettings"
          :ephemeral-api-key="ephemeralApiKeyForAssistant"
          :active-case-id="readonlyReplay?.caseId ?? null"
          @busy="chatBusy = $event"
          @select-case="handleSelectCase"
          @run-bound="handleRunBound"
          @run-completed="handleRunCompleted"
        />
      </template>
    </TestWorkspace>

    <!-- 清空确认弹窗 -->
    <AppModal
      :open="clearConfirmOpen"
      title="确认清空全部内容"
      subtitle="此操作仅重置当前页面的测试输入与响应结果，不会影响「系统设置」中保存的配置。"
      width="430px"
      @update:open="clearConfirmOpen = $event"
      @cancel="cancelClearAll"
    >
      <p class="confirm-text">
        确定要清空当前全部内容吗？将同时清空 Base URL 与前缀、临时 API Key、超时与探测项、筛选条件、模型列表及全部探测结果。
      </p>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="cancelClearAll">取消</button>
        <button type="button" class="btn btn-danger" @click="confirmClearAll">确认清空</button>
      </template>
    </AppModal>
  </div>
</template>

<style scoped>
.model-testing {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.testing-workspace {
  flex: 1;
  min-height: 0;
}

.retest-head-btn {
  white-space: nowrap;
}

/* ---------- 左栏：测试输入 ---------- */
.input-pane {
  height: 100%;
  min-height: 0;
  padding: 16px 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  background: var(--bg-surface);
  box-sizing: border-box;
}

.pane-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.title-wrap {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}

.title-copy {
  min-width: 0;
}

.title-icon {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  color: var(--accent);
  display: grid;
  place-items: center;
  flex: none;
}

.title-wrap h1 {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
}

.subtitle {
  margin: 2px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  line-height: 1.45;
}

.clear-btn {
  flex: none;
  color: var(--text-secondary);
}

.clear-btn:not(:disabled):hover {
  color: var(--danger);
  background: var(--danger-soft);
}

/* 填写系统设置两个按钮固定等宽并排，避免点击激活时因文案/字重变化触发折行或撑大 */
.system-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  flex: none;
}

.system-btn {
  width: 100%;
  min-width: 0;
  height: 27px;
  padding: 0 6px;
  gap: 4px;
  justify-content: center;
  white-space: nowrap;
}

.system-btn svg {
  flex: none;
}

.system-btn-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.system-btn-active {
  border-color: var(--accent-border);
  background: var(--accent-soft);
  color: var(--accent);
}

.input-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.key-input-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.key-input-wrap .input {
  padding-right: 34px;
}

.key-eye {
  position: absolute;
  right: 2px;
  width: 28px;
  height: 28px;
}

.check-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.check-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.check-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 9px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}

.check-pill-disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.settings-panel {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  padding: 8px 10px;
}

.settings-panel > summary {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}

.settings-body {
  margin-top: 10px;
}

.config-actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 2px;
}

.action-btn {
  width: 100%;
  justify-content: center;
  height: 34px;
}

/* ---------- 右栏：响应结果 ---------- */
.result-pane {
  height: 100%;
  min-height: 0;
  padding: 16px 20px 26px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  box-sizing: border-box;
}

.replay-banner,
.run-summary-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 9px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--info-border);
  background: var(--info-soft);
  flex: none;
}

.run-summary-banner {
  border-color: var(--border-subtle);
  background: var(--bg-subtle);
}

.replay-banner-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.replay-title {
  font-size: 12.5px;
  color: var(--text-primary);
}

.panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.discover-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-size: 12px;
  border: 1px solid transparent;
  flex: none;
}

.banner-ok {
  background: var(--success-soft);
  border-color: var(--success-border);
  color: var(--success);
}

.banner-err {
  background: var(--danger-soft);
  border-color: var(--danger-border);
  color: var(--danger);
}

.banner-msg {
  font-weight: 500;
  flex: 1;
}

.banner-meta {
  font-family: var(--font-mono);
  font-size: 11px;
  opacity: 0.9;
}

/* 统计条 */
.stats-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
  flex: none;
}

.stat-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.stat-card-success { border-left-color: var(--success); }
.stat-card-danger { border-left-color: var(--danger); }
.stat-card-accent { border-left-color: var(--accent); }
.stat-card-info { border-left-color: var(--info); }

.stat-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.stat-value {
  font-size: 18px;
  font-weight: 700;
  font-family: var(--font-mono);
  color: var(--text-primary);
}

/* 矩阵表格与多维筛选 */
.matrix-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex: none;
}

.matrix-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--panel-header-gradient);
  flex-wrap: wrap;
  flex: none;
}

.category-tabs {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
}

.cat-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 25px;
  padding: 0 9px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.cat-chip-active {
  border-color: var(--accent-border);
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}

.cat-count {
  font-family: var(--font-mono);
  font-size: 10.5px;
  opacity: 0.8;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.search-box {
  position: relative;
  display: flex;
  align-items: center;
  color: var(--text-tertiary);
}

.search-box svg {
  position: absolute;
  left: 9px;
  pointer-events: none;
}

.search-input {
  width: 200px;
  height: 27px;
  padding-left: 27px;
  font-size: 12px;
}

.filter-row {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  flex-wrap: wrap;
  flex: none;
}

.filter-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  white-space: nowrap;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-tertiary);
  margin-right: 2px;
}

.filter-select {
  width: auto;
  flex: 0 0 auto;
  min-width: 110px;
  height: 28px;
  padding: 0 24px 0 9px;
  font-size: 12px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-primary);
}

.filter-count {
  flex: none;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-tertiary);
  margin-left: auto;
}

.reset-filter-btn {
  flex: none;
  white-space: nowrap;
  height: 28px;
}

.matrix-empty {
  padding: 36px 16px;
}

.empty-actions {
  display: flex;
  justify-content: center;
  margin-top: 10px;
}

/* 模型列表独立滚动容器：列表较长时在卡片内独立滚动，表头吸顶且不把下方详情面板顶出视野 */
.table-wrap {
  flex: 1 1 auto;
  min-height: 0;
  max-height: clamp(240px, 46vh, 520px);
  overflow-x: auto;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.model-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 12px;
}

.model-table th,
.model-table td {
  padding: 9px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.model-table th {
  position: sticky;
  top: 0;
  z-index: 2;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  background: var(--bg-subtle);
  white-space: nowrap;
}

.model-table tbody tr {
  cursor: pointer;
  transition: background var(--motion-fast);
}

.model-table tbody tr:hover {
  background: var(--bg-hover);
}

.model-table tbody tr.row-active {
  background: var(--bg-active);
}

.col-check {
  width: 34px;
  text-align: center;
}

.col-actions {
  width: 84px;
  text-align: right;
}

.model-id {
  font-weight: 600;
  color: var(--text-primary);
  word-break: break-all;
}

.model-owner {
  font-size: 10.5px;
  color: var(--text-tertiary);
  margin-top: 2px;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid transparent;
  white-space: nowrap;
}

.badge-success { background: var(--success-soft); border-color: var(--success-border); color: var(--success); }
.badge-accent { background: var(--accent-soft); border-color: var(--accent-border); color: var(--accent); }
.badge-info { background: var(--info-soft); border-color: var(--info-border); color: var(--info); }
.badge-warning { background: var(--warning-soft); border-color: var(--warning-border); color: var(--warning); }
.badge-danger { background: var(--danger-soft); border-color: var(--danger-border); color: var(--danger); }
.badge-neutral { background: var(--bg-subtle); border-color: var(--border-subtle); color: var(--text-tertiary); }

.source-hint {
  margin-left: 5px;
  font-size: 10px;
  color: var(--text-tertiary);
}

.modality-group {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.mod-chip {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 500;
  border: 1px solid var(--border-subtle);
  white-space: nowrap;
}

.mod-probed {
  background: var(--success-soft);
  border-color: var(--success-border);
  color: var(--success);
}

.mod-meta {
  background: var(--info-soft);
  border-color: var(--info-border);
  color: var(--info);
}

.mod-inferred {
  background: var(--bg-subtle);
  border: 1px dashed var(--border-strong);
  color: var(--text-secondary);
}

.mod-unsupported {
  background: var(--danger-soft);
  border-color: var(--danger-border);
  color: var(--danger);
  text-decoration: line-through;
}

.mod-neutral {
  background: var(--bg-subtle);
  color: var(--text-tertiary);
}

.muted-dash {
  color: var(--text-tertiary);
}

/* 详情面板 */
.detail-card {
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: none;
}

.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.detail-head h2 {
  margin: 0;
  font-size: 14px;
}

.legend {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.replay-snapshot-card .detail-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.detail-box {
  padding: 11px 13px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
}

.detail-box h3 {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.kv-list {
  margin: 0;
  display: grid;
  gap: 6px;
  font-size: 12px;
}

.kv-list dt {
  font-size: 11px;
  color: var(--text-tertiary);
}

.kv-list dd {
  margin: 0;
  color: var(--text-primary);
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.detail-mod {
  font-size: 11.5px;
}

.raw-json {
  margin: 0;
  max-height: 150px;
  overflow: auto;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}

.subcase-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.subcase-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  color: var(--text-primary);
  font-size: 11.5px;
  cursor: pointer;
}

.subcase-chip:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
}

.subcase-chip-active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.confirm-text {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-secondary);
}

.spinning {
  animation: spin 0.9s linear infinite;
}

/* 动态 SVG 转圈等待动画与消费队列进度条 */
.probe-spinner {
  width: 14px;
  height: 14px;
  flex: none;
  display: inline-block;
  animation: spin 0.95s linear infinite;
}

.probe-spinner-sm {
  width: 12px;
  height: 12px;
}

.probe-spinner-queued {
  animation-duration: 1.55s;
  opacity: 0.85;
}

.probe-spinner-track {
  stroke: currentColor;
  stroke-width: 2.2;
  opacity: 0.22;
}

.probe-spinner-arc {
  stroke: currentColor;
  stroke-width: 2.3;
  stroke-linecap: round;
  stroke-dasharray: 28 48;
  stroke-dashoffset: 6;
  transform-origin: center;
  animation: probe-arc-sweep 1.35s ease-in-out infinite;
}

.btn-loading {
  position: relative;
  border-color: var(--accent-border);
  background: color-mix(in srgb, var(--accent-soft) 72%, var(--bg-surface));
  color: var(--accent);
}

.btn-loading:disabled {
  opacity: 0.94;
  cursor: progress;
}

.btn-accent.btn-loading {
  background: var(--accent);
  color: #ffffff;
}

.row-probe-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-width: 66px;
}

.probe-queue-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--accent-border);
  background: color-mix(in srgb, var(--accent-soft) 55%, var(--bg-surface));
  color: var(--accent);
  font-size: 12px;
  flex-wrap: wrap;
}

.probe-queue-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.probe-queue-text {
  color: var(--text-primary);
  font-weight: 500;
}

.probe-queue-chip {
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  color: var(--text-secondary);
}

.probe-queue-chip-active {
  border-color: var(--accent-border);
  background: var(--accent-soft);
  color: var(--accent);
}

.probe-queue-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.probe-progress-track {
  width: 112px;
  height: 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 18%, var(--bg-subtle));
  overflow: hidden;
}

.probe-progress-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--accent);
  transition: width 220ms ease;
}

.stop-probe-btn {
  height: 24px;
  padding: 0 8px;
  font-size: 11.5px;
  color: var(--danger);
}

.badge-waiting {
  gap: 5px;
}

.badge-probing {
  background: var(--accent-soft);
  border-color: var(--accent-border);
  color: var(--accent);
}

.badge-queued {
  background: var(--info-soft);
  border-color: var(--info-border);
  color: var(--info);
}

.model-table tbody tr.row-probing {
  background: color-mix(in srgb, var(--accent-soft) 52%, transparent);
}

.model-table tbody tr.row-queued {
  background: color-mix(in srgb, var(--info-soft) 32%, transparent);
}

.detail-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@keyframes probe-arc-sweep {
  0% {
    stroke-dasharray: 10 48;
    stroke-dashoffset: 0;
  }
  50% {
    stroke-dasharray: 32 48;
    stroke-dashoffset: -12;
  }
  100% {
    stroke-dasharray: 10 48;
    stroke-dashoffset: -45;
  }
}

/* 保留模型测试等待时的 SVG 转圈动画，确保系统减少动效设置下仍有清晰进度反馈 */
@media (prefers-reduced-motion: reduce) {
  .probe-spinner {
    animation-duration: 0.95s !important;
    animation-iteration-count: infinite !important;
  }
  .probe-spinner-queued {
    animation-duration: 1.55s !important;
    animation-iteration-count: infinite !important;
  }
  .probe-spinner-arc {
    animation-duration: 1.35s !important;
    animation-iteration-count: infinite !important;
  }
}

@media (max-width: 1180px) {
  .stats-row { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .detail-grid,
  .replay-snapshot-card .detail-grid { grid-template-columns: 1fr; }
}
</style>
