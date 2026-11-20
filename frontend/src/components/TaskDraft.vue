<script setup lang="ts">
/** Review and edit imported or historical requests before starting a new run. */
import { computed, ref } from 'vue'
import { Trash2 } from 'lucide-vue-next'
import type { Catalog, TaskSnapshot, TestCase } from '@/api/tasks'
import { buildTaskDraftGroups, caseBodyText, type DraftLocation } from '@/lib/taskResults'

const props = defineProps<{ catalog?: Catalog; snapshot?: TaskSnapshot; editing: boolean; disabled?: boolean;
  selecting?: boolean; selected?: string[] }>()
const emit = defineEmits<{
  'update:editing': [value: boolean]
  change: [location: DraftLocation, value: TestCase]
  remove: [index: number]
  toggle: [key: string]
}>()

const groups = computed(() => buildTaskDraftGroups(props.catalog, props.snapshot))
const edit = ref<{ groupKey: string; index: number; template: string; location: DraftLocation; input: TestCase } | null>(null)
const name = ref('')
const path = ref('')
const body = ref('')
const headers = ref('')
const messages = ref('')
const assertions = ref('')
const error = ref('')

/** Use separate fields so changing a body never requires editing the whole API schema. */
function beginEdit(groupKey: string, index: number, template: string, location: DraftLocation, input: TestCase) {
  edit.value = { groupKey, index, template, location, input }
  name.value = input.name
  path.value = input.path
  body.value = caseBodyText(input)
  headers.value = JSON.stringify(input.headers ?? {}, null, 2)
  messages.value = input.messages?.join('\n') ?? ''
  assertions.value = JSON.stringify(input.assertions, null, 2)
  error.value = ''
  emit('update:editing', true)
}

function closeEditor() {
  edit.value = null
  emit('update:editing', false)
}

