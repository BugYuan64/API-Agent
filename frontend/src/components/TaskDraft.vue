<script setup lang="ts">
/** Review and edit imported or historical requests before starting a new run. */
import { computed, nextTick, ref, watch } from 'vue'
import { Trash2 } from 'lucide-vue-next'
import type { Catalog, TaskSnapshot, TestCase } from '@/api/tasks'
import { buildTaskDraftGroups, caseBodyText, parseTaskCaseId, type DraftLocation } from '@/lib/taskResults'

const props = defineProps<{
  catalog?: Catalog
  snapshot?: TaskSnapshot
  editing: boolean
  disabled?: boolean
  selecting?: boolean
  selected?: string[]
  readonly?: boolean
  focusedCaseId?: string | null
  focusedCaseIndex?: number | null
}>()
const emit = defineEmits<{
  'update:editing': [value: boolean]
  change: [location: DraftLocation, value: TestCase]
  remove: [index: number]
  toggle: [key: string]
}>()

const rootRef = ref<HTMLElement | null>(null)
const isLocked = computed(() => Boolean(props.disabled || props.readonly))

const resolvedFocusedCaseIndex = computed<number | null>(() => {
  if (typeof props.focusedCaseIndex === 'number' && Number.isInteger(props.focusedCaseIndex) && props.focusedCaseIndex >= 0) {
    return props.focusedCaseIndex
  }
  if (props.focusedCaseId) {
    const parsed = parseTaskCaseId(props.focusedCaseId)
    if (parsed) return parsed.caseIndex
    const trimmed = props.focusedCaseId.trim()
    if (/^\d+$/.test(trimmed)) return Number(trimmed)
  }
  return null
})

const groups = computed(() => {
  let flatIndex = 0
  return buildTaskDraftGroups(props.catalog, props.snapshot).map(group => ({
    ...group,
    cases: group.cases.map(entry => {
      const caseIndex = entry.location.kind === 'history' ? entry.location.index : flatIndex++
      return { ...entry, caseIndex }
    }),
  }))
})

const edit = ref<{ groupKey: string; index: number; template: string; location: DraftLocation; input: TestCase } | null>(null)
const name = ref('')
const path = ref('')
const body = ref('')
const headers = ref('')
const messages = ref('')
const assertions = ref('')
const extract = ref('')
const error = ref('')

function isCaseFocused(caseIndex: number): boolean {
  return resolvedFocusedCaseIndex.value !== null && resolvedFocusedCaseIndex.value === caseIndex
}

function isGroupFocused(cases: readonly { caseIndex: number }[]): boolean {
  return resolvedFocusedCaseIndex.value !== null && cases.some(item => item.caseIndex === resolvedFocusedCaseIndex.value)
}

function headersText(input: TestCase): string {
  return Object.keys(input.headers ?? {}).length ? JSON.stringify(input.headers, null, 2) : ''
}

function assertionsText(input: TestCase): string {
  return input.assertions?.length ? JSON.stringify(input.assertions, null, 2) : ''
}

function extractText(input: TestCase): string {
  return input.extract?.length ? JSON.stringify(input.extract, null, 2) : ''
}

/** Use separate fields so changing a body never requires editing the whole API schema. */
function beginEdit(groupKey: string, index: number, template: string, location: DraftLocation, input: TestCase) {
  if (isLocked.value) return
  edit.value = { groupKey, index, template, location, input }
  name.value = input.name
  path.value = input.path
  body.value = caseBodyText(input)
  headers.value = JSON.stringify(input.headers ?? {}, null, 2)
  messages.value = input.messages?.join('\n') ?? ''
  assertions.value = JSON.stringify(input.assertions, null, 2)
  extract.value = JSON.stringify(input.extract ?? [], null, 2)
  error.value = ''
  emit('update:editing', true)
}

function closeEditor() {
  edit.value = null
  emit('update:editing', false)
}

watch(() => props.readonly, locked => {
  if (locked && edit.value) closeEditor()
})

watch(resolvedFocusedCaseIndex, focused => {
  if (focused == null) return
  void nextTick(() => {
    const root = rootRef.value
    if (!root || typeof root.querySelector !== 'function') return
    const target = root.querySelector<HTMLElement>('[data-focused="true"]')
    target?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
  })
}, { immediate: true })

