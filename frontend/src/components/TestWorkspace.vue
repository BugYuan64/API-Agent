<script setup lang="ts">
/**
 * 测试工作台：历史列 + 测试内容。
 *
 * 外壳只管理分栏，不接触请求、执行、轮询或历史回放状态。
 * 历史与编辑器/结果的具体内容由各测试页通过插槽提供。
 */
import { ref } from 'vue'
import ResizableSplit from './ResizableSplit.vue'

const props = withDefaults(
  defineProps<{
    persistKey: string
    historyWidth?: number
    minHistory?: number
    minContent?: number
  }>(),
  { historyWidth: 268, minHistory: 200, minContent: 280 },
)

const width = ref(props.historyWidth)

function restore(): number {
  try {
    const saved = Number(window.localStorage.getItem(props.persistKey))
    return Number.isFinite(saved) && saved > 0 ? saved : props.historyWidth
  } catch {
    return props.historyWidth
  }
}

width.value = restore()
</script>

<template>
  <div class="test-workspace">
    <ResizableSplit
      v-model:size="width"
      :persist-key="persistKey"
      :min="minHistory"
      :min-other="minContent"
      direction="horizontal"
      label="拖动调整历史栏与测试区宽度"
    >
      <template #first>
        <div class="workspace-history"><slot name="history" /></div>
      </template>
      <template #second>
        <div class="workspace-content"><slot /></div>
      </template>
    </ResizableSplit>
  </div>
</template>

<style scoped>
.test-workspace,
.workspace-history,
.workspace-content {
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.workspace-history,
.workspace-content {
  overflow: hidden;
}
</style>
