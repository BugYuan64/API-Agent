<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { ChevronRight } from 'lucide-vue-next'
import ResponseDetails from './ResponseDetails.vue'
import type { TaskRecord } from '@/api/tasks'
import type { ConsoleSseEvent } from '@/api/types'
import { describeAssertionKind } from '@/lib/format'
import { assertionVerdict, caseVerdict } from '@/lib/results'
import { activeTaskWait, buildTaskResultTree, caseBodyText, savedRequestData, sseDataText, taskCaseLabel, taskCaseMessage, taskCaseProtocol, taskResponsePayload } from '@/lib/taskResults'

const props = defineProps<{
  task: TaskRecord
  streamWait: ReturnType<typeof activeTaskWait>
  liveEvents?: ConsoleSseEvent[]
  liveEventsSeen?: number
}>()
const openGroups = reactive(new Set<string>())
const openCases = reactive(new Set<number>())
const liveSseIndex = computed(() => props.task.active_stream === 'sse' ? props.streamWait?.index : undefined)
const livePreview = computed(() => ({ events: props.liveEvents ?? [], total_events: props.liveEventsSeen }))
const tree = computed(() => buildTaskResultTree(props.task.snapshot, props.task.results).map(group => ({
  ...group,
  streamWait: group.cases.some(item => item.index === props.streamWait?.index) ? props.streamWait : null,
  cases: group.cases.map(item => {
    const payload = taskResponsePayload(item.result)
    const liveSse = item.index === liveSseIndex.value
    return {
      ...item,
      payload,
      liveSse,
      sse: liveSse ? livePreview.value : payload.sse,
      verdict: item.result ? caseVerdict(item.result.status, item.result.reason)
        : props.streamWait?.index === item.index ? { label: props.streamWait.label, tone: 'info', symbol: '◷' }
        : props.task.status === 'running' && props.task.current_index === item.index
          ? { label: '执行中', tone: 'info', symbol: '◷' }
          : { label: ['queued', 'running'].includes(props.task.status) ? '待执行' : '尚无结果', tone: 'neutral', symbol: '—' },
    }
  }),
})))

/** Open the receiving case once when its SSE stream starts and leave its result visible. */
watch(liveSseIndex, index => {
  if (index == null) return
  const group = tree.value.find(item => item.cases.some(testCase => testCase.index === index))
  if (group) openGroups.add(group.key)
  openCases.add(index)
}, { immediate: true })

function trackOpen<Key>(items: Set<Key>, key: Key, event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open) items.add(key)
  else items.delete(key)
}

</script>