/** Keep the reviewed route and transport identity attached to the original operation. */
function saveEdit() {
  const current = edit.value
  if (!current || isLocked.value) return
  try {
    const nextPath = path.value.trim()
    const routePattern = current.template.split(/(\{[^{}]+\})/).map(part => part.startsWith('{')
      ? '(?:[^/{}]+|\\{\\{[A-Za-z_][A-Za-z0-9_]{0,63}\\}\\})+' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('')
    const route = nextPath.split('?')[0]
    if (!name.value.trim() || !new RegExp(`^${routePattern}$`).test(route) || !nextPath.startsWith('/') ||
        nextPath.startsWith('//') || /[\s#\\]/.test(nextPath) || route.split('/').some(part => {
          const decoded = decodeURIComponent(part).replace(/\{\{[A-Za-z_][A-Za-z0-9_]{0,63}\}\}/g, 'var')
          return decoded === '.' || decoded === '..' || /[/\\{}]/.test(decoded)
        })) throw new Error('请填写用例名称和所属接口的具体路径。')
    const nextHeaders = JSON.parse(headers.value || '{}')
    const nextChecks = JSON.parse(assertions.value || '[]')
    const nextExtract = JSON.parse(extract.value || '[]')
    if (!nextHeaders || typeof nextHeaders !== 'object' || Array.isArray(nextHeaders) ||
        Object.values(nextHeaders).some(value => typeof value !== 'string') ||
        !Array.isArray(nextChecks) || nextChecks.length > 20 || nextChecks.some(check => {
          if (!check || typeof check !== 'object') return true
          if (check.kind === 'status') return !Number.isInteger(check.expected) || check.expected < 100 || check.expected > 599
          if (check.kind === 'event_count') return !Number.isInteger(check.expected) || check.expected < 0
          if (['contains', 'message_contains'].includes(check.kind)) return typeof check.expected !== 'string'
          return check.kind !== 'json_equals' || (check.pointer != null &&
            (typeof check.pointer !== 'string' || !/^(?:\/(?:[^~]|~[01])*)?$/.test(check.pointer)))
        }) ||
        !Array.isArray(nextExtract) || nextExtract.length > 10 || nextExtract.some(rule => {
          if (!rule || typeof rule !== 'object') return true
          if (typeof rule.name !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(rule.name)) return true
          const source = rule.source ?? 'json'
          if (!['json', 'header', 'status'].includes(source)) return true
          if (source === 'json') {
            return typeof rule.pointer !== 'string' || !/^(?:\/(?:[^~]|~[01])*)?$/.test(rule.pointer)
          }
          if (source === 'header') return typeof rule.header_name !== 'string' || !rule.header_name.trim()
          return false
        })) throw new Error('请检查请求头、断言和变量提取格式。')
    const nextMessages = current.input.protocol === 'websocket' ? messages.value.split(/\r?\n/).filter(Boolean) : []
    if (nextMessages.length > 20) throw new Error('每条用例最多支持 20 条 WebSocket 消息。')
    emit('change', current.location, {
      ...current.input,
      name: name.value.trim(),
      path: nextPath,
      body: body.value.trim() ? JSON.parse(body.value) : null,
      headers: nextHeaders,
      messages: nextMessages,
      assertions: nextChecks,
      ...(nextExtract.length ? { extract: nextExtract } : { extract: [] }),
    })
    closeEditor()
  } catch (failure) {
    error.value = failure instanceof SyntaxError ? '请求数据中包含无效的 JSON，请检查格式。'
      : failure instanceof Error ? failure.message : '无法保存用例。'
  }
}

function acceptsBody(input: TestCase) {
  return input.protocol !== 'websocket' && !['GET', 'HEAD', 'OPTIONS'].includes(input.method)
}
</script>

