<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, CircleStop, RefreshCw } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import {
  cancelRun,
  getRun,
  listRunAttempts,
  listRunBudget,
  listRunEvents,
} from '@/api/endpoints'
import type { BudgetEntry, DispatchAttempt, Run, RunEvent } from '@/api/types'
import {
  describeRunStatus,
  formatDuration,
  formatDateTime,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { usePolling } from '@/composables/usePolling'

const route = useRoute()
const router = useRouter()
const runId = computed(() => String(route.params.runId ?? ''))

const run = ref<Run | null>(null)
const events = ref<RunEvent[]>([])
const budget = ref<BudgetEntry[]>([])
const attempts = ref<DispatchAttempt[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)
const tab = ref<'timeline' | 'budget' | 'attempts'>('timeline')

/** 预算聚合：账本是 append-only 的，这里按资源汇总成可读余额。 */
const budgetSummary = computed(() => {
  const byResource = new Map<string, { reserved: number; consumed: number; released: number }>()
  for (const entry of budget.value) {
    const bucket = byResource.get(entry.resource) ?? { reserved: 0, consumed: 0, released: 0 }
    if (entry.status === 'reserved') bucket.reserved += entry.amount
    if (entry.status === 'consumed') bucket.consumed += entry.amount
    if (entry.status === 'released') bucket.released += entry.amount
    byResource.set(entry.resource, bucket)
  }
  return [...byResource.entries()].map(([resource, value]) => ({ resource, ...value }))
})

const polling = usePolling({
  fetch: async signal => {
    // Capture identity once: navigation cannot mix A's summary with B's evidence.
    const id = runId.value
    const [current, eventPage, budgetRows, attemptRows] = await Promise.all([
      getRun(id, signal),
      listRunEvents(id, 200, 0, signal),
      listRunBudget(id, signal),
      listRunAttempts(id, signal),
    ])
    return { current, eventPage, budgetRows, attemptRows }
  },
  onResult: ({ current, eventPage, budgetRows, attemptRows }) => {
    run.value = current
    events.value = eventPage.items
    budget.value = budgetRows
    attempts.value = attemptRows
    loading.value = false
    loadError.value = null
  },
  onError: error => {
    loading.value = false
    loadError.value = describeError(error)
    polling.stop()
  },
  isTerminal: ({ current }) => !['queued', 'running', 'cancelling'].includes(current.status),
  intervalMs: 1500,
})

function load() {
  if (!runId.value) return
  loading.value = true
  loadError.value = null
  polling.start()
}

watch(runId, () => {
  polling.stop()
  run.value = null
  events.value = []
  budget.value = []
  attempts.value = []
  load()
})

async function cancel() {
  if (!run.value) return
  try {
    await cancelRun(run.value.id)
    await load()
    useToastStore().push('info', '取消请求已提交', 'worker 会在下一个安全点收尾')
  } catch (error) {
    useToastStore().push('danger', '取消失败', describeError(error))
  }
}

function eventTone(status: string) {
  if (status === 'completed') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'cancelled') return 'neutral'
  return 'info'
}

onMounted(load)
</script>

