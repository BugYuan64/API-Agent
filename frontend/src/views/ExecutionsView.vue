<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ChevronDown, CircleCheck, CircleX, Terminal, TriangleAlert } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
import { describeError } from '@/api/client'
import { listExecutions } from '@/api/endpoints'
import type { Execution } from '@/api/types'
import {
  describeAssertionKind,
  describeExecutionStatus,
  formatDateTime,
  formatDuration,
  statusTone,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'

/** 执行结果页：与「用例计划」右侧同构，但面向回看而不是即时运行，
 *  因此把每条用例的断言默认折叠，保持长列表可扫读。 */
const items = ref<Execution[]>([])
const loading = ref(false)
const expanded = ref<string | null>(null)
const openCases = ref<Set<string>>(new Set())

function toggleCase(key: string) {
  const next = new Set(openCases.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  openCases.value = next
}

async function load() {
  loading.value = true
  try {
    const page = await listExecutions(60)
    items.value = page.items
  } catch (error) {
    useToastStore().push('danger', '加载执行结果失败', describeError(error))
  } finally {
    loading.value = false
  }
}

function toneOf(status: string) {
  if (status === 'passed') return 'success'
  if (status === 'failed') return 'warning'
  return 'danger'
}

onMounted(load)
</script>

<template>
  <div class="executions">
    <div class="page-head">
      <div>
        <h2 class="page-title">执行结果</h2>
        <p class="page-sub">
          每次执行的逐用例与逐断言结果都会落库。断言失败与执行错误是两件事：前者说明目标行为不符预期，
          后者说明请求没跑起来（CLI 缺失、连接失败、报告不完整等）。
        </p>
      </div>
      <button class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="list scroll-y">
      <article v-for="item in items" :key="item.id" class="execution">
        <button class="execution-head" @click="expanded = expanded === item.id ? null : item.id">
          <span class="tag" :class="`tag-${toneOf(item.status)}`">
            {{ describeExecutionStatus(item.status) }}
          </span>
          <span class="execution-label truncate">{{ item.label || '(未命名)' }}</span>
          <span class="hint mono">{{ item.observed_attempts }}/{{ item.reserved_requests }} 请求</span>
          <span class="hint mono">{{ formatDuration(item.duration_ms) }}</span>
          <span v-if="item.error_kind" class="hint mono">{{ item.error_kind }}</span>
          <span class="head-fill" />
          <span class="hint">{{ formatDateTime(item.created_at) }}</span>
          <ChevronDown
            :size="14"
            class="chevron"
            :class="{ 'chevron-open': expanded === item.id }"
          />
        </button>

        <div v-if="expanded === item.id" class="execution-body">
          <div v-for="caseItem in item.executions" :key="caseItem.id" class="case">
            <button class="case-head" @click="toggleCase(`${item.id}:${caseItem.id}`)">
              <CircleCheck v-if="caseItem.status === 'passed'" :size="13" class="icon ok" />
              <CircleX v-else-if="caseItem.status === 'failed'" :size="13" class="icon bad" />
              <TriangleAlert v-else :size="13" class="icon unknown" />
              <span class="mono case-id">{{ caseItem.id }}</span>
              <span
                v-if="caseItem.http_status"
                class="tag"
                :class="`tag-${statusTone(caseItem.http_status) === 'neutral' ? '' : statusTone(caseItem.http_status)}`"
              >
                {{ caseItem.http_status }}
              </span>
              <span v-if="caseItem.response_time_ms != null" class="hint mono">
                {{ formatDuration(caseItem.response_time_ms) }}
              </span>
              <span v-if="caseItem.error_kind" class="case-error mono">{{ caseItem.error_kind }}</span>
              <span class="head-fill" />
              <span class="hint">
                {{ caseItem.assertions.filter((a) => a.passed).length }}/{{ caseItem.assertions.length }}
                断言通过
              </span>
            </button>
            <div v-if="openCases.has(`${item.id}:${caseItem.id}`) || item.executions.length === 1" class="assertions">
              <div v-for="assertion in caseItem.assertions" :key="assertion.index" class="assertion">
                <span class="assertion-mark" :class="assertion.passed ? 'ok' : 'bad'">
                  {{ assertion.passed ? '✓' : '✕' }}
                </span>
                <span class="assertion-kind">{{ describeAssertionKind(assertion.kind) }}</span>
                <span class="hint mono">#{{ assertion.index + 1 }}</span>
                <span v-if="assertion.message" class="assertion-msg">{{ assertion.message }}</span>
              </div>
            </div>
          </div>
        </div>
      </article>

      <EmptyState
        v-if="!items.length && !loading"
        :icon="Terminal"
        title="还没有执行记录"
        description="在「用例计划」页执行一份计划后，这里会保留每次运行的完整证据。"
      />
    </div>
  </div>
</template>

<style scoped>
.executions {
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
  padding: 16px 16px 12px;
  flex: none;
}

.page-title {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.015em;
}

.page-sub {
  margin: 3px 0 0;
  font-size: 11.5px;
  color: var(--text-tertiary);
  max-width: 88ch;
  line-height: 1.7;
}

.list {
  flex: 1;
  min-height: 0;
  padding: 0 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.execution {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  overflow: hidden;
}

.execution-head {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  padding: 9px 11px;
  border: 0;
  background: transparent;
  text-align: left;
}

.execution-head:hover {
  background: var(--bg-hover);
}

.execution-label {
  font-size: 12.5px;
  font-weight: 600;
  max-width: 26ch;
}

.head-fill {
  flex: 1;
}

.chevron {
  color: var(--text-tertiary);
  transition: transform 0.15s;
}

.chevron-open {
  transform: rotate(180deg);
}

.execution-body {
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  padding: 6px 9px 9px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.case {
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  overflow: hidden;
}

.case-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 9px;
  border: 0;
  background: transparent;
  text-align: left;
  font-size: 11.5px;
}

.case-head:hover {
  background: var(--bg-hover);
}

.icon.ok { color: var(--success); }
.icon.bad { color: var(--danger); }
.icon.unknown { color: var(--warning); }

.case-id {
  font-weight: 600;
}

.case-error {
  color: var(--danger);
}

.assertions {
  padding: 4px 9px 8px 28px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.assertion {
  display: flex;
  align-items: baseline;
  gap: 7px;
  font-size: 11.5px;
}

.assertion-mark {
  font-family: var(--font-mono);
  font-weight: 700;
}

.assertion-mark.ok { color: var(--success); }
.assertion-mark.bad { color: var(--danger); }

.assertion-kind {
  color: var(--text-primary);
}

.assertion-msg {
  color: var(--danger);
  font-family: var(--font-mono);
  font-size: 10.5px;
  word-break: break-word;
}
</style>