<template>
  <p v-if="!tree.length" class="muted">任务没有可展示的接口或用例。</p>
  <ul v-else class="operation-tree" aria-label="接口测试结果">
    <li v-for="group in tree" :key="group.key">
      <details class="operation" :open="openGroups.has(group.key)" @toggle="trackOpen(openGroups, group.key, $event)">
        <summary class="operation-head">
          <ChevronRight class="chevron" :size="15" aria-hidden="true" />
          <span class="node-copy">
            <span class="route"><span class="tag tag-accent">{{ group.method }}</span><code>{{ group.path }}</code></span>
            <span v-if="group.example && group.example.name !== group.path && group.example.name !== group.example.path" class="muted">{{ group.example.name }}</span>
          </span>
          <span class="operation-meta">
            <span v-if="group.streamWait" class="tag tag-info verdict">{{ group.streamWait.label }} <strong>1</strong></span>
            <span v-for="outcome in group.outcomes" :key="outcome.status" class="tag verdict" :class="`tag-${outcome.tone}`">
              <span aria-hidden="true">{{ outcome.symbol }}</span>{{ outcome.label }} <strong>{{ outcome.count }}</strong>
            </span>
            <span class="tag" :class="group.protocol === 'http' ? 'tag-info' : 'tag-accent'">{{ group.protocol.toUpperCase() }}</span>
            <span class="muted total">{{ group.cases.length }} 条用例</span>
          </span>
        </summary>
        <div class="operation-body">
          <details class="api-format">
            <summary class="api-format-head">
              <ChevronRight class="chevron" :size="13" aria-hidden="true" />
              API 格式
            </summary>
            <div class="api-format-body">
              <p class="signature"><code>{{ group.protocol.toUpperCase() }} · {{ group.method }} {{ group.path }}</code></p>
              <template v-if="group.example">
                <p class="muted">接口请求示例与预期断言</p>
                <pre>{{ JSON.stringify(savedRequestData(group.example), null, 2) }}</pre>
              </template>
            </div>
          </details>
          <ul class="branches" aria-label="测试用例">
            <li v-for="(item, position) in group.cases" :key="item.index">
              <details class="case outcome-card" :class="`outcome-${item.verdict.tone}`" :open="openCases.has(item.index)" @toggle="trackOpen(openCases, item.index, $event)">
                <summary class="case-head">
                  <ChevronRight class="chevron" :size="15" aria-hidden="true" />
                  <span class="node-copy">
                    <span class="case-name"><span class="muted">#{{ position + 1 }}</span> {{ item.input.name }} <span class="case-source muted">{{ taskCaseLabel(item.input, group.example) }}</span></span>
                  </span>
                  <span class="tag verdict" :class="`tag-${item.verdict.tone}`"><span aria-hidden="true">{{ item.verdict.symbol }}</span>{{ item.verdict.label }}</span>
                </summary>
                <div v-if="openCases.has(item.index)" class="case-body">
                  <section class="detail">
                    <h4>测试数据</h4>
                    <p class="signature"><code>{{ taskCaseProtocol(item.input, item.result).toUpperCase() }} · {{ item.input.method }} {{ item.input.path }}</code></p>
                    <pre v-if="caseBodyText(item.input)">{{ caseBodyText(item.input) }}</pre>
                    <pre v-if="item.input.messages?.length">{{ item.input.messages.join('\n') }}</pre>
                  </section>
                  <section class="detail">
                    <header class="result-head">
                      <h4>测试结果</h4>
                      <ResponseDetails :data="item.result ? { ...item.result, request: item.input } : item.liveSse ? { request: item.input, response: { sse: item.sse }, pending: true } : null"
                        note="详情使用本次用例记录中的请求、断言和响应。正文或事件可能按记录上限截断。" />
                    </header>
                    <div v-if="item.sse" class="case-response" aria-label="SSE 响应正文" :aria-busy="item.liveSse">
                      <pre v-if="item.sse.events.length">{{ sseDataText(item.sse.events) }}</pre>
                      <p v-else class="muted">{{ item.liveSse ? '正在等待首条 SSE 事件…' : '未收到 SSE 事件。' }}</p>
                      <p v-if="(item.sse.total_events ?? 0) > item.sse.events.length" class="muted">显示前 {{ item.sse.events.length }} 条事件。</p>
                    </div>
                    <template v-else-if="item.result">
                      <pre v-if="item.payload.body" aria-label="响应正文">{{ item.payload.body }}</pre>
                      <div v-else-if="item.payload.messages?.length" aria-label="接收的 WebSocket 消息">
                        <pre v-for="event in item.payload.messages" :key="event.sequence">{{ event.data }}</pre>
                      </div>
                    </template>
                    <template v-if="item.result">
                      <p v-if="taskCaseMessage(item.result)" class="reason">{{ taskCaseMessage(item.result) }}</p>
                      <p v-if="!item.payload.sse && !item.result.assertions.length" class="muted">没有记录断言结果。</p>
                      <div v-for="(assertion, index) in item.payload.sse ? [] : item.result.assertions" :key="index" class="assertion-row">
                        <span class="tag verdict" :class="`tag-${assertionVerdict(assertion.passed).tone}`">
                          <span aria-hidden="true">{{ assertionVerdict(assertion.passed).symbol }}</span>{{ assertionVerdict(assertion.passed).label }}
                        </span>
                        <div>
                          <strong>{{ describeAssertionKind(assertion.kind) }}</strong> <code v-if="assertion.pointer">{{ assertion.pointer }}</code>
                          <div class="muted">期望 <code>{{ JSON.stringify(assertion.expected) ?? '—' }}</code> · 实际 <code>{{ JSON.stringify(assertion.actual) ?? '—' }}</code></div>
                        </div>
                      </div>
                    </template>
                    <p v-else-if="!item.liveSse" class="muted">{{ item.verdict.label }}</p>
                  </section>
                </div>
              </details>
            </li>
          </ul>
        </div>
      </details>
    </li>
  </ul>
</template>

