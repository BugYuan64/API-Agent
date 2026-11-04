<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import {
  CircleCheck,
  CircleX,
  Clock,
  FileText,
  Play,
  RotateCw,
  Square,
  TriangleAlert,
  Wand2,
} from 'lucide-vue-next'
import { describeError } from '@/api/client'
import { cancelAgentRun, getAgentReport, getAgentRun, startAgentRun } from '@/api/endpoints'
import type {
  AgentEvidence,
  AgentPlan,
  AgentRequestCase,
  AgentRound,
  AgentRun,
  AgentRunInput,
} from '@/api/types'
import type { HistoryEntry } from '@/components/HistoryList.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import { formatDateTime, formatRelative } from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

/** AI 编排页：左侧提交契约与授权范围，右侧按 events 时间线实时展示闭环过程。
 *
 *  设计约束（见「开发实施契约」第 19 节）：
 *  - 授权范围（尤其 origin）必须人工确认，界面只从契约预填可勾选的 path×method；
 *  - evidence 是代码算出的观测事实，hypotheses 是模型假设，两者视觉上必须分开；
 *  - 轮询间隔 1 秒，按 events[].sequence 增量追加，不整表重绘。 */

const POLL_INTERVAL = 1000
const TERMINAL = new Set(['completed', 'error', 'timed_out', 'cancelled', 'interrupted'])

const toast = useToastStore()

/** 表单状态 */
const form = reactive({
  contractText: '',
  origin: '',
  methods: new Set<string>(['GET', 'POST']),
  selectedPaths: new Set<string>(),
  manualPaths: '',
  discovery: [] as { path: string; method: string }[],
  maxCases: 8,
  targetRequests: 16,
  modelCalls: 8,
  maxRounds: 3,
  wallTimeSeconds: 120,
  allowMutating: false,
})

const ALL_METHODS = ['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE'] as const

const submitting = ref(false)
const cancelling = ref(false)
const run = ref<AgentRun | null>(null)

/** 本次会话内发起过的运行。后端未提供「列出全部运行」接口，
 *  因此这里只保留本页启动过的记录，用于左栏快速切回。 */
interface RunHistoryItem { id: string; label: string; status: string; at: string }
const runHistory = ref<RunHistoryItem[]>([])

/** 把本次会话的运行记录映射成共享历史列的条目形状。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  runHistory.value.map((item) => ({
    id: item.id,
    label: item.label,
    status: item.status,
    tone: statusTone(item.status),
    meta: formatRelative(item.at),
    at: item.at,
    source: 'auto',
  })),
)

function rememberRun(item: AgentRun): void {
  const label = `${item.status === 'completed' ? '完成' : '运行'} · ${item.id.slice(0, 8)}`
  const existing = runHistory.value.findIndex((h) => h.id === item.id)
  const entry: RunHistoryItem = {
    id: item.id,
    label,
    status: item.status,
    at: (item as { created_at?: string }).created_at ?? new Date().toISOString(),
  }
  if (existing >= 0) runHistory.value.splice(existing, 1, entry)
  else runHistory.value.unshift(entry)
}

/** 供外壳的历史列点击时调用（按 id 切回一条运行）。 */
async function openHistory(runId: string): Promise<void> {
  if (run.value?.id === runId) return
  try {
    const latest = await getAgentRun(runId)
    if (disposed) return
    run.value = latest
    expandedRounds.value = new Set([0])
    report.value = null
    rememberRun(latest)
    if (!TERMINAL.has(latest.status)) startPolling(runId)
    else void loadReport()
  } catch (error) {
    toast.push('danger', '读取运行失败', describeError(error))
  }
}
const report = ref<string | null>(null)
const reportLoading = ref(false)
const expandedRounds = ref<Set<number>>(new Set())

let timer: number | null = null
let pollGeneration = 0
let pollInFlight = false
let reportGeneration = 0
let disposed = false

watch(() => run.value?.id, () => {
  reportGeneration += 1
  report.value = null
  reportLoading.value = false
}, { flush: 'sync' })

/** 从粘贴的 OpenAPI/Swagger 文档中提取 path×method，供用户勾选确认。
 *  只做展示与预填，绝不据此决定 origin——授权范围始终由操作者确认。 */
