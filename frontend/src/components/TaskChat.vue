<script setup lang="ts">
/** Independent assistant transcript and bottom composer with server-backed threads, background reconnect, atomic fork, and clickable case links. */
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { ArrowUp, Check, ChevronDown, Square } from 'lucide-vue-next'
import AiMarkdown from './AiMarkdown.vue'
import AssistantAvatar from './AssistantAvatar.vue'
import { COPY } from '@/lib/copy'
import {
  chatTranscriptKey,
  dedupeCaseLinkBlocks,
  normalizeCaseLinkBlock,
  normalizeCaseLinkBlocks,
} from '@/lib/chatHistory'
import type { AnyAssistantTestContext } from '@/lib/assistantContext'
import { ApiError, describeError } from '@/api/client'
import {
  cancelAssistantRun,
  followAssistantRun,
  getActiveAssistantRuns,
  getAssistantThread,
  getLLMConfig,
  getLLMModels,
  migrateAssistantThread,
  selectLLMModel,
  startAssistantRun,
} from '@/api/endpoints'
import {
  chatWithAssistant,
  type AssistantCurrentTestContext,
  type AssistantStatusPhase,
  type Catalog,
  type TaskChatMessage,
} from '@/api/tasks'
import type {
  AssistantRunBoundPayload,
  CaseLinkBlock,
  PageScope,
} from '@/api/types'
import { useWorkbenchRunsStore } from '@/stores/workbenchRuns'

const VALID_PAGE_SCOPES: readonly PageScope[] = ['console', 'websocket', 'tasks', 'model_testing']

function normalizePageScope(raw: string): PageScope {
  return (VALID_PAGE_SCOPES as readonly string[]).includes(raw) ? (raw as PageScope) : 'console'
}

const props = defineProps<{
  taskId?: string
  scope: PageScope | string
  executionId?: string | null
  draftVersion?: number
  currentTest?: AnyAssistantTestContext | AssistantCurrentTestContext | null
  settings?: Record<string, unknown> | null
  baseCatalog?: Catalog | null
  ephemeralApiKey?: string | null
  activeCaseId?: string | null
}>()

const emit = defineEmits<{
  busy: [value: boolean]
  'select-case': [block: CaseLinkBlock]
  'run-bound': [payload: AssistantRunBoundPayload]
  'run-completed': [payload: { scope: PageScope; historyId: string | null }]
}>()

const normalizedScope = computed<PageScope>(() => normalizePageScope(props.scope))

function resolveWorkbenchRuns() {
  if (typeof useWorkbenchRunsStore !== 'function') return null
  try {
    return useWorkbenchRunsStore()
  } catch {
    return null
  }
}

const workbenchRuns = resolveWorkbenchRuns()
const hasServerAssistantApi =
  typeof startAssistantRun === 'function' && typeof followAssistantRun === 'function'

const input = ref('')
const turns = ref<TaskChatMessage[]>([])
const liveBlocks = ref<CaseLinkBlock[]>([])
const partial = ref('')
const error = ref('')
const sending = ref(false)
const cancelling = ref(false)
const currentRunId = ref<string | null>(null)
const statusPhase = ref<AssistantStatusPhase | null>(null)
const dotCount = ref(1)
const BOTTOM_THRESHOLD = 24
const menuId = useId()
const output = ref<HTMLElement | null>(null)
const pinnedToBottom = ref(true)
const modelMenu = ref<HTMLDivElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const loadingModels = ref(false)
const model = ref('')
const customModel = ref('')
const models = ref<string[]>([])
const modelOptions = computed(() => [...new Set([model.value, ...models.value].filter(Boolean))])
const savingModel = ref(false)
const modelDisabled = computed(() => sending.value || savingModel.value)
const avatarState = computed<'idle' | 'thinking' | 'reading'>(() => {
  if (!sending.value || partial.value) return 'idle'
  if (statusPhase.value === 'reading_data' || statusPhase.value === 'testing') return 'reading'
  return 'thinking'
})
const statusLabel = computed(() => {
  switch (statusPhase.value) {
    case 'reading_data':
      return '读取数据中'
    case 'testing':
      return '正在执行 AI 测试'
    case 'cooling_down':
      return '轮间冷却中'
    default:
      return '努力思考中'
  }
})
const statusDots = computed(() => '.'.repeat(dotCount.value))

let activeExecutionId = props.executionId ?? null
let activeDraftVersion = props.draftVersion
let pendingModel: Promise<boolean> | undefined
let modelRevision = 0
let lastScrollTop = 0
let dotTimer: number | undefined
let observerAbort: AbortController | null = null
let threadLoadGeneration = 0
let unmounted = false

function hasMultipleRounds(blocks: CaseLinkBlock[] | undefined): boolean {
  return Boolean(blocks && blocks.some(block => block.round > 1))
}

