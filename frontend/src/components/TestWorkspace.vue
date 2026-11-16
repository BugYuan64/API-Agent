<script setup lang="ts">
/**
 * 测试工作台：历史列 + 测试内容。
 *
 * 外壳只管理分栏，不接触请求、执行、轮询或历史回放状态。
 * 历史与编辑器/结果的具体内容由各测试页通过插槽提供。
 */
import { computed, provide, ref } from 'vue'
import ResizableSplit from './ResizableSplit.vue'
import { workspaceSplitKey } from '@/lib/split'

const props = withDefaults(
  defineProps<{
    persistKey: string
    historyWidth?: number
    minHistory?: number
    minContent?: number
    /** 当前可见的内层分栏标识；未指定时维持普通双栏语义。 */
    activePane?: string
  }>(),
  { historyWidth: 268, minHistory: 200, minContent: 280 },
)

const width = ref(props.historyWidth)
const reservations = ref<Record<string, { width: number; minimum: number }>>({})
/** 历史只借用中栏空间：末栏期望宽度与中栏最小值共同限定历史右边界。 */
const contentMinimum = computed(() => Math.max(
  props.minContent,
  props.activePane ? reservations.value[props.activePane]?.width ?? 0 : 0,
))
/** 极窄视口按三栏真实最小值降级；末栏偏好不参与物理最小值计算。 */
const contentFloor = computed(() => props.activePane
  ? reservations.value[props.activePane]?.minimum ?? props.minContent
  : undefined)

provide(workspaceSplitKey, {
  reserve(pane, reservedWidth, minimum = reservedWidth ?? 0) {
    if (reservedWidth === null) delete reservations.value[pane]
    else reservations.value[pane] = { width: reservedWidth, minimum }
  },
})

/** 缺失、非法值或存储不可用均回退初始历史宽度。 */
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
      :min-other="contentMinimum"
      :fit-other="contentFloor"
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
