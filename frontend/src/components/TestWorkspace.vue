<script setup lang="ts">
/**
 * 三种测试共用的分栏外壳。页面通过插槽提供内容；外壳管理尺寸和拖动偏好。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ResizableSplit from './ResizableSplit.vue'
import { readStoredSize, writeStoredSize } from '@/lib/split'
import { allocateWorkspaceWidth, adjacentWidthRange, getWorkspaceMaxWidth, resolveWorkspaceBounds, WORKSPACE_LAYOUT_BOUNDS as bounds, workspaceMinWidth, type WorkspaceWidths } from '@/lib/workspaceLayout'

const props = defineProps<{
  historyKey: string
  editorKey: string
  assistantKey: string
  editorWidth: number
  hideHistoryOnNarrow?: boolean
  /** 可选放宽结果区最大宽度（例如大模型能力矩阵传入 1400）；默认沿用 720。 */
  resultsMax?: number
}>()

const root = ref<HTMLElement | null>(null)
/** 无已保存尺寸时，上方占满余量，让小Y保持最小高度。 */
const upperHeight = ref(Infinity)
const historyHidden = ref(false)
const widths = ref<WorkspaceWidths>({ history: bounds.history.min, editor: props.editorWidth, results: bounds.results.min })
let desktopWidths: WorkspaceWidths | undefined
let compactWidths: WorkspaceWidths | undefined
let observer: ResizeObserver | undefined
let narrowViewport: MediaQueryList | undefined
let measuredWidth = 0
const resultKey = `${props.editorKey}.second`
const effectiveResultsMax = computed(() => resolveWorkspaceBounds(props.resultsMax).results.max)
const historyRange = computed(() => adjacentWidthRange(widths.value, 'history', historyHidden.value, props.resultsMax))
const editorRange = computed(() => adjacentWidthRange(widths.value, 'editor', historyHidden.value, props.resultsMax))
const shellStyle = computed(() => ({
  maxWidth: `${widths.value.history + widths.value.editor + widths.value.results}px`,
  '--workspace-results-max': `${effectiveResultsMax.value}px`,
}))

/** Distribute width changes without overwriting saved preferences or a layout temporarily squeezed below its minimum. */
function measureWidth(): void {
  const total = root.value?.getBoundingClientRect().width ?? 0
  if (total <= 0) return
  const hidden = Boolean(
    props.hideHistoryOnNarrow &&
      ((narrowViewport?.matches ?? false) || total < workspaceMinWidth(false)),
  )
  if (total === measuredWidth && hidden === historyHidden.value) return
  measuredWidth = total
  historyHidden.value = hidden
  const resultsMax = effectiveResultsMax.value
  let previous = hidden ? compactWidths : desktopWidths
  let available = total
  if (!previous) {
    const savedHistory = readStoredSize(props.historyKey, 0)
    const history = savedHistory || bounds.history.min
    if (!savedHistory && !hidden) available = Math.min(total, bounds.history.min + bounds.editor.max + resultsMax)
    const editor = readStoredSize(props.editorKey, props.editorWidth)
    previous = {
      history,
      editor,
      results: readStoredSize(resultKey, Math.min(available, getWorkspaceMaxWidth(resultsMax)) - (hidden ? 0 : history) - editor),
    }
  }
  const next = allocateWorkspaceWidth(available, previous, hidden, resultsMax)
  widths.value = next
  if (total >= workspaceMinWidth(hidden)) {
    if (hidden) compactWidths = next
    else desktopWidths = next
  }
}

watch(() => props.resultsMax, () => {
  measuredWidth = 0
  measureWidth()
})

/** Dragging the history divider trades width with the editor; dragging the editor divider trades with results. */
function resizeNeighbor(pane: 'history' | 'editor', value: number): void {
  const neighbor = pane === 'history' ? 'editor' : 'results'
  const current = widths.value
  const next = { ...current, [pane]: value, [neighbor]: current[neighbor] + current[pane] - value }
  widths.value = next
  if (historyHidden.value) compactWidths = next
  else desktopWidths = next
}

function persistNeighbor(): void {
  const current = widths.value
  if (!historyHidden.value) writeStoredSize(props.historyKey, current.history)
  writeStoredSize(props.editorKey, current.editor)
  writeStoredSize(resultKey, current.results)
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
      @size-commit="persistNeighbor"
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
              @size-commit="persistNeighbor"
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