/** Keep the reviewed route and transport identity attached to the original operation. */
function saveEdit() {
  const current = edit.value
  if (!current || props.disabled) return
  try {
    const nextPath = path.value.trim()
    const routePattern = current.template.split(/(\{[^{}]+\})/).map(part => part.startsWith('{')
      ? '[^/{}]+' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('')
    const route = nextPath.split('?')[0]
    if (!name.value.trim() || !new RegExp(`^${routePattern}$`).test(route) || !nextPath.startsWith('/') ||
        nextPath.startsWith('//') || /[\s#\\]/.test(nextPath) || route.split('/').some(part => {
          const decoded = decodeURIComponent(part)
          return decoded === '.' || decoded === '..' || /[/\\{}]/.test(decoded)
        })) throw new Error('请填写用例名称和所属接口的具体路径。')
    const nextHeaders = JSON.parse(headers.value || '{}')
    const nextChecks = JSON.parse(assertions.value || '[]')
    if (!nextHeaders || typeof nextHeaders !== 'object' || Array.isArray(nextHeaders) ||
        Object.values(nextHeaders).some(value => typeof value !== 'string') ||
        !Array.isArray(nextChecks) || nextChecks.length > 20 || nextChecks.some(check => {
          if (!check || typeof check !== 'object') return true
          if (check.kind === 'status') return !Number.isInteger(check.expected) || check.expected < 100 || check.expected > 599
          if (check.kind === 'event_count') return !Number.isInteger(check.expected) || check.expected < 0
          if (['contains', 'message_contains'].includes(check.kind)) return typeof check.expected !== 'string'
          return check.kind !== 'json_equals' || (check.pointer != null &&
            (typeof check.pointer !== 'string' || !/^(?:\/(?:[^~]|~[01])*)?$/.test(check.pointer)))
        })) throw new Error('请检查请求头和断言格式。')
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
  <div class="draft" aria-label="待执行接口和用例">
    <div v-for="(group, index) in groups" :key="group.key" class="draft-operation">
      <input v-if="selecting && group.method" type="checkbox" class="operation-control" :aria-label="`选择 ${group.method} ${group.path}`"
        :checked="selected?.includes(group.key)" :disabled="disabled" @change="emit('toggle', group.key)" />
      <button v-else-if="group.method" type="button" class="btn btn-ghost btn-icon operation-control" title="从本次测试移除此接口"
        :aria-label="`删除 ${group.method} ${group.path}`" :disabled="disabled || editing" @click="emit('remove', group.operation ?? index)"><Trash2 :size="13" /></button>
      <details :open="!!group.method">
      <summary><span v-if="group.method" class="tag tag-accent">{{ group.method }}</span> <code>{{ group.path }}</code><small>{{ group.cases.length }} 条<span v-if="group.method"> · {{ group.testCount }} test</span></small></summary>
      <ol>
        <li v-for="({ input, location, label }, index) in group.cases" :key="index" class="draft-case">
          <details>
            <summary>{{ input.name }} <small>{{ label }}</small></summary>
            <form v-if="edit && edit.groupKey === group.key && edit.index === index" @submit.prevent="saveEdit">
              <label>用例名称 <input v-model="name" class="input" maxlength="160" required /></label>
              <label>路径与查询参数 <input v-model="path" class="input mono" maxlength="1800" required /></label>
              <label v-if="acceptsBody(input)">请求体（JSON） <textarea v-model="body" class="textarea mono" rows="3" /></label>
              <label v-if="input.protocol === 'websocket'">发送消息（每行一条） <textarea v-model="messages" class="textarea mono" rows="3" /></label>
              <details class="advanced">
                <summary>请求头和断言</summary>
                <label>请求头（JSON 对象） <textarea v-model="headers" class="textarea mono" rows="3" /></label>
                <label>断言（JSON 数组） <textarea v-model="assertions" class="textarea mono" rows="3" /></label>
              </details>
              <p v-if="error" class="warning" role="alert">{{ error }}</p>
              <div class="edit-actions"><button class="btn btn-primary btn-sm" type="submit">保存修改</button><button class="btn btn-sm" type="button" @click="closeEditor">取消</button></div>
            </form>
            <div v-else class="request-data">
              <code>{{ input.path }}</code>
              <pre v-if="caseBodyText(input)">{{ caseBodyText(input) }}</pre>
              <pre v-if="input.messages?.length">{{ input.messages.join('\n') }}</pre>
              <button class="btn btn-sm" type="button" :disabled="disabled || editing || selecting" @click="beginEdit(group.key, index, group.method ? group.path : input.path.split('?')[0], location, input)">{{ label === '契约示例' ? '修改请求' : label === 'test' ? '修改 test' : '修改用例' }}</button>
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
.draft-operation { position: relative; min-width: 0; border: 1px solid var(--accent-border); border-radius: var(--radius-sm); background: var(--accent-soft); }
.draft-operation > details > summary { padding-inline-end: 34px; }
.operation-control { position: absolute; z-index: 1; inset: 4px 5px auto auto; width: 24px; height: 24px; }
input.operation-control { inset-inline-end: 10px; width: 15px; height: 15px; accent-color: var(--accent); }
summary { padding: 5px 8px; font-weight: 600; cursor: pointer; overflow-wrap: anywhere; }
small { margin-inline-start: 6px; color: var(--text-secondary); font-weight: 400; }
ol { margin: 0; padding: 3px 8px 6px 29px; }
.draft-case { margin: 3px 0; border-radius: var(--radius-sm); background: var(--bg-surface); font-size: 12px; }
.request-data, form { display: grid; gap: 6px; padding: 4px 8px 8px; min-width: 0; overflow-wrap: anywhere; }
.request-data .btn { justify-self: start; }
label { display: grid; gap: 4px; }
.advanced { padding: 4px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
.advanced label + label { margin-top: 6px; }
pre { margin: 0; padding: 6px; background: var(--bg-canvas); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 140px; overflow: auto; }
.edit-actions { display: flex; gap: 6px; }
.warning { color: var(--warning); margin: 0; }
</style>
