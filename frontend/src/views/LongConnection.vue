<script setup lang="ts">
/** WebSocket 调试页面：依赖 streams API、请求头行模型与共享布局，启动会话、增量读取事件、跨页重连并回放历史与 AI 多轮测试子用例。 */

import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Cable,
  ChevronDown,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  CircleStop,
  Loader2,
  Lock,
  Play,
  Radio,
  RotateCcw,
  Send,
  Sparkles,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import ResponseDetails from '@/components/ResponseDetails.vue'
import TaskChat from '@/components/TaskChat.vue'
import HistoryList, { type HistoryBadge, type HistoryEntry } from '@/components/HistoryList.vue'
import TestWorkspace from '@/components/TestWorkspace.vue'
import UrlInput from '@/components/UrlInput.vue'
import { useHeaderRows } from '@/composables/useHeaderRows'
import { usePolling } from '@/composables/usePolling'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import {
  cancelStreamSuite,
  deleteStream,
  followStreamSuite,
  getActiveStreams,
  getStreamSubCase,
  listStreams,
  listStreamSubCases,
  readStream,
  readStreamInputs,
  sendStreamMessage,
  startStream,
  stopStream,
} from '@/api/endpoints'
import type {
  ActiveStreamItem,
  AssistantRunBoundPayload,
  CaseLinkBlock,
  StreamEvent,
  StreamHistoryItem,
  StreamSession,
  StreamSubCaseSummary,
  StreamSuiteLiveFrame,
  StreamSuiteStatus,
} from '@/api/types'
import {
  describeStopReason,
  formatBytes,
  formatDuration,
  formatRelative,
  looksLikeJson,
  prettyJson,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { useWorkbenchRunsStore } from '@/stores/workbenchRuns'
import { useStreamWorkbenchStore, type StreamReadonlyReplayMeta } from '@/stores/streamWorkbench'
import { presentResult, resultCodeLabel } from '@/lib/results'
import { webSocketSettings } from '@/lib/testSettings'
import TestSettingsFields from '@/components/TestSettingsFields.vue'
import { joinTargetUrl, urlSuffix } from '@/lib/urlPrefixes'
import { buildWebSocketTestContext } from '@/lib/assistantContext'
import { removeTestChat } from '@/lib/chatHistory'

const ACTIVE_STREAM_STATUSES = new Set(['queued', 'connecting', 'running', 'open'])

/** 一组示例值，与接口测试页保持一致，方便一键填入常用回环 WebSocket 演示参数。 */
const SAMPLES = [
  {
    label: 'CRM 实时对话',
    url: 'ws://127.0.0.1:8088/api/ws/chat?room=lobby&name=API测试端',
    subprotocols: '',
    scheduled: '你好，我是 API-Agent！',
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

function resolveStreamWorkbench() {
  if (typeof useStreamWorkbenchStore !== 'function') return null
  try {
    return useStreamWorkbenchStore()
  } catch {
    return null
  }
}

const workbenchRuns = resolveWorkbenchRuns()
const streamWorkbench = resolveStreamWorkbench()

/** 连接地址、子协议和待发消息随当前表单编辑。 */
const form = reactive({
  url: 'ws://127.0.0.1:8010/ws',
  subprotocols: '',
  scheduled: '',
})

const urlPrefix = ref('')

const session = ref<StreamSession | null>(null)
const events = ref<StreamEvent[]>([])
const cursor = ref(0)
const connecting = ref(false)
const restoringHistory = ref(false)
const stopping = ref(false)
const cancellingSuite = ref(false)
const outgoing = ref('')
const sendingMessage = ref(false)
const history = ref<(StreamHistoryItem & { finished?: boolean })[]>([])
const activeHistoryId = ref<string | null>(null)
const activeSessionId = ref<string | null>(null)
const activeSuiteId = ref<string | null>(null)
const suiteStatus = ref<StreamSuiteStatus | null>(null)
const subCases = ref<StreamSubCaseSummary[]>([])
const subCasesHistoryId = ref<string | null>(null)
const readonlyReplay = ref<StreamReadonlyReplayMeta | null>(null)
const draftVersion = ref(0)
const filter = ref<'all' | 'in' | 'out'>('all')
const autoScroll = ref(true)
const logRef = ref<HTMLElement | null>(null)
/** 按事件序号记录用户主动折叠的条目；每条收/发结果可独立折叠或展开。 */
const collapsedSequences = ref<number[]>([])

/** 头行编辑与折叠规则与接口调试页共用同一份实现。 */
const {
  rows: headerRows,
  headers: activeHeaders,
  count: headerCount,
} = useHeaderRows()

// 可取消的长轮询与 AI 套件观察流控制器：卸载页面仅停本地读取，绝不终止后台会话/套件。
let pollingSessionId: string | null = null
let suiteObserverAbort: AbortController | null = null
let replayGeneration = 0
let workspaceGeneration = 0
let subCaseLoadGeneration = 0
let unmounted = false

/** 当前是否处于单条 AI 子用例只读回放锁定态。 */
const isReadOnlyReplay = computed(() => readonlyReplay.value !== null)

/** 当前是否正在跟踪一个后台运行的 WebSocket AI 多轮测试套件。 */
const isSuiteRunning = computed(() => Boolean(activeSuiteId.value))

/** 按会话是否存在、未结束且非只读回放/AI 套件判断仍可实时双向收发的手动会话；不以事件数量推断连接状态。 */
const isLive = computed(
  () =>
    !isReadOnlyReplay.value &&
    !isSuiteRunning.value &&
    !!session.value &&
    !session.value.finished &&
    session.value.source !== 'ai',
)

/** 是否应展示「再次测试」入口（只读回放态，或查看已结束历史记录时）。 */
const canRetest = computed(
  () =>
    isReadOnlyReplay.value ||
    Boolean(activeHistoryId.value && !isLive.value && !isSuiteRunning.value),
)

/** 统一消息输入绑定：未连接时编辑连接后自动发送内容，已连接时直接作为实时发送消息实现双向互通。 */
const messageDraft = computed({
  get: () => (isLive.value ? outgoing.value : form.scheduled),
  set: (value: string) => {
    if (isReadOnlyReplay.value) return
    if (isLive.value) {
      outgoing.value = value
    } else {
      form.scheduled = value
    }
  },
})

/** 按收发方向过滤当前事件用于展示；全部模式返回完整列表，不移动服务端游标。 */
const filteredEvents = computed(() =>
  filter.value === 'all' ? events.value : events.value.filter((item) => item.direction === filter.value),
)

/** 根据当前会话及 finished 生成展示色调；运行中 AI 套件为 info，终止错误为 danger，其余结束状态按终态区分。 */
const statusTone = computed(() => {
  if (isSuiteRunning.value) return 'info'
  if (!session.value) return 'neutral'
  if (session.value.finished) {
    if (session.value.status === 'error' || session.value.status === 'failed') return 'danger'
    if (session.value.status === 'passed') return 'success'
    return 'neutral'
  }
  return 'success'
})

/** 将当前会话状态转换为中文标签；无会话显示未连接，未知状态保留原值。 */
const statusLabel = computed(() => {
  if (isSuiteRunning.value) {
    if (suiteStatus.value?.stage === 'cooling_down') return '轮间冷却中'
    return 'AI 测试运行中'
  }
  if (!session.value) return '未连接'
  const labels: Record<string, string> = {
    queued: '排队中',
    connecting: '握手中',
    running: '运行中',
    open: '已连接',
    closed: '已关闭',
    completed: '已完成',
    passed: '通过',
    failed: '断言失败',
    error: '出错',
    cancelled: '已停止',
    interrupted: '已中断',
  }
  return labels[session.value.status] ?? session.value.status
})

/** 当前视图是否展示分组 AI 套件面板（运行中的套件、或已加载子用例/AI 来源历史）。 */
const showSuitePanel = computed(() =>
  Boolean(
    suiteStatus.value ||
    subCases.value.length > 0 ||
    session.value?.source === 'ai' ||
    (session.value?.sub_case_count ?? 0) > 0,
  ),
)

/** 当前 AI 套件的汇总文本（优先实时快照，其次持久化会话摘要）。 */
const suiteSummaryText = computed(
  () => suiteStatus.value?.summary || session.value?.summary || null,
)

/** 新事件到达且 autoScroll 开关开启时，等待 DOM 更新后滚动到底部；不检测用户滚动位置。 */
watch(
  () => events.value.length,
  async () => {
    if (!autoScroll.value) return
    await nextTick()
    const element = logRef.value
    if (element) element.scrollTop = element.scrollHeight
  },
)

/** 把逗号分隔的子协议文本解析成数组，既供握手提交也供折叠标题统计数量。 */
const subprotocolList = computed(() =>
  form.subprotocols
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean),
)

/** 把多行文本框拆成待发消息列表：空行忽略，但保留行内空格。 */
const scheduledMessages = computed(() =>
  form.scheduled.split(/\r?\n/).filter((line) => line.trim().length > 0),
)

/** 透传给 AI 助手小Y 的 WebSocket 工作台设置（含冷却时间等）。 */
const assistantSettings = computed(() => ({ ...webSocketSettings }))

/** 供 AI 助手小Y 读取当前长连接会话、设置与时间线证据的脱敏快照。 */
const assistantContext = computed(() =>
  buildWebSocketTestContext({
    url: joinTargetUrl(urlPrefix.value, form.url) || form.url,
    headers: activeHeaders.value,
    subprotocols: subprotocolList.value,
    session: session.value,
    events: events.value,
    settings: { ...webSocketSettings },
  }),
)

/** 将当前页面状态同步至 Pinia 跨页持久化存储，供路由切回时无损恢复。 */
function syncToStore(): void {
  if (!streamWorkbench) return
  streamWorkbench.saveSnapshot({
    url: form.url,
    subprotocols: form.subprotocols,
    scheduled: form.scheduled,
    urlPrefix: urlPrefix.value,
    headerRows: headerRows.value,
    outgoing: outgoing.value,
    filter: filter.value,
    autoScroll: autoScroll.value,
    activeHistoryId: activeHistoryId.value,
    activeSessionId: activeSessionId.value,
    activeSuiteId: activeSuiteId.value,
    suiteStatus: suiteStatus.value,
    session: session.value,
    events: events.value,
    cursor: cursor.value,
    collapsedSequences: collapsedSequences.value,
    subCases: subCases.value,
    subCasesHistoryId: subCasesHistoryId.value,
    readonlyReplay: readonlyReplay.value,
    draftVersion: draftVersion.value,
  })
}

/** 从 Pinia 跨页存储恢复先前的表单、时间线、AI 套件与只读回放状态。 */
function restoreFromStore(): void {
  if (streamWorkbench?.hydrated) {
    form.url = streamWorkbench.form.url
    form.subprotocols = streamWorkbench.form.subprotocols
    form.scheduled = streamWorkbench.form.scheduled
    urlPrefix.value = streamWorkbench.urlPrefix
    headerRows.value = streamWorkbench.headerRows.length
      ? streamWorkbench.headerRows.map((row) => ({ name: row.name, value: row.value }))
      : [{ name: '', value: '' }]
    outgoing.value = streamWorkbench.outgoing
    filter.value = streamWorkbench.filter
    autoScroll.value = streamWorkbench.autoScroll
    activeHistoryId.value = streamWorkbench.activeHistoryId ?? workbenchRuns?.activeHistoryId.websocket ?? null
    activeSessionId.value = streamWorkbench.activeSessionId
    activeSuiteId.value = streamWorkbench.activeSuiteId
    suiteStatus.value = streamWorkbench.suiteStatus ? { ...streamWorkbench.suiteStatus } : null
    session.value = streamWorkbench.session ? { ...streamWorkbench.session } : null
    events.value = streamWorkbench.events.slice()
    cursor.value = streamWorkbench.cursor
    collapsedSequences.value = streamWorkbench.collapsedSequences.slice()
    subCases.value = streamWorkbench.subCases.slice()
    subCasesHistoryId.value = streamWorkbench.subCasesHistoryId
    readonlyReplay.value = streamWorkbench.readonlyReplay ? { ...streamWorkbench.readonlyReplay } : null
    draftVersion.value = streamWorkbench.draftVersion
    return
  }
  if (workbenchRuns) {
    if (workbenchRuns.activeHistoryId.websocket) {
      activeHistoryId.value = workbenchRuns.activeHistoryId.websocket
    }
    const storedReplay = workbenchRuns.readonlyReplay.websocket
    if (storedReplay) {
      readonlyReplay.value = {
        historyId: storedReplay.historyId,
        caseId: storedReplay.caseId,
        round: storedReplay.round ?? 1,
        label: storedReplay.label || storedReplay.caseId,
        status: 'completed',
      }
    }
  }
}

restoreFromStore()

/** 退出只读回放锁定态并同步清理跨页回放句柄。 */
function exitReadonlyReplay(): void {
  readonlyReplay.value = null
  workbenchRuns?.exitReadonlyReplay('websocket')
}

/** 判断指定序号的事件条目是否处于折叠态。 */
function isEventCollapsed(sequence: number): boolean {
  return collapsedSequences.value.includes(sequence)
}

/** 切换单条收/发事件的展开与折叠状态。 */
function toggleEvent(sequence: number): void {
  if (isEventCollapsed(sequence)) {
    collapsedSequences.value = collapsedSequences.value.filter((item) => item !== sequence)
  } else {
    collapsedSequences.value = [...collapsedSequences.value, sequence]
  }
}

/** 当前筛选结果是否全部处于折叠态，用于切换“全部折叠/全部展开”按钮。 */
const allFilteredCollapsed = computed(
  () => filteredEvents.value.length > 0 && filteredEvents.value.every((item) => isEventCollapsed(item.sequence)),
)

/** 批量折叠或展开当前筛选结果中的所有收发条目。 */
function toggleAllEvents(): void {
  const visible = filteredEvents.value.map((item) => item.sequence)
  if (!visible.length) return
  if (allFilteredCollapsed.value) {
    const visibleSet = new Set(visible)
    collapsedSequences.value = collapsedSequences.value.filter((seq) => !visibleSet.has(seq))
  } else {
    collapsedSequences.value = Array.from(new Set([...collapsedSequences.value, ...visible]))
  }
}

/** Keep the endpoint suffix while changing the base URL. */
function selectUrlPrefix(prefix: string) {
  if (isReadOnlyReplay.value) return
  form.url = prefix ? urlSuffix(form.url, prefix, urlPrefix.value) : joinTargetUrl(urlPrefix.value, form.url) || form.url
  urlPrefix.value = prefix
  syncToStore()
}

/** 将示例连接地址、子协议和自动发送消息填入表单，不自动建立连接。 */
function applySample(sample: (typeof SAMPLES)[number]) {
  if (isReadOnlyReplay.value) return
  urlPrefix.value = ''
  form.url = sample.url
  form.subprotocols = sample.subprotocols
  form.scheduled = sample.scheduled
  headerRows.value = [{ name: '', value: '' }]
  syncToStore()
}

/** 基于 usePolling 封装单飞长轮询：仅负责前端观测，卸载或切换时不结束服务端会话。 */
const streamPolling = usePolling({
  fetch: (signal) => {
    const targetId = pollingSessionId
    if (!targetId) {
      return Promise.reject(new DOMException('No active stream session', 'AbortError'))
    }
    return readStream(targetId, cursor.value, 1200, signal)
  },
  onResult: (page) => {
    const targetId = pollingSessionId
    if (!targetId || !session.value || session.value.id !== targetId) return
    session.value = page.session
    if (page.events.length) {
      events.value.push(...page.events)
      cursor.value = page.next_cursor
    }
    if (page.finished) {
      if (activeSessionId.value === targetId) activeSessionId.value = null
      if (workbenchRuns?.activeRunId.websocket === targetId) {
        workbenchRuns.setActiveRun('websocket', null)
      }
      syncToStore()
      void loadHistory()
    } else {
      syncToStore()
    }
  },
  onError: (error) => {
    stopPolling()
    useToastStore().push('warning', '读取会话失败', describeError(error))
  },
  isTerminal: (page) =>
    Boolean(page.finished || !session.value || session.value.id !== pollingSessionId || session.value.finished),
  intervalMs: 60,
  stopOnUnmount: false,
})

/** 轮询循环：用 wait_ms 长轮询，服务端有事件就立刻返回。
 *  这样界面接近实时，又不必维持第二条浏览器长连接。 */
function startPolling(sessionId: string) {
  if (!session.value || session.value.id !== sessionId || session.value.finished) return
  pollingSessionId = sessionId
  streamPolling.start()
}

/** 中止当前读取并清理下次调度；只停止浏览器观测，不结束后端会话。 */
function stopPolling() {
  pollingSessionId = null
  streamPolling.stop()
}

/** 仅中止本地 AI 套件 NDJSON 观察流，绝不取消后台运行中的多轮测试套件。 */
function stopSuiteObserver() {
  if (suiteObserverAbort) {
    suiteObserverAbort.abort()
    suiteObserverAbort = null
  }
}

/** 读取分组 AI WebSocket 历史下的全部子用例摘要列表。 */
async function loadSubCases(sessionId: string): Promise<void> {
  if (typeof listStreamSubCases !== 'function') return
  const generation = ++subCaseLoadGeneration
  try {
    const response = await listStreamSubCases(sessionId)
    if (unmounted || generation !== subCaseLoadGeneration) return
    if (activeHistoryId.value === sessionId || activeSuiteId.value === sessionId || readonlyReplay.value?.historyId === sessionId) {
      subCases.value = response.items ?? []
      subCasesHistoryId.value = sessionId
      syncToStore()
    }
  } catch {
    // 子用例列表查询失败时不清空已有时间线或状态。
  }
}

/** 将后端子用例状态映射为芯片语义色。 */
function subCaseTone(item: StreamSubCaseSummary): 'success' | 'danger' | 'warning' | 'info' | 'neutral' {
  if (item.error_kind || item.status === 'error' || item.status === 'failed') return 'danger'
  if (item.status === 'cancelled' || item.status === 'interrupted') return 'warning'
  if (item.status === 'closed' || item.status === 'completed' || item.status === 'passed') return 'success'
  if (ACTIVE_STREAM_STATUSES.has(item.status)) return 'info'
  return 'neutral'
}

/** 将后端子用例状态映射为简洁中文标签。 */
function subCaseStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    passed: '通过',
    failed: '失败',
    closed: '已关闭',
    completed: '已完成',
    error: '异常',
    cancelled: '已取消',
    interrupted: '已中断',
    running: '运行中',
    connecting: '握手中',
    open: '已连接',
  }
  return labels[status] ?? status
}