<style scoped>
.operation-tree, .branches { list-style: none; margin: 0; padding: 0; }
.operation-tree { display: grid; gap: 8px; }
.operation { background: var(--bg-surface); border: 1px solid var(--accent-border); border-radius: var(--radius-md); }
.operation-head, .case-head { display: flex; align-items: center; gap: 6px; padding: 4px 8px; min-height: 28px; cursor: pointer; list-style: none; border-radius: inherit; }
.operation-head {
  flex-wrap: wrap;
  border-inline-start: 4px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, var(--accent-soft));
}
.operation-head:hover { background: color-mix(in srgb, var(--accent) 16%, var(--accent-soft)); }
.operation[open] > .operation-head { border-end-start-radius: 0; border-end-end-radius: 0; }
.operation-head .node-copy { flex-basis: 160px; }
.operation-meta { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 6px; min-width: 0; margin-inline-start: auto; }
.operation-head::-webkit-details-marker, .api-format-head::-webkit-details-marker, .case-head::-webkit-details-marker { display: none; }
.case-head:hover, .case[open] > summary { background: color-mix(in srgb, var(--outcome-ink) 10%, transparent); }
.chevron { flex: none; color: var(--text-secondary); }
.operation-head > .chevron { color: var(--accent); }
.api-format-head > .chevron { color: inherit; }
.case-head > .chevron { color: var(--outcome-ink); }
details[open] > summary > .chevron { transform: rotate(90deg); }
.node-copy { flex: 1; min-width: 0; display: flex; align-items: baseline; flex-wrap: wrap; gap: 2px 6px; overflow-wrap: anywhere; }
.route { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.route code { min-width: 0; font-weight: 600; }
.case-name { font-weight: 600; }
.case-name .muted { font-variant-numeric: tabular-nums; }
.case-source { font-weight: 400; margin-inline-start: 4px; }
.operation-tree :deep(.tag) { height: 18px; padding-inline: 5px; }
.case.outcome-card {
  margin: 0;
  border-color: color-mix(in srgb, var(--outcome-rail) 45%, var(--border-subtle));
  border-inline-start: 4px solid var(--outcome-rail);
  background: color-mix(in srgb, var(--outcome-surface) 76%, var(--bg-surface));
}
.total { white-space: nowrap; }
.operation-body { padding: 8px 10px 9px; border-top: 1px solid var(--accent-border); }
.api-format { margin-inline-start: 10px; margin-block-end: 8px; font-size: 12px; }
.api-format-head {
  display: flex;
  align-items: center;
  gap: 4px;
  inline-size: fit-content;
  min-height: 24px;
  padding: 2px 7px;
  list-style: none;
  cursor: pointer;
  border: 1px solid color-mix(in srgb, var(--info) 52%, var(--info-border));
  border-radius: var(--radius-sm);
  background: var(--info-soft);
  color: var(--info);
  font-weight: 650;
}
.api-format-head:hover { background: color-mix(in srgb, var(--info) 10%, var(--info-soft)); }
.api-format-body {
  margin-block-start: 4px;
  padding: 5px 8px 7px;
  background: color-mix(in srgb, var(--info-soft) 60%, var(--bg-surface));
  border: 1px solid var(--info-border);
  border-inline-start: 3px solid var(--info);
  border-radius: var(--radius-sm);
}
.api-format-body > p { margin: 0 0 5px; }
h4 { margin: 0; font-size: 12px; }
.branches {
  --branch-rail: color-mix(in srgb, var(--accent) 42%, var(--border-default));
  --branch-indent: 16px;
  margin-inline-start: 15px;
  padding-inline-start: var(--branch-indent);
  border-inline-start: 1px solid var(--branch-rail);
}
.branches > li { position: relative; }
.branches > li + li { margin-top: 5px; }
.branches > li::before {
  content: '';
  position: absolute;
  inline-size: var(--branch-indent);
  inset-block-start: 14px;
  inset-inline-start: calc(-1 * var(--branch-indent));
  border-block-start: 1px solid var(--branch-rail);
}
.case-body { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 210px), 1fr)); gap: 6px; padding: 6px; border-top: 1px solid var(--border-subtle); }
.detail { min-width: 0; overflow-wrap: anywhere; }
.result-head { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.signature { margin: 6px 0; overflow-wrap: anywhere; }
.muted { color: var(--text-secondary); font-size: 12px; }
.reason { margin: 6px 0; color: var(--outcome-ink); }
.assertion-row { display: flex; align-items: flex-start; gap: 6px; padding: 6px 0; font-size: 12px; overflow-wrap: anywhere; }
pre { margin: 4px 0 0; padding: 6px; max-height: 180px; overflow: auto; background: var(--bg-canvas); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
.api-format pre { max-height: 120px; }
</style>
