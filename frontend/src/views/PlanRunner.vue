<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  Braces,
  ChevronDown,
  CircleCheck,
  CircleX,
  FileJson2,
  Loader2,
  Play,
  RotateCcw,
  TriangleAlert,
} from 'lucide-vue-next'
import CopyButton from '@/components/CopyButton.vue'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import { engineStatus, listExecutions, listProjects, runExecution } from '@/api/endpoints'
import type { EngineStatus, Execution, Project } from '@/api/types'
import {
  describeAssertionKind,
  describeExecutionStatus,
  formatDuration,
  formatRelative,
  prettyJson,
  statusTone,
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
const engine = ref<EngineStatus | null>(null)
const result = ref<Execution | null>(null)
const history = ref<Execution[]>([])
const expanded = ref<string | null>(null)

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
  const requests = Array.isArray(value.requests) ? (value.requests as Record<string, unknown>[]) : []
  const assertions = requests.reduce<number>(
    (total, request) => total + (Array.isArray(request.assertions) ? request.assertions.length : 0),
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

async function refreshEngine() {
  try {
    engine.value = await engineStatus()
  } catch {
    engine.value = { available: false, error_kind: 'unreachable' }
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
    })
    result.value = execution
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

onMounted(async () => {
  await Promise.all([refreshEngine(), loadProjects(), loadHistory()])
})
</script>

<template>
  <div class="plan">
    <!-- 左：计划编辑 -->
    <section class="editor scroll-y">
      <div class="editor-head">
        <div>
          <h2 class="title">用例计划</h2>
          <p class="subtitle">
            结构化计划 → Postman Collection → 逐条断言结果。计划只描述数据，不含可执行脚本。
          </p>
        </div>
        <div v-if="engine" class="engine-chip" :class="engine.available ? 'chip-ok' : 'chip-warn'">
          {{ engine.available ? `CLI ${engine.version}` : 'CLI 不可用' }}
        </div>
      </div>

      <div class="meta-row">
        <label class="field label-field">
          <span class="field-label">标签</span>
          <input v-model="label" class="input" placeholder="例如 crm-smoke" maxlength="80" />
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

    <!-- 右：结果 + 历史 -->
    <section class="results">
      <div class="results-head">
        <span class="results-title">{{ result ? '本次执行' : '执行结果' }}</span>
        <CopyButton v-if="result" :text="JSON.stringify(result, null, 2)" label="复制结果 JSON" />
      </div>

      <div class="results-body scroll-y">
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

          <div class="cases">
            <article
              v-for="item in result.executions"
              :key="item.id"
              class="case"
              :class="`case-${item.status}`"
            >
              <button class="case-head" @click="expanded = expanded === item.id ? null : item.id">
                <CircleCheck v-if="item.status === 'passed'" :size="14" class="case-icon ok" />
                <CircleX v-else-if="item.status === 'failed'" :size="14" class="case-icon bad" />
                <TriangleAlert v-else :size="14" class="case-icon unknown" />
                <span class="case-id mono">{{ item.id }}</span>
                <span
                  v-if="item.http_status"
                  class="tag"
                  :class="`tag-${statusTone(item.http_status) === 'neutral' ? '' : statusTone(item.http_status)}`"
                >
                  {{ item.http_status }}
                </span>
                <span v-if="item.response_time_ms != null" class="case-time">
                  {{ formatDuration(item.response_time_ms) }}
                </span>
                <span v-if="item.error_kind" class="case-error">{{ item.error_kind }}</span>
                <span class="toolbar-fill" />
                <span class="case-count">
                  {{ item.assertions.filter((a) => a.passed).length }}/{{ item.assertions.length }}
                </span>
                <ChevronDown
                  :size="14"
                  class="chevron"
                  :class="{ 'chevron-open': expanded === item.id }"
                />
              </button>
              <div v-if="expanded === item.id" class="assertions">
                <div v-for="assertion in item.assertions" :key="assertion.index" class="assertion">
                  <span class="assertion-icon" :class="assertion.passed ? 'ok' : 'bad'">
                    {{ assertion.passed ? '✓' : '✕' }}
                  </span>
                  <span class="assertion-kind">{{ describeAssertionKind(assertion.kind) }}</span>
                  <span class="assertion-idx">#{{ assertion.index + 1 }}</span>
                  <span v-if="assertion.message" class="assertion-msg">
                    {{ assertion.message }}
                  </span>
                </div>
                <div v-if="!item.assertions.length" class="hint">
                  该用例没有回收到断言结果（可能请求未发出）。
                </div>
              </div>
            </article>
          </div>
        </template>

        <EmptyState
          v-else
          :icon="Play"
          title="还没有执行结果"
          description="左侧计划通过后端校验后会交给 Postman CLI 执行；每条断言的通过与否都会落库，可在下方历史里回看。"
        />

        <div v-if="history.length" class="history-block">
          <div class="mini-label">历史执行</div>
          <button
            v-for="item in history"
            :key="item.id"
            class="history-item"
            @click="result = item"
          >
            <span class="history-label truncate">{{ item.label || '(未命名)' }}</span>
            <span class="tag" :class="`tag-${item.status === 'passed' ? 'success' : item.status === 'failed' ? 'warning' : 'danger'}`">
              {{ describeExecutionStatus(item.status) }}
            </span>
            <span class="history-meta">{{ formatRelative(item.created_at) }}</span>
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.plan {
  display: flex;
  height: 100%;
  min-height: 0;
}

.editor {
  width: 56%;
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

.cases {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 12px;
}

.case {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  overflow: hidden;
}

.case-failed {
  border-left: 2px solid var(--warning);
}

.case-error,
.case-unknown {
  border-left: 2px solid var(--danger);
}

.case-passed {
  border-left: 2px solid var(--success);
}

.case-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 11px;
  border: 0;
  background: transparent;
  text-align: left;
}

.case-head:hover {
  background: var(--bg-hover);
}

.case-icon.ok { color: var(--success); }
.case-icon.bad { color: var(--danger); }
.case-icon.unknown { color: var(--warning); }

.case-id {
  font-weight: 600;
  font-size: 12px;
}

.case-time {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.case-error {
  font-size: 11px;
  color: var(--danger);
  font-family: var(--font-mono);
}

.case-count {
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.chevron {
  color: var(--text-tertiary);
  transition: transform 0.15s;
}

.chevron-open {
  transform: rotate(180deg);
}

.assertions {
  padding: 4px 11px 9px 33px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.assertion {
  display: flex;
  align-items: baseline;
  gap: 7px;
  font-size: 11.5px;
}

.assertion-icon {
  font-weight: 700;
  font-family: var(--font-mono);
}

.assertion-icon.ok { color: var(--success); }
.assertion-icon.bad { color: var(--danger); }

.assertion-kind {
  color: var(--text-primary);
  font-weight: 500;
}

.assertion-idx {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.assertion-msg {
  color: var(--danger);
  font-family: var(--font-mono);
  font-size: 10.5px;
  word-break: break-word;
}

.history-block {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.mini-label {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.history-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
}

.history-item:hover {
  background: var(--bg-hover);
}

.history-label {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  font-weight: 500;
}

.history-meta {
  font-size: 10.5px;
  color: var(--text-tertiary);
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