function discover() {
  const text = form.contractText.trim()
  if (!text) {
    form.discovery = []
    return
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    form.discovery = []
    toast.push('info', '未能解析为 JSON', '契约不是合法 JSON，模型仍可把它当描述文本理解，但无法预填接口列表。')
    return
  }
  const paths = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
    ? (parsed as { paths?: Record<string, unknown> }).paths : null
  if (!paths || typeof paths !== 'object' || Array.isArray(paths)) {
    form.discovery = []
    toast.push('info', '未发现 paths', '文档中没有 OpenAPI 的 paths 字段，将按描述文本处理。')
    return
  }
  const found: { path: string; method: string }[] = []
  for (const [path, item] of Object.entries(paths)) {
    if (!item || typeof item !== 'object') continue
    for (const method of Object.keys(item as Record<string, unknown>)) {
      const upper = method.toUpperCase()
      if ((ALL_METHODS as readonly string[]).includes(upper)) {
        found.push({ path, method: upper })
      }
    }
  }
  found.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method))
  form.discovery = found
  form.selectedPaths = new Set(found.map((f) => f.path))
  if (found.length) {
    toast.push('success', `识别到 ${found.length} 个接口`, '请确认勾选的路径与方法，并填写目标 origin。')
  }
}

const selectedMethodList = computed(() => [...form.methods])
const selectedPathList = computed(() => [...new Set([
  ...form.selectedPaths,
  ...form.manualPaths.split(/\r?\n/).map(path => path.trim()).filter(Boolean),
])])

function toggleMethod(method: string) {
  const next = new Set(form.methods)
  if (next.has(method)) next.delete(method)
  else next.add(method)
  form.methods = next
}

function togglePath(path: string) {
  const next = new Set(form.selectedPaths)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  form.selectedPaths = next
}

const canSubmit = computed(
  () =>
    !!form.contractText.trim() &&
    !!form.origin.trim() &&
    form.methods.size > 0 &&
    selectedPathList.value.length > 0 &&
    !submitting.value &&
    !isRunning.value,
)

const isRunning = computed(
  () => !!run.value && !TERMINAL.has(run.value.status),
)

function buildPayload(): AgentRunInput {
  const text = form.contractText.trim()
  let contract: string | Record<string, unknown> = text
  try {
    const parsedContract: unknown = JSON.parse(text)
    if (parsedContract && typeof parsedContract === 'object' && !Array.isArray(parsedContract)) {
      contract = parsedContract as Record<string, unknown>
    }
  } catch {
    // 保持字符串：模型可按描述文本理解
  }
  return {
    contract,
    scope: {
      origin: form.origin.trim(),
      allowed_methods: selectedMethodList.value,
      path_templates: selectedPathList.value,
    },
    max_cases: form.maxCases,
    limits: {
      target_requests: form.targetRequests,
      model_calls: form.modelCalls,
      max_rounds: form.maxRounds,
      wall_time_seconds: form.wallTimeSeconds,
    },
    allow_mutating_followups: form.allowMutating,
  }
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  report.value = null
  try {
    const started = await startAgentRun(buildPayload())
    if (disposed) return
    run.value = started
    rememberRun(started)
    expandedRounds.value = new Set([0])
    startPolling(started.id)
  } catch (error) {
    toast.push('danger', '启动失败', describeError(error))
  } finally {
    submitting.value = false
  }
}

function startPolling(runId: string) {
  stopPolling()
  if (disposed || run.value?.id !== runId || TERMINAL.has(run.value.status)) return
  const generation = pollGeneration
  const current = () => !disposed && generation === pollGeneration && run.value?.id === runId
  function schedule() {
    timer = window.setTimeout(async () => {
      timer = null
      if (!current()) return
      if (pollInFlight) {
        schedule()
        return
      }
      pollInFlight = true
      try {
        const latest = await getAgentRun(runId)
        if (!current()) return
        // A terminal state already observed for this run is never overwritten.
        if (TERMINAL.has(run.value!.status)) {
          stopPolling()
          return
        }
        if (latest.id !== runId) {
          stopPolling()
          return
        }
        run.value = latest
        if (TERMINAL.has(latest.status)) {
          stopPolling()
          for (const round of latest.rounds) expandedRounds.value.add(round.index)
          expandedRounds.value = new Set(expandedRounds.value)
        } else if (current()) schedule()
      } catch (error) {
        if (!current()) return
        stopPolling()
        toast.push('danger', '轮询失败', describeError(error))
      } finally {
        pollInFlight = false
      }
    }, POLL_INTERVAL)
  }
  schedule()
}

function stopPolling() {
  pollGeneration += 1
  if (timer !== null) {
    window.clearTimeout(timer)
    timer = null
  }
}

