<script setup lang="ts">
/**
 * 三种测试共用的分栏外壳。页面通过插槽提供内容；外壳管理尺寸和拖动偏好。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import ResizableSplit from './ResizableSplit.vue'
import { readStoredSize, writeStoredSize } from '@/lib/split'
import { allocateWorkspaceWidth, adjacentWidthRange, WORKSPACE_LAYOUT_BOUNDS as bounds, workspaceMaxWidth, workspaceMinWidth, type WorkspaceWidths } from '@/lib/workspaceLayout'

const props = withDefaults(
  defineProps<{
    historyKey: string
    editorKey: string
    assistantKey: string
    historyWidth?: number
    editorWidth: number
    upperHeight?: number
    hideHistoryOnNarrow?: boolean
  }>(),
  { historyWidth: 268, upperHeight: 260 },
)

const root = ref<HTMLElement | null>(null)
const upperHeight = ref(props.upperHeight)
const historyHidden = ref(false)
const widths = ref<WorkspaceWidths>({ history: props.historyWidth, editor: props.editorWidth, results: bounds.results.min })
let desktopWidths: WorkspaceWidths | undefined
let compactWidths: WorkspaceWidths | undefined
let observer: ResizeObserver | undefined
let narrowViewport: MediaQueryList | undefined
const resultKey = `${props.editorKey}.second`
const historyRange = computed(() => adjacentWidthRange(widths.value, 'history', historyHidden.value))
const editorRange = computed(() => adjacentWidthRange(widths.value, 'editor', historyHidden.value))
const shellStyle = computed(() => ({ maxWidth: `${workspaceMaxWidth - (historyHidden.value ? bounds.history.max : 0)}px` }))

/** Distribute width changes without overwriting saved preferences or a layout temporarily squeezed below its minimum. */
function measureWidth(): void {
  const total = root.value?.getBoundingClientRect().width ?? 0
  if (total <= 0) return
  const hidden = narrowViewport?.matches ?? false
  historyHidden.value = hidden
  let previous = hidden ? compactWidths : desktopWidths
  if (!previous) {
    const history = readStoredSize(props.historyKey, props.historyWidth)
    const editor = readStoredSize(props.editorKey, props.editorWidth)
    previous = {
      history,
      editor,
      results: readStoredSize(resultKey, Math.min(total, workspaceMaxWidth) - (hidden ? 0 : history) - editor),
    }
  }
  const next = allocateWorkspaceWidth(total, previous, hidden)
  widths.value = next
  if (total >= workspaceMinWidth(hidden)) {
    if (hidden) compactWidths = next
    else desktopWidths = next
  }
}

/** Dragging the history divider trades width with the editor; dragging the editor divider trades with results. */
function resizeNeighbor(pane: 'history' | 'editor', value: number): void {
  const neighbor = pane === 'history' ? 'editor' : 'results'
  const current = widths.value
  const next = { ...current, [pane]: value, [neighbor]: current[neighbor] + current[pane] - value }
  widths.value = next
  if (historyHidden.value) compactWidths = next
  else {
    desktopWidths = next
    writeStoredSize(props.historyKey, next.history)
  }
  writeStoredSize(props.editorKey, next.editor)
  writeStoredSize(resultKey, next.results)
}

onMounted(() => {
  if (props.hideHistoryOnNarrow) {
    narrowViewport = window.matchMedia(`(max-width: ${bounds.narrowViewport}px)`)
    narrowViewport.addEventListener('change', measureWidth)
  }
  measureWidth()
  observer = new ResizeObserver(measureWidth)
  if (root.value) observer.observe(root.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
  narrowViewport?.removeEventListener('change', measureWidth)
})
</script>

<template>
  <div ref="root" class="test-workspace">
    <ResizableSplit
      class="workspace-split"
      :class="{ 'history-hidden': historyHidden }"
      :style="shellStyle"
      :size="widths.history"
      :range="historyRange"
      direction="horizontal"
      label="拖动调整历史栏与填写区宽度"
      @update:size="resizeNeighbor('history', $event)"
    >
      <template #first>
        <div v-show="!historyHidden" class="workspace-history"><slot name="history" /></div>
      </template>
      <template #second>
        <ResizableSplit
          v-model:size="upperHeight"
          direction="vertical"
          :persist-key="assistantKey"
          :min="bounds.upperMinHeight"
          :min-other="bounds.assistantHeight.min"
          :max-other="bounds.assistantHeight.max"
          :fit-other="bounds.assistantHeight.min"
          label="调整测试区与 AI 对话高度"
        >
          <template #first>
            <ResizableSplit
              :size="widths.editor"
              :range="editorRange"
              label="拖动调整填写区与结果区宽度"
              @update:size="resizeNeighbor('editor', $event)"
            >
              <template #first><slot name="editor" /></template>
              <template #second><slot name="results" /></template>
            </ResizableSplit>
          </template>
          <template #second><slot name="assistant" /></template>
        </ResizableSplit>
      </template>
    </ResizableSplit>
  </div>
</template>

<style scoped>
.test-workspace,
.workspace-history {
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.workspace-history { overflow: hidden; }
.workspace-split { margin-inline-start: auto; }
.history-hidden > :deep(.split-handle) { display: none; }
</style>
