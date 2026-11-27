<script setup lang="ts">
/**
 * 带阈值的可拖动分栏。
 *
 * 只负责布局，不知道两个插槽里是什么业务。尺寸以像素计算并夹在
 * `[min, available - minOther]` 内，因此拖到阈值后继续移动不会
 * 把另一侧挤没。resize 时也会重新夹紧，避免窗口缩小后留下旧的大尺寸。
 * size/update:size 是首栏宽度偏好；layout 独立发布本轮分配的实际尺寸。
 * range 由父级提供实际尺寸与拖动阈值，避免嵌套分栏各自分配同一份空间。
 * resizeMinOther 仅约束用户调整；resize 传出实际位移供嵌套布局分配相邻区域。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { clampSize, readStoredSize, writeStoredSize } from '@/lib/split'

/** 长按阈值：按住这么久才真正进入拖动，避免“碰到边界就误改布局”。 */
const HOLD_MS = 260

const props = withDefaults(
  defineProps<{
    direction?: 'horizontal' | 'vertical'
    size: number
    min?: number
    max?: number
    minOther?: number
    maxOther?: number
    resizeMinOther?: number
    persistKey?: string
    label?: string
    fitOther?: number
    /** A parent layout may supply exact track widths and adjacent drag limits. */
    range?: { min: number; max: number }
  }>(),
  { direction: 'horizontal', min: 220, minOther: 360, label: '调整区域大小' },
)

const emit = defineEmits<{
  'update:size': [value: number]
  'size-commit': []
  layout: [sizes: { first: number; second: number }]
  resize: [change: { delta: number; remaining: number }]
}>()
const root = ref<HTMLElement | null>(null)
const dragging = ref(false)
/** 已按下但尚未满足长按阈值。 */
const holding = ref(false)
const available = ref(0)
/** 请求宽度与临时显示宽度分离，避免ResizeObserver回写把窗口压缩变成新偏好。 */
const requestedSize = ref(props.size)
let pointerId: number | null = null
let holdTimer: number | undefined
let startX = 0
let startY = 0
let startSize = 0
let sessionCleanup: (() => void) | null = null
let moveFrame: number | null = null
let queuedSize: number | null = null
let dragChanged = false

const currentSize = computed(() =>
  props.range ? props.size : clampSize({
    size: requestedSize.value === Infinity && available.value === 0 ? props.min : requestedSize.value,
    min: props.min,
    maxOther: props.maxOther,
    max: props.max,
    minOther: props.minOther,
    available: available.value,
    fitOther: props.fitOther,
  }),
)
const resizeMinOther = computed(() => Math.max(props.minOther, props.resizeMinOther ?? 0))
const dragRange = computed(() => {
  if (props.range) return props.range
  const clamp = (size: number) => clampSize({
    size,
    min: props.min,
    max: props.max,
    minOther: resizeMinOther.value,
    maxOther: props.maxOther,
    available: available.value,
    fitOther: props.fitOther,
  })
  const min = clamp(-Infinity)
  return { min, max: props.max === undefined && available.value <= 0 ? min : clamp(Infinity) }
})

const style = computed(() => {
  const basis = `${currentSize.value}px`
  return props.direction === 'horizontal'
    ? { gridTemplateColumns: `${basis} minmax(0, 1fr)` }
    : { gridTemplateRows: `${basis} minmax(0, 1fr)` }
})

/** 分隔条自身的定位必须单独绑定；加到容器上会把整个 grid 平移。 */
const handleStyle = computed(() => {
  const basis = `${currentSize.value}px`
  return props.direction === 'horizontal' ? { left: basis } : { top: basis }
})

function readAvailable(): void {
  const rect = root.value?.getBoundingClientRect()
  const size = props.direction === 'horizontal' ? rect?.width : rect?.height
  if (size && size > 0) available.value = size
}