<template>
  <div class="detail-page">
    <div class="detail-head">
      <button class="btn btn-ghost btn-sm" @click="router.push('/runs')">
        <ArrowLeft :size="13" />
        返回列表
      </button>
      <div class="head-fill" />
      <button class="btn btn-sm" :disabled="loading" @click="load">
        <RefreshCw :size="13" :class="{ spinning: loading }" />
        刷新
      </button>
      <button
        v-if="run && ['queued', 'running'].includes(run.status)"
        class="btn btn-sm btn-danger"
        @click="cancel"
      >
        <CircleStop :size="13" />
        取消任务
      </button>
    </div>

    <div v-if="loadError" class="load-error">
      {{ loadError }}
    </div>

    <template v-if="run">
      <div class="summary">
        <div class="summary-top">
          <span class="tag" :class="`tag-${run.status === 'completed' ? 'success' : run.status === 'failed' ? 'danger' : 'info'}`">
            {{ describeRunStatus(run.status) }}
          </span>
          <span class="mono run-id">{{ run.id }}</span>
        </div>
        <dl class="kv">
          <dt>进度</dt>
          <dd class="mono">{{ run.current_action }} / {{ run.action_count }}</dd>
          <dt>结束原因</dt>
          <dd>{{ run.stop_reason ?? '—' }}</dd>
          <dt>模型模式</dt>
          <dd class="mono">
            {{ run.configured_llm_mode }} / {{ run.actual_llm_status }}
            <span v-if="run.degradation_reason" class="hint">（{{ run.degradation_reason }}）</span>
          </dd>
          <dt>创建</dt>
          <dd>{{ formatDateTime(run.created_at) }}</dd>
          <dt>开始</dt>
          <dd>{{ formatDateTime(run.started_at) }}</dd>
          <dt>结束</dt>
          <dd>
            {{ formatDateTime(run.ended_at) }}
            <span v-if="run.started_at && run.ended_at" class="hint">
              · 用时
              {{ formatDuration(new Date(run.ended_at).getTime() - new Date(run.started_at).getTime()) }}
            </span>
          </dd>
        </dl>
      </div>

      <div class="tabs">
        <button
          v-for="item in [
            { key: 'timeline', label: `事件时间线 ${events.length}` },
            { key: 'budget', label: `预算账本 ${budget.length}` },
            { key: 'attempts', label: `派发尝试 ${attempts.length}` },
          ]"
          :key="item.key"
          class="tab"
          :class="{ 'tab-active': tab === item.key }"
          @click="tab = item.key as typeof tab"
        >
          {{ item.label }}
        </button>
      </div>

      <div v-if="tab === 'timeline'" class="panel-body">
        <div v-for="event in events" :key="event.id" class="event">
          <div class="event-head">
            <span class="event-seq mono">#{{ event.sequence }}</span>
            <span class="tag" :class="`tag-${eventTone(event.status)}`">{{ event.status }}</span>
            <span class="event-node mono">{{ event.node }} · {{ event.event_type }}</span>
            <span v-if="event.tool_name" class="tag">{{ event.tool_name }}</span>
            <div class="head-fill" />
            <span class="hint">{{ formatDateTime(event.created_at) }}</span>
          </div>
          <p v-if="event.decision_reason" class="event-reason">{{ event.decision_reason }}</p>
          <div v-if="Object.keys(event.observation_summary).length" class="event-json mono">
            {{ JSON.stringify(event.observation_summary) }}
          </div>
          <div v-if="Object.keys(event.budget_summary).length" class="event-json mono">
            预算 {{ JSON.stringify(event.budget_summary) }}
          </div>
        </div>
        <EmptyState v-if="!events.length" title="还没有事件" description="任务被 worker 认领后会写入第一条事件。" />
      </div>

      <div v-else-if="tab === 'budget'" class="panel-body">
        <div v-if="budgetSummary.length" class="budget-summary">
          <div v-for="item in budgetSummary" :key="item.resource" class="budget-card">
            <span class="budget-resource mono">{{ item.resource }}</span>
            <div class="budget-values">
              <span>预留 <strong>{{ item.reserved }}</strong></span>
              <span>消耗 <strong>{{ item.consumed }}</strong></span>
              <span>释放 <strong>{{ item.released }}</strong></span>
            </div>
          </div>
        </div>
        <table v-if="budget.length" class="table">
          <thead>
            <tr>
              <th>序号</th>
              <th>资源</th>
              <th>动作</th>
              <th>数量</th>
              <th>状态</th>
              <th>关联</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(entry, index) in budget" :key="entry.id">
              <td class="mono">{{ index + 1 }}</td>
              <td class="mono">{{ entry.resource }}</td>
              <td>{{ entry.action }}</td>
              <td class="mono">{{ entry.amount }}</td>
              <td>
                <span class="tag" :class="`tag-${entry.status === 'consumed' ? 'success' : entry.status === 'released' ? 'neutral' : 'info'}`">
                  {{ entry.status }}
                </span>
              </td>
              <td class="mono hint">{{ entry.related_id?.slice(0, 8) ?? '—' }}</td>
              <td class="hint">{{ formatDateTime(entry.created_at) }}</td>
            </tr>
          </tbody>
        </table>
        <EmptyState v-else title="还没有预算记录" description="预算在动作派发时原子预留，结束后结算为消耗或释放。" />
      </div>

      <div v-else class="panel-body">
        <table v-if="attempts.length" class="table">
          <thead>
            <tr>
              <th>序号</th>
              <th>类型</th>
              <th>状态</th>
              <th>错误类别</th>
              <th>开始</th>
              <th>结束</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="attempt in attempts" :key="attempt.id">
              <td class="mono">{{ attempt.sequence }}</td>
              <td>{{ attempt.kind }}</td>
              <td>
                <span class="tag" :class="`tag-${attempt.status === 'completed' ? 'success' : attempt.status === 'unknown' ? 'warning' : 'neutral'}`">
                  {{ attempt.status }}
                </span>
              </td>
              <td class="mono hint">{{ attempt.error_kind ?? '—' }}</td>
              <td class="hint">{{ formatDateTime(attempt.started_at) }}</td>
              <td class="hint">{{ formatDateTime(attempt.ended_at) }}</td>
            </tr>
          </tbody>
        </table>
        <EmptyState v-else title="还没有派发记录" description="每次动作派发都会留一条尝试记录，用于区分「跑了但失败」与「根本没跑」。" />
      </div>
    </template>

    <EmptyState
      v-else-if="!loading"
      title="任务不存在"
      description="该 ID 在数据库里找不到，可能已被清理。"
    />
  </div>
</template>

<style scoped>
.detail-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  height: 100%;
}

.detail-head {
  display: flex;
  align-items: center;
  gap: 7px;
  flex: none;
}

.head-fill {
  flex: 1;
}

.load-error {
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 12px;
}

.summary {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  padding: 13px 15px;
  flex: none;
}

.summary-top {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.run-id {
  font-size: 11.5px;
  color: var(--text-tertiary);
}

.kv {
  display: grid;
  grid-template-columns: 84px 1fr 84px 1fr;
  gap: 7px 12px;
  margin: 0;
  font-size: 12px;
}

.kv dt {
  color: var(--text-tertiary);
  font-size: 11.5px;
}

.kv dd {
  margin: 0;
  word-break: break-word;
}

.tabs {
  display: flex;
  gap: 2px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.tab {
  height: 32px;
  padding: 0 11px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12.5px;
}

.tab-active {
  color: var(--accent);
  border-bottom-color: var(--accent);
  font-weight: 600;
}

.panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.event {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  padding: 9px 11px;
}

.event-head {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
}

.event-seq {
  color: var(--text-tertiary);
}

.event-node {
  color: var(--text-secondary);
}

.event-reason {
  margin: 6px 0 0;
  font-size: 11.5px;
  color: var(--text-secondary);
  line-height: 1.7;
}

.event-json {
  margin-top: 5px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  font-size: 11px;
  color: var(--text-secondary);
  word-break: break-all;
}

.budget-summary {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.budget-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 9px 11px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.budget-resource {
  font-size: 11px;
  color: var(--text-tertiary);
}

.budget-values {
  display: flex;
  gap: 10px;
  font-size: 11.5px;
  color: var(--text-secondary);
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