async function cancel() {
  if (!run.value || cancelling.value) return
  const runId = run.value.id
  stopPolling()
  const generation = pollGeneration
  const current = () => !disposed && generation === pollGeneration && run.value?.id === runId
  cancelling.value = true
  try {
    const updated = await cancelAgentRun(runId)
    if (current() && !TERMINAL.has(run.value!.status)) {
      run.value = updated
      if (!TERMINAL.has(updated.status)) startPolling(runId)
    }
  } catch (error) {
    if (current()) {
      toast.push('danger', '取消失败', describeError(error))
      startPolling(runId)
    }
  } finally {
    cancelling.value = false
  }
}

async function loadReport() {
  if (!run.value || reportLoading.value) return
  const runId = run.value.id
  const generation = ++reportGeneration
  const current = () => !disposed && generation === reportGeneration && run.value?.id === runId
  reportLoading.value = true
  try {
    const text = await getAgentReport(runId)
    if (!current()) return
    report.value = text
    if (!report.value.trim()) {
      toast.push('info', '报告为空', '本次运行可能还没有可渲染的内容。')
    }
  } catch (error) {
    if (current()) toast.push('danger', '获取报告失败', describeError(error))
  } finally {
    if (current()) reportLoading.value = false
  }
}

function toggleRound(index: number) {
  const next = new Set(expandedRounds.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  expandedRounds.value = next
}

onBeforeUnmount(() => {
  disposed = true
  reportGeneration += 1
  stopPolling()
})

/** 状态标签与色调：如实呈现后端状态，不压缩成“成功/失败”。 */
function statusTone(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'completed' || status === 'accepted' || status === 'received') return 'success'
  if (status === 'running' || status === 'dispatching' || status === 'queued') return 'info'
  if (status === 'failed' || status === 'error' || status === 'rejected') return 'danger'
  if (status === 'timed_out' || status === 'cancelled' || status === 'interrupted') return 'warning'
  return 'info'
}

function percent(line: { limit: number; consumed: number; reserved: number }): number {
  if (!line.limit) return 0
  return Math.min(100, Math.round(((line.consumed + line.reserved) / line.limit) * 100))
}

function caseTitle(c: AgentRequestCase): string {
  const query = c.query && Object.keys(c.query).length
    ? '?' + new URLSearchParams(c.query).toString()
    : ''
  return `${c.method} ${c.path}${query}`
}

function passedCount(round: AgentRound): { passed: number; failed: number; unknown: number } {
  let passed = 0
  let failed = 0
  let unknown = 0
  for (const e of round.evidence) {
    if (e.assertion_index === null && e.error_kind === null) continue
    if (e.passed === true) passed += 1
    else if (e.passed === false) failed += 1
    else unknown += 1
  }
  return { passed, failed, unknown }
}

function intentOf(round: AgentRound, caseId: string) {
  return round.intents.find((i) => i.case_id === caseId) ?? null
}

/** 模板表达式不支持 TS 非空断言，这里提供两个取值辅助。 */
function basisOf(round: AgentRound, caseId: string): 'contract' | 'inferred' | null {
  return intentOf(round, caseId)?.basis ?? null
}

function rationaleOf(round: AgentRound, caseId: string): string {
  return intentOf(round, caseId)?.rationale ?? ''
}

function evidenceFor(round: AgentRound, caseId: string): AgentEvidence[] {
  return round.evidence.filter((e) => e.case_id === caseId && e.assertion_index !== null)
}

function planOf(round: AgentRound): AgentPlan | null {
  return round.plan ?? null
}

/* ---------- 对外接口：历史条目由外壳统一渲染 ---------- */
const emit = defineEmits<{
  (event: 'history', entries: HistoryEntry[]): void
  (event: 'active', id: string | null): void
}>()

defineExpose({ openHistory })

watch(historyEntries, (entries) => emit('history', entries), { immediate: true })
watch(run, (value) => emit('active', value?.id ?? null), { immediate: true })

/** 输入栏宽度：左右分栏的受控尺寸，持久化到本页专属 key。 */
const inputWidth = ref(460)
try {
  const saved = Number(window.localStorage.getItem('api-agent.layout.auto.input'))
  if (Number.isFinite(saved) && saved > 0) inputWidth.value = saved
} catch {
  // 存储不可用时保持默认宽度。
}
</script>