/** 启动「. → .. → ...」循环动画计时器，仅在等待回复或执行测试/冷却阶段运行。 */
function startDotAnimation() {
  stopDotAnimation()
  dotCount.value = 1
  if (typeof window === 'undefined' || typeof window.setInterval !== 'function') return
  dotTimer = window.setInterval(() => {
    dotCount.value = (dotCount.value % 3) + 1
  }, 360)
}

function stopDotAnimation() {
  if (dotTimer !== undefined) {
    if (typeof window !== 'undefined' && typeof window.clearInterval === 'function') {
      window.clearInterval(dotTimer)
    }
    dotTimer = undefined
  }
  dotCount.value = 1
}

/** 判断对话输出区当前是否显示到最新输出的最后一行（或尚无溢出输出）。 */
function isAtBottom(element: HTMLElement | null = output.value): boolean {
  if (!element) return true
  return element.scrollHeight - element.scrollTop - element.clientHeight <= BOTTOM_THRESHOLD
}

/** 仅在锚定底部时自动跟进到最新输出行，未锚定时保持用户当前阅读位置。 */
function scrollToBottom() {
  if (!pinnedToBottom.value || !output.value) return
  output.value.scrollTop = output.value.scrollHeight
  lastScrollTop = output.value.scrollTop
}

/** 监听手动滚动：向上离开末行时解除锚点，滚回末行时恢复锚点。 */
function onOutputScroll() {
  const element = output.value
  if (!element) return
  const currentTop = element.scrollTop
  if (isAtBottom(element)) {
    pinnedToBottom.value = true
  } else if (currentTop < lastScrollTop) {
    pinnedToBottom.value = false
  }
  lastScrollTop = currentTop
}

/** 从浏览器本地存储读取并校验旧版对话记录（仅用作回退与一次性迁移源）。 */
function readLocalTurns(scope: string, executionId: string | null): TaskChatMessage[] {
  if (!executionId) return []
  try {
    const stored = window.localStorage.getItem(chatTranscriptKey(scope, executionId))
    const parsed: unknown = stored ? JSON.parse(stored) : []
    if (!Array.isArray(parsed)) return []
    const valid: TaskChatMessage[] = []
    for (const item of parsed) {
      if (!item || typeof item !== 'object') continue
      const candidate = item as Record<string, unknown>
      if (candidate.role !== 'user' && candidate.role !== 'assistant') continue
      if (typeof candidate.content !== 'string' || candidate.content.length > 16_000) continue
      const blocks = dedupeCaseLinkBlocks(normalizeCaseLinkBlocks(candidate.blocks))
      valid.push({
        ...(typeof candidate.id === 'string' ? { id: candidate.id } : {}),
        role: candidate.role,
        content: candidate.content,
        ...(blocks.length ? { blocks } : {}),
        ...(typeof candidate.run_id === 'string' ? { run_id: candidate.run_id } : {}),
      })
    }
    return valid.slice(-40)
  } catch {
    return []
  }
}

/** 将服务端线程消息规范化为前端渲染使用的 TaskChatMessage 列表。 */
function normalizeServerMessages(
  messages: Array<{
    id?: string
    role: 'user' | 'assistant'
    content: string
    blocks?: unknown
    run_id?: string | null
  }>,
): TaskChatMessage[] {
  const normalized: TaskChatMessage[] = []
  for (const item of messages) {
    if (item.role !== 'user' && item.role !== 'assistant') continue
    if (typeof item.content !== 'string') continue
    const blocks = dedupeCaseLinkBlocks(normalizeCaseLinkBlocks(item.blocks))
    normalized.push({
      ...(item.id ? { id: item.id } : {}),
      role: item.role,
      content: item.content,
      ...(blocks.length ? { blocks } : {}),
      ...(item.run_id ? { run_id: item.run_id } : {}),
    })
  }
  return normalized.slice(-40)
}

/** Skip drafts, and never recreate a transcript deleted while a reply was in flight. */
function persistConversation(key: string | undefined, messages: TaskChatMessage[], existingOnly = false) {
  if (!key) return
  try {
    if (!existingOnly || window.localStorage.getItem(key) !== null) {
      window.localStorage.setItem(key, JSON.stringify(messages.slice(-40)))
    }
  } catch {
    // Conversation display remains usable when browser storage is unavailable.
  }
}

