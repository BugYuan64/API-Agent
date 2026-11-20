<script setup lang="ts">
/** Read-only JSON details for the current response or saved result. */
import { computed, onBeforeUnmount, ref } from 'vue'
import AppModal from './AppModal.vue'
import CopyButton from './CopyButton.vue'
import { describeError } from '@/api/client'

const props = defineProps<{
  data?: object | null
  load?: (signal: AbortSignal) => Promise<object>
  disabled?: boolean
  note?: string
}>()

const open = ref(false)
const loading = ref(false)
const loaded = ref<object | null>(null)
const error = ref('')
const text = computed(() => {
  const data = props.load ? loaded.value : props.data
  return data ? JSON.stringify(data, null, 2) : ''
})
let pending: AbortController | undefined

/** Cancel a read when the dialog closes or its response is replaced. */
function close() {
  pending?.abort()
  pending = undefined
  open.value = false
}

async function show() {
  open.value = true
  loading.value = false
  loaded.value = null
  error.value = ''
  if (!props.load) return
  const controller = new AbortController()
  pending = controller
  loading.value = true
  try {
    const result = await props.load(controller.signal)
    if (!controller.signal.aborted) loaded.value = result
  } catch (failure) {
    if (!controller.signal.aborted) error.value = describeError(failure)
  } finally {
    if (pending === controller) {
      pending = undefined
      loading.value = false
    }
  }
}

onBeforeUnmount(close)
</script>

<template>
  <button type="button" class="btn btn-ghost btn-sm" :disabled="disabled || (!data && !load)" @click="show">详情</button>
  <AppModal v-if="open" :open="open" title="请求与响应详情" cancel-text="关闭" confirm-text="" width="920px" @update:open="close">
    <p v-if="note" class="hint details-note">{{ note }}</p>
    <p v-if="loading" role="status">正在读取详情…</p>
    <p v-else-if="error" role="alert">{{ error }}</p>
    <template v-else-if="text">
      <div class="details-actions"><CopyButton :text="text" label="复制 JSON" /></div>
      <pre class="details-json">{{ text }}</pre>
    </template>
  </AppModal>
</template>

<style scoped>
.details-note { margin: 0 0 8px; }
.details-actions { display: flex; justify-content: flex-end; }
.details-json { margin: 4px 0 0; padding: 12px; border-radius: var(--radius-md); background: var(--bg-canvas); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