<template>
  <div class="agent-page">
    <div class="agent-right"><slot name="mode-switch" />
      <ResizableSplit
        v-model:size="inputWidth"
        class="workspace"
        persist-key="api-agent.layout.auto.input"
        :min="300"
        :min-other="360"
        label="拖动调整测试输入与执行过程宽度"
      >
      <template #first>
      <!-- 左：输入 -->
      <section class="panel input-panel scroll-y">
        <div class="panel-head">
          <span class="panel-title">测试输入</span>
          <span v-if="isRunning" class="chip chip-info">
            <Clock :size="12" /> 运行中
          </span>
        </div>

        <label class="field">
          <span class="field-label">接口文档 / 描述</span>
          <textarea
            v-model="form.contractText"
            class="textarea mono"
            rows="8"
            placeholder="粘贴 OpenAPI / Swagger JSON，或直接写一段接口描述文本…"
            :disabled="isRunning"
          />
        </label>
        <div class="row-actions">
          <button class="btn btn-ghost btn-sm" :disabled="isRunning" @click="discover">
            <Wand2 :size="14" /> 从文档识别接口
          </button>
          <span class="hint">{{ form.discovery.length }} 个可选项</span>
        </div>

        <label class="field">
          <span class="field-label">目标 origin（必须在本机白名单内）</span>
          <input
            v-model="form.origin"
            class="input mono"
            placeholder="http://127.0.0.1:9100"
            :disabled="isRunning"
          />
        </label>

        <div class="field">
          <span class="field-label">允许方法</span>
          <div class="chips">
            <button
              v-for="method in ALL_METHODS"
              :key="method"
              class="chip chip-toggle"
              :class="{ 'is-on': form.methods.has(method) }"
              :disabled="isRunning"
              @click="toggleMethod(method)"
            >
              {{ method }}
            </button>
          </div>
        </div>

        <div class="field">
          <span class="field-label">路径模板（{{ selectedPathList.length }} 已确认）</span>
          <div v-if="form.discovery.length" class="path-list">
            <label v-for="item in form.discovery" :key="item.path + item.method" class="path-item">
              <input
                type="checkbox"
                :checked="form.selectedPaths.has(item.path)"
                :disabled="isRunning"
                @change="togglePath(item.path)"
              />
              <span class="method-badge" :class="`m-${item.method.toLowerCase()}`">{{ item.method }}</span>
              <code>{{ item.path }}</code>
            </label>
          </div>
          <p v-else class="hint">
            未识别到 paths；请在下方人工填写授权路径模板，或用「从文档识别接口」。
          </p>
          <label class="field">
            <span class="field-label">人工授权路径模板（每行一项）</span>
            <textarea
              v-model="form.manualPaths"
              class="textarea mono"
              rows="3"
              placeholder="/health&#10;/users/{id}"
              :disabled="isRunning"
            />
          </label>
          <p class="hint">开始测试即确认以上 origin、方法与勾选或手工填写的路径范围；接口描述不会自动扩大授权。</p>
        </div>

        <details class="advanced">
          <summary>预算与写操作</summary>
          <div class="grid-2">
            <label class="field">
              <span class="field-label">最多用例数</span>
              <input v-model.number="form.maxCases" type="number" min="1" max="50" class="input" :disabled="isRunning" />
            </label>
            <label class="field">
              <span class="field-label">请求预算</span>
              <input v-model.number="form.targetRequests" type="number" min="1" max="100" class="input" :disabled="isRunning" />
            </label>
            <label class="field">
              <span class="field-label">模型调用</span>
              <input v-model.number="form.modelCalls" type="number" min="1" max="30" class="input" :disabled="isRunning" />
            </label>
            <label class="field">
              <span class="field-label">最多轮次</span>
              <input v-model.number="form.maxRounds" type="number" min="1" max="10" class="input" :disabled="isRunning" />
            </label>
          </div>
          <label class="field">
            <span class="field-label">总时长（秒）</span>
            <input v-model.number="form.wallTimeSeconds" type="number" min="1" max="600" class="input" :disabled="isRunning" />
          </label>
          <label class="check-line">
            <input v-model="form.allowMutating" type="checkbox" :disabled="isRunning" />
            <span>
              允许对写操作自动补测
              <em>开启后会对 POST/PUT/PATCH/DELETE 自动再次发送；仅在可重置或无状态目标上使用。</em>
            </span>
          </label>
        </details>

        <div class="submit-row">
          <button class="btn btn-primary" :disabled="!canSubmit" @click="submit">
            <Play :size="14" /> 开始测试
          </button>
          <button v-if="isRunning" class="btn btn-ghost" :disabled="cancelling" @click="cancel">
            <Square :size="14" /> 取消
          </button>
        </div>
      </section>

      </template>
      <template #second>
      <!-- 右：实时过程 -->
      <section class="panel output-panel scroll-y">
        <div class="panel-head">
          <span class="panel-title">执行过程</span>
          <div class="head-actions">
            <span v-if="run" class="chip" :class="`chip-${statusTone(run.status)}`">
              {{ run.status }}
            </span>
            <button v-if="run" class="btn btn-ghost btn-sm" :disabled="reportLoading" @click="loadReport">
              <FileText :size="14" /> 报告
            </button>
          </div>
        </div>

        <div v-if="!run" class="placeholder">
          <p>提交后这里会实时显示：预算进度、事件时间线、每轮真实请求与断言结果、诊断假设与补测决策。</p>
        </div>

        <template v-else>
          <!-- 状态摘要 -->
          <div class="status-bar">
            <div class="status-item">
              <span class="k">停止原因</span>
              <span class="v mono">{{ run.stop_reason ?? '—' }}</span>
            </div>
            <div class="status-item">
              <span class="k">报告完整性</span>
              <span class="v" :class="run.report_status === 'complete' ? 'ok' : 'warn'">
                {{ run.report_status }}
              </span>
            </div>
            <div class="status-item">
              <span class="k">模型状态</span>
              <span class="v mono">{{ run.actual_llm_status }}</span>
            </div>
            <div class="status-item">
              <span class="k">快照哈希</span>
              <span class="v mono small">{{ run.snapshot_sha256.slice(0, 12) }}…</span>
            </div>
          </div>

          <div v-if="run.degradation_reasons.length" class="degrade">
            <TriangleAlert :size="14" />
            <span>退化原因：{{ run.degradation_reasons.join('、') }}</span>
          </div>

          <!-- 预算 -->
          <div class="budgets">
            <div class="budget">
              <div class="budget-head">
                <span>目标请求</span>
                <span class="mono">
                  consumed {{ run.budget.target_http.consumed }} / limit {{ run.budget.target_http.limit }}
                </span>
              </div>
              <div class="bar"><i :style="{ width: percent(run.budget.target_http) + '%' }" /></div>
              <div class="budget-foot">
                <span>reserved {{ run.budget.target_http.reserved }}</span>
                <span>observed {{ run.budget.target_http.observed_attempts ?? 0 }}</span>
                <span>unknown {{ run.budget.target_http.unknown_requests ?? 0 }}</span>
              </div>
            </div>
            <div class="budget">
              <div class="budget-head">
                <span>模型调用</span>
                <span class="mono">
                  consumed {{ run.budget.model_call.consumed }} / limit {{ run.budget.model_call.limit }}
                </span>
              </div>
              <div class="bar"><i :style="{ width: percent(run.budget.model_call) + '%' }" /></div>
              <div class="budget-foot">
                <span>reserved {{ run.budget.model_call.reserved }}</span>
              </div>
            </div>
          </div>

          <!-- 时间线 -->
          <div class="section-label">事件时间线</div>
          <ol class="timeline">
            <li v-for="event in run.events" :key="event.sequence" class="tl-item">
              <span class="tl-dot" :class="`dot-${statusTone(event.status)}`" />
              <div class="tl-body">
                <div class="tl-head">
                  <span class="tl-node mono">{{ event.node }}</span>
                  <span class="tl-status">{{ event.status }}</span>
                  <span class="tl-time mono">{{ formatDateTime(event.timestamp) }}</span>
                </div>
                <p v-if="event.reason" class="tl-reason">{{ event.reason }}</p>
              </div>
            </li>
          </ol>

          <!-- 轮次 -->
          <div class="section-label">轮次（{{ run.rounds.length }}）</div>
          <div v-for="round in run.rounds" :key="round.index" class="round">
            <button class="round-head" @click="toggleRound(round.index)">
              <span class="round-idx">第 {{ round.index }} 轮</span>
              <span class="round-stat">
                通过 {{ passedCount(round).passed }} · 失败 {{ passedCount(round).failed }} · 未知
                {{ passedCount(round).unknown }}
              </span>
              <span class="round-hint">{{ expandedRounds.has(round.index) ? '收起' : '展开' }}</span>
            </button>

            <div v-if="expandedRounds.has(round.index)" class="round-body">
              <!-- 真实请求 -->
              <div class="sub-label">请求（{{ planOf(round)?.requests.length ?? 0 }}）</div>
              <div v-if="planOf(round)" class="case-list">
                <div v-for="c in planOf(round)?.requests ?? []" :key="c.id" class="case">
                  <div class="case-head">
                    <span class="method-badge" :class="`m-${c.method.toLowerCase()}`">{{ c.method }}</span>
                    <code class="case-path">{{ caseTitle(c) }}</code>
                    <span
                      v-if="basisOf(round, c.id)"
                      class="chip chip-xs"
                      :class="basisOf(round, c.id) === 'inferred' ? 'chip-warning' : 'chip-success'"
                    >
                      {{ basisOf(round, c.id) === 'inferred' ? '推断期望' : '契约依据' }}
                    </span>
                  </div>
                  <p v-if="rationaleOf(round, c.id)" class="case-rationale">
                    {{ rationaleOf(round, c.id) }}
                  </p>
                  <ul class="assert-list">
                    <li v-for="e in evidenceFor(round, c.id)" :key="e.id" class="assert">
                      <CircleCheck v-if="e.passed === true" :size="13" class="ic-ok" />
                      <CircleX v-else-if="e.passed === false" :size="13" class="ic-bad" />
                      <TriangleAlert v-else :size="13" class="ic-warn" />
                      <span class="assert-kind">{{ e.kind }}</span>
                      <span class="assert-exp mono">期望 {{ JSON.stringify(e.expected) }}</span>
                      <span v-if="e.http_status" class="assert-http mono">HTTP {{ e.http_status }}</span>
                      <span class="assert-id mono">{{ e.id }}</span>
                    </li>
                  </ul>
                </div>
              </div>

              <!-- 诊断 -->
              <template v-if="round.diagnosis">
                <div class="sub-label">诊断</div>
                <p class="summary">{{ round.diagnosis.analysis.summary }}</p>
                <div v-if="round.diagnosis.analysis.hypotheses.length" class="hypo-list">
                  <div
                    v-for="h in round.diagnosis.analysis.hypotheses"
                    :key="h.id"
                    class="hypo"
                  >
                    <div class="hypo-head">
                      <span class="tag-hypo">假设</span>
                      <span class="mono">{{ h.id }}</span>
                    </div>
                    <p class="hypo-cause">{{ h.cause }}</p>
                    <p class="hypo-next">下一步：{{ h.next_check }}</p>
                    <p class="hypo-ref mono">引用证据：{{ h.evidence_ids.join(', ') }}</p>
                  </div>
                </div>
                <p v-if="round.diagnosis.analysis.coverage_gaps.length" class="gaps">
                  覆盖盲区：{{ round.diagnosis.analysis.coverage_gaps.join('；') }}
                </p>
              </template>

              <!-- 补测决策 -->
              <template v-if="round.adaptation">
                <div class="sub-label">补测决策</div>
                <p class="summary">
                  {{ round.adaptation.continue_testing ? '继续补测' : '停止' }}：{{ round.adaptation.reason }}
                </p>
                <ul v-if="round.adaptation.mutations.length" class="mut-list">
                  <li v-for="(m, i) in round.adaptation.mutations" :key="i" class="mut">
                    <span class="tag-mut">{{ m.location }}</span>
                    <span class="mono">{{ m.parent_id }} → {{ m.pointer || '(path)' }}</span>
                    <span class="mut-val">{{ m.reason }}</span>
                  </li>
                </ul>
                <p v-if="round.adaptation.rejected.length" class="rejected">
                  被拒：{{ round.adaptation.rejected.join('、') }}
                </p>
              </template>
            </div>
          </div>

          <!-- AI 概述 -->
          <template v-if="run.commentary">
            <div class="section-label">AI 概述（模型撰写，非确定性事实）</div>
            <div class="commentary">
              <p>{{ run.commentary.overview }}</p>
              <ul v-if="run.commentary.recommendations.length">
                <li v-for="(r, i) in run.commentary.recommendations" :key="i">{{ r }}</li>
              </ul>
            </div>
          </template>

          <!-- 报告 -->
          <template v-if="report !== null">
            <div class="section-label">
              报告
              <button class="btn btn-ghost btn-xs" :disabled="reportLoading" @click="loadReport">
                <RotateCw :size="12" /> 刷新
              </button>
            </div>
            <pre class="report mono">{{ report }}</pre>
          </template>
        </template>
      </section>
      </template>
      </ResizableSplit>
    </div>
  </div>
