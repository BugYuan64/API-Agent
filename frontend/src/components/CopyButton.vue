<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, Copy } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    text: string
    label?: string
    /** 触发复制后显示的文案，便于在不同语境下改措辞。 */
    copiedLabel?: string
  }>(),
  { label: '复制', copiedLabel: '已复制' },
)

const copied = ref(false)
// 用 token 保证连续点击时只显示最后一次的反馈。
let timer: number | undefined

/** 优先用 Clipboard API；它需要安全上下文，失败时回退到临时 textarea。 */
async function copy() {
  try {
    await navigator.clipboard.writeText(props.text)
  } catch {
    const area = document.createElement('textarea')
    area.value = props.text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.append(area)
    area.select()
    document.execCommand('copy')
    area.remove()
  }
  copied.value = true
  window.clearTimeout(timer)
  timer = window.setTimeout(() => (copied.value = false), 1600)
}

const icon = computed(() => (copied.value ? Check : Copy))
</script>

<template>
  <button class="btn btn-ghost btn-sm copy-button" @click="copy">
    <component :is="icon" :size="13" />
    <span>{{ copied ? copiedLabel : label }}</span>
  </button>
</template>

<style scoped>
.copy-button {
  color: var(--text-tertiary);
}

.copy-button:hover {
  color: var(--text-primary);
}
</style>