/** 订阅后台多轮 WebSocket AI 测试套件进度流；断线与切页仅中止本地读取，不取消后台套件。 */
function startSuiteObserver(suiteId: string): void {
  if (typeof followStreamSuite !== 'function') return
  stopSuiteObserver()
  const controller = new AbortController()
  suiteObserverAbort = controller
  activeSuiteId.value = suiteId
  workbenchRuns?.setActiveRun('websocket', suiteId)
  syncToStore()

  const applyFrame = (frame: StreamSuiteLiveFrame) => {
    if (controller.signal.aborted || unmounted) return
    if (frame.type === 'heartbeat') return
    if (frame.type === 'progress') {
      const existingLinks = suiteStatus.value?.case_links
      suiteStatus.value = {
        ...frame,
        ...(existingLinks ? { case_links: existingLinks } : {}),
      }
      if (!isReadOnlyReplay.value && activeHistoryId.value === suiteId) {
        session.value = {
          ...(session.value ?? {
            id: suiteId,
            kind: 'websocket',
            url: frame.url,
            status: 'running',
            finished: false,
            stop_reason: null,
            error_kind: null,
            error_message: null,
            http_status: null,
            content_type: null,
            subprotocol: null,
            close_code: null,
            close_reason: null,
            event_count: 0,
            byte_count: 0,
            sent_count: 0,
            truncated_count: 0,
            opened_at: null,
            closed_at: null,
          }),
          id: suiteId,
          url: frame.url,
          status: frame.status as StreamSession['status'],
          finished: !ACTIVE_STREAM_STATUSES.has(frame.status) && frame.stage === 'finished',
          source: frame.source || 'ai',
          title: frame.title,
          badge: frame.badge,
          total_rounds: frame.total_rounds,
          cooldown_ms: frame.cooldown_ms,
          summary: frame.summary,
          sub_case_count: frame.completed_cases,
          error_kind: frame.error_kind,
          error_message: frame.error_message,
        }
      }
      syncToStore()
      return
    }
    if (frame.type === 'cooling_down') {
      if (suiteStatus.value) {
        suiteStatus.value = {
          ...suiteStatus.value,
          stage: 'cooling_down',
          current_round: frame.current_round,
          total_rounds: frame.total_rounds,
          cooldown_ms: frame.cooldown_ms,
          cooldown_remaining_ms: frame.cooldown_remaining_ms,
        }
      }
      syncToStore()
      return
    }
    if (frame.type === 'case_done') {
      if (suiteStatus.value) {
        const existing = suiteStatus.value.case_links ?? []
        const filtered = existing.filter((item) => item.case_id !== frame.case_link.case_id)
        suiteStatus.value = {
          ...suiteStatus.value,
          case_links: [...filtered, frame.case_link],
        }
      }
      void loadSubCases(suiteId)
      syncToStore()
      return
    }
    if (frame.type === 'done') {
      suiteStatus.value = { ...frame }
      if (activeSuiteId.value === suiteId) activeSuiteId.value = null
      if (workbenchRuns?.activeRunId.websocket === suiteId) {
        workbenchRuns.setActiveRun('websocket', null)
      }
      syncToStore()
      void loadHistory()
      void loadSubCases(suiteId)
      if (!isReadOnlyReplay.value && activeHistoryId.value === suiteId) {
        void readStream(suiteId, 0, 0)
          .then((page) => {
            if (!unmounted && activeHistoryId.value === suiteId && !isReadOnlyReplay.value) {
              session.value = page.session
              syncToStore()
            }
          })
          .catch(() => {})
      }
    }
  }

  void followStreamSuite(suiteId, controller.signal, applyFrame)
    .catch((error) => {
      if (controller.signal.aborted || unmounted) return
      if (activeSuiteId.value === suiteId) activeSuiteId.value = null
      if (workbenchRuns?.activeRunId.websocket === suiteId) {
        workbenchRuns.setActiveRun('websocket', null)
      }
      syncToStore()
      useToastStore().push('warning', 'AI 套件进度连接中断', describeError(error))
      void loadHistory()
    })
    .finally(() => {
      if (suiteObserverAbort === controller) {
        suiteObserverAbort = null
      }
    })
}