</template>

<style scoped>
.agent-page {
  height: 100%;
  min-height: 0;
}

/* 左：历史运行 */
/* 右：模式栏 + 输入/过程分栏 */
.agent-right {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
}
.page-head {
  margin-bottom: 16px;
}
.page-title {
  margin: 0 0 4px;
  font-size: 20px;
  font-weight: 650;
  color: var(--text-primary);
}
.page-sub {
  margin: 0;
  max-width: 900px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}

/* 分栏根：由 ResizableSplit 控制列宽，这里只补间距与高度约束。 */
/* 与手动模式同一套齐边布局：两栏紧贴分隔线，不套卡片边框与外边距，
   这样切换手动/自动时分栏位置与整体尺寸完全不跳动。 */
.workspace {
  flex: 1;
  min-height: 0;
}

.workspace :deep(.split-first),
.workspace :deep(.split-second) {
  overflow: hidden;
}

.panel {
  background: var(--bg-surface);
}
.input-panel,
.output-panel {
  display: flex;
  flex-direction: column;
  padding: 14px 16px 20px;
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}

/* 左栏首个标题条与右栏对齐：手动模式左侧没有独立标题，
   这里用同样的上内边距让表单首行与右栏标题条基线一致。 */

/* 与手动模式 `.results-head` 同高同边距：切换模式时两栏顶部基线一致。 */
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 0 12px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}
.panel-title {
  font-size: 14px;
  font-weight: 620;
  color: var(--text-primary);
}
.head-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.field {
  display: block;
  margin-bottom: 12px;
}
.field-label {
  display: block;
  margin-bottom: 5px;
  font-size: 12px;
  font-weight: 560;
  color: var(--text-secondary);
}
.input,
.textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 13px;
  font-family: inherit;
  box-sizing: border-box;
}
.textarea {
  resize: vertical;
  line-height: 1.5;
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}
.input:focus,
.textarea:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.row-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.hint {
  font-size: 12px;
  color: var(--text-tertiary);
  margin: 0;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 560;
  background: var(--bg-subtle);
  color: var(--text-secondary);
  border: 1px solid transparent;
}
.chip-xs {
  padding: 1px 6px;
  font-size: 10px;
}
.chip-toggle {
  cursor: pointer;
  background: var(--bg-surface);
  border-color: var(--border-default);
}
.chip-toggle.is-on {
  background: var(--accent-soft);
  border-color: var(--accent-border);
  color: var(--accent);
}
.chip-toggle:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}
.chip-success {
  background: var(--success-soft);
  color: var(--success);
}
.chip-warning {
  background: var(--warning-soft);
  color: var(--warning);
}
.chip-danger {
  background: var(--danger-soft);
  color: var(--danger);
}
.chip-info {
  background: var(--info-soft);
  color: var(--info);
}