/** 拉取服务端持久化线程；为空时保留并异步迁移旧版 localStorage 记录，若存在活跃运行则自动续接。 */
async function loadServerThread(executionId: string, localFallback: TaskChatMessage[], generation: number) {
  if (typeof getAssistantThread !== 'function') return
  const scope = normalizedScope.value
  try {
    const thread = await getAssistantThread(scope, executionId)
    if (unmounted || generation !== threadLoadGeneration || activeExecutionId !== executionId) return
    const serverMessages = normalizeServerMessages(thread.messages ?? [])
    if (serverMessages.length > 0) {
      turns.value = serverMessages
    } else if (localFallback.length > 0 && typeof migrateAssistantThread === 'function') {
      void migrateAssistantThread(scope, executionId, {
        messages: localFallback.map(turn => ({
          role: turn.role,
          content: turn.content,
          ...(turn.blocks?.length ? { blocks: turn.blocks } : {}),
        })),
      }).catch(() => {})
    }
    if (thread.active_run_id && currentRunId.value !== thread.active_run_id && !sending.value) {
      void attachToRun(thread.active_run_id, executionId)
    }
  } catch {
    // 服务端暂不可用或旧后端未提供线程路由时，保留本地只读回放。
  }
}

/** Restore only validated user/assistant turns when the selected test changes. */
function restoreConversation(executionId: string | null) {
  threadLoadGeneration++
  const generation = threadLoadGeneration
  const localTurns = readLocalTurns(props.scope, executionId)
  turns.value = localTurns
  liveBlocks.value = []
  partial.value = ''
  error.value = ''
  pinnedToBottom.value = false
  lastScrollTop = output.value?.scrollTop ?? 0
  if (executionId && typeof getAssistantThread === 'function') {
    void loadServerThread(executionId, localTurns, generation)
  }
}

function isApiStatus(failure: unknown, statuses: number[]): boolean {
  if (!failure || typeof failure !== 'object') return false
  if (failure instanceof ApiError) return statuses.includes(failure.status)
  const candidate = failure as { name?: unknown; status?: unknown }
  return candidate.name === 'ApiError' && typeof candidate.status === 'number' && statuses.includes(candidate.status)
}

/** 仅中止当前本地流观察器并复位本组件流状态，绝不取消后台任务或清除跨页运行句柄。 */
function detachLocalObserver() {
  if (observerAbort) {
    observerAbort.abort()
    observerAbort = null
  }
  currentRunId.value = null
  cancelling.value = false
  stopDotAnimation()
  statusPhase.value = null
  partial.value = ''
  liveBlocks.value = []
  if (sending.value) {
    sending.value = false
    emit('busy', false)
  }
}

/** Finish the current reply before changing threads; never copy it to a different history entry. */
function syncConversation() {
  const nextExecutionId = props.executionId ?? null
  const draftChanged = activeDraftVersion !== props.draftVersion
  if (draftChanged && nextExecutionId === null) {
    detachLocalObserver()
    workbenchRuns?.setActiveAssistantRun(normalizedScope.value, null)
    activeExecutionId = null
    activeDraftVersion = props.draftVersion
    input.value = ''
    restoreConversation(null)
    return
  }
  if (activeExecutionId === nextExecutionId && !draftChanged) return
  if (sending.value) {
    // 后台运行模式下允许用户切看其他历史（仅脱离本地观察流，后台继续执行；切回时由 active_run_id 自动重连）。
    if (hasServerAssistantApi && observerAbort) {
      detachLocalObserver()
    } else {
      return
    }
  }
  activeExecutionId = nextExecutionId
  activeDraftVersion = props.draftVersion
  input.value = ''
  restoreConversation(activeExecutionId)
}
watch(() => [props.executionId, props.draftVersion], syncConversation)
restoreConversation(activeExecutionId)

/** Read the effective setting on entry and before showing provider choices. */
async function readModel() {
  const revision = modelRevision
  try {
    const config = await getLLMConfig()
    if (revision === modelRevision && !savingModel.value) model.value = config.model
  } catch {
    // The server still resolves the effective model when sending.
  }
}

