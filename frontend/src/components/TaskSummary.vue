<script setup lang="ts">
/** Show task report status and pass streamed content to the shared AI renderer. */
import AiMarkdown from './AiMarkdown.vue'

defineProps<{ text?: string | null; status: string; pending?: boolean }>()
</script>

<template>
  <section class="task-summary" aria-label="AI 总结">
    <header>
      <h3>AI 总结</h3>
      <span v-if="pending" class="tag tag-info" role="status">正在生成</span>
    </header>
    <AiMarkdown v-if="text?.trim()" :text="text" />
    <p v-else class="summary-hint">{{ status }}</p>
  </section>
</template>

<style scoped>
.task-summary { margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border-subtle); }
header { display: flex; align-items: center; gap: 10px; }
header h3 { margin: 0; font-size: 15px; }
.summary-hint { color: var(--text-secondary); margin: 8px 0; }
</style>