.path-list {
  max-height: 180px;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 6px;
  background: var(--bg-subtle);
}
.path-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 5px;
  border-radius: var(--radius-sm);
  font-size: 12px;
}
.path-item:hover {
  background: var(--bg-hover);
}
.path-item code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-primary);
}

.method-badge {
  display: inline-block;
  min-width: 46px;
  text-align: center;
  padding: 1px 5px;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 640;
  background: var(--bg-sunken);
  color: var(--text-secondary);
}
.m-get { color: var(--method-get); background: var(--success-soft); }
.m-post { color: var(--method-post); background: var(--warning-soft); }
.m-put { color: var(--method-put); background: var(--info-soft); }
.m-patch { color: var(--method-patch); background: var(--accent-soft); }
.m-delete { color: var(--method-delete); background: var(--danger-soft); }

.advanced {
  margin: 8px 0 12px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
}
.advanced summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 560;
  color: var(--text-secondary);
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 10px;
}
.check-line {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  font-size: 12px;
  color: var(--text-secondary);
  margin-top: 8px;
}
.check-line em {
  display: block;
  font-style: normal;
  color: var(--text-tertiary);
  font-size: 11.5px;
  margin-top: 2px;
}

.submit-row {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  font-size: 13px;
  font-weight: 560;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.15s ease;
}
.btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.btn-primary {
  background: var(--text-primary);
  color: var(--text-inverse);
}
.btn-primary:not(:disabled):hover {
  background: #2c2c33;
}
.btn-ghost {
  background: var(--bg-surface);
  border-color: var(--border-default);
  color: var(--text-secondary);
}
.btn-ghost:not(:disabled):hover {
  background: var(--bg-hover);
}
.btn-sm {
  padding: 5px 10px;
  font-size: 12px;
}
.btn-xs {
  padding: 2px 7px;
  font-size: 11px;
  margin-left: 8px;
}