/** 跨页切回或刷新后自动重连后台仍在运行的 AI 助手任务，不打断后端执行。 */
async function reconnectActiveRun() {
  if (!hasServerAssistantApi || sending.value || unmounted) return
  const scope = normalizedScope.value
  const storedRunId = workbenchRuns?.activeAssistantRunId[scope] ?? null
  let activeItems: Array<{ run_id: string; history_id: string | null }> = []
  if (typeof getActiveAssistantRuns === 'function') {
    try {
      const response = await getActiveAssistantRuns(scope)
      activeItems = response.items ?? []
    } catch {
      activeItems = []
    }
  }
  if (unmounted || sending.value) return

  if (storedRunId) {
    const matched = activeItems.find(item => item.run_id === storedRunId)
    if (matched) {
      const targetHistory = matched.history_id ?? activeExecutionId
      if (targetHistory && targetHistory !== activeExecutionId) {
        activeExecutionId = targetHistory
        if (typeof getAssistantThread === 'function') {
          await loadServerThread(targetHistory, readLocalTurns(props.scope, targetHistory), ++threadLoadGeneration)
        }
      }
      if (!sending.value && !unmounted) {
        void attachToRun(matched.run_id, targetHistory)
      }
      return
    }
    if (typeof getActiveAssistantRuns === 'function') {
      // 离开期间后台任务已结束：清理运行句柄并刷新持久化线程以展示最终回复。
      workbenchRuns?.setActiveAssistantRun(scope, null)
      if (activeExecutionId && typeof getAssistantThread === 'function') {
        void loadServerThread(
          activeExecutionId,
          readLocalTurns(props.scope, activeExecutionId),
          ++threadLoadGeneration,
        )
      }
    } else {
      void attachToRun(storedRunId, activeExecutionId)
      return
    }
  }

  const candidate = activeExecutionId
    ? activeItems.find(item => item.history_id === activeExecutionId)
    : activeItems[0]
  if (candidate) {
    const targetHistory = candidate.history_id ?? activeExecutionId
    if (targetHistory && targetHistory !== activeExecutionId) {
      activeExecutionId = targetHistory
      if (typeof getAssistantThread === 'function') {
        await loadServerThread(targetHistory, readLocalTurns(props.scope, targetHistory), ++threadLoadGeneration)
      }
    }
    if (!sending.value && !unmounted) {
      void attachToRun(candidate.run_id, targetHistory)
    }
  }
}

onMounted(() => {
  void readModel()
  void reconnectActiveRun()
})

/** 切页卸载时仅中止本地流观察器，绝不取消后台正在执行的 AI 助手任务。 */
onBeforeUnmount(() => {
  unmounted = true
  stopDotAnimation()
  if (observerAbort) {
    observerAbort.abort()
    observerAbort = null
  }
})

/** Custom names remain available even when the provider does not list models. */
async function loadModels() {
  if (modelDisabled.value || loadingModels.value) return
  customModel.value = ''
  loadingModels.value = true
  try {
    const [, list] = await Promise.allSettled([readModel(), getLLMModels()])
    models.value = list.status === 'fulfilled' && list.value.ok
      ? list.value.models.filter(name => name.length > 0 && name.length <= 120) : []
  } finally {
    loadingModels.value = false
  }
}

/** Commit the shared setting before presenting a new model as active. */
async function selectModel(value: string) {
  const name = value.trim()
  if (modelDisabled.value || !name) return
  if (name === model.value) {
    modelMenu.value?.hidePopover()
    trigger.value?.focus()
    return
  }
  modelRevision++
  savingModel.value = true
  error.value = ''
  pendingModel = selectLLMModel(name).then(config => {
    model.value = config.model
    modelMenu.value?.hidePopover()
    return true
  }).catch(failure => {
    error.value = `模型切换失败：${describeError(failure)}`
    return false
  })
  try {
    if (await pendingModel && !sending.value) trigger.value?.focus()
  } finally {
    pendingModel = undefined
    savingModel.value = false
  }
}