/** 显式取消正在后台运行的 WebSocket AI 多轮测试套件。 */
async function cancelActiveSuite(): Promise<void> {
  const suiteId = activeSuiteId.value
  if (!suiteId || cancellingSuite.value || typeof cancelStreamSuite !== 'function') return
  const generation = workspaceGeneration
  cancellingSuite.value = true
  try {
    const cancelled = await cancelStreamSuite(suiteId)
    if (generation === workspaceGeneration) {
      stopSuiteObserver()
      suiteStatus.value = cancelled
      activeSuiteId.value = null
      if (workbenchRuns?.activeRunId.websocket === suiteId) {
        workbenchRuns.setActiveRun('websocket', null)
      }
      syncToStore()
    }
    await loadHistory()
    await loadSubCases(suiteId)
    if (generation === workspaceGeneration) {
      useToastStore().push('info', '已停止 AI 测试套件', cancelled.summary ?? undefined)
    }
  } catch (error) {
    if (generation === workspaceGeneration) {
      useToastStore().push('danger', '停止 AI 套件失败', describeError(error))
    }
  } finally {
    if (generation === workspaceGeneration) {
      cancellingSuite.value = false
    }
  }
}

/** 重置事件游标并用当前全局设置启动新会话，成功后开始长轮询并刷新历史。 */
async function start() {
  if (isReadOnlyReplay.value || connecting.value || restoringHistory.value || isLive.value) return
  const url = joinTargetUrl(urlPrefix.value, form.url)
  if (!url) {
    useToastStore().push('warning', urlPrefix.value ? '请填写有效的 URL 后缀' : '请先填写长连接地址')
    return
  }
  stopPolling()
  stopSuiteObserver()
  replayGeneration++
  connecting.value = true
  activeSuiteId.value = null
  suiteStatus.value = null
  subCases.value = []
  subCasesHistoryId.value = null
  events.value = []
  collapsedSequences.value = []
  cursor.value = 0
  const generation = workspaceGeneration
  try {
    const created = await startStream({
      kind: 'websocket',
      url,
      headers: activeHeaders.value,
      subprotocols: subprotocolList.value,
      max_events: webSocketSettings.maxEvents,
      max_duration_ms: webSocketSettings.maxDurationMs,
      connect_timeout_ms: webSocketSettings.connectTimeoutMs,
      ping_interval_ms: webSocketSettings.pingIntervalMs,
      send: scheduledMessages.value,
      send_interval_ms: webSocketSettings.sendIntervalMs,
      project_id: null,
    })
    if (generation === workspaceGeneration) {
      session.value = created
      activeHistoryId.value = created.id
      activeSessionId.value = created.finished ? null : created.id
      workbenchRuns?.setActiveHistory('websocket', created.id)
      workbenchRuns?.setActiveRun('websocket', created.finished ? null : created.id)
      syncToStore()
      if (!created.finished) {
        startPolling(created.id)
      }
    }
    await loadHistory()
  } catch (error) {
    if (generation === workspaceGeneration) useToastStore().push('danger', '长连接未启动', describeError(error))
  } finally {
    if (generation === workspaceGeneration) connecting.value = false
  }
}

/** 请求服务端结束当前会话，成功后停轮询、刷新历史并展示结束原因；失败提示并始终解除 stopping。 */
async function stop() {
  if (!session.value) return
  const id = session.value.id
  const generation = workspaceGeneration
  stopping.value = true
  try {
    const stopped = await stopStream(id)
    if (generation === workspaceGeneration && session.value?.id === id) {
      session.value = stopped
      stopPolling()
      if (activeSessionId.value === id) activeSessionId.value = null
      if (workbenchRuns?.activeRunId.websocket === id) {
        workbenchRuns.setActiveRun('websocket', null)
      }
      syncToStore()
    }
    await loadHistory()
    if (generation !== workspaceGeneration) return
    useToastStore().push('info', '已停止会话', `结束原因：${describeStopReason(stopped.stop_reason)}`)
  } catch (error) {
    if (generation === workspaceGeneration) useToastStore().push('danger', '停止失败', describeError(error))
  } finally {
    if (generation === workspaceGeneration) stopping.value = false
  }
}

/** 将非空输入作为消息发送到当前会话并更新摘要；成功后清空输入，失败保留输入并提示，始终解除发送态。 */
async function sendMessage() {
  if (isReadOnlyReplay.value || !session.value || !outgoing.value.trim()) return
  const id = session.value.id
  const generation = workspaceGeneration
  sendingMessage.value = true
  try {
    const updated = await sendStreamMessage(id, outgoing.value)
    if (generation === workspaceGeneration && session.value?.id === id) {
      session.value = updated
      outgoing.value = ''
      syncToStore()
    }
  } catch (error) {
    if (generation === workspaceGeneration) useToastStore().push('danger', '发送失败', describeError(error))
  } finally {
    if (generation === workspaceGeneration) sendingMessage.value = false
  }
}

/** 回填保存的连接输入参数至表单。 */
function applyReplayInputs(inputs: {
  url: string
  request_headers: Record<string, unknown>
  subprotocols: string[]
  scheduled_messages: string[] | null
}): void {
  urlPrefix.value = ''
  form.url = inputs.url
  form.subprotocols = (inputs.subprotocols ?? []).join(', ')
  form.scheduled = inputs.scheduled_messages?.join('\n') ?? ''
  const rows = Object.entries(inputs.request_headers ?? {}).map(([name, value]) => ({
    name,
    value: String(value ?? ''),
  }))
  headerRows.value = rows.length ? rows : [{ name: '', value: '' }]
}

/** 回放指定历史的事件并更新会话摘要；AI 分组记录自动加载子用例并按运行状态续接套件观察流。 */
async function replay(record: (typeof history.value)[number]) {
  if (connecting.value) return
  stopPolling()
  stopSuiteObserver()
  exitReadonlyReplay()
  const generation = ++replayGeneration
  activeHistoryId.value = record.id
  activeSessionId.value = null
  activeSuiteId.value = null
  suiteStatus.value = null
  subCases.value = []
  subCasesHistoryId.value = null
  events.value = []
  collapsedSequences.value = []
  cursor.value = 0
  restoringHistory.value = true
  workbenchRuns?.setActiveHistory('websocket', record.id)
  const isAiRecord = record.source === 'ai' || (record.sub_case_count ?? 0) > 0
  try {
    const inputs = await readStreamInputs(record.id)
    if (generation !== replayGeneration) return
    applyReplayInputs(inputs)
    let hasMore = true
    let guard = 0
    while (hasMore && guard < 40) {
      const page = await readStream(record.id, cursor.value, 0)
      if (generation !== replayGeneration) return
      session.value = page.session
      if (page.events.length) {
        events.value.push(...page.events)
        cursor.value = page.next_cursor
      }
      hasMore = page.has_more
      guard += 1
      if (!page.events.length) break
    }
    const isAiSuite = isAiRecord || session.value?.source === 'ai' || (session.value?.sub_case_count ?? 0) > 0
    if (isAiSuite) {
      await loadSubCases(record.id)
      if (generation !== replayGeneration) return
      if (session.value && !session.value.finished) {
        startSuiteObserver(record.id)
      }
      syncToStore()
      useToastStore().push('info', '已加载 AI 测试记录', '点击下方子用例或助手中的用例链接可进入只读回放。')
      return
    }
    if (session.value && !session.value.finished) {
      activeSessionId.value = record.id
      workbenchRuns?.setActiveRun('websocket', record.id)
      startPolling(record.id)
    }
    syncToStore()
    useToastStore().push('info', '已回填历史连接', inputs.scheduled_messages === null
      ? '这条旧记录未保存自动发送的消息，可手动补填后重新连接。'
      : '请确认参数后重新连接，已脱敏的凭据需重新填写。')
  } catch (error) {
    if (generation === replayGeneration) useToastStore().push('danger', '回放失败', describeError(error))
  } finally {
    if (generation === replayGeneration) restoringHistory.value = false
  }
}