.placeholder {
  padding: 48px 16px;
  text-align: center;
  color: var(--text-tertiary);
  font-size: 13px;
  line-height: 1.7;
}

.status-bar {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  padding: 10px 12px;
  background: var(--bg-subtle);
  border-radius: var(--radius-md);
  margin-bottom: 12px;
}
.status-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.status-item .k {
  font-size: 11px;
  color: var(--text-tertiary);
}
.status-item .v {
  font-size: 12.5px;
  font-weight: 560;
  color: var(--text-primary);
}
.status-item .v.ok { color: var(--success); }
.status-item .v.warn { color: var(--warning); }
.status-item .v.small { font-size: 11px; }

.degrade {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: var(--warning-soft);
  color: var(--warning);
  border-radius: var(--radius-sm);
  font-size: 12px;
  margin-bottom: 12px;
}

.budgets {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 16px;
}
.budget {
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
}
.budget-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}
.bar {
  height: 5px;
  background: var(--bg-sunken);
  border-radius: 999px;
  overflow: hidden;
}
.bar i {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}
.budget-foot {
  display: flex;
  gap: 12px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.section-label {
  display: flex;
  align-items: center;
  font-size: 12px;
  font-weight: 640;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin: 18px 0 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border-subtle);
}

.timeline {
  list-style: none;
  margin: 0;
  padding: 0 0 0 4px;
}
.tl-item {
  display: flex;
  gap: 10px;
  padding-bottom: 10px;
  position: relative;
}
.tl-item:not(:last-child)::before {
  content: '';
  position: absolute;
  left: 5px;
  top: 14px;
  bottom: 0;
  width: 1px;
  background: var(--border-subtle);
}
.tl-dot {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  margin-top: 3px;
  flex-shrink: 0;
  border: 2px solid var(--bg-surface);
}
.dot-success { background: var(--success); }
.dot-danger { background: var(--danger); }
.dot-warning { background: var(--warning); }
.dot-info { background: var(--info); }
.tl-body {
  flex: 1;
  min-width: 0;
}
.tl-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
}
.tl-node {
  font-weight: 620;
  color: var(--text-primary);
}
.tl-status {
  color: var(--text-secondary);
}
.tl-time {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--text-tertiary);
}
.tl-reason {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}