/** 订阅后台 AI 助手运行流（支持新建发送与跨页重连），处理状态阶段、原子分叉与子用例回放链接。 */
async function observeAssistantRun(
  runId: string,
  options: {
    scope: PageScope
    initialHistoryId: string | null
    triggeringUserMessage: TaskChatMessage | null
    baseTurns: TaskChatMessage[]
    isReconnect: boolean
  },
) {
  if (observerAbort) observerAbort.abort()
  const controller = new AbortController()
  observerAbort = controller
  currentRunId.value = runId
  cancelling.value = false
  sending.value = true
  statusPhase.value = statusPhase.value ?? 'thinking'
  liveBlocks.value = []
  partial.value = ''
  error.value = ''
  startDotAnimation()
  emit('busy', true)
  workbenchRuns?.setActiveAssistantRun(options.scope, runId)

  let boundHistoryId = options.initialHistoryId
  let baseTurns = options.baseTurns.slice()
  let didFork = false

  try {
    const doneFrame = await followAssistantRun(runId, controller.signal, frame => {
      if (controller.signal.aborted || unmounted) return
      if (frame.type === 'status') {
        statusPhase.value = frame.phase
        if (!partial.value && dotTimer === undefined) startDotAnimation()
        return
      }
      if (frame.type === 'run_bound') {
        const previousId = frame.previous_history_id ?? boundHistoryId
        const forked = Boolean(
          frame.forked || (previousId !== null && previousId !== frame.history_id),
        )
        boundHistoryId = frame.history_id
        activeExecutionId = frame.history_id
        workbenchRuns?.setActiveHistory(frame.scope, frame.history_id)
        if (forked) {
          didFork = true
          baseTurns = []
          turns.value = options.triggeringUserMessage ? [options.triggeringUserMessage] : []
        }
        const boundPayload: AssistantRunBoundPayload = {
          scope: frame.scope,
          history_id: frame.history_id,
          previous_history_id: frame.previous_history_id ?? null,
          badge: frame.badge ?? null,
          title: frame.title ?? null,
          forked: Boolean(frame.forked),
          run_id: frame.run_id ?? runId,
        }
        emit('run-bound', boundPayload)
        return
      }
      if (frame.type === 'case_link') {
        const block = normalizeCaseLinkBlock(frame.block)
        if (block) {
          liveBlocks.value = dedupeCaseLinkBlocks([...liveBlocks.value, block])
        }
        return
      }
      if (frame.type === 'text') {
        if (frame.text) stopDotAnimation()
        partial.value = frame.text ?? ''
      }
    })

    if (controller.signal.aborted || unmounted) return

    const finalHistoryId = doneFrame.history_id ?? boundHistoryId
    if (finalHistoryId && finalHistoryId !== activeExecutionId) {
      activeExecutionId = finalHistoryId
      workbenchRuns?.setActiveHistory(options.scope, finalHistoryId)
    }
    const mergedBlocks = dedupeCaseLinkBlocks([
      ...liveBlocks.value,
      ...normalizeCaseLinkBlocks(doneFrame.blocks),
    ])
    const assistantTurn: TaskChatMessage = {
      role: 'assistant',
      content: doneFrame.text ?? partial.value,
      ...(mergedBlocks.length ? { blocks: mergedBlocks } : {}),
      run_id: runId,
    }

    if (options.triggeringUserMessage) {
      turns.value = [...baseTurns.slice(-38), options.triggeringUserMessage, assistantTurn]
    } else if (finalHistoryId && typeof getAssistantThread === 'function') {
      try {
        const refreshed = await getAssistantThread(options.scope, finalHistoryId)
        if (!unmounted && activeExecutionId === finalHistoryId) {
          const serverMessages = normalizeServerMessages(refreshed.messages ?? [])
          turns.value = serverMessages.length > 0 ? serverMessages : [...turns.value.slice(-39), assistantTurn]
        }
      } catch {
        turns.value = [...turns.value.slice(-39), assistantTurn]
      }
    } else {
      turns.value = [...turns.value.slice(-39), assistantTurn]
    }

    workbenchRuns?.setActiveAssistantRun(options.scope, null)
    emit('run-completed', { scope: options.scope, historyId: finalHistoryId })
  } catch (failure) {
    if (controller.signal.aborted || unmounted) return
    workbenchRuns?.setActiveAssistantRun(options.scope, null)
    if (
      options.isReconnect &&
      isApiStatus(failure, [404]) &&
      boundHistoryId &&
      typeof getAssistantThread === 'function'
    ) {
      void loadServerThread(
        boundHistoryId,
        readLocalTurns(props.scope, boundHistoryId),
        ++threadLoadGeneration,
      )
      return
    }
    error.value = describeError(failure)
  } finally {
    if (observerAbort === controller) {
      observerAbort = null
      currentRunId.value = null
      cancelling.value = false
      stopDotAnimation()
      statusPhase.value = null
      partial.value = ''
      liveBlocks.value = []
      sending.value = false
      emit('busy', false)
      // 原子分叉后若父页面尚未回传新 executionId，保持在已绑定的新历史对话上不回滚旧历史。
      const waitingParentForFork =
        didFork && (props.executionId ?? null) === options.initialHistoryId
      if (!unmounted && !waitingParentForFork) {
        syncConversation()
      }
    }
  }
}

/** 重连到指定后台 AI 助手运行 ID（若已在观察同一运行则幂等跳过）。 */
async function attachToRun(runId: string, historyId: string | null) {
  if (!hasServerAssistantApi || !runId || currentRunId.value === runId) return
  await observeAssistantRun(runId, {
    scope: normalizedScope.value,
    initialHistoryId: historyId,
    triggeringUserMessage: null,
    baseTurns: turns.value.slice(),
    isReconnect: true,
  })
}

/** 显式停止当前正在后台运行的 AI 助手任务，保留已完成子用例与终态摘要。 */
async function cancelRun() {
  const runId = currentRunId.value
  if (!runId || cancelling.value || typeof cancelAssistantRun !== 'function') return
  cancelling.value = true
  try {
    await cancelAssistantRun(runId)
  } catch (failure) {
    error.value = `停止失败：${describeError(failure)}`
    cancelling.value = false
  }
}