<template>
  <div ref="rootRef" class="draft" :class="{ 'draft-readonly': readonly }" :data-readonly="readonly ? 'true' : undefined" aria-label="待执行接口和用例">
    <div v-for="(group, groupIndex) in groups" :key="group.key" class="draft-operation">
      <input v-if="selecting && group.method" type="checkbox" class="operation-control" :aria-label="`选择 ${group.method} ${group.path}`"
        :checked="selected?.includes(group.key)" :disabled="isLocked" @change="emit('toggle', group.key)" />
      <button v-else-if="group.method" type="button" class="btn btn-ghost btn-icon operation-control" :title="readonly ? '只读回放模式不可移除接口' : '从本次测试移除此接口'"
        :aria-label="`删除 ${group.method} ${group.path}`" :disabled="isLocked || editing" @click="emit('remove', group.operation ?? groupIndex)"><Trash2 :size="13" /></button>
      <details :open="!!group.method || isGroupFocused(group.cases)">
      <summary><span v-if="group.method" class="tag tag-accent">{{ group.method }}</span> <code>{{ group.path }}</code><small>{{ group.cases.length }} 条<span v-if="group.method"> · {{ group.testCount }} test</span></small></summary>
      <ol>
        <li
          v-for="({ input, location, label, caseIndex }, casePos) in group.cases"
          :key="casePos"
          class="draft-case"
          :class="{ 'draft-case-focused': isCaseFocused(caseIndex) }"
          :data-case-index="caseIndex"
          :data-focused="isCaseFocused(caseIndex) ? 'true' : undefined"
        >
          <details :open="isCaseFocused(caseIndex) || (edit?.groupKey === group.key && edit?.index === casePos)">
            <summary>
              {{ input.name }} <small>{{ label }}</small>
              <span v-if="readonly && isCaseFocused(caseIndex)" class="tag tag-info focused-badge">当前回放</span>
            </summary>
            <form v-if="!readonly && edit && edit.groupKey === group.key && edit.index === casePos" @submit.prevent="saveEdit">
              <label>用例名称 <input v-model="name" class="input" maxlength="160" :disabled="isLocked" required /></label>
              <label>路径与查询参数 <input v-model="path" class="input mono" maxlength="1800" :disabled="isLocked" required /></label>
              <label v-if="acceptsBody(input)">请求体（JSON） <textarea v-model="body" class="textarea mono" rows="3" :disabled="isLocked" /></label>
              <label v-if="input.protocol === 'websocket'">发送消息（每行一条） <textarea v-model="messages" class="textarea mono" rows="3" :disabled="isLocked" /></label>
              <details class="advanced">
                <summary>请求头、断言与变量提取</summary>
                <label>请求头（JSON 对象） <textarea v-model="headers" class="textarea mono" rows="3" :disabled="isLocked" /></label>
                <label>断言（JSON 数组） <textarea v-model="assertions" class="textarea mono" rows="3" :disabled="isLocked" /></label>
                <label>变量提取（JSON 数组，如 {"name":"token","pointer":"/data/token"}） <textarea v-model="extract" class="textarea mono" rows="2" :disabled="isLocked" /></label>
              </details>
              <p v-if="error" class="warning" role="alert">{{ error }}</p>
              <div class="edit-actions"><button class="btn btn-primary btn-sm" type="submit" :disabled="isLocked">保存修改</button><button class="btn btn-sm" type="button" @click="closeEditor">取消</button></div>
            </form>
            <div v-else class="request-data">
              <template v-if="readonly">
                <div class="readonly-fields">
                  <label>请求方法 <input class="input mono" :value="input.method" readonly disabled /></label>
                  <label>请求路径 <input class="input mono" :value="input.path" readonly disabled /></label>
                  <label v-if="headersText(input)">请求头 <textarea class="textarea mono" :value="headersText(input)" rows="2" readonly disabled /></label>
                  <label v-if="caseBodyText(input)">请求体（JSON） <textarea class="textarea mono" :value="caseBodyText(input)" rows="3" readonly disabled /></label>
                  <label v-if="input.messages?.length">发送消息 <textarea class="textarea mono" :value="input.messages.join('\n')" rows="2" readonly disabled /></label>
                  <label v-if="assertionsText(input)">断言 <textarea class="textarea mono" :value="assertionsText(input)" rows="2" readonly disabled /></label>
                  <label v-if="extractText(input)">变量提取 <textarea class="textarea mono" :value="extractText(input)" rows="2" readonly disabled /></label>
                </div>
              </template>
              <template v-else>
                <code>{{ input.path }}</code>
                <small v-if="input.extract?.length">提取变量：{{ input.extract.map(item => item.name).join(', ') }}</small>
                <pre v-if="caseBodyText(input)">{{ caseBodyText(input) }}</pre>
                <pre v-if="input.messages?.length">{{ input.messages.join('\n') }}</pre>
              </template>
              <button class="btn btn-sm" type="button" :disabled="isLocked || editing || selecting" :title="readonly ? '只读回放模式，点击「再次测试」后可修改' : undefined" @click="beginEdit(group.key, casePos, group.method ? group.path : input.path.split('?')[0], location, input)">{{ label === '契约示例' ? '修改请求' : label === 'test' ? '修改 test' : '修改用例' }}</button>
            </div>
          </details>
        </li>
      </ol>
      </details>
    </div>
  </div>
</template>

<style scoped>
.draft { display: grid; gap: 6px; margin: 12px 0; }
.draft-readonly .draft-operation { border-color: var(--border-default); background: var(--bg-subtle); }
.draft-operation { position: relative; min-width: 0; border: 1px solid var(--accent-border); border-radius: var(--radius-sm); background: var(--accent-soft); }
.draft-operation > details > summary { padding-inline-end: 34px; }
.operation-control { position: absolute; z-index: 1; inset: 4px 5px auto auto; width: 24px; height: 24px; }
input.operation-control { inset-inline-end: 10px; width: 15px; height: 15px; accent-color: var(--accent); }
summary { padding: 5px 8px; font-weight: 600; cursor: pointer; overflow-wrap: anywhere; }
small { margin-inline-start: 6px; color: var(--text-secondary); font-weight: 400; }
ol { margin: 0; padding: 3px 8px 6px 29px; }
.draft-case { margin: 3px 0; border-radius: var(--radius-sm); background: var(--bg-surface); font-size: 12px; border: 1px solid transparent; }
.draft-case-focused { border-color: var(--accent); background: color-mix(in srgb, var(--accent-soft) 55%, var(--bg-surface)); box-shadow: 0 0 0 1px var(--accent-border); }
.focused-badge { margin-inline-start: 6px; font-size: 10.5px; }
.request-data, form { display: grid; gap: 6px; padding: 4px 8px 8px; min-width: 0; overflow-wrap: anywhere; }
.readonly-fields { display: grid; gap: 6px; }
.readonly-fields .input,
.readonly-fields .textarea { opacity: 0.92; cursor: default; background: var(--bg-canvas); }
.request-data .btn { justify-self: start; }
label { display: grid; gap: 4px; }
.advanced { padding: 4px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
.advanced label + label { margin-top: 6px; }
pre { margin: 0; padding: 6px; background: var(--bg-canvas); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 140px; overflow: auto; }
.edit-actions { display: flex; gap: 6px; }
.warning { color: var(--warning); margin: 0; }
</style>
