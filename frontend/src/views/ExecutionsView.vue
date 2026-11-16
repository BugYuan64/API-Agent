<script setup lang="ts">
/** 执行结果回看页：依赖执行列表 API、格式化函数及 ExecutionCases，展示落库执行与可折叠断言。只维护展开状态，不重新执行计划；列表失败通过全局提示反馈。 */

import { onMounted, reactive, ref } from 'vue'
import { ChevronDown, Terminal } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
import ExecutionCases from '@/components/ExecutionCases.vue'
import { describeError } from '@/api/client'
import { listExecutions } from '@/api/endpoints'
import type { Execution } from '@/api/types'
import {
  formatDateTime,
  formatDuration,
} from '@/lib/format'
import { useToastStore } from '@/stores/toasts'
import { presentResult, resultCodeLabel } from '@/lib/results'

/** 执行结果页：与「用例计划」右侧同构，但面向回看而不是即时运行，
 *  因此把每条用例的断言默认折叠，保持长列表可扫读。 */
const items = ref<Execution[]>([])
const loading = ref(false)
const expanded = ref<string | null>(null)
/** 每条执行记录内展开的用例 id；由 ExecutionCases 受控维护。 */
const caseExpanded = reactive<Record<string, string | null>>({})

/** 读取最近执行记录到结果列表；失败通过全局提示反馈，始终解除 loading。 */
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

/** 将执行状态映射为展示色调：通过为 success，断言失败为 warning，其余为 danger；不重新裁决结果。 */
function toneOf(item: Execution) {
  return presentResult(item).tone
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
          后者表示超时、连接、引擎或报告异常，不能据此断言请求未发出。
        </p>
      </div>
      <button class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
    </div>

    <div class="list scroll-y">
      <article v-for="item in items" :key="item.id" class="execution">
        <button class="execution-head" @click="expanded = expanded === item.id ? null : item.id">
          <span class="tag" :class="`tag-${toneOf(item)}`" :title="presentResult(item).message">
            {{ presentResult(item).title }}
            <template v-if="resultCodeLabel(presentResult(item).resultStatus)"> · {{ resultCodeLabel(presentResult(item).resultStatus) }}</template>
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
          <ExecutionCases v-model:expanded="caseExpanded[item.id]" :executions="item.executions" />
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


</style>