/** 兼容旧版 /api/tasks/chat 的流式回退路径（旧后端返回 404/405 或测试环境仅桩旧接口时启用）。 */
async function runLegacyChat(params: {
  executionId: string | null
  taskId: string | undefined
  previous: TaskChatMessage[]
  message: TaskChatMessage
  context: TaskChatMessage[]
}) {
  const key = params.executionId ? chatTranscriptKey(props.scope, params.executionId) : undefined
  const updated = [...params.previous.slice(-39), params.message]
  persistConversation(key, updated)
  try {
    const response = await chatWithAssistant(
      params.taskId,
      params.context,
      text => {
        if (text) stopDotAnimation()
        partial.value = text
      },
      {
        currentTest: props.currentTest ?? null,
        onStatus: status => {
          statusPhase.value = status.phase
          if (!partial.value && dotTimer === undefined) startDotAnimation()
        },
      },
    )
    const completed: TaskChatMessage[] = [
      ...params.previous.slice(-38),
      params.message,
      { role: 'assistant', content: response },
    ]
    persistConversation(key, completed, true)
    turns.value = completed
  } catch (failure) {
    error.value = describeError(failure)
  } finally {
    stopDotAnimation()
    statusPhase.value = null
    partial.value = ''
    liveBlocks.value = []
    sending.value = false
    emit('busy', false)
    syncConversation()
  }
}

/** Prevent duplicate sends without blocking testing or the summary stream. */
async function send() {
  const question = input.value.trim()
  if (!question || sending.value) return
  const hasOutput = turns.value.length > 0 || Boolean(error.value)
  pinnedToBottom.value = !hasOutput || isAtBottom()
  lastScrollTop = output.value?.scrollTop ?? 0
  const executionId = activeExecutionId
  const taskId = props.taskId === executionId ? props.taskId : undefined
  const previous = turns.value.slice()
  const message: TaskChatMessage = { role: 'user', content: question }
  turns.value = [...previous.slice(-39), message]
  input.value = ''

  const context: TaskChatMessage[] = [message]
  let size = question.length
  for (let index = previous.length - 2; index >= 0 && context.length < 13; index -= 2) {
    const pair = previous.slice(index, index + 2).map(turn => ({
      role: turn.role,
      content: turn.content.slice(0, 16000),
    }))
    const length = pair.reduce((sum, turn) => sum + turn.content.length, 0)
    if (size + length > 32000) break
    size += length
    context.unshift(...pair)
  }

  sending.value = true
  statusPhase.value = 'thinking'
  liveBlocks.value = []
  startDotAnimation()
  emit('busy', true)
  modelMenu.value?.hidePopover()
  partial.value = ''
  error.value = ''

  if (pendingModel && !(await pendingModel)) {
    stopDotAnimation()
    statusPhase.value = null
    sending.value = false
    emit('busy', false)
    return
  }

  if (!hasServerAssistantApi) {
    await runLegacyChat({ executionId, taskId, previous, message, context })
    return
  }

  const scope = normalizedScope.value
  try {
    const started = await startAssistantRun({
      scope,
      history_id: executionId,
      task_id: taskId ?? null,
      messages: context.map(item => ({ role: item.role, content: item.content })),
      current_test: (props.currentTest as Record<string, unknown> | null) ?? null,
      settings: props.settings ?? null,
      model: model.value || null,
      base_catalog: props.baseCatalog ?? null,
      ephemeral_api_key: props.ephemeralApiKey ?? null,
    })
    await observeAssistantRun(started.run_id, {
      scope,
      initialHistoryId: started.history_id ?? executionId,
      triggeringUserMessage: message,
      baseTurns: previous,
      isReconnect: false,
    })
  } catch (failure) {
    if (isApiStatus(failure, [404, 405])) {
      await runLegacyChat({ executionId, taskId, previous, message, context })
      return
    }
    error.value = describeError(failure)
    stopDotAnimation()
    statusPhase.value = null
    partial.value = ''
    liveBlocks.value = []
    sending.value = false
    emit('busy', false)
    syncConversation()
  }
}

watch([turns, partial, liveBlocks, sending, statusPhase, error], scrollToBottom, { flush: 'post' })

/** Send on plain Enter while preserving Shift+Enter newlines and IME composition. */
function onInputKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  void send()
}
</script>

