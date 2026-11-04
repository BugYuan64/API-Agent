<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Braces,
  CircleCheck,
  CircleX,
  Cpu,
  FileJson2,
  Loader2,
  Play,
  RotateCcw,
  Sparkles,
  Terminal,
  TriangleAlert,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import ExecutionCases from '@/components/ExecutionCases.vue'
import type { HistoryEntry } from '@/components/HistoryList.vue'
import ResizableSplit from '@/components/ResizableSplit.vue'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import {
  diagnoseExecution,
  listExecutions,
  listProjects,
  runExecution,
} from '@/api/endpoints'
import type { AgentDiagnosis, Execution, Project } from '@/api/types'
import { engineLabel, useEngineSelection } from '@/lib/engines'
import {
  describeExecutionStatus,
  formatDuration,
  formatRelative,
  prettyJson,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

/** 默认计划：一个可直接运行的 GET，演示结构完整但不含任何真实凭据。 */
const DEFAULT_PLAN = `{
  "origin": "http://127.0.0.1:8010",
  "allowed_methods": ["GET"],
  "path_templates": ["/health"],
  "requests": [
    {
      "id": "health-ok",
      "method": "GET",
      "path": "/health",
      "assertions": [
        { "kind": "status", "expected": 200 },
        { "kind": "json_equals", "pointer": "/status", "expected": "ok" }
      ]
    }
  ]
}`

const planText = ref(DEFAULT_PLAN)
const label = ref('health check')
const projectId = ref<string>('')
const projects = ref<Project[]>([])
const running = ref(false)
/** 引擎列表与当前选择来自共享 composable，与设置页共用同一份存储。 */
const {
  engines,
  selected: selectedEngine,
  activeEngine,
  refresh: refreshEngines,
  select: selectEngine,
} = useEngineSelection()
const result = ref<Execution | null>(null)
const history = ref<Execution[]>([])
const expanded = ref<string | null>(null)

/** 把执行记录映射成共享历史列的条目形状（只做展示映射，不改变业务数据）。 */
const historyEntries = computed<HistoryEntry[]>(() =>
  history.value.map((item) => ({
    id: item.id,
    label: item.label || '(未命名)',
    status: describeExecutionStatus(item.status),
    tone:
      item.status === 'passed' ? 'success' : item.status === 'failed' ? 'warning' : 'danger',
    meta: formatRelative(item.created_at),
    // 外壳据此把多条来源合并排序，并在点击时分派回本面板。
    at: item.created_at,
    source: 'manual',
  })),
)



/** 实时解析计划文本，用于给用户即时反馈（不替代后端校验）。 */
const parsed = computed(() => {
  try {
    const value = JSON.parse(planText.value)
    return { ok: true as const, value, requests: Array.isArray(value?.requests) ? value.requests.length : 0 }
  } catch (error) {
    return { ok: false as const, message: (error as Error).message }
  }
})

const planSummary = computed(() => {
  if (!parsed.value.ok) return null
  const value = parsed.value.value as Record<string, unknown>
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const requests = Array.isArray(value.requests) ? (value.requests as Record<string, unknown>[]) : []
  const assertions = requests.reduce<number>(
    (total, request) => total + (Array.isArray(request?.assertions) ? request.assertions.length : 0),
    0,
  )
  return {
    origin: typeof value.origin === 'string' ? value.origin : '—',
    requests: requests.length,
    assertions,
  }
})

/** 断言统计：通过 / 失败 / 未知，用于结果卡片上的小计。 */
function assertionStats(execution: Execution) {
  let passed = 0
  let failed = 0
  let unknown = 0
  for (const item of execution.executions) {
    for (const assertion of item.assertions) {
      if (assertion.passed === true) passed += 1
      else if (assertion.passed === false) failed += 1
      else unknown += 1
    }
  }
  return { passed, failed, unknown }
}

/* ---------- AI 分析（显式触发，不改变已有执行记录） ---------- */
const diagnosing = ref(false)
const diagnosis = ref<AgentDiagnosis | null>(null)
const contractRef = ref('')
let diagnosisGeneration = 0
const canAnalyze = computed(() => !!result.value?.plan && !diagnosing.value)

function clearDiagnosis() {
  diagnosisGeneration += 1
  diagnosis.value = null
  diagnosing.value = false
}

watch([() => result.value?.id, planText, contractRef], clearDiagnosis, { flush: 'sync' })
onBeforeUnmount(clearDiagnosis)

/* ---------- 对外接口：历史条目由外壳统一渲染 ---------- */
const emit = defineEmits<{
  (event: 'history', entries: HistoryEntry[]): void
  (event: 'active', id: string | null): void
}>()

/** 供外壳的历史列点击时调用（按 id 选择一条执行）。 */
function openHistory(id: string): void {
  const target = history.value.find((item) => item.id === id)
  if (target) selectExecution(target)
}

defineExpose({ openHistory })

watch(historyEntries, (entries) => emit('history', entries), { immediate: true })
watch(result, (value) => emit('active', value?.id ?? null), { immediate: true })

function selectExecution(execution: Execution) {
  clearDiagnosis()
  result.value = execution
  planText.value = execution.plan ? JSON.stringify(execution.plan, null, 2) : ''
  expanded.value = execution.executions[0]?.id ?? null
}

/** 服务端从 execution_id 恢复原计划与结果，编辑器内容不参与证据配对。
 *  约束：仅当用户点击时发生；失败不影响已落库的执行记录。 */
async function analyzeWithAI() {
  if (!result.value || !canAnalyze.value) return
  const executionId = result.value.id
  const generation = ++diagnosisGeneration
  diagnosing.value = true
  diagnosis.value = null
  try {
    const response = await diagnoseExecution(executionId, {
      contract: contractRef.value.trim() || '（未提供契约；仅就断言差异作观察）',
    })
    if (generation === diagnosisGeneration && result.value?.id === executionId) diagnosis.value = response
  } catch (error) {
    if (generation === diagnosisGeneration) useToastStore().push('danger', 'AI 分析失败', describeError(error))
  } finally {
    if (generation === diagnosisGeneration) diagnosing.value = false
  }
}



async function loadProjects() {
  try {
    const page = await listProjects(50)
    projects.value = page.items
    if (!projectId.value && page.items.length) projectId.value = page.items[0].id
  } catch {
    projects.value = []
  }
}

async function loadHistory() {
  try {
    const page = await listExecutions(20)
    history.value = page.items
  } catch {
    history.value = []
  }
}

function formatPlan() {
  if (!parsed.value.ok) {
    useToastStore().push('warning', '当前内容不是合法 JSON', parsed.value.message)
    return
  }
  planText.value = prettyJson(planText.value)
}

function resetPlan() {
  planText.value = DEFAULT_PLAN
  label.value = 'health check'
  result.value = null
  diagnosis.value = null
}

async function run() {
  if (!parsed.value.ok) {
    useToastStore().push('warning', '计划不是合法 JSON', parsed.value.message)
    return
  }
  running.value = true
  result.value = null
  try {
    const execution = await runExecution({
      plan: parsed.value.value,
      label: label.value.trim() || 'untitled',
      project_id: projectId.value || null,
      engine: selectedEngine.value,
    })
    result.value = execution
    diagnosis.value = null
    expanded.value = execution.executions[0]?.id ?? null
    const tone =
      execution.status === 'passed' ? 'success' : execution.status === 'failed' ? 'warning' : 'danger'
    useToastStore().push(
      tone,
      `执行完成：${describeExecutionStatus(execution.status)}`,
      `${execution.observed_attempts}/${execution.reserved_requests} 请求有观测结果`,
    )
    await loadHistory()
  } catch (error) {
    useToastStore().push('danger', '计划被拒绝或执行失败', describeError(error))
  } finally {
    running.value = false
  }
}

/** 计划编辑区宽度：内层分栏（编辑|结果）的受控尺寸，默认约为半宽。 */
const editorWidth = ref(460)
try {
  const saved = Number(window.localStorage.getItem('api-agent.layout.manual.editor'))
  if (Number.isFinite(saved) && saved > 0) editorWidth.value = saved
} catch {
  // 存储不可用时保持默认宽度。
}

onMounted(async () => {
  await Promise.all([refreshEngines(), loadProjects(), loadHistory()])
})
</script>

<template>
  <div class="plan">
    <div class="plan-right">
      <slot name="mode-switch" />
      <ResizableSplit
        v-model:size="editorWidth"
        class="plan-main"
        persist-key="api-agent.layout.manual.editor"
        :min="320"
        :min-other="300"
        label="拖动调整计划编辑区与执行结果区宽度"
      >
        <template #first>
        <section class="editor scroll-y">
          <div class="editor-head">
            <div>
              <p class="subtitle">
                结构化计划 → 可插拔执行引擎（原生 HTTPX / Postman CLI）→ 逐条断言结果。计划只描述数据，不含可执行脚本。
              </p>
            </div>
            <div
              v-if="activeEngine"
              class="engine-chip"
              :class="activeEngine.available ? 'chip-ok' : 'chip-warn'"
            >
              <component :is="selectedEngine === 'native' ? Cpu : Terminal" :size="12" />
              {{ activeEngine.available ? `${activeEngine.id} 就绪` : `${activeEngine.id} 不可用` }}
            </div>
          </div>

      <div class="meta-row">
        <label class="field label-field">
          <span class="field-label">标签</span>
          <input v-model="label" class="input" placeholder="例如 crm-smoke" maxlength="80" />
        </label>
        <label class="field label-field">
          <span class="field-label">执行引擎</span>
          <select
            :value="selectedEngine"
            class="select"
            @change="selectEngine(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="item in engines" :key="item.id" :value="item.id">
              {{ engineLabel(item.id) }}
              {{ item.available ? '' : '· 不可用' }}
            </option>
          </select>
        </label>
        <label class="field label-field">
          <span class="field-label">关联项目（可选）</span>
          <select v-model="projectId" class="select">
            <option value="">不关联</option>
            <option v-for="project in projects" :key="project.id" :value="project.id">
              {{ project.name }}
            </option>
          </select>
        </label>
      </div>

      <div class="editor-toolbar">
        <span class="field-label">计划 JSON</span>
        <div class="toolbar-fill" />
        <button class="btn btn-ghost btn-sm" @click="formatPlan">
          <Braces :size="13" />
          格式化
        </button>
        <button class="btn btn-ghost btn-sm" @click="resetPlan">
          <RotateCcw :size="13" />
          重置示例
        </button>
      </div>

      <textarea v-model="planText" class="textarea mono plan-editor" spellcheck="false" />

      <label class="field contract-field">
        <span class="field-label">契约摘要（可选，仅用于 AI 分析）</span>
        <textarea
          v-model="contractRef"
          class="textarea mono contract-input"
          rows="2"
          placeholder="例如：POST /api/user/register 缺少必填字段时应返回 code=400。留空则 AI 只就断言差异作观察。"
        />
      </label>

      <div class="editor-foot">
        <div v-if="parsed.ok && planSummary" class="plan-summary">
          <span class="tag tag-info">{{ planSummary.requests }} 请求</span>
          <span class="tag">{{ planSummary.assertions }} 断言</span>
          <span class="tag mono">{{ planSummary.origin }}</span>
        </div>
        <div v-else class="plan-invalid">
          <TriangleAlert :size="13" />
          <span>JSON 语法错误：{{ parsed.message }}</span>
        </div>
        <div class="toolbar-fill" />
        <button class="btn btn-accent run-button" :disabled="running || !parsed.ok" @click="run">
          <Loader2 v-if="running" :size="14" class="spinning" />
          <Play v-else :size="14" />
          {{ running ? '执行中' : '执行计划' }}
        </button>
      </div>

      <div class="policy-note">
        <FileJson2 :size="14" />
        <div>
          <strong>执行边界</strong>
          <p>
            目标 origin 必须同时满足两条：是数字回环地址，且出现在后端配置
            <code>TARGET_ALLOWED_ORIGINS</code> 中。凭据只能通过
            <code>auth_ref.env_var</code> 引用环境变量名，计划里永远不出现真实密钥。
          </p>
        </div>
      </div>
    </section>
        </template>
        <template #second>
    <!-- 右：结果 + 历史 -->
    <section class="results">
      <div class="results-head">
        <span class="results-title">{{ result ? '本次执行' : '执行结果' }}</span>
        <div class="head-actions">
          <button
            v-if="result"
            class="btn-ai"
            :disabled="!canAnalyze"
            :title="result.plan ? '分析本次执行保存的原计划和结果' : '旧记录未保存原计划，请重新执行后分析'"
            @click="analyzeWithAI"
          >
            <Loader2 v-if="diagnosing" :size="13" class="spin" />
            <Sparkles v-else :size="13" />
            {{ diagnosing ? '分析中…' : 'AI 分析' }}
          </button>
          <CopyButton v-if="result" :text="JSON.stringify(result, null, 2)" label="复制结果 JSON" />
        </div>
      </div>

      <div class="results-body scroll-y">
        <p v-if="result && !result.plan" class="subtitle">此旧记录未保存原计划，无法可靠配对 AI 分析；请重新执行计划。</p>
        <template v-if="result">
          <div class="summary-card" :class="`summary-${result.status}`">
            <div class="summary-main">
              <div class="summary-status">
                <CircleCheck v-if="result.status === 'passed'" :size="17" />
                <CircleX v-else-if="result.status === 'failed'" :size="17" />
                <TriangleAlert v-else :size="17" />
                {{ describeExecutionStatus(result.status) }}
              </div>
              <div class="summary-meta">
                <span>{{ formatDuration(result.duration_ms) }}</span>
                <span>·</span>
                <span>{{ result.observed_attempts }}/{{ result.reserved_requests }} 请求有观测</span>
                <span v-if="result.unknown_requests > 0">· {{ result.unknown_requests }} 未知</span>
                <span v-if="result.error_kind">· <code>{{ result.error_kind }}</code></span>
              </div>
            </div>
            <div class="summary-assertions">
              <span class="assert-count ok">{{ assertionStats(result).passed }} 通过</span>
              <span class="assert-count bad">{{ assertionStats(result).failed }} 失败</span>
              <span v-if="assertionStats(result).unknown" class="assert-count unknown">
                {{ assertionStats(result).unknown }} 未知
              </span>
            </div>
          </div>

          <ExecutionCases v-model:expanded="expanded" :executions="result.executions" />

          <!-- AI 诊断：evidence 是代码算出的观测事实，hypotheses 是模型假设 -->
          <div v-if="diagnosis" class="diag">
            <div class="diag-head">
              <Sparkles :size="14" />
              <span>AI 诊断</span>
              <span class="diag-note">模型假设，未证实；请对照契约核对</span>
            </div>
            <p class="diag-summary">{{ diagnosis.analysis.summary }}</p>
            <div v-if="diagnosis.analysis.hypotheses.length" class="diag-hypos">
              <div v-for="h in diagnosis.analysis.hypotheses" :key="h.id" class="diag-hypo">
                <div class="diag-hypo-head">
                  <span class="diag-tag">假设</span>
                  <span class="mono">{{ h.id }}</span>
                </div>
                <p>{{ h.cause }}</p>
                <p class="diag-next">下一步：{{ h.next_check }}</p>
                <p class="diag-ref mono">引用证据：{{ h.evidence_ids.join(', ') }}</p>
              </div>
            </div>
            <p v-else class="diag-empty">模型未给出待验证假设。</p>
            <p v-if="diagnosis.analysis.coverage_gaps.length" class="diag-gaps">
              覆盖盲区：{{ diagnosis.analysis.coverage_gaps.join('；') }}
            </p>
          </div>
        </template>

        <EmptyState
          v-else
          :icon="Play"
          title="还没有执行结果"
          description="左侧计划通过后端校验后会交给 Postman CLI 执行；每条断言的通过与否都会落库，可在下方历史里回看。"
        />

      </div>
    </section>
        </template>
      </ResizableSplit>
    </div>
  </div>
</template>

<style scoped>
.plan {
  height: 100%;
  min-height: 0;
}

/* 历史列：填满左侧插槽，自身滚动 */
/* 右半：模式栏（固定高）+ 计划编辑与结果（左右并排） */
.plan-right {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.plan-main {
  flex: 1;
  min-height: 0;
}

/* 内层分栏必须撑满父级；否则按内容塌缩会让可用宽度和拖动上限都算错。 */
.plan-main > .resizable-split {
  width: 100%;
  min-width: 0;
}

.editor {
  height: 100%;
  min-width: 0;
  padding: 18px 18px 26px;
  border-right: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  display: flex;
  flex-direction: column;
  gap: 11px;
}

.editor-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.015em;
}

.subtitle {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  line-height: 1.7;
  max-width: 60ch;
}

.engine-chip {
  flex: none;
  height: 23px;
  padding: 0 9px;
  display: inline-flex;
  align-items: center;
  border-radius: 99px;
  font-size: 11px;
  font-weight: 500;
  font-family: var(--font-mono);
}

.chip-ok {
  background: var(--success-soft);
  color: var(--success);
}

.chip-warn {
  background: var(--warning-soft);
  color: var(--warning);
}

.meta-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.label-field {
  min-width: 0;
}

.editor-toolbar {
  display: flex;
  align-items: center;
  gap: 5px;
}

.toolbar-fill {
  flex: 1;
}

.plan-editor {
  flex: 1;
  min-height: 300px;
  font-size: 11.5px;
  line-height: 1.7;
  background: var(--bg-sunken);
}

.contract-field {
  margin-top: 10px;
}
.contract-input {
  min-height: 54px;
  font-size: 11.5px;
  line-height: 1.6;
  background: var(--bg-subtle);
}

.editor-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plan-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.plan-invalid {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--warning);
  font-size: 11.5px;
}