/** 读取单条 WebSocket AI 子用例完整快照，回填表单与事件时间线并进入只读回放锁定态。 */
async function inspectSubCase(
  historyId: string,
  caseId: string,
  meta?: { round?: number; label?: string; status?: string; tone?: string },
): Promise<void> {
  if (typeof getStreamSubCase !== 'function') return
  stopPolling()
  const generation = ++replayGeneration
  restoringHistory.value = true
  try {
    const detail = await getStreamSubCase(historyId, caseId)
    if (generation !== replayGeneration || unmounted) return
    applyReplayInputs(detail.inputs)
    outgoing.value = ''
    session.value = detail.session
    events.value = detail.events.slice()
    cursor.value = detail.events.length ? detail.events[detail.events.length - 1].sequence : 0
    collapsedSequences.value = []
    activeHistoryId.value = historyId
    activeSessionId.value = null
    const replayMeta: StreamReadonlyReplayMeta = {
      historyId,
      caseId: detail.case_id || caseId,
      round: detail.round_index ?? meta?.round ?? 1,
      caseIndex: detail.case_index,
      label: detail.label || meta?.label || caseId,
      status: detail.session.status || meta?.status || 'closed',
      tone: meta?.tone,
      notes: detail.notes ?? [],
    }
    readonlyReplay.value = replayMeta
    workbenchRuns?.setActiveHistory('websocket', historyId)
    workbenchRuns?.enterReadonlyReplay('websocket', {
      historyId: replayMeta.historyId,
      caseId: replayMeta.caseId,
      round: replayMeta.round,
      label: replayMeta.label,
    })
    syncToStore()
    if (subCasesHistoryId.value !== historyId) {
      void loadSubCases(historyId)
    }
  } catch (error) {
    if (generation === replayGeneration) {
      useToastStore().push('danger', '加载子用例失败', describeError(error))
    }
  } finally {
    if (generation === replayGeneration) {
      restoringHistory.value = false
    }
  }
}

/** 响应 AI 助手中可点击子用例超链接：加载子用例快照并锁定全部请求输入。 */
async function handleSelectCase(block: CaseLinkBlock): Promise<void> {
  if (!block || !block.history_id || !block.case_id) return
  await inspectSubCase(block.history_id, block.case_id, {
    round: block.round,
    label: block.label,
    status: block.status,
    tone: block.tone,
  })
}

/** 响应 AI 助手创建/分叉新 WebSocket 测试历史：刷新历史、绑定选中项、解除过期只读锁并订阅套件进度流。 */
function handleRunBound(payload: AssistantRunBoundPayload): void {
  const historyId = payload.history_id || (payload as unknown as { historyId?: string }).historyId
  if (!historyId) return
  stopPolling()
  stopSuiteObserver()
  exitReadonlyReplay()
  activeSessionId.value = null
  activeHistoryId.value = historyId
  activeSuiteId.value = historyId
  subCases.value = []
  subCasesHistoryId.value = historyId
  events.value = []
  collapsedSequences.value = []
  cursor.value = 0
  suiteStatus.value = {
    id: historyId,
    status: 'running',
    stage: 'executing',
    source: 'ai',
    title: payload.title ?? null,
    badge: payload.badge ?? 'AI 测试',
    url: joinTargetUrl(urlPrefix.value, form.url) || form.url,
    current_round: 1,
    total_rounds: 1,
    completed_cases: 0,
    total_cases: 0,
    cooldown_ms: webSocketSettings.cooldownMs,
    cooldown_remaining_ms: null,
    summary: null,
    error_kind: null,
    error_message: null,
  }
  workbenchRuns?.setActiveHistory('websocket', historyId)
  workbenchRuns?.setActiveRun('websocket', historyId)
  syncToStore()
  void loadHistory()
  startSuiteObserver(historyId)
}

/** 响应 AI 助手执行完成事件：刷新历史列表并在当前选中该套件时重载子用例摘要。 */
async function handleRunCompleted(payload: { scope: string; historyId: string | null }): Promise<void> {
  const targetId = payload.historyId ?? activeHistoryId.value
  await loadHistory()
  if (targetId && (activeHistoryId.value === targetId || subCasesHistoryId.value === targetId)) {
    await loadSubCases(targetId)
    if (!isReadOnlyReplay.value && activeHistoryId.value === targetId) {
      try {
        const page = await readStream(targetId, 0, 0)
        if (!unmounted && activeHistoryId.value === targetId && !isReadOnlyReplay.value) {
          session.value = page.session
          syncToStore()
        }
      } catch {
        // 忽略已完成套件的补充刷新异常。
      }
    }
  }
}

/** Clear local events while retaining the live cursor so future frames can still arrive. */
function clearTimeline() {
  replayGeneration++
  restoringHistory.value = false
  events.value = []
  collapsedSequences.value = []
  if (session.value?.finished) session.value = null
  syncToStore()
}

/** Read the session independently of the visible timeline and its direction filter. */
async function loadResponseDetails(signal: AbortSignal) {
  if (readonlyReplay.value && typeof getStreamSubCase === 'function') {
    const detail = await getStreamSubCase(readonlyReplay.value.historyId, readonlyReplay.value.caseId, signal)
    return {
      request: detail.inputs,
      response: { session: detail.session, events: detail.events },
    }
  }
  const id = session.value?.id
  if (!id) throw new Error('当前没有连接记录。')
  const inputs = await readStreamInputs(id, signal)
  const recordedEvents: StreamEvent[] = []
  let page = await readStream(id, 0, 0, signal)
  recordedEvents.push(...page.events)
  while (page.has_more) {
    if (!page.events.length) throw new Error('连接详情的事件分页未能读完。')
    page = await readStream(id, page.next_cursor, 0, signal)
    recordedEvents.push(...page.events)
  }
  return {
    request: inputs,
    response: { session: page.session, events: recordedEvents },
  }
}

/** 再次测试：解除只读回放锁、清空历史绑定与会话展示、开启全新空白 AI 对话，保留已解锁的表单参数供继续编辑连接。 */
function retest() {
  workspaceGeneration++
  replayGeneration++
  stopPolling()
  stopSuiteObserver()
  exitReadonlyReplay()
  activeHistoryId.value = null
  activeSessionId.value = null
  activeSuiteId.value = null
  suiteStatus.value = null
  subCases.value = []
  subCasesHistoryId.value = null
  session.value = null
  events.value = []
  collapsedSequences.value = []
  cursor.value = 0
  connecting.value = false
  restoringHistory.value = false
  stopping.value = false
  cancellingSuite.value = false
  sendingMessage.value = false
  outgoing.value = ''
  draftVersion.value++
  workbenchRuns?.setActiveHistory('websocket', null)
  syncToStore()
}

/** Reset the editor and detach from the old timeline, which remains in history. */
function newTest() {
  workspaceGeneration++
  stopPolling()
  stopSuiteObserver()
  exitReadonlyReplay()
  clearTimeline()
  activeHistoryId.value = null
  activeSessionId.value = null
  activeSuiteId.value = null
  suiteStatus.value = null
  subCases.value = []
  subCasesHistoryId.value = null
  draftVersion.value++
  session.value = null
  cursor.value = 0
  connecting.value = false
  stopping.value = false
  cancellingSuite.value = false
  sendingMessage.value = false
  outgoing.value = ''
  filter.value = 'all'
  autoScroll.value = true
  urlPrefix.value = ''
  Object.assign(form, { url: '', subprotocols: '', scheduled: '' })
  headerRows.value = [{ name: '', value: '' }]
  workbenchRuns?.setActiveHistory('websocket', null)
  syncToStore()
}

/** 读取最近长连接会话供回放；管理请求失败时回退空列表，不影响当前连接。 */
async function loadHistory() {
  try {
    const page = await listStreams(20)
    history.value = page.items.filter((item) => item.kind === 'websocket')
  } catch {
    history.value = []
  }
}

/**
 * 删除一条会话记录。
 *
 * 作用：删除的是历史回看线索；若该会话正在跑，后端会先收尾再删，
 * 因此本页必须同时把当前连接状态复位，否则界面还显示一个已不存在的连接。
 * 实现：成功后从本地列表移除；若删的正是当前选中项，清空选中并断开显示。
 */
async function removeHistory(record: (typeof history.value)[number]) {
  try {
    await deleteStream(record.id)
    history.value = history.value.filter((item) => item.id !== record.id)
    removeTestChat('websocket', record.id)
    if (
      activeHistoryId.value === record.id ||
      session.value?.id === record.id ||
      activeSuiteId.value === record.id ||
      readonlyReplay.value?.historyId === record.id
    ) {
      stopPolling()
      stopSuiteObserver()
      exitReadonlyReplay()
      activeHistoryId.value = null
      activeSessionId.value = null
      activeSuiteId.value = null
      suiteStatus.value = null
      subCases.value = []
      subCasesHistoryId.value = null
      session.value = null
      events.value = []
      collapsedSequences.value = []
      cursor.value = 0
      workbenchRuns?.setActiveHistory('websocket', null)
      if (workbenchRuns?.activeRunId.websocket === record.id) {
        workbenchRuns.setActiveRun('websocket', null)
      }
      syncToStore()
    }
    useToastStore().push('success', '会话记录已删除')
  } catch (error) {
    useToastStore().push('danger', '删除失败', describeError(error))
  }
}