<template>
  <section class="task-chat" aria-labelledby="task-ai-heading">
    <header class="ai-heading">
      <h2 id="task-ai-heading">
        <AssistantAvatar :size="30" :state="avatarState" />
        <span>{{ COPY.assistant.heading }}</span>
      </h2>
    </header>
    <div ref="output" class="ai-output scroll-y" role="region" aria-label="AI 对话输出" tabindex="0" @scroll="onOutputScroll">
      <div class="chat-transcript">
        <article v-for="(turn, index) in turns" :key="turn.id || index" class="chat-turn">
          <strong>{{ turn.role === 'user' ? '你' : COPY.assistant.replyName }}</strong>
          <AiMarkdown v-if="turn.role === 'assistant'" :text="turn.content" />
          <p v-else>{{ turn.content }}</p>
          <div v-if="turn.role === 'assistant' && turn.blocks?.length" class="case-link-list" role="list" aria-label="子用例回放链接">
            <button
              v-for="block in turn.blocks"
              :key="`${block.scope}:${block.history_id}:${block.round}:${block.case_id}`"
              type="button"
              class="case-link-chip"
              :data-tone="block.tone"
              :data-case-id="block.case_id"
              :aria-current="activeCaseId === block.case_id ? 'true' : undefined"
              :title="`查看用例回放：${block.label}`"
              @click="emit('select-case', block)"
            >
              <span v-if="block.round > 1 || hasMultipleRounds(turn.blocks)" class="case-link-round">R{{ block.round }}</span>
              <span class="case-link-label truncate">{{ block.label }}</span>
              <span class="case-link-status">{{ block.status }}</span>
            </button>
          </div>
        </article>
        <article v-if="sending" class="chat-turn" role="status">
          <strong>{{ COPY.assistant.replyName }}</strong>
          <AiMarkdown v-if="partial" :text="partial" />
          <p v-else class="chat-status-line" :data-phase="statusPhase || 'thinking'">
            <span class="chat-status-dot" aria-hidden="true" />
            <span class="chat-status-label">{{ statusLabel }}</span><span class="chat-status-dots" aria-hidden="true">{{ statusDots }}</span>
          </p>
          <div v-if="liveBlocks.length" class="case-link-list" role="list" aria-label="实时子用例回放链接">
            <button
              v-for="block in liveBlocks"
              :key="`${block.scope}:${block.history_id}:${block.round}:${block.case_id}`"
              type="button"
              class="case-link-chip"
              :data-tone="block.tone"
              :data-case-id="block.case_id"
              :aria-current="activeCaseId === block.case_id ? 'true' : undefined"
              :title="`查看用例回放：${block.label}`"
              @click="emit('select-case', block)"
            >
              <span v-if="block.round > 1 || hasMultipleRounds(liveBlocks)" class="case-link-round">R{{ block.round }}</span>
              <span class="case-link-label truncate">{{ block.label }}</span>
              <span class="case-link-status">{{ block.status }}</span>
            </button>
          </div>
        </article>
        <p v-if="error" class="chat-error" role="alert">{{ error }}</p>
      </div>
    </div>
    <form class="composer" aria-label="AI 对话" @submit.prevent="send">
      <textarea v-model="input" class="textarea" rows="1" maxlength="4000" :placeholder="COPY.assistant.inputPlaceholder" :aria-label="COPY.assistant.inputPlaceholder" @keydown="onInputKeydown" />
      <div class="chat-actions">
        <button ref="trigger" class="btn btn-ghost btn-sm model-choice" type="button" :popovertarget="menuId"
          :aria-label="`对话模型：${model || '默认模型'}`" :disabled="modelDisabled" @click="loadModels">
          <span class="truncate">{{ model || '默认模型' }}</span><ChevronDown :size="13" aria-hidden="true" />
        </button>
        <div :id="menuId" ref="modelMenu" class="model-menu" popover="auto" role="group" aria-label="切换对话模型">
          <button v-for="item in modelOptions" :key="item" class="model-option" type="button" :aria-pressed="model === item"
            :disabled="modelDisabled" :title="item" @click="selectModel(item)">
            <span class="truncate">{{ item }}</span><Check v-if="model === item" :size="14" aria-hidden="true" />
          </button>
          <p v-if="loadingModels" class="hint model-loading" role="status">正在加载模型…</p>
          <div class="custom-model">
            <input v-model.trim="customModel" class="input" maxlength="120" aria-label="模型名称" placeholder="模型名称"
              :disabled="modelDisabled" @keydown.enter.prevent="selectModel(customModel)" />
            <button class="btn btn-sm btn-icon" type="button" aria-label="应用模型" title="应用模型"
              :disabled="modelDisabled || !customModel" @click="selectModel(customModel)"><Check :size="14" aria-hidden="true" /></button>
          </div>
        </div>
        <button
          v-if="sending && currentRunId"
          class="btn btn-ghost btn-icon chat-cancel"
          type="button"
          aria-label="停止"
          title="停止当前 AI 运行"
          :disabled="cancelling"
          @click="cancelRun"
        ><Square :size="13" aria-hidden="true" /></button>
        <button
          v-else
          class="btn btn-primary btn-icon chat-send"
          type="submit"
          aria-label="发送"
          title="发送"
          :disabled="sending || !input.trim()"
        ><ArrowUp :size="17" aria-hidden="true" /></button>
      </div>
    </form>
  </section>
</template>