/** 鼠标和键盘共用同一尺寸阈值；拖动期间只更新显示。 */
function emitSize(value: number, round = true): void {
  const previous = currentSize.value
  const { min, max } = dragRange.value
  const next = Math.min(max, Math.max(min, round ? Math.round(value) : value))
  if (next === previous) return
  if (!props.range) {
    const minimumOther = props.resizeMinOther === undefined
      ? props.fitOther ?? props.minOther
      : resizeMinOther.value
    if (available.value < props.min + minimumOther) return
    requestedSize.value = next
  }
  emit('update:size', next)
  if (!props.range) emit('resize', { delta: next - previous, remaining: available.value - next })
  if (dragging.value) dragChanged = true
  else commitSize()
}

function commitSize(): void {
  if (!props.range) writeStoredSize(props.persistKey, requestedSize.value)
  emit('size-commit')
}

/** 高频移动仅保留最新位置，避免每条事件都触发 Vue 更新和内容重排。 */
function flushMove(): void {
  moveFrame = null
  if (queuedSize === null) return
  const next = queuedSize
  queuedSize = null
  emitSize(next)
}

function scheduleSize(value: number): void {
  queuedSize = value
  if (moveFrame !== null) return
  moveFrame = window.requestAnimationFrame(flushMove)
}

/**
 * 拖动会话的收尾监听挂在 window 上。
 *
 * 原因：pointer capture 会把后续事件重定向到 handle，但真实鼠标拖动中
 * 指针可能移出 handle 甚至移出窗口，此时浏览器不再向 handle 派发
 * pointerup——拖动状态就此卡死。window 级监听保证无论指针在哪，
 * 释放和取消都一定会被收到。
 */
function bindSession(id: number): void {
  const onMove = (e: PointerEvent) => {
    if (e.pointerId !== id) return
    if (!dragging.value) {
      // 待命期移动超过 6px：判定为滑选文本，取消长按。
      if (holding.value) {
        const drift = props.direction === 'horizontal'
          ? Math.abs(e.clientX - startX)
          : Math.abs(e.clientY - startY)
        if (drift > 6) endSession(id)
      }
      return
    }
    const delta = props.direction === 'horizontal' ? e.clientX - startX : e.clientY - startY
    scheduleSize(startSize + delta)
  }
  const onUp = (e: PointerEvent) => {
    if (e.pointerId === id) endSession(id)
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
  window.addEventListener('pointercancel', onUp)
  sessionCleanup = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
    sessionCleanup = null
  }
}

/** 幂等清理长按计时、全局监听与拖动态，释放/取消/卸载共用。 */
function endSession(id: number): void {
  if (moveFrame !== null) {
    window.cancelAnimationFrame(moveFrame)
  }
  flushMove()
  window.clearTimeout(holdTimer)
  holdTimer = undefined
  holding.value = false
  dragging.value = false
  if (pointerId === id) pointerId = null
  document.body.classList.remove('split-dragging', 'split-dragging-vertical')
  if (dragChanged) {
    dragChanged = false
    commitSize()
  }
  sessionCleanup?.()
}

/** 主键按下后登记长按会话；阈值内漂移取消，未达到阈值不改变布局。 */
function start(event: PointerEvent): void {
  if (event.button !== 0) return
  event.preventDefault()
  // 残留会话（上一次 pointercancel 未正常收尾）必须先清空。
  if (pointerId !== null) endSession(pointerId)
  pointerId = event.pointerId
  startX = event.clientX
  startY = event.clientY
  startSize = currentSize.value
  holding.value = true
  bindSession(event.pointerId)
  holdTimer = window.setTimeout(() => {
    holdTimer = undefined
    if (pointerId !== event.pointerId) return
    dragging.value = true
    holding.value = false
    document.body.classList.add('split-dragging')
    // 垂直分栏用上下箭头光标；类名只在拖动期间存在，结束即移除。
    if (props.direction !== 'horizontal') {
      document.body.classList.add('split-dragging-vertical')
    }
  }, HOLD_MS)
}

/** Home/End 到阈值，方向键微调；Shift 使用40px步长。 */
function keydown(event: KeyboardEvent): void {
  const step = event.shiftKey ? 40 : 10
  if (event.key === 'Home') {
    event.preventDefault()
    emitSize(dragRange.value.min, false)
  } else if (event.key === 'End') {
    event.preventDefault()
    emitSize(dragRange.value.max, false)
  } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
    event.preventDefault()
    emitSize(currentSize.value - step)
  } else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
    event.preventDefault()
    emitSize(currentSize.value + step)
  }
}