/** 按共享历史列表提供的 ID 发起删除；记录未加载时忽略。 */
function removeHistoryById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) removeHistory(target)
}

/** 重新挂载到正在后台运行的手动 WebSocket 会话，恢复参数、补齐事件并续接长轮询。 */
async function reattachManualSession(target: ActiveStreamItem, generation: number): Promise<void> {
  const sameSession = session.value?.id === target.id
  activeSessionId.value = target.id
  activeSuiteId.value = null
  suiteStatus.value = null
  activeHistoryId.value = target.id
  workbenchRuns?.setActiveRun('websocket', target.id)
  workbenchRuns?.setActiveHistory('websocket', target.id)

  if (!sameSession) {
    events.value = []
    collapsedSequences.value = []
    cursor.value = 0
    try {
      const inputs = await readStreamInputs(target.id)
      if (unmounted || generation !== workspaceGeneration) return
      applyReplayInputs(inputs)
    } catch {
      // 无法读取保存输入时保留地址并继续重连实时事件流。
      if (!form.url) form.url = target.url
    }
  }

  try {
    let hasMore = true
    let guard = 0
    while (hasMore && guard < 20) {
      const page = await readStream(target.id, cursor.value, 0)
      if (unmounted || generation !== workspaceGeneration) return
      session.value = page.session
      if (page.events.length) {
        events.value.push(...page.events)
        cursor.value = page.next_cursor
      }
      hasMore = page.has_more
      guard += 1
      if (!page.events.length) break
    }
  } catch {
    if (unmounted || generation !== workspaceGeneration) return
  }

  if (session.value && !session.value.finished) {
    syncToStore()
    startPolling(target.id)
  } else {
    activeSessionId.value = null
    if (workbenchRuns?.activeRunId.websocket === target.id) {
      workbenchRuns.setActiveRun('websocket', null)
    }
    syncToStore()
    void loadHistory()
  }
}

/** 重新挂载到正在后台运行的 WebSocket AI 多轮测试套件。 */
function reattachAiSuite(target: ActiveStreamItem): void {
  activeSuiteId.value = target.id
  activeSessionId.value = null
  if (!isReadOnlyReplay.value) {
    activeHistoryId.value = target.id
    workbenchRuns?.setActiveHistory('websocket', target.id)
  }
  workbenchRuns?.setActiveRun('websocket', target.id)
  if (!suiteStatus.value || suiteStatus.value.id !== target.id) {
    suiteStatus.value = {
      id: target.id,
      status: target.status,
      stage: target.stage || 'executing',
      source: target.source || 'ai',
      title: target.title,
      badge: target.badge || 'AI 测试',
      url: target.url,
      current_round: target.current_round,
      total_rounds: target.total_rounds,
      completed_cases: target.completed_cases,
      total_cases: target.total_cases,
      cooldown_ms: target.cooldown_ms,
      cooldown_remaining_ms: target.cooldown_remaining_ms,
      summary: null,
      error_kind: null,
      error_message: null,
    }
  }
  syncToStore()
  void loadSubCases(target.id)
  startSuiteObserver(target.id)
}

/** 切页返回或刷新时探测后台活跃手动会话与 AI 套件，自动无感续接而不中断执行。 */
async function reconnectActiveStreams(): Promise<void> {
  const generation = workspaceGeneration
  if (typeof getActiveStreams !== 'function') {
    if (activeSuiteId.value) {
      startSuiteObserver(activeSuiteId.value)
    } else if (session.value && !session.value.finished && session.value.source !== 'ai') {
      startPolling(session.value.id)
    }
    return
  }

  let activeManuals: ActiveStreamItem[] = []
  let activeSuites: ActiveStreamItem[] = []
  try {
    const response = await getActiveStreams()
    if (unmounted || generation !== workspaceGeneration) return
    const items = response.items ?? []
    activeManuals = response.manual_sessions ?? items.filter((item) => item.kind === 'manual')
    activeSuites = response.ai_suites ?? items.filter((item) => item.kind === 'ai_suite')
  } catch {
    if (unmounted || generation !== workspaceGeneration) return
    if (activeSuiteId.value) {
      startSuiteObserver(activeSuiteId.value)
    } else if (session.value && !session.value.finished && session.value.source !== 'ai') {
      startPolling(session.value.id)
    }
    return
  }

  const preferredId =
    workbenchRuns?.activeRunId.websocket ??
    activeSuiteId.value ??
    activeSessionId.value ??
    activeHistoryId.value

  const matchedManual = preferredId
    ? activeManuals.find((item) => item.id === preferredId) ?? null
    : null
  const matchedSuite = preferredId
    ? activeSuites.find((item) => item.id === preferredId) ?? null
    : null

  if (matchedSuite) {
    reattachAiSuite(matchedSuite)
    return
  }
  if (matchedManual) {
    await reattachManualSession(matchedManual, generation)
    return
  }
  if (activeSuites.length > 0) {
    reattachAiSuite(activeSuites[0])
    return
  }
  if (activeManuals.length > 0) {
    await reattachManualSession(activeManuals[0], generation)
    return
  }

  // 切页离开期间若后台会话或套件已自然结束，同步补拉终态并清理运行句柄。
  const previousSessionId = activeSessionId.value
  const previousSuiteId = activeSuiteId.value
  if (previousSessionId) {
    activeSessionId.value = null
    if (workbenchRuns?.activeRunId.websocket === previousSessionId) {
      workbenchRuns.setActiveRun('websocket', null)
    }
    try {
      const page = await readStream(previousSessionId, cursor.value, 0)
      if (!unmounted && generation === workspaceGeneration && (!session.value || session.value.id === previousSessionId)) {
        session.value = page.session
        if (page.events.length) {
          events.value.push(...page.events)
          cursor.value = page.next_cursor
        }
      }
    } catch {
      // 忽略补拉失败。
    }
    syncToStore()
    void loadHistory()
  }
  if (previousSuiteId) {
    activeSuiteId.value = null
    if (workbenchRuns?.activeRunId.websocket === previousSuiteId) {
      workbenchRuns.setActiveRun('websocket', null)
    }
    syncToStore()
    void loadHistory()
    void loadSubCases(previousSuiteId)
  }
}

/** 按事件方向和种类返回中文标签；发送方向优先，未知事件种类保留原值。 */
function eventLabel(event: StreamEvent): string {
  if (event.direction === 'out') return '发送'
  const labels: Record<string, string> = {
    open: '已连接',
    close: '已关闭',
    comment: '注释',
    message: event.event_name ? `事件 ${event.event_name}` : '消息',
    text: '文本帧',
    binary: '二进制帧',
    error: '错误',
    raw: '原始行',
  }
  return labels[event.kind] ?? event.kind
}

/** 生成事件正文展示：二进制显示截断的 base64 预览，合法 JSON 美化，其余原样保留。 */
function eventBody(event: StreamEvent): string {
  const data = event.data ?? ''
  if (event.kind === 'binary') return `base64: ${data.slice(0, 600)}`
  return looksLikeJson(data) ? prettyJson(data) : data
}

/** 折叠态在标题栏展示单行内容摘要，便于快速识别每条收发消息。 */
function eventPreview(event: StreamEvent): string {
  const raw = (event.data ?? '').replace(/\s+/g, ' ').trim()
  if (!raw) return ''
  const text = event.kind === 'binary' ? `base64: ${raw}` : raw
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

/** 按生命周期与收发方向映射色彩语义，突出区分接收、发送、连接、关闭与错误帧。 */
function eventTone(event: StreamEvent): 'in' | 'out' | 'open' | 'close' | 'error' | 'comment' {
  if (event.kind === 'error') return 'error'
  if (event.kind === 'open') return 'open'
  if (event.kind === 'close') return 'close'
  if (event.kind === 'comment') return 'comment'
  return event.direction === 'out' ? 'out' : 'in'
}

/** 判断历史记录是否仍处于活跃运行态（同时兼容 finished 布尔字段与后端状态字段）。 */
function isStreamHistoryRunning(record: StreamHistoryItem & { finished?: boolean }): boolean {
  if (typeof record.finished === 'boolean') return !record.finished
  return ACTIVE_STREAM_STATUSES.has(record.status)
}

/** 将历史条目的运行状态或结束原因转为中文状态文案。 */
function historyStatusLabel(record: StreamHistoryItem & { finished?: boolean }, running: boolean): string {
  if (running) {
    if (record.status === 'open') return '已连接'
    if (record.status === 'connecting') return '握手中'
    return '运行中'
  }
  if (record.stop_reason) {
    return typeof describeStopReason === 'function' ? describeStopReason(record.stop_reason) : record.stop_reason
  }
  const fallback: Record<string, string> = {
    completed: '已完成',
    closed: '已关闭',
    cancelled: '已停止',
    interrupted: '已中断',
    error: '出错',
  }
  if (record.status && fallback[record.status]) return fallback[record.status]
  return typeof describeStopReason === 'function' ? describeStopReason(record.stop_reason) : '—'
}

/** 把会话记录映射成共享历史列的 WebSocket 条目（含 AI 徽标与后台运行脉冲状态）。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  history.value.map((record) => {
    const running = isStreamHistoryRunning(record)
    const isAi = Boolean(record.badge || record.source === 'ai')
    const badges: HistoryBadge[] | undefined = isAi
      ? [
          { label: record.badge || 'AI 测试', tone: 'info' },
          ...(record.total_rounds && record.total_rounds > 1
            ? [{ label: `${record.total_rounds} 轮`, tone: 'neutral' as const, title: `共 ${record.total_rounds} 轮测试` }]
            : []),
        ]
      : undefined
    const tone = running
      ? 'info'
      : record.status === 'closed' || record.status === 'completed'
        ? 'success'
        : record.status === 'error'
          ? 'danger'
          : record.status === 'cancelled' || record.status === 'interrupted'
            ? 'warning'
            : 'neutral'
    const label = record.title?.trim() ? record.title.trim() : `WS · ${record.url}`
    const metaPrefix =
      record.sub_case_count && record.sub_case_count > 0
        ? `${record.sub_case_count} 用例 · ${record.event_count} 条`
        : `${record.event_count} 条`
    return {
      id: record.id,
      label,
      status: historyStatusLabel(record, running),
      tone,
      meta: `${metaPrefix} · ${formatRelative(record.created_at)}`,
      badges,
      running,
      source: record.source,
      at: record.created_at,
    }
  }),
)

/** 共享历史列只回传 id。 */
function replayById(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) replay(target)
}