<style scoped>
.task-chat {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  height: 100%;
  min-width: 0;
  padding: 9px 18px 10px;
  background: var(--bg-surface);
  container-type: size;
}
.ai-heading { display: flex; align-items: center; gap: 8px; min-height: 32px; }
.ai-heading h2 { display: inline-flex; align-items: center; gap: 8px; font-size: 15px; margin: 0; }
.ai-output { min-width: 0; padding: 6px clamp(12px, 5cqw, 48px) 10px; overscroll-behavior: contain; scrollbar-gutter: stable both-edges; }
.ai-output:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }
.chat-transcript { width: min(100%, 850px); margin-inline: auto; }
.chat-turn { padding: 6px 0; border-bottom: 1px solid var(--border-subtle); }
.chat-turn > p { margin: 3px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.case-link-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.case-link-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  min-height: 26px;
  padding: 3px 9px;
  border: 1px solid var(--border-default);
  border-radius: 999px;
  background: var(--bg-subtle);
  color: var(--text-primary);
  font-size: 12px;
  line-height: 1.3;
  transition: border-color var(--motion-fast, 140ms) ease, background var(--motion-fast, 140ms) ease;
}
.case-link-chip:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
}
.case-link-chip[aria-current='true'] {
  border-color: var(--accent);
  background: var(--accent-soft);
  box-shadow: 0 0 0 1px var(--accent-border);
}
.case-link-chip[data-tone='success'] {
  border-color: var(--success-border);
  background: var(--success-soft);
}
.case-link-chip[data-tone='danger'] {
  border-color: var(--danger-border);
  background: var(--danger-soft);
}
.case-link-chip[data-tone='warning'] {
  border-color: var(--warning-border);
  background: var(--warning-soft);
}
.case-link-chip[data-tone='info'] {
  border-color: var(--info-border);
  background: var(--info-soft);
}
.case-link-round {
  flex: none;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-family: var(--font-mono);
  font-size: 10.5px;
}
.case-link-label {
  min-width: 0;
  max-width: 260px;
}
.case-link-status {
  flex: none;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
}
.case-link-chip[data-tone='success'] .case-link-status { color: var(--success); }
.case-link-chip[data-tone='danger'] .case-link-status { color: var(--danger); }
.case-link-chip[data-tone='warning'] .case-link-status { color: var(--warning); }
.case-link-chip[data-tone='info'] .case-link-status { color: var(--info); }
.chat-status-line {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 12.5px;
}
.chat-status-line[data-phase='reading_data'],
.chat-status-line[data-phase='testing'] {
  color: var(--assistant-color, var(--accent));
}
.chat-status-line[data-phase='cooling_down'] {
  color: var(--warning);
}
.chat-status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: status-pulse 1.1s ease-in-out infinite;
}
.chat-status-dots {
  display: inline-block;
  min-width: 1.5em;
  text-align: left;
  font-family: var(--font-mono);
}
@keyframes status-pulse {
  0%, 100% { transform: scale(0.8); opacity: 0.45; }
  50% { transform: scale(1.2); opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .chat-status-dot {
    animation-duration: 1.1s !important;
    animation-iteration-count: infinite !important;
  }
}
.chat-error { color: var(--danger); }
.composer {
  justify-self: center;
  width: min(100%, 850px);
  margin-top: 6px;
  padding: 4px 7px 6px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-xl);
  background: var(--bg-subtle);
}
.composer:focus-within { border-color: var(--accent-border); }
.composer textarea {
  display: block;
  min-height: 28px;
  max-height: clamp(28px, calc(100cqh - 95px), 92px);
  field-sizing: content;
  overflow-y: auto;
  padding: 4px;
  border: 0;
  background: transparent;
  resize: none;
}
.composer textarea:focus { box-shadow: none; }
.chat-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; min-width: 0; }
.model-choice { min-width: 0; max-width: min(240px, calc(100% - 34px)); }
.model-choice svg, .model-option svg { flex: none; }
.chat-send,
.chat-cancel { flex: none; width: 28px; height: 28px; border-radius: 50%; }
.chat-cancel {
  border: 1px solid var(--danger-border);
  background: var(--danger-soft);
  color: var(--danger);
}
.chat-cancel:hover:not(:disabled) {
  border-color: var(--danger);
}
.model-menu {
  position: fixed;
  inset: auto;
  position-area: top span-left;
  position-try-fallbacks: flip-inline;
  justify-self: end;
  align-self: end;
  width: min(260px, calc(100vw - 24px));
  max-height: min(260px, 50dvh);
  margin: 0 0 6px;
  padding: 4px;
  overflow: auto;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-md);
}
.model-option { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; min-height: 30px; padding: 4px 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: inherit; text-align: left; }
.model-option:hover:not(:disabled) { background: var(--bg-hover); }
.model-option[aria-pressed='true'] { color: var(--accent); }
.custom-model { display: flex; align-items: center; gap: 4px; padding: 4px; }
.custom-model .input { min-width: 0; }
.custom-model .btn { flex: none; }
.model-loading { margin: 4px 8px; }
</style>
