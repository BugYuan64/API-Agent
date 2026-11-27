<script setup lang="ts">
/** 汇总接口、长连接、智能群测和大模型测试的持久化结果，选中任务后按需读取完整证据。 */

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ChevronRight, Cpu, ExternalLink, History, Inbox, Radio, Search, Trash2, X } from 'lucide-vue-next'
import AiMarkdown from '@/components/AiMarkdown.vue'
import CopyButton from '@/components/CopyButton.vue'
import EmptyState from '@/components/EmptyState.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import TaskResultTree from '@/components/TaskResultTree.vue'
import { ApiError, describeError } from '@/api/client'
import {
  clearConsoleHistory,
  clearModelTestRuns,
  deleteConsoleHistory,
  deleteModelTestRun,
  deleteStream,
  getModelTestRun,
  listConsoleHistory,
  listModelTestRuns,
  listStreams,
  readStream,
} from '@/api/endpoints'
import { deleteTask, listTasks, readTask, taskHistoryLabel, type TaskHistory, type TaskRecord } from '@/api/tasks'
import type {
  CapabilityResult,
  ConsoleHistory,
  ModelTestRunDetail,
  ModelTestRunListItem,
  ModelTestRunStatus,
  StreamEvent,
  StreamHistory,
} from '@/api/types'
import { clearTestChats, removeTestChat } from '@/lib/chatHistory'
import {
  describeStopReason,
  formatBytes,
  formatDateTime,
  formatDuration,
  isSseHistory,
  prettyJson,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { useWorkbenchRunsStore } from '@/stores/workbenchRuns'
import { countCaseOutcomes, presentResult, resultCodeLabel, taskProgress } from '@/lib/results'
import { activeStreamWait } from '@/lib/taskResults'

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

const router = resolveRouter()
const workbenchRuns = resolveWorkbenchRuns()

const tab = ref<'http' | 'stream' | 'task' | 'model_testing'>('http')
const searchQuery = ref('')
const statusFilter = ref('all')
const httpItems = ref<ConsoleHistory[]>([])
const streamItems = ref<StreamHistory[]>([])
const taskItems = ref<TaskHistory[]>([])
const modelTestItems = ref<ModelTestRunListItem[]>([])

const STATUS_OPTIONS = {
  http: [
    { value: 'all', label: '全部状态' },
    { value: '2xx', label: '2xx 成功' },
    { value: '3xx', label: '3xx 重定向' },
    { value: '4xx', label: '4xx 客户端错误' },
    { value: '5xx', label: '5xx 服务端错误' },
    { value: 'error', label: '传输/超时失败' },
    { value: 'sse', label: 'SSE 流' },
  ],
  stream: [
    { value: 'all', label: '全部状态' },
    { value: 'open', label: '连接中 / 已连接' },
    { value: 'closed', label: '已关闭' },
    { value: 'error', label: '出错' },
    { value: 'cancelled', label: '已停止' },
  ],
  task: [
    { value: 'all', label: '全部状态' },
    { value: 'running', label: '进行中' },
    { value: 'completed', label: '流程完成' },
    { value: 'partial', label: '部分完成' },
    { value: 'timed_out', label: '已超时' },
    { value: 'failed', label: '任务失败' },
    { value: 'cancelled', label: '已取消' },
    { value: 'interrupted', label: '已中断' },
  ],
  model_testing: [
    { value: 'all', label: '全部状态' },
    { value: 'running', label: '探测中' },
    { value: 'completed', label: '已完成' },
    { value: 'cancelled', label: '已停止' },
    { value: 'error', label: '异常' },
    { value: 'interrupted', label: '已中断' },
  ],
} as const

const MODEL_PROTOCOL_LABELS: Record<string, string> = {
  auto: '自动识别',
  openai: 'OpenAI 兼容',
  anthropic: 'Anthropic',
  gemini: 'Gemini',
}

const statusOptions = computed(() => STATUS_OPTIONS[tab.value])
const searchPlaceholder = computed(() =>
  tab.value === 'http' ? '按 URL、方法或状态码筛选…'
    : tab.value === 'stream' ? '按 URL 或结束原因筛选…'
      : tab.value === 'task' ? '按任务标题或地址筛选…'
        : '按标题、Base URL 或模型 ID 筛选…',
)
const normalizedQuery = computed(() => searchQuery.value.trim().toLowerCase())
const hasActiveFilter = computed(() => Boolean(normalizedQuery.value || statusFilter.value !== 'all'))

/** 切换标签时重置状态筛选，避免跨标签状态值不匹配导致误过滤。 */
watch(tab, () => { statusFilter.value = 'all' })

/** 清空当前关键词与状态筛选。 */
function resetFilters() {
  searchQuery.value = ''
  statusFilter.value = 'all'
}

/** 统一解析跨四个测试页的 AI 测试徽标（显式 badge 优先，其次 source === 'ai' 回退为「AI 测试」）。 */
function resolveAiBadge(record?: { badge?: string | null; source?: string | null } | null): string | null {
  if (!record) return null
  const custom = record.badge?.trim()
  if (custom) return custom
  return record.source === 'ai' ? 'AI 测试' : null
}

/** 将模型探测任务状态映射为中文文案与语义色。 */
function modelRunStatusMeta(status: ModelTestRunStatus | string): { label: string; tone: string } {
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

function modelCapabilityLabel(cap: CapabilityResult | undefined, mode: 'availability' | 'tool' | 'streaming' | 'structured'): string {
  const status = cap?.status ?? 'untested'
  if (mode === 'availability') {
    if (status === 'available') return cap?.latency_ms != null ? `可用 · ${Math.round(cap.latency_ms)}ms` : '可用'
    if (status === 'unavailable') return '不可用'
    if (status === 'unsupported') return '暂不支持'
    if (status === 'error') return '错误'
    return '未测'
  }
  if (mode === 'streaming') {
    if (status === 'supported') return cap?.ttft_ms != null ? `TTFT ${Math.round(cap.ttft_ms)}ms` : '支持流式'
    if (status === 'partial') return '非 SSE'
    if (status === 'unsupported') return '不支持'
    if (status === 'error') return '错误'
    return '未测'
  }
  if (mode === 'structured') {
    if (status === 'supported') return 'Schema'
    if (status === 'partial') return 'JSON Object'
    if (status === 'unsupported') return '不支持'
    if (status === 'error') return '错误'
    return '未测'
  }
  if (status === 'supported') return '支持'
  if (status === 'partial') return '接受参数'
  if (status === 'unsupported') return '不支持'
  if (status === 'inferred') return '元数据声明'
  if (status === 'error') return '错误'
  return '未测'
}

function modelCapabilityTone(status: string | undefined): string {
  if (status === 'available' || status === 'supported') return 'success'
  if (status === 'partial' || status === 'inferred') return 'info'
  if (status === 'unsupported') return 'warning'
  if (status === 'unavailable' || status === 'error') return 'danger'
  return 'neutral'
}

/** 按 HTTP 状态分组或 SSE 标记过滤单条请求记录。 */
function matchesHttpStatus(record: ConsoleHistory, filter: string): boolean {
  if (filter === 'all') return true
  if (filter === 'error') return !record.ok
  if (filter === 'sse') return isSseHistory(record)
  if (!record.ok || record.status_code == null) return false
  const band = Math.floor(record.status_code / 100)
  return `${band}xx` === filter
}

const filteredHttpItems = computed(() => {
  const query = normalizedQuery.value
  const status = statusFilter.value
  return httpItems.value.filter(record => {
    if (!matchesHttpStatus(record, status)) return false
    if (!query) return true
    const fields = [
      record.title ?? '',
      record.url,
      record.method,
      String(record.status_code ?? ''),
      record.error_kind ?? '',
      resolveAiBadge(record) ?? '',
      isSseHistory(record) ? 'sse' : '',
    ]
    return fields.some(value => value.toLowerCase().includes(query))
  })
})

const filteredStreamItems = computed(() => {
  const query = normalizedQuery.value
  const status = statusFilter.value
  return streamItems.value.filter(record => {
    if (status !== 'all') {
      if (status === 'open') {
        if (record.status !== 'open' && record.status !== 'connecting') return false
      } else if (record.status !== status) {
        return false
      }
    }
    if (!query) return true
    const fields = [
      record.title ?? '',
      record.url,
      record.kind,
      record.status,
      record.stop_reason ?? '',
      resolveAiBadge(record) ?? '',
    ]
    return fields.some(value => value.toLowerCase().includes(query))
  })
})

const filteredTaskItems = computed(() => {
  const query = normalizedQuery.value
  const status = statusFilter.value
  return taskItems.value.filter(item => {
    if (status !== 'all') {
      if (status === 'running') {
        if (item.status !== 'queued' && item.status !== 'running') return false
      } else if (item.status !== status) {
        return false
      }
    }
    if (!query) return true
    const fields = [
      taskHistoryLabel(item),
      item.title ?? '',
      item.origin ?? '',
      item.status,
      item.error_kind ?? '',
      resolveAiBadge(item) ?? '',
    ]
    return fields.some(value => value.toLowerCase().includes(query))
  })
})

const filteredModelTestItems = computed(() => {
  const query = normalizedQuery.value
  const status = statusFilter.value
  return modelTestItems.value.filter(item => {
    if (status !== 'all') {
      if (status === 'running') {
        if (item.status !== 'queued' && item.status !== 'running') return false
      } else if (item.status !== status) {
        return false
      }
    }
    if (!query) return true
    const fields = [
      item.title ?? '',
      item.base_url,
      item.protocol,
      item.resolved_protocol ?? '',
      item.status,
      item.summary_text ?? '',
      resolveAiBadge(item) ?? '',
      ...(item.target_models ?? []),
    ]
    return fields.some(value => value.toLowerCase().includes(query))
  })
})

const loading = ref(false)
const selectedHttp = ref<ConsoleHistory | null>(null)
const selectedStream = ref<StreamHistory | null>(null)
const streamEvents = ref<StreamEvent[]>([])
const streamLoading = ref(false)
const selectedTaskId = ref('')
const task = ref<TaskRecord | null>(null)
const taskLoading = ref(false)
const selectedModelTestId = ref('')
const selectedModelTest = ref<ModelTestRunDetail | null>(null)
const modelTestLoading = ref(false)
const taskOutcomes = computed(() => countCaseOutcomes(task.value?.results ?? []).filter(item => item.count > 0))
const streamWait = computed(() => task.value ? activeStreamWait(task.value) : null)
const viewport = window.matchMedia('(max-width: 760px)')
const narrow = ref(viewport.matches)
const listWidth = ref(360)
const listHeight = ref(190)
let taskRequest: AbortController | undefined
let streamRequest: AbortController | undefined
let modelTestRequest: AbortController | undefined

function resizeLayout(event: MediaQueryListEvent) { narrow.value = event.matches }

/** 选中长连接历史时按需加载时间线事件，切换记录时取消前序请求防止迟到覆盖。 */
async function selectStream(record: StreamHistory) {
  streamRequest?.abort()
  const request = new AbortController()
  streamRequest = request
  if (selectedStream.value?.id !== record.id) streamEvents.value = []
  selectedStream.value = record
  streamLoading.value = true
  try {
    const page = await readStream(record.id, 0, 0, request.signal)
    if (!request.signal.aborted) streamEvents.value = page.events
  } catch (error) {
    if (!request.signal.aborted) useToastStore().push('danger', '读取会话事件失败', describeError(error))
  } finally {
    if (!request.signal.aborted) streamLoading.value = false
  }
}

/** Cancel stale reads after switching tasks so the detail always matches the selected row. */
async function selectTask(id: string) {
  taskRequest?.abort()
  const request = new AbortController()
  taskRequest = request
  if (selectedTaskId.value !== id) task.value = null
  selectedTaskId.value = id
  taskLoading.value = true
  try {
    const record = await readTask(id, request.signal)
    if (!request.signal.aborted) task.value = record
  } catch (error) {
    if (!request.signal.aborted) useToastStore().push('danger', '读取任务失败', describeError(error))
  } finally {
    if (!request.signal.aborted) taskLoading.value = false
  }
}

/** 选中大模型测试历史时按需加载完整能力矩阵与子用例快照，切换时中止过期请求。 */
async function selectModelTest(id: string) {
  if (typeof getModelTestRun !== 'function') return
  modelTestRequest?.abort()
  const request = new AbortController()
  modelTestRequest = request
  if (selectedModelTestId.value !== id) selectedModelTest.value = null
  selectedModelTestId.value = id
  modelTestLoading.value = true
  try {
    const detail = await getModelTestRun(id, request.signal)
    if (!request.signal.aborted) selectedModelTest.value = detail
  } catch (error) {
    if (!request.signal.aborted) useToastStore().push('danger', '读取大模型测试详情失败', describeError(error))
  } finally {
    if (!request.signal.aborted) modelTestLoading.value = false
  }
}

/** 跳转到对应工作台并打开该条历史记录及其持久化 AI 对话线程。 */
function openModelTestInWorkbench(id: string) {
  if (workbenchRuns && typeof workbenchRuns.setActiveHistory === 'function') {
    workbenchRuns.setActiveHistory('model_testing', id)
  }
  if (router && typeof router.push === 'function') {
    void router.push({ path: '/model-testing', query: { historyId: id } })
  }
}

/** 根据选中 HTTP 历史的状态码计算色调；未选择时使用 neutral。 */
const activeTone = computed(() => (selectedHttp.value ? presentResult(selectedHttp.value, selectedHttp.value.status_code).tone : 'neutral'))

/** 并行读取四类历史，成功来源独立显示，失败来源置空并汇总一条提示；始终解除 loading。 */
async function load() {
  loading.value = true
  try {
    const modelTestPromise = typeof listModelTestRuns === 'function'
      ? listModelTestRuns(60)
      : Promise.resolve({ items: [] as ModelTestRunListItem[] })
    const [http, streams, tasks, modelTests] = await Promise.allSettled([
      listConsoleHistory(60),
      listStreams(60),
      listTasks(),
      modelTestPromise,
    ])
    httpItems.value = http.status === 'fulfilled' ? http.value.items : []
    streamItems.value = streams.status === 'fulfilled' ? streams.value.items : []
    taskItems.value = tasks.status === 'fulfilled' ? tasks.value.items : []
    modelTestItems.value = modelTests.status === 'fulfilled' ? modelTests.value.items : []
    if (selectedTaskId.value && tab.value === 'task' && tasks.status === 'fulfilled') {
      if (taskItems.value.some(item => item.id === selectedTaskId.value)) void selectTask(selectedTaskId.value)
      else {
        taskRequest?.abort()
        selectedTaskId.value = ''
        task.value = null
        taskLoading.value = false
      }
    }
    if (selectedModelTestId.value && tab.value === 'model_testing' && modelTests.status === 'fulfilled') {
      if (modelTestItems.value.some(item => item.id === selectedModelTestId.value)) {
        void selectModelTest(selectedModelTestId.value)
      } else {
        modelTestRequest?.abort()
        selectedModelTestId.value = ''
        selectedModelTest.value = null
        modelTestLoading.value = false
      }
    }
    const failure = [http, streams, tasks, modelTests].find((item) => item.status === 'rejected')
    if (failure && failure.status === 'rejected') {
      useToastStore().push('warning', '部分历史加载失败', describeError(failure.reason))
    }
  } finally {
    loading.value = false
  }
}

/** 删除指定 HTTP 历史并同步列表、当前选中项及关联对话；失败提示。 */
async function removeRecord(record: ConsoleHistory) {
  try {
    await deleteConsoleHistory(record.id)
    httpItems.value = httpItems.value.filter((item) => item.id !== record.id)
    removeTestChat('console', record.id)
    if (selectedHttp.value?.id === record.id) selectedHttp.value = null
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 删除指定长连接会话历史并同步列表、事件预览及关联对话；失败提示。 */
async function removeStreamRecord(record: StreamHistory) {
  try {
    await deleteStream(record.id)
    streamItems.value = streamItems.value.filter((item) => item.id !== record.id)
    removeTestChat('websocket', record.id)
    if (selectedStream.value?.id === record.id) {
      streamRequest?.abort()
      selectedStream.value = null
      streamEvents.value = []
      streamLoading.value = false
    }
    useToastStore().push('success', '会话记录已删除')
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 删除指定智能群测历史并同步列表、详情及关联对话；运行中任务由后端 409 拒绝并提示。 */
async function removeTaskRecord(id: string) {
  try {
    await deleteTask(id)
    taskItems.value = taskItems.value.filter((item) => item.id !== id)
    removeTestChat('tasks', id)
    if (selectedTaskId.value === id) {
      taskRequest?.abort()
      selectedTaskId.value = ''
      task.value = null
      taskLoading.value = false
    }
    useToastStore().push('success', '任务记录已删除')
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 删除指定大模型测试历史并同步列表、详情及关联对话；运行中任务由后端 409 拒绝并提示。 */
async function removeModelTestRecord(id: string) {
  if (typeof deleteModelTestRun !== 'function') return
  try {
    await deleteModelTestRun(id)
    modelTestItems.value = modelTestItems.value.filter((item) => item.id !== id)
    removeTestChat('model_testing', id)
    if (selectedModelTestId.value === id) {
      modelTestRequest?.abort()
      selectedModelTestId.value = ''
      selectedModelTest.value = null
      modelTestLoading.value = false
    }
    useToastStore().push('success', '大模型测试记录已删除')
  } catch (error) {
    const isActiveConflict =
      typeof ApiError === 'function' && error instanceof ApiError && error.status === 409
    useToastStore().push(
      isActiveConflict ? 'warning' : 'danger',
      isActiveConflict ? '无法删除运行中的任务' : '删除失败',
      isActiveConflict ? '该大模型测试仍在后台运行中，请先在工作台停止后再删除。' : describeError(error),
    )
  }
}

/** 经用户确认后清空服务端 HTTP 历史，同步清理本页选择及关联对话；失败提示。 */
async function clearAll() {
  if (!window.confirm('确定要清空全部 HTTP 请求历史吗？')) return
  try {
    await clearConsoleHistory()
    httpItems.value = []
    clearTestChats('console')
    selectedHttp.value = null
    useToastStore().push('success', 'HTTP 历史已清空')
  } catch (error) {
    useToastStore().push('danger', '清空失败', describeError(error))
  }
}

/** 经用户确认后清空服务端全部终态大模型测试历史（不影响运行中任务）。 */
async function clearModelTests() {
  if (typeof clearModelTestRuns !== 'function') return
  if (!window.confirm('确定要清空全部已结束的大模型测试历史吗？')) return
  try {
    await clearModelTestRuns()
    modelTestItems.value = modelTestItems.value.filter(
      (item) => item.status === 'queued' || item.status === 'running',
    )
    clearTestChats('model_testing')
    if (
      selectedModelTest.value
      && !modelTestItems.value.some((item) => item.id === selectedModelTest.value?.id)
    ) {
      modelTestRequest?.abort()
      selectedModelTestId.value = ''
      selectedModelTest.value = null
      modelTestLoading.value = false
    }
    useToastStore().push('success', '大模型测试历史已清空')
  } catch (error) {
    useToastStore().push('danger', '清空失败', describeError(error))
  }
}

onMounted(() => { viewport.addEventListener('change', resizeLayout); void load() })
onBeforeUnmount(() => {
  viewport.removeEventListener('change', resizeLayout)
  taskRequest?.abort()
  streamRequest?.abort()
  modelTestRequest?.abort()
})
</script>

<template>
  <div class="history-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">执行结果</h2>
      </div>
      <div class="page-actions">
        <button class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
        <button
          v-if="tab === 'http' && httpItems.length"
          class="btn btn-sm btn-danger"
          @click="clearAll"
        >
          <Trash2 :size="12" />
          清空
        </button>
        <button
          v-else-if="tab === 'model_testing' && modelTestItems.length"
          class="btn btn-sm btn-danger"
          @click="clearModelTests"
        >
          <Trash2 :size="12" />
          清空
        </button>
      </div>
    </div>

    <div class="tabs">
      <button
        v-for="item in [
          { key: 'http', label: `HTTP 请求 ${httpItems.length}` },
          { key: 'stream', label: `长连接会话 ${streamItems.length}` },
          { key: 'task', label: `智能群测 ${taskItems.length}` },
          { key: 'model_testing', label: `大模型测试 ${modelTestItems.length}` },
        ]"
        :key="item.key"
        class="tab"
        :class="{ 'tab-active': tab === item.key }"
        @click="tab = item.key as typeof tab"
      >
        {{ item.label }}
      </button>
    </div>

    <ResizableSplit :key="Number(narrow)" class="results-split" :direction="narrow ? 'vertical' : 'horizontal'"
      :size="narrow ? listHeight : listWidth" :min="narrow ? 110 : 215" :min-other="narrow ? 180 : 300"
      :persist-key="narrow ? 'api-agent.layout.results.height' : 'api-agent.layout.results.width'"
      label="调整记录列表与详情大小" @update:size="narrow ? listHeight = $event : listWidth = $event">
      <template #first>
        <div class="list-pane">
          <div class="filter-bar" role="search" aria-label="筛选历史记录">
            <label class="filter-search">
              <Search :size="13" class="filter-icon" aria-hidden="true" />
              <input
                v-model="searchQuery"
                type="search"
                class="input filter-input"
                :placeholder="searchPlaceholder"
                aria-label="按 URL 或关键词筛选"
                spellcheck="false"
              />
            </label>
            <select v-model="statusFilter" class="select filter-status" aria-label="按状态筛选">
              <option v-for="option in statusOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
            <button
              v-if="hasActiveFilter"
              type="button"
              class="btn btn-ghost btn-sm filter-clear"
              title="清空筛选"
              aria-label="清空筛选"
              @click="resetFilters"
            >
              <X :size="12" aria-hidden="true" />
            </button>
          </div>

          <div v-if="tab === 'http'" class="list scroll-y">
            <button
              v-for="record in filteredHttpItems"
              :key="record.id"
              class="row"
              :class="{ 'row-active': selectedHttp?.id === record.id }"
              @click="selectedHttp = record"
            >
              <div class="row-main">
                <div class="row-top">
                  <span class="method" :class="`method-${record.method.toLowerCase()}`">{{ record.method }}</span>
                  <span v-if="isSseHistory(record)" class="tag tag-accent">SSE</span>
                  <span class="row-status" :class="`tone-${presentResult(record, record.status_code).tone}`">
                    {{ record.ok ? record.status_code : presentResult(record).title }}
                  </span>
                  <span class="row-time">{{ formatDuration(record.elapsed_ms) }}</span>
                  <span class="row-fill" />
                  <span class="row-date">{{ formatDateTime(record.created_at) }}</span>
                </div>
                <div class="row-title-line">
                  <span class="row-url mono truncate" :title="record.title || record.url">{{ record.title || record.url }}</span>
                  <span v-if="resolveAiBadge(record)" class="tag tag-info ai-badge">{{ resolveAiBadge(record) }}</span>
                </div>
              </div>
              <ChevronRight :size="13" class="row-chevron" />
            </button>
            <EmptyState
              v-if="!filteredHttpItems.length && !loading"
              :icon="History"
              :title="hasActiveFilter ? '没有匹配的 HTTP 记录' : '没有 HTTP 请求记录'"
              :description="hasActiveFilter ? '尝试修改 URL 关键词或切换状态筛选。' : '在「接口测试」页发送请求后，这里会保留脱敏后的请求与响应摘要。'"
            />
          </div>

          <div v-else-if="tab === 'stream'" class="list scroll-y">
            <button v-for="record in filteredStreamItems" :key="record.id" class="row"
              :class="{ 'row-active': selectedStream?.id === record.id }" @click="selectStream(record)">
              <div class="row-main">
                <div class="row-top">
                  <span class="tag tag-accent">{{ record.kind === 'sse' ? 'SSE' : 'WS' }}</span>
                  <span class="row-status">{{ record.status }}</span>
                  <span class="row-fill" />
                  <span class="row-date">{{ formatDateTime(record.created_at) }}</span>
                </div>
                <div class="row-title-line">
                  <span class="row-url mono truncate" :title="record.title || record.url">{{ record.title || record.url }}</span>
                  <span v-if="resolveAiBadge(record)" class="tag tag-info ai-badge">{{ resolveAiBadge(record) }}</span>
                </div>
              </div>
              <ChevronRight :size="13" class="row-chevron" />
            </button>
            <EmptyState v-if="!filteredStreamItems.length && !loading" :icon="Radio"
              :title="hasActiveFilter ? '没有匹配的长连接会话' : '没有长连接会话'"
              :description="hasActiveFilter ? '尝试修改 URL 关键词或切换状态筛选。' : '在「长连接测试」页建立连接后，这里会显示会话结果。'" />
          </div>

          <div v-else-if="tab === 'task'" class="list scroll-y">
            <button v-for="item in filteredTaskItems" :key="item.id" class="row"
              :class="{ 'row-active': selectedTaskId === item.id }" @click="selectTask(item.id)">
              <div class="row-main">
                <div class="row-top">
                  <span class="tag" :class="`tag-${taskProgress(item.status).tone}`">{{ taskProgress(item.status).label }}</span>
                  <span class="row-fill" />
                  <span class="row-date">{{ formatDateTime(item.created_at) }}</span>
                </div>
                <div class="row-title-line">
                  <span class="row-url truncate" :title="taskHistoryLabel(item)">{{ taskHistoryLabel(item) }}</span>
                  <span v-if="resolveAiBadge(item)" class="tag tag-info ai-badge">{{ resolveAiBadge(item) }}</span>
                </div>
                <div v-if="item.origin" class="row-origin mono truncate" :title="item.origin">{{ item.origin }}</div>
              </div>
              <ChevronRight :size="13" class="row-chevron" />
            </button>
            <EmptyState v-if="!filteredTaskItems.length && !loading" :icon="History"
              :title="hasActiveFilter ? '没有匹配的智能群测记录' : '没有智能群测记录'"
              :description="hasActiveFilter ? '尝试修改关键词或切换状态筛选。' : '在「智能群测」页运行文档任务后，这里会显示逐条结果与总结。'" />
          </div>

          <div v-else class="list scroll-y">
            <button
              v-for="item in filteredModelTestItems"
              :key="item.id"
              class="row"
              :class="{ 'row-active': selectedModelTestId === item.id }"
              @click="selectModelTest(item.id)"
            >
              <div class="row-main">
                <div class="row-top">
                  <span class="tag" :class="`tag-${modelRunStatusMeta(item.status).tone}`">
                    {{ modelRunStatusMeta(item.status).label }}
                  </span>
                  <span v-if="item.resolved_protocol || item.protocol" class="tag">
                    {{ MODEL_PROTOCOL_LABELS[item.resolved_protocol || item.protocol] || (item.resolved_protocol || item.protocol) }}
                  </span>
                  <span v-if="item.total_rounds > 1" class="tag">{{ item.completed_rounds }}/{{ item.total_rounds }} 轮</span>
                  <span class="row-fill" />
                  <span class="row-date">{{ formatDateTime(item.created_at) }}</span>
                </div>
                <div class="row-title-line">
                  <span class="row-url truncate" :title="item.title || item.base_url">{{ item.title || item.base_url }}</span>
                  <span v-if="resolveAiBadge(item)" class="tag tag-info ai-badge">{{ resolveAiBadge(item) }}</span>
                </div>
                <div class="row-origin mono truncate" :title="item.base_url">
                  {{ item.base_url }} · {{ item.summary?.tested_count ?? item.target_models.length }} 模型
                </div>
              </div>
              <ChevronRight :size="13" class="row-chevron" />
            </button>
            <EmptyState
              v-if="!filteredModelTestItems.length && !loading"
              :icon="Cpu"
              :title="hasActiveFilter ? '没有匹配的大模型测试记录' : '没有大模型测试记录'"
              :description="hasActiveFilter ? '尝试修改关键词或切换状态筛选。' : '在「大模型测试」页执行探测后，这里会保留脱敏的能力矩阵与多轮结果摘要。'"
            />
          </div>
        </div>
      </template>
      <template #second>
      <div v-if="tab === 'http'" class="detail scroll-y">
        <template v-if="selectedHttp">
          <div class="detail-head">
            <div class="detail-title-row">
              <span class="method" :class="`method-${selectedHttp.method.toLowerCase()}`">
                {{ selectedHttp.method }}
              </span>
              <span v-if="isSseHistory(selectedHttp)" class="tag tag-accent">SSE</span>
              <span class="tag" :class="`tag-${activeTone === 'neutral' ? '' : activeTone}`">
                {{ selectedHttp.ok ? selectedHttp.status_code : presentResult(selectedHttp).title }}
              </span>
              <span v-if="resolveAiBadge(selectedHttp)" class="tag tag-info ai-badge">{{ resolveAiBadge(selectedHttp) }}</span>
              <span class="tag">{{ formatDuration(selectedHttp.elapsed_ms) }}</span>
              <span class="tag">{{ formatBytes(selectedHttp.response_bytes) }}</span>
              <div class="row-fill" />
              <CopyButton
                :text="String(selectedHttp.response_preview ?? '')"
                label="复制响应"
              />
              <button class="btn btn-ghost btn-sm" title="删除这条记录" aria-label="删除这条记录" @click="removeRecord(selectedHttp)">
                <Trash2 :size="12" />
              </button>
            </div>
            <div v-if="selectedHttp.title" class="detail-title-text">{{ selectedHttp.title }}</div>
            <div class="detail-url mono">{{ selectedHttp.url }}</div>
            <div class="hint">{{ formatDateTime(selectedHttp.created_at) }}</div>
          </div>

          <div v-if="!selectedHttp.ok" class="detail-error">
            <strong>
              {{ presentResult(selectedHttp).title }}<template v-if="resultCodeLabel(presentResult(selectedHttp).resultStatus)"> · {{ resultCodeLabel(presentResult(selectedHttp).resultStatus) }}</template>
              <template v-if="selectedHttp.error_kind"> · <code>{{ selectedHttp.error_kind }}</code></template>
            </strong>
            <span>{{ presentResult(selectedHttp).message }}</span>
          </div>

          <div class="detail-section">
            <div class="mini-label">请求头（已脱敏）</div>
            <div v-for="(value, name) in selectedHttp.request_headers" :key="name" class="header-line">
              <span class="header-name mono">{{ name }}</span>
              <span class="header-value mono">{{ value }}</span>
            </div>
            <p v-if="!Object.keys(selectedHttp.request_headers).length" class="hint">未设置自定义头。</p>
          </div>

          <div v-if="selectedHttp.body_preview" class="detail-section">
            <div class="mini-label">请求体预览</div>
            <pre class="code">{{ prettyJson(selectedHttp.body_preview) }}</pre>
          </div>

          <div class="detail-section">
            <div class="mini-label">响应头</div>
            <div v-for="(value, name) in selectedHttp.response_headers" :key="name" class="header-line">
              <span class="header-name mono">{{ name }}</span>
              <span class="header-value mono">{{ JSON.stringify(value).replace(/[[\]"]/g, '') }}</span>
            </div>
            <p v-if="!Object.keys(selectedHttp.response_headers).length" class="hint">
              没有记录到响应头（请求未送达）。
            </p>
          </div>

          <div v-if="selectedHttp.response_preview" class="detail-section">
            <div class="mini-label">
              响应体预览
              <span class="hint">（最多保留前 8KiB；完整长度 {{ formatBytes(selectedHttp.response_bytes) }}）</span>
            </div>
            <pre class="code">{{ prettyJson(selectedHttp.response_preview) }}</pre>
          </div>
        </template>
        <EmptyState
          v-else
          :icon="Inbox"
          title="选择左侧一条记录"
          description="这里会显示脱敏后的请求头、请求体预览与响应内容。"
        />
      </div>
      <div v-else-if="tab === 'stream'" class="detail scroll-y">
        <template v-if="selectedStream">
          <div class="detail-head">
            <div class="detail-title-row">
              <span class="tag tag-accent">{{ selectedStream.kind === 'sse' ? 'SSE' : 'WS' }}</span>
              <span class="tag" :class="`tag-${selectedStream.status === 'closed' ? 'success' : selectedStream.status === 'error' ? 'danger' : 'neutral'}`">{{ selectedStream.status }}</span>
              <span v-if="resolveAiBadge(selectedStream)" class="tag tag-info ai-badge">{{ resolveAiBadge(selectedStream) }}</span>
              <span class="tag">{{ selectedStream.event_count }} 事件</span>
              <span class="tag">{{ formatBytes(selectedStream.byte_count) }}</span>
              <div class="row-fill" />
              <button class="btn btn-ghost btn-sm" title="删除这条会话" aria-label="删除这条会话" @click="removeStreamRecord(selectedStream)">
                <Trash2 :size="12" />
              </button>
            </div>
            <div v-if="selectedStream.title" class="detail-title-text">{{ selectedStream.title }}</div>
            <div class="detail-url mono">{{ selectedStream.url }}</div>
            <div class="hint">{{ formatDateTime(selectedStream.opened_at ?? selectedStream.created_at) }} · {{ describeStopReason(selectedStream.stop_reason) }}</div>
            <p v-if="selectedStream.error_message" class="detail-error">{{ selectedStream.error_message }}</p>
          </div>
          <div class="detail-section">
            <div class="mini-label">已记录事件（最多前 500 条）</div>
            <p v-if="streamLoading" class="hint" role="status">正在读取会话事件…</p>
            <p v-else-if="!streamEvents.length" class="hint">暂无记录事件。</p>
            <div v-else class="stream-events">
              <div v-for="item in streamEvents" :key="item.sequence" class="stream-event-item">
                <div class="row-top">
                  <span class="tag">{{ item.direction === 'out' ? '发送' : item.kind }}</span>
                  <span class="row-time">#{{ item.sequence }} · {{ formatDuration(item.elapsed_ms) }}</span>
                </div>
                <pre v-if="item.data" class="code">{{ item.data }}</pre>
              </div>
            </div>
          </div>
        </template>
        <EmptyState v-else :icon="Inbox" title="选择左侧一条会话" />
      </div>
      <div v-else-if="tab === 'task'" class="detail scroll-y">
        <template v-if="task">
          <div class="detail-head">
            <div class="detail-title-row">
              <strong class="detail-url">{{ taskHistoryLabel(task) }}</strong>
              <span v-if="resolveAiBadge(task)" class="tag tag-info ai-badge">{{ resolveAiBadge(task) }}</span>
            </div>
            <div class="detail-title-row">
              <span class="tag" :class="`tag-${taskProgress(task.status).tone}`">{{ taskProgress(task.status).label }}</span>
              <span class="hint">{{ task.results.length }} / {{ task.snapshot.cases.length }} 条已记录</span>
              <a class="btn btn-sm" :href="`/api/tasks/${encodeURIComponent(task.id)}/report`" download>导出总结</a>
              <div class="row-fill" />
              <button class="btn btn-ghost btn-sm" title="删除这条任务" aria-label="删除这条任务" @click="removeTaskRecord(task.id)">
                <Trash2 :size="12" />
              </button>
            </div>
            <div class="detail-title-row">
              <span v-if="streamWait" class="tag tag-info">{{ streamWait.label }} 1</span>
              <span v-for="item in taskOutcomes" :key="item.status" class="tag" :class="`tag-${item.tone}`">{{ item.label }} {{ item.count }}</span>
            </div>
          </div>
          <p v-if="task.error_kind" class="detail-error">{{ task.error_kind }}</p>
          <TaskResultTree :key="task.id" :task="task" :stream-wait="streamWait" />
          <div class="detail-section">
            <div class="mini-label">AI 总结</div>
            <AiMarkdown v-if="task.summary" :text="task.summary" />
            <p v-else class="hint">暂无总结。</p>
          </div>
        </template>
        <p v-else-if="taskLoading" class="hint" role="status">正在读取智能群测结果…</p>
        <EmptyState v-else :icon="Inbox" title="选择左侧一条用例任务" />
      </div>
      <div v-else class="detail scroll-y">
        <template v-if="selectedModelTest">
          <div class="detail-head">
            <div class="detail-title-row">
              <strong class="detail-title-text">{{ selectedModelTest.title || selectedModelTest.base_url }}</strong>
              <span v-if="resolveAiBadge(selectedModelTest)" class="tag tag-info ai-badge">
                {{ resolveAiBadge(selectedModelTest) }}
              </span>
            </div>
            <div class="detail-title-row">
              <span class="tag" :class="`tag-${modelRunStatusMeta(selectedModelTest.status).tone}`">
                {{ modelRunStatusMeta(selectedModelTest.status).label }}
              </span>
              <span class="tag">
                {{ MODEL_PROTOCOL_LABELS[selectedModelTest.resolved_protocol || selectedModelTest.protocol] || (selectedModelTest.resolved_protocol || selectedModelTest.protocol) }}
              </span>
              <span class="tag">{{ selectedModelTest.target_models.length }} 模型</span>
              <span v-if="selectedModelTest.total_rounds > 1" class="tag">
                {{ selectedModelTest.completed_rounds }} / {{ selectedModelTest.total_rounds }} 轮 · 冷却 {{ selectedModelTest.cooldown_ms }}ms
              </span>
              <span v-if="selectedModelTest.summary?.avg_latency_ms != null" class="tag">
                平均 {{ Math.round(selectedModelTest.summary.avg_latency_ms) }}ms
              </span>
              <div class="row-fill" />
              <button
                type="button"
                class="btn btn-sm btn-accent"
                @click="openModelTestInWorkbench(selectedModelTest.id)"
              >
                <ExternalLink :size="12" />
                在工作台打开
              </button>
              <button
                type="button"
                class="btn btn-ghost btn-sm"
                title="删除这条记录"
                aria-label="删除这条记录"
                @click="removeModelTestRecord(selectedModelTest.id)"
              >
                <Trash2 :size="12" />
              </button>
            </div>
            <div class="detail-url mono">{{ selectedModelTest.base_url }}</div>
            <div class="hint">
              创建于 {{ formatDateTime(selectedModelTest.created_at) }}
              <template v-if="selectedModelTest.finished_at"> · 结束于 {{ formatDateTime(selectedModelTest.finished_at) }}</template>
            </div>
          </div>

          <p v-if="selectedModelTest.error_message" class="detail-error">{{ selectedModelTest.error_message }}</p>

          <div v-if="selectedModelTest.summary_text || selectedModelTest.summary" class="detail-section">
            <div class="mini-label">探测摘要</div>
            <p v-if="selectedModelTest.summary_text" class="model-summary-text">{{ selectedModelTest.summary_text }}</p>
            <div v-if="selectedModelTest.summary" class="detail-title-row">
              <span class="tag tag-success">可用 {{ selectedModelTest.summary.available_count }}</span>
              <span v-if="selectedModelTest.summary.unavailable_count" class="tag tag-danger">不可用 {{ selectedModelTest.summary.unavailable_count }}</span>
              <span v-if="selectedModelTest.summary.error_count" class="tag tag-danger">异常 {{ selectedModelTest.summary.error_count }}</span>
              <span class="tag tag-info">工具调用 {{ selectedModelTest.summary.tool_supported_count }}</span>
              <span class="tag">流式 {{ selectedModelTest.summary.streaming_supported_count }}</span>
              <span class="tag">结构化 {{ selectedModelTest.summary.structured_output_supported_count }}</span>
              <span v-if="selectedModelTest.summary.avg_ttft_ms != null" class="tag">
                平均 TTFT {{ Math.round(selectedModelTest.summary.avg_ttft_ms) }}ms
              </span>
            </div>
          </div>

          <div class="detail-section">
            <div class="mini-label">模型能力矩阵（{{ selectedModelTest.models_snapshot.length }}）</div>
            <div v-if="selectedModelTest.models_snapshot.length" class="model-table-wrap">
              <table class="history-model-table">
                <thead>
                  <tr>
                    <th>模型 ID</th>
                    <th>类型</th>
                    <th>可用性</th>
                    <th>工具调用</th>
                    <th>流式 SSE</th>
                    <th>结构化输出</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="model in selectedModelTest.models_snapshot" :key="model.id">
                    <td class="mono">{{ model.id }}</td>
                    <td>{{ model.model_type }}</td>
                    <td>
                      <span class="tag" :class="`tag-${modelCapabilityTone(model.availability?.status)}`">
                        {{ modelCapabilityLabel(model.availability, 'availability') }}
                      </span>
                    </td>
                    <td>
                      <span class="tag" :class="`tag-${modelCapabilityTone(model.tool_calling?.status)}`">
                        {{ modelCapabilityLabel(model.tool_calling, 'tool') }}
                      </span>
                    </td>
                    <td>
                      <span class="tag" :class="`tag-${modelCapabilityTone(model.streaming?.status)}`">
                        {{ modelCapabilityLabel(model.streaming, 'streaming') }}
                      </span>
                    </td>
                    <td>
                      <span class="tag" :class="`tag-${modelCapabilityTone(model.structured_output?.status)}`">
                        {{ modelCapabilityLabel(model.structured_output, 'structured') }}
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else class="hint">暂无模型画像快照。</p>
          </div>

          <div v-if="selectedModelTest.sub_cases.length" class="detail-section">
            <div class="mini-label">子用例快照（{{ selectedModelTest.sub_cases.length }}）</div>
            <div class="subcase-list">
              <div v-for="sub in selectedModelTest.sub_cases" :key="sub.case_id" class="subcase-row">
                <span class="tag">R{{ sub.round_index }}</span>
                <span class="mono subcase-model">{{ sub.model_id }}</span>
                <span class="tag" :class="`tag-${modelCapabilityTone(sub.status)}`">{{ sub.status }}</span>
                <span v-if="sub.latency_ms != null" class="row-time">{{ Math.round(sub.latency_ms) }}ms</span>
                <span v-if="sub.ttft_ms != null" class="row-time">TTFT {{ Math.round(sub.ttft_ms) }}ms</span>
              </div>
            </div>
          </div>
        </template>
        <p v-else-if="modelTestLoading" class="hint" role="status">正在读取大模型测试详情…</p>
        <EmptyState
          v-else
          :icon="Inbox"
          title="选择左侧一条大模型测试记录"
          description="这里会显示目标协议、多轮摘要、模型能力矩阵与子用例快照，并支持一键在工作台打开。"
        />
      </div>
      </template>
    </ResizableSplit>
  </div>
</template>

<style scoped>
.history-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 16px 10px;
  flex: none;
}

.page-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.015em;
}

.page-actions {
  display: flex;
  gap: 7px;
  flex: none;
}

.tabs {
  display: flex;
  gap: 2px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
  flex-wrap: wrap;
}

.tab {
  height: 33px;
  padding: 0 11px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12.5px;
}

.tab-active {
  color: var(--accent);
  font-weight: 600;
}

.results-split { flex: 1; }

.list-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.filter-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.filter-search {
  position: relative;
  display: flex;
  align-items: center;
  flex: 1 1 130px;
  min-width: 0;
}

.filter-icon {
  position: absolute;
  left: 8px;
  color: var(--text-tertiary);
  pointer-events: none;
}

.filter-input {
  width: 100%;
  min-width: 0;
  height: 28px;
  padding: 0 8px 0 26px;
  font-size: 11.5px;
}

.filter-status {
  flex: none;
  width: 112px;
  height: 28px;
  padding: 0 6px;
  font-size: 11.5px;
}

.filter-clear {
  flex: none;
  width: 26px;
  height: 28px;
  padding: 0;
  justify-content: center;
}

.list {
  flex: 1;
  min-height: 0;
  padding: 10px 12px 16px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
}

.row:hover {
  background: var(--bg-hover);
}

.row-active {
  background: var(--accent-soft);
  border-color: var(--accent-border);
}

.row-main {
  flex: 1;
  min-width: 0;
}

.row-top {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.row-title-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  margin-top: 2px;
}

.row-title-line .row-url {
  margin-top: 0;
  flex: 1;
  min-width: 0;
}

.ai-badge {
  flex: none;
  font-size: 10px;
  line-height: 1.2;
}

.row-status {
  font-family: var(--font-mono);
  font-weight: 600;
}

.tone-success { color: var(--success); }
.tone-info { color: var(--info); }
.tone-warning { color: var(--warning); }
.tone-danger { color: var(--danger); }
.tone-neutral { color: var(--text-tertiary); }

.row-time,
.row-date {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.row-fill {
  flex: 1;
}

.row-url {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-secondary);
}

.row-origin {
  margin-top: 1px;
  font-size: 10.5px;
  color: var(--text-tertiary);
}

.row-chevron {
  color: var(--text-tertiary);
  flex: none;
}

.detail {
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 12px 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-head {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.detail-title-row {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.detail-title-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  word-break: break-word;
}

.detail-url {
  font-size: 12px;
  color: var(--text-secondary);
  word-break: break-all;
}

.detail-error {
  display: flex;
  gap: 8px;
  padding: 9px 11px;
  border-radius: var(--radius-md);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 11.5px;
}

.detail-section {
  border-top: 1px solid var(--border-subtle);
  padding-top: 10px;
}

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
  gap: 10px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border-subtle);
  font-size: 11px;
}

.header-name {
  flex: none;
  min-width: 130px;
  font-weight: 600;
  color: var(--text-secondary);
}

.header-value {
  color: var(--text-tertiary);
  word-break: break-all;
}

.code {
  margin: 0;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  font-size: 11.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}

.model-summary-text {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--text-primary);
  line-height: 1.6;
}

.model-table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
}

.history-model-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11.5px;
}

.history-model-table th,
.history-model-table td {
  padding: 7px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
  white-space: nowrap;
}

.history-model-table th {
  background: var(--bg-subtle);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
}

.subcase-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.subcase-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  font-size: 11.5px;
}

.subcase-model {
  font-weight: 600;
  color: var(--text-primary);
}

.results-split.split-horizontal .list-pane { border-right: 1px solid var(--border-subtle); }
.results-split.split-vertical .list-pane { border-bottom: 1px solid var(--border-subtle); }
.stream-events { display: flex; flex-direction: column; gap: 8px; }
.stream-event-item { display: flex; flex-direction: column; gap: 4px; }
</style>