onMounted(() => {
  void loadHistory()
  void reconnectActiveStreams()
})

/** 切页卸载时仅保存状态并中止本地轮询/观察流，绝不调用 stopStream 或 cancelStreamSuite 杀掉后台任务。 */
onBeforeUnmount(() => {
  unmounted = true
  syncToStore()
  workspaceGeneration++
  replayGeneration++
  stopPolling()
  stopSuiteObserver()
})
</script>

<template>
  <TestWorkspace history-key="api-agent.layout.long-connection.history" editor-key="api-agent.layout.long-connection.config"
    assistant-key="api-agent.layout.websocket.chat" :editor-width="320" hide-history-on-narrow>
    <!-- 左：最近会话（独立列，可拖动宽度） -->
    <template #history>
      <HistoryList
        title="最近会话"
        :items="historyEntries"
        :active-id="activeHistoryId"
        empty-text="还没有会话记录。建立一次连接后会出现在这里。"
        :on-delete="removeHistoryById"
        :on-new-test="newTest"
        @select="replayById"
      >
      </HistoryList>
    </template>

    <template #editor>
      <aside class="lc-config scroll-y">
      <div class="config-head">
        <h2 class="config-title">WebSocket 调试</h2>
        <p class="config-sub">WebSocket 双向连接调试</p>
      </div>

      <div v-if="isReadOnlyReplay && readonlyReplay" class="readonly-banner" role="status">
        <div class="readonly-banner-top">
          <span class="readonly-badge">
            <Lock :size="12" aria-hidden="true" />
            只读回放
          </span>
          <span class="readonly-round">第 {{ readonlyReplay.round }} 轮</span>
          <span class="readonly-status">{{ subCaseStatusLabel(readonlyReplay.status) }}</span>
        </div>
        <p class="readonly-label" :title="readonlyReplay.label">{{ readonlyReplay.label }}</p>
        <ul v-if="readonlyReplay.notes?.length" class="readonly-notes">
          <li v-for="(note, idx) in readonlyReplay.notes" :key="idx">{{ note }}</li>
        </ul>
        <p class="readonly-hint">正在查看 AI 子用例执行快照，所有连接参数已锁定，不可编辑。</p>
        <button type="button" class="btn btn-accent btn-sm retest-btn" @click="retest">
          <RotateCcw :size="13" aria-hidden="true" />
          再次测试
        </button>
      </div>

      <div class="field">
        <span class="field-label">地址</span>
        <UrlInput v-model:address="form.url" :model-value="urlPrefix" websocket label="长连接地址"
          placeholder="ws://127.0.0.1:9001/ws" :disabled="isReadOnlyReplay" @update:model-value="selectUrlPrefix" @submit="start" />
      </div>

      <div class="samples">
        <span class="samples-label">示例</span>
        <button
          v-for="sample in SAMPLES"
          :key="sample.label"
          type="button"
          class="sample-chip"
          :disabled="isReadOnlyReplay"
          @click="applySample(sample)"
        >
          {{ sample.label }}
        </button>
      </div>

      <div class="field message-composer">
        <div class="message-head">
          <span class="field-label">{{ isLive ? '发送消息（双向实时互通）' : '输入内容（连接后自动发送，每行一条）' }}</span>
          <span class="composer-mode" :class="{ 'composer-mode-live': isLive, 'composer-mode-locked': isReadOnlyReplay }">
            {{ isReadOnlyReplay ? '只读回放 · 已锁定' : isLive ? '已连接 · 实时发送' : '未连接 · 自动发送' }}
          </span>
        </div>
        <textarea
          v-model="messageDraft"
          class="textarea mono"
          rows="3"
          spellcheck="false"
          :disabled="isReadOnlyReplay"
          :readonly="isReadOnlyReplay"
          :placeholder="isLive ? '输入要发送的文本或 JSON，点击「发送」或按 Ctrl+Enter 实时发送…' : '{&quot;type&quot;:&quot;subscribe&quot;}'"
          @keydown.ctrl.enter.prevent="!isReadOnlyReplay && isLive && sendMessage()"
          @keydown.meta.enter.prevent="!isReadOnlyReplay && isLive && sendMessage()"
        />
        <div class="composer-foot">
          <span class="composer-hint">
            {{
              isReadOnlyReplay
                ? '只读回放模式下不可编辑或发送消息，点击「再次测试」解锁参数并开始新测试。'
                : isLive
                  ? 'Ctrl+Enter 快捷发送，实时收发记录见右侧时间线。'
                  : '点击下方「开始连接」建立会话后，可在此直接点击发送实时消息。'
            }}
          </span>
          <button
            type="button"
            class="btn btn-sm composer-send-btn"
            :class="{ 'btn-accent': isLive && !isReadOnlyReplay }"
            :disabled="isReadOnlyReplay || !isLive || sendingMessage || !outgoing.trim()"
            :title="isReadOnlyReplay ? '只读回放模式下不可发送消息' : isLive ? '向当前 WebSocket 会话实时发送消息' : '建立连接后可实时发送消息'"
            @click="sendMessage"
          >
            <Loader2 v-if="sendingMessage" :size="13" class="spinning" />
            <Send v-else :size="13" />
            {{ sendingMessage ? '发送中' : '发送' }}
          </button>
        </div>
      </div>

      <details class="connection-section">
        <summary class="section-summary">
          <span class="section-title">请求头</span>
          <span v-if="headerCount" class="section-badge">{{ headerCount }}</span>
        </summary>
        <div class="section-body">
          <div class="header-list">
            <div v-for="(row, index) in headerRows" :key="index" class="header-row">
              <input v-model="row.name" class="input mono" placeholder="名称" spellcheck="false" :disabled="isReadOnlyReplay" />
              <input v-model="row.value" class="input mono" placeholder="值" spellcheck="false" :disabled="isReadOnlyReplay" />
              <button
                class="btn btn-ghost btn-icon btn-sm"
                title="删除"
                :disabled="isReadOnlyReplay"
                @click="headerRows.splice(index, 1)"
              >
                ×
              </button>
            </div>
          </div>
          <button class="btn btn-sm add-header-btn" :disabled="isReadOnlyReplay" @click="headerRows.push({ name: '', value: '' })">
            + 添加
          </button>
        </div>
      </details>

      <details class="connection-section">
        <summary class="section-summary">
          <span class="section-title">子协议（逗号分隔）</span>
          <span v-if="subprotocolList.length" class="section-badge">{{ subprotocolList.length }}</span>
        </summary>
        <div class="section-body">
          <input v-model="form.subprotocols" class="input mono" placeholder="graphql-ws" :disabled="isReadOnlyReplay" />
        </div>
      </details>

      <details class="connection-settings">
        <summary>设置</summary>
        <p class="hint">连接限制和发送间隔自动保存在当前浏览器，适用于后续新建连接。</p>
        <TestSettingsFields group="websocket" :disabled="isReadOnlyReplay" />
      </details>

      <div class="actions">
        <button
          v-if="isSuiteRunning"
          type="button"
          class="btn btn-danger start-button"
          :disabled="cancellingSuite"
          @click="cancelActiveSuite"
        >
          <Loader2 v-if="cancellingSuite" :size="14" class="spinning" />
          <CircleStop v-else :size="14" />
          {{ cancellingSuite ? '停止中' : '停止 AI 测试' }}
        </button>
        <button
          v-else-if="!isLive"
          type="button"
          class="btn btn-accent start-button"
          :disabled="isReadOnlyReplay || connecting || restoringHistory"
          @click="start"
        >
          <Loader2 v-if="connecting" :size="14" class="spinning" />
          <Play v-else :size="14" />
          {{ connecting ? '连接中' : '开始连接' }}
        </button>
        <button v-else type="button" class="btn btn-danger start-button" :disabled="stopping" @click="stop">
          <CircleStop :size="14" />
          {{ stopping ? '停止中' : '停止会话' }}
        </button>
        <button
          v-if="canRetest"
          type="button"
          class="btn btn-sm retest-action-btn"
          title="解锁参数并开启全新的测试与空白 AI 对话"
          @click="retest"
        >
          <RotateCcw :size="13" aria-hidden="true" />
          再次测试
        </button>
      </div>

    </aside>
        </template>
        <template #results>
    <!-- 右：会话与时间线 -->
    <section class="lc-stage">
      <div class="stage-head">
        <div class="stage-title">
          <span class="stage-dot" :class="`dot-${statusTone}`" />
          <span>{{ statusLabel }}</span>
          <span v-if="session || isSuiteRunning" class="tag">WebSocket</span>
          <span v-if="isReadOnlyReplay && readonlyReplay" class="tag tag-info">
            只读回放 · 第 {{ readonlyReplay.round }} 轮
          </span>
          <span v-else-if="suiteStatus?.badge || session?.badge || session?.source === 'ai'" class="tag tag-info">
            {{ suiteStatus?.badge || session?.badge || 'AI 测试' }}
          </span>
          <span v-if="session?.subprotocol" class="tag tag-info">子协议 {{ session.subprotocol }}</span>
        </div>
        <div class="stage-stats" v-if="session || isSuiteRunning">
          <template v-if="session">
            <span class="stat stat-in"><ArrowDownToLine :size="12" />{{ session.event_count }} 事件</span>
            <span class="stat stat-out"><ArrowUpFromLine :size="12" />{{ session.sent_count }} 发送</span>
            <span class="stat">{{ formatBytes(session.byte_count) }}</span>
            <span v-if="session.truncated_count" class="stat stat-warn">
              {{ session.truncated_count }} 条截断
            </span>
            <span class="stat">{{ describeStopReason(session.stop_reason) }}</span>
            <CopyButton v-if="session" :text="session.url" label="复制地址" />
          </template>
          <button
            v-if="isSuiteRunning"
            type="button"
            class="btn btn-danger btn-sm"
            :disabled="cancellingSuite"
            @click="cancelActiveSuite"
          >
            <CircleStop :size="12" />
            {{ cancellingSuite ? '停止中' : '停止 AI 测试' }}
          </button>
          <button
            v-else-if="canRetest"
            type="button"
            class="btn btn-ghost btn-sm"
            @click="retest"
          >
            <RotateCcw :size="12" />
            再次测试
          </button>
        </div>
      </div>

      <div v-if="showSuitePanel" class="suite-panel">
        <div class="suite-panel-head">
          <span class="suite-panel-title">
            <Sparkles :size="13" aria-hidden="true" />
            {{ suiteStatus?.title || session?.title || 'AI 多轮 WebSocket 测试套件' }}
          </span>
          <div class="suite-panel-meta">
            <span v-if="suiteStatus" class="tag tag-info">
              第 {{ suiteStatus.current_round }} / {{ suiteStatus.total_rounds }} 轮
            </span>
            <span v-if="suiteStatus && suiteStatus.total_cases > 0" class="suite-count">
              已完成 {{ suiteStatus.completed_cases }} / {{ suiteStatus.total_cases }} 用例
            </span>
            <span v-else-if="subCases.length" class="suite-count">
              共 {{ subCases.length }} 条子用例
            </span>
            <span v-if="suiteStatus?.stage === 'cooling_down'" class="tag tag-warning">
              冷却中{{ suiteStatus.cooldown_remaining_ms != null ? ` · ${suiteStatus.cooldown_remaining_ms}ms` : '' }}
            </span>
          </div>
        </div>
        <p v-if="suiteSummaryText" class="suite-summary">{{ suiteSummaryText }}</p>
        <div v-if="subCases.length" class="subcase-list" role="group" aria-label="AI 测试子用例列表">
          <button
            v-for="item in subCases"
            :key="item.id"
            type="button"
            class="subcase-chip"
            :class="[
              `subcase-tone-${subCaseTone(item)}`,
              { 'subcase-chip-active': readonlyReplay?.caseId === item.id },
            ]"
            :aria-current="readonlyReplay?.caseId === item.id ? 'true' : undefined"
            @click="inspectSubCase(item.parent_session_id || subCasesHistoryId || activeHistoryId || '', item.id, { round: item.round_index, label: item.label, status: item.status, tone: subCaseTone(item) })"
          >
            <span class="subcase-round">R{{ item.round_index }}</span>
            <span class="subcase-name">{{ item.label }}</span>
            <span class="subcase-status">{{ subCaseStatusLabel(item.status) }}</span>
          </button>
        </div>
      </div>

      <div v-if="session?.error_kind" class="stage-error">
        <strong>{{ presentResult(session).title }}<template v-if="resultCodeLabel(presentResult(session).resultStatus)"> · {{ resultCodeLabel(presentResult(session).resultStatus) }}</template></strong>
        <span>{{ presentResult(session).message }}</span>
      </div>

      <div class="timeline-toolbar">
        <div class="filter-group">
          <button
            v-for="item in [
              { key: 'all', label: '全部' },
              { key: 'in', label: '接收' },
              { key: 'out', label: '发送' },
            ]"
            :key="item.key"
            class="filter-chip"
            :class="[`filter-chip-${item.key}`, { 'filter-chip-active': filter === item.key }]"
            @click="filter = item.key as typeof filter"
          >
            {{ item.label }}
          </button>
        </div>
        <div class="toolbar-fill" />
        <label class="auto-scroll">
          <input v-model="autoScroll" type="checkbox" class="switch" />
          自动滚动
        </label>
        <button
          v-if="filteredEvents.length"
          type="button"
          class="btn btn-ghost btn-sm"
          :title="allFilteredCollapsed ? '展开当前列表中的所有收发记录' : '折叠当前列表中的所有收发记录'"
          @click="toggleAllEvents"
        >
          <ChevronsUpDown v-if="allFilteredCollapsed" :size="13" />
          <ChevronsDownUp v-else :size="13" />
          {{ allFilteredCollapsed ? '全部展开' : '全部折叠' }}
        </button>
        <CopyButton
          v-if="events.length"
          :text="events.map((event) => `[${event.sequence}] ${event.direction} ${eventLabel(event)}: ${event.data ?? ''}`).join('\n')"
          label="导出时间线"
        />
        <ResponseDetails :key="session?.id" :load="loadResponseDetails" :disabled="!session || connecting || restoringHistory"
          note="包含已记录的握手参数与收发事件。连接进行中时，详情显示打开时已收到的事件。" />
        <button type="button" class="btn btn-ghost btn-sm" title="清除当前时间线显示" :disabled="!events.length && !session?.finished" @click="clearTimeline">清除</button>
      </div>

      <div ref="logRef" class="timeline scroll-y">
        <div v-if="!session" class="timeline-empty">
          <EmptyState
            :icon="Cable"
            title="开始 WebSocket 调试"
            description="记录每一帧、关闭码与协商到的子协议。收尾原因会区分目标主动关闭和本服务按上限停止。"
          />
        </div>
        <div v-else-if="!filteredEvents.length" class="timeline-empty">
          <EmptyState
            :icon="Radio"
            :title="isLive ? '等待目标推送…' : subCases.length ? '请选择上方子用例查看事件流' : '没有符合筛选的事件'"
            :description="isLive ? '连接已建立，目标还没有发来数据。' : subCases.length ? '点击上方子用例标签或右侧 AI 助手中的用例链接即可只读回放完整收发事件。' : '可以切换筛选条件，或检查结束原因。'"
          />
        </div>
        <article
          v-for="event in filteredEvents"
          :key="event.sequence"
          class="event"
          :class="[
            `event-${event.direction}`,
            `event-tone-${eventTone(event)}`,
            {
              'event-lifecycle': ['open', 'close', 'error', 'comment'].includes(event.kind),
              'event-collapsed': isEventCollapsed(event.sequence),
            },
          ]"
        >
          <button
            type="button"
            class="event-head"
            :aria-expanded="!isEventCollapsed(event.sequence)"
            :title="isEventCollapsed(event.sequence) ? '点击展开此条内容' : '点击折叠此条内容'"
            @click="toggleEvent(event.sequence)"
          >
            <span class="event-chevron" aria-hidden="true">
              <ChevronRight v-if="isEventCollapsed(event.sequence)" :size="13" />
              <ChevronDown v-else :size="13" />
            </span>
            <span class="event-seq">#{{ event.sequence }}</span>
            <span class="event-dir" :class="`dir-${event.direction}`">
              <ArrowDownToLine v-if="event.direction === 'in'" :size="11" />
              <ArrowUpFromLine v-else :size="11" />
              {{ event.direction === 'in' ? '收' : '发' }}
            </span>
            <span class="event-kind" :class="`kind-${eventTone(event)}`">{{ eventLabel(event) }}</span>
            <span v-if="isEventCollapsed(event.sequence) && event.data" class="event-preview">{{ eventPreview(event) }}</span>
            <span class="event-fill" />
            <span class="event-size">{{ formatBytes(event.data_bytes) }}</span>
            <span class="event-time">+{{ formatDuration(event.elapsed_ms) }}</span>
          </button>
          <div v-if="!isEventCollapsed(event.sequence) && (event.data || event.truncated)" class="event-content">
            <pre v-if="event.data" class="event-body">{{ eventBody(event) }}</pre>
            <div v-if="event.truncated" class="event-truncated">该条事件已截断（保留前 64KiB）</div>
          </div>
        </article>
      </div>

      <footer v-if="session" class="stage-foot">
        <span>会话 {{ session.id.slice(0, 8) }}</span>
        <span v-if="session.http_status">HTTP {{ session.http_status }}</span>
        <span v-if="session.content_type" class="truncate">{{ session.content_type }}</span>
        <span v-if="session.close_code != null">关闭码 {{ session.close_code }}</span>
        <span v-if="session.opened_at">建立于 {{ formatRelative(session.opened_at) }}</span>
        <span class="foot-fill" />
        <span>{{ cursor }} / {{ session.event_count }} 已加载</span>
      </footer>
      </section>
        </template>
    <template #assistant>
      <TaskChat
        scope="websocket"
        :execution-id="activeHistoryId"
        :draft-version="draftVersion"
        :current-test="assistantContext"
        :settings="assistantSettings"
        :active-case-id="readonlyReplay?.caseId ?? null"
        @run-bound="handleRunBound"
        @run-completed="handleRunCompleted"
        @select-case="handleSelectCase"
      />
    </template>
  </TestWorkspace>
