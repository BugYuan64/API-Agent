<script setup lang="ts">
/**
 * 带阈值的可拖动分栏。
 *
 * 只负责布局，不知道两个插槽里是什么业务。尺寸以像素计算并夹在
 * `[min, available - minOther]` 内，因此拖到阈值后继续移动不会
 * 把另一侧挤没。resize 时也会重新夹紧，避免窗口缩小后留下旧的大尺寸。
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
    persistKey?: string
    label?: string
  }>(),
  { direction: 'horizontal', min: 220, minOther: 360, label: '调整区域大小' },
)

const emit = defineEmits<{ (event: 'update:size', value: number): void }>()
const root = ref<HTMLElement | null>(null)
const dragging = ref(false)
/** 已按下但尚未满足长按阈值。 */
const holding = ref(false)
const available = ref(0)
let pointerId: number | null = null
let holdTimer: number | undefined
let startX = 0
let startY = 0
let sessionCleanup: (() => void) | null = null

const currentSize = computed(() =>
  clampSize({
    size: props.size,
    min: props.min,
    max: props.max,
    minOther: props.minOther,
    available: available.value,
  }),
)

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
  if (!rect) return
  available.value = props.direction === 'horizontal' ? rect.width : rect.height
}

function emitSize(value: number): void {
  // 先夹紧再外发与持久化：拖到阈值外时，存储和父级状态都不应记录越界值。
  const next = clampSize({
    size: value,
    min: props.min,
    max: props.max,
    minOther: props.minOther,
    available: available.value,
  })
  emit('update:size', next)
  writeStoredSize(props.persistKey, next)
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
    const rect = root.value?.getBoundingClientRect()
    if (!rect) return
    emitSize(props.direction === 'horizontal' ? e.clientX - rect.left : e.clientY - rect.top)
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

function endSession(id: number): void {
  window.clearTimeout(holdTimer)
  holdTimer = undefined
  holding.value = false
  dragging.value = false
  if (pointerId === id) pointerId = null
  document.body.classList.remove('split-dragging')
  sessionCleanup?.()
}

function start(event: PointerEvent): void {
  if (event.button !== 0) return
  event.preventDefault()
  // 残留会话（上一次 pointercancel 未正常收尾）必须先清空。
  if (pointerId !== null) endSession(pointerId)
  pointerId = event.pointerId
  startX = event.clientX
  startY = event.clientY
  holding.value = true
  bindSession(event.pointerId)
  holdTimer = window.setTimeout(() => {
    holdTimer = undefined
    if (pointerId !== event.pointerId) return
    dragging.value = true
    holding.value = false
    document.body.classList.add('split-dragging')
  }, HOLD_MS)
}

function keydown(event: KeyboardEvent): void {
  const step = event.shiftKey ? 40 : 10
  if (event.key === 'Home') {
    event.preventDefault()
    emitSize(props.min)
  } else if (event.key === 'End') {
    event.preventDefault()
    const maxAllowed = props.max ?? available.value - (props.minOther ?? 0)
    emitSize(Math.max(props.min, maxAllowed))
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
  readAvailable()
  observer = new ResizeObserver(readAvailable)
  if (root.value) observer.observe(root.value)
  // 持久化尺寸可能大于当前窗口，挂载后先夹紧一次再使用。
  if (props.persistKey) {
    const saved = readStoredSize(props.persistKey, props.size)
    if (saved !== props.size) emit('update:size', clampSize({
      size: saved,
      min: props.min,
      max: props.max,
      minOther: props.minOther,
      available: available.value,
    }))
  }
})

watch(currentSize, () => {
  // 外部传入的尺寸也可能越界（例如窗口缩小后旧持久化值），同步回父级。
  if (currentSize.value !== props.size) emitSize(currentSize.value)
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
      :aria-valuemin="min"
      :aria-valuemax="Math.max(min, available - minOther)"
      :style="handleStyle"
      @pointerdown="start"
      @keydown="keydown"
    >
      <span class="split-grip" aria-hidden="true">
        <svg class="grip-arrow" v-if="direction === 'horizontal'" viewBox="0 0 16 12" width="16" height="12">
          <path d="M1 6h14M1 6l4-4M1 6l4 4M15 6l-4-4M15 6l-4 4" />
        </svg>
        <svg class="grip-arrow" v-else viewBox="0 0 12 16" width="12" height="16">
          <path d="M6 1v14M6 1l-4 4M6 1l4 4M6 15l-4-4M6 15l4-4" />
        </svg>
      </span>
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

.split-handle {
  position: absolute;
  z-index: 5;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  background: transparent;
  touch-action: none;
  /* 自定义双向箭头光标：部分宿主（含内嵌 webview）不渲染系统 resize 光标，
     用内联 SVG 保证形态一致。末尾保留 col-resize 作为加载失败时的回退。 */
  cursor: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'><g fill='none' stroke='%2317171a' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M7 12h10'/><path d='M7 12l3-3M7 12l3 3'/><path d='M17 12l-3-3M17 12l-3 3'/></g></svg>") 12 12, col-resize;
}

.split-vertical > .split-handle {
  cursor: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' width='24' height='24' viewBox='0 0 24 24'><g fill='none' stroke='%2317171a' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 7v10'/><path d='M12 7l-3 3M12 7l3 3'/><path d='M12 17l-3-3M12 17l3-3'/></g></svg>") 12 12, row-resize;
}

.split-horizontal > .split-handle {
  top: 0;
  bottom: 0;
  /* 命中带覆盖整条边界；14px 足够宽，鼠标靠近即命中，同时不遮挡内容。 */
  width: 14px;
  transform: translateX(-50%);
}

.split-vertical > .split-handle {
  left: 0;
  right: 0;
  height: 14px;
  transform: translateY(-50%);
}

/* 细线本身：默认透明，避免在白面上出现一条贯穿的灰色带（“有缝感”）。
   悬浮/待命/拖动时才显现，作为“此处可调”的提示。 */
.split-grip {
  display: block;
  background: transparent;
  transition: background 0.14s ease;
}

.split-horizontal .split-grip {
  width: 1px;
  height: 100%;
}

.split-vertical .split-grip {
  width: 100%;
  height: 1px;
}

/* 悬浮：边界线加深，提示此处可调。 */
.split-handle:hover .split-grip,
.split-handle:focus-visible .split-grip {
  background: var(--border-strong);
}

/* 长按待命：边界线转为强调色，提示“继续按住即可拖动”。 */
.holding .split-grip {
  background: var(--accent);
}

/* 拖动中：保持强调色并加一点外发光。 */
.dragging .split-grip {
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}

/* 边界中央的双向箭头：悬浮、长按待命、拖动时显现。
   它既是视觉提示，也弥补了部分宿主不绘制自定义光标的场景。 */
.grip-arrow {
  fill: none;
  stroke: var(--text-secondary);
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  /* 常驻可见（淡），保证用户一眼看到“这里可以调”；悬浮/待命时变实。 */
  opacity: 0.45;
  transition: opacity 0.14s ease, stroke 0.14s ease;
  position: absolute;
  pointer-events: none;
  background: var(--bg-surface);
  border-radius: 3px;
  padding: 1px;
  /* 压在边界正中央：handle 是 place-items:center 的 grid，
     绝对定位元素需要显式居中才不会跑到左上角。 */
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
}

.split-handle:hover .grip-arrow,
.split-handle:focus-visible .grip-arrow,
.holding .grip-arrow,
.dragging .grip-arrow {
  opacity: 1;
}

.holding .grip-arrow,
.dragging .grip-arrow {
  stroke: var(--accent);
}

:global(body.split-dragging) {
  cursor: col-resize;
  user-select: none !important;
}

:global(body.split-dragging *) {
  user-select: none !important;
}
</style>
