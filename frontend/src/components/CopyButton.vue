<script setup lang="ts">
/** 复制按钮：接收待复制文本和反馈文案，依赖浏览器剪贴板与临时 textarea 回退。点击后短暂显示已复制状态；回退通道也异常时会向调用环境传播。 */

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
// 连续点击会清理旧计时器，使反馈从最后一次复制起计时。
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

/** 按当前复制反馈状态选择复制或勾选图标；不读取剪贴板确认实际内容。 */
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