</template>

<style scoped>
/* ---------- 配置栏 ---------- */
.lc-config {
  height: 100%;
  padding: 16px 16px 24px;
  border-right: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  display: flex;
  flex-direction: column;
  gap: 13px;
}

.config-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.config-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.config-sub {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
}

.readonly-banner {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 11px;
  border: 1px solid var(--info-border);
  border-radius: var(--radius-md);
  background: var(--info-soft);
}

.readonly-banner-top {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.readonly-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  border: 1px solid var(--info-border);
  color: var(--info);
  font-size: 11px;
  font-weight: 600;
}

.readonly-round,
.readonly-status {
  font-size: 11px;
  color: var(--text-secondary);
}

.readonly-label {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  word-break: break-word;
}

.readonly-hint {
  margin: 0;
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.45;
}

.readonly-notes {
  margin: 0;
  padding-left: 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  color: var(--danger);
  line-height: 1.45;
}

.retest-btn {
  align-self: flex-start;
}

.samples {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: -5px;
}

.samples-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.sample-chip {
  height: 24px;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  color: var(--text-secondary);
  font-size: 11.5px;
  transition: border-color var(--motion-fast), color var(--motion-fast), background var(--motion-fast);
}

.sample-chip:hover:not(:disabled) {
  border-color: var(--accent-border);
  color: var(--accent);
  background: var(--accent-soft);
}