.round {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  margin-bottom: 8px;
  overflow: hidden;
}
.round-head {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  background: var(--bg-subtle);
  border: none;
  cursor: pointer;
  font-family: inherit;
  font-size: 12.5px;
  text-align: left;
}
.round-head:hover {
  background: var(--bg-hover);
}
.round-idx {
  font-weight: 620;
  color: var(--text-primary);
}
.round-stat {
  color: var(--text-secondary);
}
.round-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--accent);
}
.round-body {
  padding: 12px;
}

.sub-label {
  font-size: 11.5px;
  font-weight: 620;
  color: var(--text-tertiary);
  margin: 10px 0 6px;
}
.sub-label:first-child {
  margin-top: 0;
}

.case {
  border-left: 2px solid var(--border-default);
  padding: 0 0 6px 10px;
  margin-bottom: 10px;
}
.case-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.case-path {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-primary);
  word-break: break-all;
}
.case-rationale {
  margin: 4px 0 5px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.assert-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.assert {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  padding: 2px 0;
  color: var(--text-secondary);
  flex-wrap: wrap;
}
.ic-ok { color: var(--success); }
.ic-bad { color: var(--danger); }
.ic-warn { color: var(--warning); }
.assert-kind {
  font-weight: 560;
  color: var(--text-primary);
}
.assert-exp,
.assert-http {
  font-size: 11px;
  color: var(--text-secondary);
}
.assert-id {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-tertiary);
}

.summary {
  margin: 0 0 8px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-primary);
}
.hypo-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.hypo {
  padding: 9px 11px;
  border: 1px solid var(--warning-soft);
  background: var(--warning-soft);
  border-radius: var(--radius-sm);
}
.hypo-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.tag-hypo {
  font-size: 10px;
  font-weight: 640;
  color: var(--warning);
  background: var(--bg-surface);
  padding: 1px 6px;
  border-radius: 4px;
}
.hypo-cause {
  margin: 0 0 3px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-primary);
}
.hypo-next,
.hypo-ref {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-secondary);
  line-height: 1.5;
}
.gaps {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
}

.mut-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.mut {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  padding: 3px 0;
  color: var(--text-secondary);
  flex-wrap: wrap;
}
.tag-mut {
  font-size: 10px;
  font-weight: 640;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 1px 6px;
  border-radius: 4px;
}
.mut-val {
  color: var(--text-tertiary);
}
.rejected {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--danger);
}

.commentary {
  padding: 12px;
  background: var(--bg-subtle);
  border-radius: var(--radius-md);
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--text-primary);
}
.commentary p {
  margin: 0 0 8px;
}
.commentary ul {
  margin: 0;
  padding-left: 18px;
}
.commentary li {
  margin-bottom: 3px;
}

.report {
  margin: 0;
  padding: 12px;
  background: var(--bg-sunken);
  border-radius: var(--radius-md);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 520px;
  overflow: auto;
}

@media (max-width: 1100px) {
  .budgets {
    grid-template-columns: 1fr;
  }
}
</style>
