<script setup lang="ts">
/** 用例与断言结果组件：接收 CaseResult 列表及空态文案，依赖展示格式化函数和图标库。通过 expanded 模型交由父级控制展开项；只展示已返回证据，不重新执行断言。 */

/**
 * 逐用例 + 逐断言的结果列表（可折叠）。
 *
 * 作用：用例计划页与执行结果页此前各写了一份几乎逐行相同的
 * 「用例卡片 → 断言明细」标记与样式（含同样的 case/assertion CSS 约 120 行）。
 * 抽成一个受控组件：展开状态由父级持有（`v-model:expanded`），
 * 这样两页可以各自决定「默认展开第一条」还是「全部折叠」。
 */
import { ChevronDown, CircleCheck, CircleX, TriangleAlert } from 'lucide-vue-next'
import type { CaseResult } from '@/api/types'
import { describeAssertionKind, formatDuration, statusTone } from '@/lib/format'
import { assertionVerdict, caseVerdict, presentResult, resultCodeLabel } from '@/lib/results'

const props = withDefaults(
  defineProps<{
    executions: CaseResult[]
    /** 空列表时的说明文案。 */
    emptyHint?: string
  }>(),
  { emptyHint: '没有回收到用例结果（可能请求未发出）。' },
)

const expanded = defineModel<string | null>('expanded', { default: null })

/** 切换指定用例的展开 ID；再次点击当前项收起，变更通过 expanded 模型同步父级。 */
function toggle(id: string): void {
  expanded.value = expanded.value === id ? null : id
}

/** 统计给定用例中 passed 为真值的断言数量；未执行或失败断言不计入通过数。 */
function passedCount(item: CaseResult): number {
  return item.assertions.filter((a) => a.passed).length
}

/** 把 HTTP 状态码映射成 tag 色调；neutral 不带后缀（用默认样式）。 */
function httpTone(status: number | null): string {
  const tone = statusTone(status ?? undefined)
  return tone === 'neutral' ? '' : tone
}
</script>

<template>
  <div class="cases">
    <article
      v-for="item in props.executions"
      :key="item.id"
      class="case outcome-card"
      :class="`outcome-${caseVerdict(item.status).tone}`"
    >
      <button type="button" class="case-head" :aria-expanded="expanded === item.id" @click="toggle(item.id)">
        <CircleCheck v-if="item.status === 'passed'" :size="14" class="case-icon ok" />
        <CircleX v-else-if="item.status === 'failed'" :size="14" class="case-icon bad" />
        <TriangleAlert v-else :size="14" class="case-icon unknown" />
        <span class="case-id mono">{{ item.id }}</span>
        <span v-if="item.http_status" class="tag" :class="`tag-${httpTone(item.http_status)}`">
          {{ item.http_status }}
        </span>
        <span v-if="item.response_time_ms != null" class="case-time">
          {{ formatDuration(item.response_time_ms) }}
        </span>
        <span class="tag verdict" :class="`tag-${caseVerdict(item.status).tone}`">
          {{ presentResult(item).title }}
          <template v-if="resultCodeLabel(presentResult(item).resultStatus)"> · {{ resultCodeLabel(presentResult(item).resultStatus) }}</template>
        </span>
        <span v-if="item.error_kind" class="case-error" :title="presentResult(item).message">{{ item.error_kind }}</span>
        <span class="fill" />
        <span class="case-count">
          {{ passedCount(item) }}/{{ item.assertions.length }}
        </span>
        <ChevronDown
          :size="14"
          class="chevron"
          :class="{ 'chevron-open': expanded === item.id }"
        />
      </button>
      <div v-if="expanded === item.id" class="assertions">
        <div v-for="assertion in item.assertions" :key="assertion.index" class="assertion">
          <span class="assertion-icon" :class="assertion.passed === true ? 'ok' : assertion.passed === false ? 'bad' : 'unknown'"
            :title="assertionVerdict(assertion.passed).label">
            {{ assertionVerdict(assertion.passed).symbol }}
          </span>
          <span class="assertion-kind">{{ describeAssertionKind(assertion.kind) }}</span>
          <span class="assertion-idx">#{{ assertion.index + 1 }}</span>
          <span v-if="assertion.passed == null" class="hint">未评估</span>
          <span v-if="assertion.message" class="assertion-msg">{{ assertion.message }}</span>
        </div>
        <div v-if="!item.assertions.length" class="hint">
          该用例没有回收到断言结果（可能请求未发出）。
        </div>
      </div>
    </article>
    <p v-if="!props.executions.length" class="hint">{{ props.emptyHint }}</p>
  </div>
</template>

<style scoped>
.cases {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.case {
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.assertion-icon.unknown { color: var(--text-tertiary); }

.case-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 12px;
  border: 0;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.case-head:hover {
  background: var(--bg-hover);
}

.case-icon.ok {
  color: var(--success);
}

.case-icon.bad {
  color: var(--danger);
}

.case-icon.unknown {
  color: var(--outcome-ink);
}

.case-id {
  font-size: 11.5px;
  font-weight: 600;
}

.case-time,
.case-count {
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.case-error {
  font-size: 11px;
  color: var(--danger);
}

.fill {
  flex: 1;
}

.chevron {
  color: var(--text-tertiary);
  transition: transform 0.15s;
}

.chevron-open {
  transform: rotate(180deg);
}

.assertions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 12px 10px 34px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-canvas);
}

.assertion {
  display: flex;
  align-items: baseline;
  gap: 7px;
  font-size: 11.5px;
}

.assertion-icon {
  font-weight: 700;
}

.assertion-icon.ok {
  color: var(--success);
}

.assertion-icon.bad {
  color: var(--danger);
}

.assertion-kind {
  color: var(--text-secondary);
}

.assertion-idx {
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.assertion-msg {
  color: var(--text-tertiary);
  line-height: 1.5;
  word-break: break-word;
}
</style>