.sample-chip:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.message-composer {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.message-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.composer-mode {
  display: inline-flex;
  align-items: center;
  height: 20px;
  padding: 0 7px;
  border-radius: 999px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 500;
}

.composer-mode-live {
  border-color: var(--success-border);
  background: var(--success-soft);
  color: var(--success);
}

.composer-mode-locked {
  border-color: var(--info-border);
  background: var(--info-soft);
  color: var(--info);
}

.composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.composer-hint {
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.4;
}

.composer-send-btn {
  flex: none;
  min-width: 72px;
}

.header-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 5px;
}

.header-row {
  display: grid;
  grid-template-columns: 1fr 1.2fr 26px;
  gap: 5px;
}

.header-row .input {
  height: 29px;
}

.connection-section,
.connection-settings {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  padding: 7px 10px;
  transition: border-color var(--motion-fast), background var(--motion-fast);
}

.connection-section:hover,
.connection-settings:hover {
  border-color: var(--border-default);
}

.connection-section[open],
.connection-settings[open] {
  background: var(--bg-surface);
  border-color: var(--border-default);
}

.connection-section summary,
.connection-settings summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  user-select: none;
}

.connection-section[open] > summary,
.connection-settings[open] > summary {
  color: var(--text-primary);
}

.section-summary {
  display: list-item;
}

.section-title {
  margin-left: 2px;
}

.section-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 17px;
  height: 17px;
  margin-left: 6px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  vertical-align: middle;
}

.section-body {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.add-header-btn {
  width: 100%;
}

.connection-settings .field {
  margin-top: 8px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 2px;
}

.start-button {
  flex: 1;
  height: 34px;
}

.retest-action-btn {
  height: 34px;
  white-space: nowrap;
}


/* ---------- 舞台 ---------- */
.lc-stage {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  /* 与其他测试页保持一致的主题表面。 */
  background: var(--bg-surface);
}

.stage-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 16px;
  background: var(--panel-header-gradient);
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
  flex-wrap: wrap;
}

.stage-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 600;
  flex-wrap: wrap;
}

.stage-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.dot-success {
  background: var(--success);
  box-shadow: 0 0 0 3px var(--success-soft);
  animation: pulse 1.8s ease-in-out infinite;
}

.dot-info {
  background: var(--info);
  box-shadow: 0 0 0 3px var(--info-soft);
  animation: pulse 1.8s ease-in-out infinite;
}

.dot-danger {
  background: var(--danger);
}

.dot-neutral {
  background: var(--border-strong);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}

.stage-stats {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text-tertiary);
  flex-wrap: wrap;
}

.stat {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.stat-in {
  color: var(--success);
  font-weight: 600;
}

.stat-out {
  color: var(--accent);
  font-weight: 600;
}

.stat-warn {
  color: var(--warning);
}

.suite-panel {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  flex: none;
}

.suite-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.suite-panel-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.suite-panel-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.suite-count {
  font-size: 11px;
  color: var(--text-secondary);
}

.suite-summary {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.subcase-list {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.subcase-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 25px;
  padding: 0 8px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 11px;
  cursor: pointer;
  transition: border-color var(--motion-fast), background var(--motion-fast);
}

.subcase-chip:hover {
  border-color: var(--accent-border);
  background: var(--bg-hover);
}

.subcase-chip-active {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}

.subcase-round {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}

.subcase-name {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.subcase-status {
  font-size: 10px;
  font-weight: 600;
}

.subcase-tone-success .subcase-status { color: var(--success); }
.subcase-tone-danger .subcase-status { color: var(--danger); }
.subcase-tone-warning .subcase-status { color: var(--warning); }
.subcase-tone-info .subcase-status { color: var(--info); }
.subcase-tone-neutral .subcase-status { color: var(--text-tertiary); }

.stage-error {
  display: flex;
  gap: 8px;
  padding: 8px 16px;
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 11.5px;
  flex: none;
}

.stage-error strong {
  font-family: var(--font-mono);
}

.timeline-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 9px;
  padding: 8px 16px;
  flex: none;
}

.timeline-toolbar > .btn,
.timeline-toolbar > .auto-scroll {
  flex: none;
  white-space: nowrap;
}

.filter-group {
  display: flex;
  gap: 2px;
  padding: 2px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
}

.filter-chip {
  height: 22px;
  padding: 0 9px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11.5px;
}

.filter-chip-active {
  background: var(--control-gradient);
  color: var(--text-primary);
  font-weight: 600;
  box-shadow: var(--shadow-sm);
}

.filter-chip-in.filter-chip-active {
  background: var(--success-soft);
  color: var(--success);
}

.filter-chip-out.filter-chip-active {
  background: var(--accent-soft);
  color: var(--accent);
}

.toolbar-fill {
  flex: 1;
}

.auto-scroll {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--text-secondary);
}

.switch {
  width: 14px;
  height: 14px;
  accent-color: var(--accent);
}

.timeline {
  flex: 1;
  min-height: 0;
  padding: 0 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.timeline-empty {
  display: grid;
  place-items: center;
  flex: 1;
}

.event {
  border: 1px solid var(--border-default);
  border-left: 4px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
  transition: border-color var(--motion-fast), box-shadow var(--motion-fast), background var(--motion-fast);
}

.event-tone-in {
  border-color: var(--success-border);
  border-left-color: var(--success);
  background: color-mix(in srgb, var(--success-soft) 45%, var(--bg-surface));
}

.event-tone-out {
  border-color: var(--accent-border);
  border-left-color: var(--accent);
  background: color-mix(in srgb, var(--accent-soft) 48%, var(--bg-surface));
}

.event-tone-open {
  border-color: var(--info-border);
  border-left-color: var(--info);
  background: color-mix(in srgb, var(--info-soft) 55%, var(--bg-surface));
}

.event-tone-close {
  border-color: var(--warning-border);
  border-left-color: var(--warning);
  background: color-mix(in srgb, var(--warning-soft) 55%, var(--bg-surface));
}

.event-tone-error {
  border-color: var(--danger-border);
  border-left-color: var(--danger);
  background: color-mix(in srgb, var(--danger-soft) 62%, var(--bg-surface));
}

.event-tone-comment {
  border-color: var(--border-default);
  border-left-color: var(--text-tertiary);
  background: var(--bg-subtle);
}

.event-head {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  border: 0;
  padding: 7px 10px;
  background: transparent;
  font-size: 11.5px;
  color: var(--text-tertiary);
  text-align: left;
  cursor: pointer;
  transition: background var(--motion-fast);
}

.event-tone-in .event-head {
  background: color-mix(in srgb, var(--success-soft) 88%, var(--bg-surface));
}

.event-tone-in .event-head:hover {
  background: color-mix(in srgb, var(--success-soft) 100%, var(--bg-hover));
}

.event-tone-out .event-head {
  background: color-mix(in srgb, var(--accent-soft) 90%, var(--bg-surface));
}

.event-tone-out .event-head:hover {
  background: color-mix(in srgb, var(--accent-soft) 100%, var(--bg-hover));
}

.event-tone-open .event-head {
  background: color-mix(in srgb, var(--info-soft) 90%, var(--bg-surface));
}

.event-tone-open .event-head:hover {
  background: color-mix(in srgb, var(--info-soft) 100%, var(--bg-hover));
}

.event-tone-close .event-head {
  background: color-mix(in srgb, var(--warning-soft) 90%, var(--bg-surface));
}

.event-tone-close .event-head:hover {
  background: color-mix(in srgb, var(--warning-soft) 100%, var(--bg-hover));
}

.event-tone-error .event-head {
  background: color-mix(in srgb, var(--danger-soft) 92%, var(--bg-surface));
}

.event-tone-error .event-head:hover {
  background: color-mix(in srgb, var(--danger-soft) 100%, var(--bg-hover));
}

.event-chevron {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
  flex: none;
}

.event-seq {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-secondary);
  flex: none;
}

.event-dir {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 700;
  flex: none;
}

.dir-in {
  background: var(--success-soft);
  border: 1px solid var(--success-border);
  color: var(--success);
}

.dir-out {
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  color: var(--accent);
}

.event-kind {
  font-weight: 600;
  color: var(--text-primary);
  flex: none;
}

.kind-open { color: var(--info); }
.kind-close { color: var(--warning); }
.kind-error { color: var(--danger); }

.event-preview {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
  padding-left: 4px;
}

.event-fill {
  flex: 1;
}

.event-collapsed .event-fill {
  flex: 0 1 auto;
}

.event-size,
.event-time {
  font-family: var(--font-mono);
  font-size: 10.5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--bg-surface) 75%, transparent);
  color: var(--text-secondary);
  flex: none;
}

.event-content {
  border-top: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-surface) 92%, transparent);
}

.event-tone-in .event-content {
  border-top-color: color-mix(in srgb, var(--success-border) 65%, transparent);
}

.event-tone-out .event-content {
  border-top-color: color-mix(in srgb, var(--accent-border) 65%, transparent);
}

.event-tone-open .event-content {
  border-top-color: color-mix(in srgb, var(--info-border) 65%, transparent);
}

.event-tone-close .event-content {
  border-top-color: color-mix(in srgb, var(--warning-border) 65%, transparent);
}

.event-tone-error .event-content {
  border-top-color: color-mix(in srgb, var(--danger-border) 65%, transparent);
}

.event-body {
  margin: 0;
  padding: 8px 11px;
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-primary);
}

.event-truncated {
  padding: 5px 11px;
  background: var(--warning-soft);
  color: var(--warning);
  font-size: 10.5px;
}

.stage-foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px 12px;
  flex: none;
  min-height: 30px;
  padding: 4px 16px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  font-size: 10.5px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.foot-fill {
  flex: 1;
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

</style>