.run-button {
  height: 32px;
}

.policy-note {
  display: flex;
  gap: 9px;
  padding: 11px 13px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle);
  color: var(--text-tertiary);
}

.policy-note strong {
  font-size: 11.5px;
  color: var(--text-secondary);
}

.policy-note p {
  margin: 3px 0 0;
  font-size: 11px;
  line-height: 1.7;
}

/* ---------- 结果栏 ---------- */
.results {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  background: var(--bg-canvas);
}

.results-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  flex: none;
}

.results-title {
  font-size: 12.5px;
  font-weight: 600;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* AI 分析按钮：紫色强调，与主色的墨黑按钮区分，表明它是一次模型调用 */
.btn-ai {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--accent-border);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
  font-weight: 560;
  font-family: inherit;
  cursor: pointer;
}
.btn-ai:not(:disabled):hover {
  background: var(--accent);
  color: var(--text-inverse);
}
.btn-ai:disabled {
  cursor: progress;
  opacity: 0.7;
}
.spin {
  animation: spin 1s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* AI 诊断块：与确定性结果视觉分离，避免模型文字被当成已证实结论 */
.diag {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--accent-border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}
.diag-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 620;
  color: var(--accent);
  margin-bottom: 8px;
}
.diag-note {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-tertiary);
}
.diag-summary {
  margin: 0 0 10px;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--text-primary);
}
.diag-hypos {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.diag-hypo {
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  background: var(--warning-soft);
}
.diag-hypo-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.diag-tag {
  font-size: 10px;
  font-weight: 640;
  color: var(--warning);
  background: var(--bg-surface);
  padding: 1px 6px;
  border-radius: 4px;
}
.diag-hypo p {
  margin: 0 0 3px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-primary);
}
.diag-next,
.diag-ref {
  font-size: 11.5px !important;
  color: var(--text-secondary) !important;
}
.diag-empty,
.diag-gaps {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
}

.results-body {
  flex: 1;
  min-height: 0;
  padding: 14px 16px 22px;
}

.summary-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-subtle);
  background: var(--bg-surface);
  flex-wrap: wrap;
}

.summary-passed {
  border-color: #cfeee2;
  background: linear-gradient(0deg, var(--success-soft), var(--success-soft));
}

.summary-failed {
  border-color: #f3e0bd;
  background: var(--warning-soft);
}

.summary-error,
.summary-timed_out {
  border-color: #f5d0d0;
  background: var(--danger-soft);
}

.summary-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 650;
}

.summary-meta {
  display: flex;
  gap: 6px;
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-secondary);
  flex-wrap: wrap;
}

.summary-assertions {
  display: flex;
  gap: 8px;
  font-size: 11.5px;
  font-weight: 600;
}

.assert-count.ok { color: var(--success); }
.assert-count.bad { color: var(--danger); }
.assert-count.unknown { color: var(--text-tertiary); }

.history-block {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 1100px) {
  .plan {
    flex-direction: column;
  }

  .editor {
    width: 100%;
    border-right: 0;
    border-bottom: 1px solid var(--border-subtle);
  }
}
</style>