let observer: ResizeObserver | undefined
onMounted(() => {
  if (props.range) return
  const saved = readStoredSize(props.persistKey, props.size)
  requestedSize.value = saved
  if (saved !== props.size) emit('update:size', saved)
  readAvailable()
  if (root.value) {
    observer = new ResizeObserver(readAvailable)
    observer.observe(root.value)
  }
})

watch([available, currentSize], ([total, first]) => {
  if (total > 0) emit('layout', { first, second: Math.max(0, total - first) })
}, { flush: 'sync' })

/** 区分外部修改与 v-model 回传；被动压缩不触发偏好变更。 */
watch(() => props.size, value => {
  if (!props.range) requestedSize.value = value
})

onBeforeUnmount(() => {
  observer?.disconnect()
  if (pointerId !== null) endSession(pointerId)
  document.body.classList.remove('split-dragging')
})
</script>

<template>
  <div
    ref="root"
    class="resizable-split"
    :class="[`split-${direction}`, { dragging, holding }]"
    :style="style"
  >
    <div class="split-pane split-first"><slot name="first" /></div>
    <button
      class="split-handle"
      type="button"
      role="separator"
      :aria-label="label"
      :aria-orientation="direction === 'horizontal' ? 'vertical' : 'horizontal'"
      :aria-valuenow="currentSize"
      :aria-valuemin="Math.min(dragRange.min, currentSize)"
      :aria-valuemax="Math.max(dragRange.max, currentSize)"
      :style="handleStyle"
      @pointerdown="start"
      @keydown="keydown"
    >
      <span class="split-grip" aria-hidden="true" />
    </button>
    <div class="split-pane split-second"><slot name="second" /></div>
  </div>
</template>

<style scoped>
.resizable-split {
  display: grid;
  position: relative;
  min-width: 0;
  min-height: 0;
  height: 100%;
  max-height: 100%;
  overflow: hidden;
}

.split-pane {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  /* 关键：让嵌套的 100% 高度有确定的参照，否则内层分栏会撑出父容器。 */
  position: relative;
  height: 100%;
}

.split-pane > :deep(*) {
  min-height: 0;
}

/* 方向光标用自定义 SVG 而非系统 col-resize/row-resize：
   系统光标本身没有描边，在浅色界面上对比偏弱。这里画成白色双向箭头配
   1px 黑描边，任何底色下都清晰。data URI 内联，不额外产生资源文件与构建配置。
   末尾的 col-resize/row-resize 是图像不可用时的回退，语义保持不变。 */
.split-handle {
  position: absolute;
  z-index: 5;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  background: transparent;
  touch-action: none;
  /* 悬停命中带时的光标；拖动期间由 body.split-dragging 整页锁定同一光标，
     指针移出这 16px 范围也不会退化成普通箭头。 */
  cursor:
    url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%3E%3Cg%20fill='%23fff'%20stroke='%23000'%20stroke-width='1'%20stroke-linejoin='round'%3E%3Cpolygon%20points='1,8%205,4%205,6%2011,6%2011,4%2015,8%2011,12%2011,10%205,10%205,12'/%3E%3C/g%3E%3C/svg%3E")
      8 8,
    col-resize;
}

.split-vertical > .split-handle {
  cursor:
    url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%3E%3Cg%20fill='%23fff'%20stroke='%23000'%20stroke-width='1'%20stroke-linejoin='round'%3E%3Cpolygon%20points='8,1%204,5%206,5%206,11%204,11%208,15%2012,11%2010,11%2010,5%2012,5'/%3E%3C/g%3E%3C/svg%3E")
      8 8,
    row-resize;
}

.split-horizontal > .split-handle {
  top: 0;
  bottom: 0;
  /* 命中带覆盖整条边界；宽度兼顾“靠近即命中”与“不遮挡内容”。
     16px 配合拖动期整页光标锁定，指针略微移出也仍可操作。 */
  width: 16px;
  transform: translateX(-50%);
}

.split-vertical > .split-handle {
  left: 0;
  right: 0;
  height: 16px;
  transform: translateY(-50%);
}

/* Keep the divider visible at rest; interaction states strengthen its accent and glow. */
.split-handle > .split-grip {
  display: block;
  position: absolute;
  pointer-events: none;
  background: var(--border-strong);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--bg-canvas) 70%, transparent);
  opacity: 0.9;
  transition: background 0.14s ease, width 0.14s ease, height 0.14s ease;
}

.split-horizontal > .split-handle > .split-grip {
  inset-block: 0;
  left: 50%;
  transform: translateX(-50%);
  width: 2px;
}

.split-vertical > .split-handle > .split-grip {
  inset-inline: 0;
  top: 50%;
  transform: translateY(-50%);
  height: 2px;
}

/* Hover and keyboard focus highlight only the divider, not its full hit area. */
.split-handle:hover > .split-grip,
.split-handle:focus-visible > .split-grip {
  background: var(--accent);
}

.split-horizontal > .split-handle:hover > .split-grip,
.split-horizontal > .split-handle:focus-visible > .split-grip {
  width: 3px;
}

.split-vertical > .split-handle:hover > .split-grip,
.split-vertical > .split-handle:focus-visible > .split-grip {
  height: 3px;
}

/* 长按待命与拖动：线条保持紫并加粗（hover 已是同样状态），
   拖动时再叠一圈柔和外发光，作为“正在改变尺寸”的额外确认。
   使用直接子级选择器，避免父分隔组件的方向和状态影响嵌套手柄。 */
.holding > .split-handle > .split-grip,
.dragging > .split-handle > .split-grip {
  background: var(--accent);
}

.split-horizontal.holding > .split-handle > .split-grip,
.split-horizontal.dragging > .split-handle > .split-grip {
  width: 3px;
}

.split-vertical.holding > .split-handle > .split-grip,
.split-vertical.dragging > .split-handle > .split-grip {
  height: 3px;
}

.dragging > .split-handle > .split-grip {
  box-shadow: 0 0 0 2px var(--accent-soft);
}

:global(body.split-dragging) {
  user-select: none !important;
}

:global(body.split-dragging *) {
  user-select: none !important;
}

/* 拖动中的光标锁定。
   原因：手柄命中带只有 16px 宽，鼠标一旦拖出这段区域，浏览器会把光标
   回退成普通箭头，用户就失去了“还在拖动”的反馈。拖动期间在 body 上
   强制整页保持同一光标，指针移到任何位置都仍是双向箭头。
   body 与后代都覆盖：子元素各自的 cursor 声明（如按钮的 pointer）
   优先级更高，只写 body 会被它们盖掉。
   这里必须重复同一张自定义光标图——CSS 无法从别处继承“图像 + 热点”，
   只写关键字会退回无描边的系统光标。 */
:global(body.split-dragging),
:global(body.split-dragging *) {
  cursor:
    url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%3E%3Cg%20fill='%23fff'%20stroke='%23000'%20stroke-width='1'%20stroke-linejoin='round'%3E%3Cpolygon%20points='1,8%205,4%205,6%2011,6%2011,4%2015,8%2011,12%2011,10%205,10%205,12'/%3E%3C/g%3E%3C/svg%3E")
      8 8,
    col-resize !important;
}

:global(body.split-dragging.split-dragging-vertical),
:global(body.split-dragging.split-dragging-vertical *) {
  cursor:
    url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='16'%20height='16'%20viewBox='0%200%2016%2016'%3E%3Cg%20fill='%23fff'%20stroke='%23000'%20stroke-width='1'%20stroke-linejoin='round'%3E%3Cpolygon%20points='8,1%204,5%206,5%206,11%204,11%208,15%2012,11%2010,11%2010,5%2012,5'/%3E%3C/g%3E%3C/svg%3E")
      8 8,
    row-resize !important;
}
</style>
