<script setup lang="ts">
/**
 * 带阈值的可拖动分栏。
 *
 * 只负责布局，不知道两个插槽里是什么业务。尺寸以像素计算并夹在
 * `[min, available - minOther]` 内，因此拖到阈值后继续移动不会
 * 把另一侧挤没。resize 时也会重新夹紧，避免窗口缩小后留下旧的大尺寸。
 * size/update:size 是受控首栏宽度；workspacePane 显式启用末栏锚定，
 * 通过工作台上下文保留中栏最小值与末栏宽度，不接触插槽里的业务状态。
 */
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { clampSize, readStoredSize, workspaceSplitKey, writeStoredSize } from '@/lib/split'

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
    workspacePane?: string
    fitOther?: number
  }>(),
  { direction: 'horizontal', min: 220, minOther: 360, label: '调整区域大小' },
)

const emit = defineEmits<{ (event: 'update:size', value: number): void }>()
const root = ref<HTMLElement | null>(null)
const dragging = ref(false)
/** 已按下但尚未满足长按阈值。 */
const holding = ref(false)
const available = ref(0)
/** 请求宽度与临时显示宽度分离，避免ResizeObserver回写把窗口压缩变成新偏好。 */
const requestedSize = ref(props.size)
const workspace = inject(workspaceSplitKey, null)
/** 末栏偏好独立于首栏受控值；容器暂时变窄不覆盖偏好，恢复时可还原。 */
const secondSize = ref<number | undefined>()
let pointerId: number | null = null
let holdTimer: number | undefined
let startX = 0
let startY = 0
let sessionCleanup: (() => void) | null = null

const currentSize = computed(() =>
  clampSize({
    size: requestedSize.value,
    min: props.min,
    max: props.max,
    minOther: props.minOther,
    available: available.value,
    second: secondSize.value,
    fitOther: props.fitOther,
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

/** 隐藏的 v-show 面板宽度为0，不能用它覆盖末栏偏好或工作台保留量。 */
function readAvailable(initialSize = props.size): void {
  const rect = root.value?.getBoundingClientRect()
  if (!rect) return
  const measured = props.direction === 'horizontal' ? rect.width : rect.height
  if (measured <= 0) return
  available.value = measured
  if (props.workspacePane && secondSize.value === undefined) {
    const first = clampSize({ size: initialSize, min: props.min, max: props.max, minOther: props.minOther, available: measured })
    secondSize.value = readStoredSize(
      props.persistKey ? `${props.persistKey}.second` : undefined,
      Math.max(props.minOther, measured - first),
    )
    writeStoredSize(props.persistKey ? `${props.persistKey}.second` : undefined, secondSize.value)
  }
}

/** 同步实际首栏尺寸；尺寸观察产生的回写不改变末栏偏好。 */
function publishSize(next: number, persist = false): void {
  emit('update:size', next)
  if (persist) writeStoredSize(props.persistKey, next)
}

/** 接收拖动或键盘期望尺寸，夹紧后通知父级；只在空间足够时保存新偏好。 */
function emitSize(value: number): void {
  // 先夹紧再外发与持久化：拖到阈值外时，存储和父级状态都不应记录越界值。
  const next = clampSize({
    size: value,
    min: props.min,
    max: props.max,
    minOther: props.minOther,
    available: available.value,
    fitOther: props.fitOther ?? (props.workspacePane ? props.minOther : undefined),
  })
  // 只有用户拖动/键盘调整中间边界才建立新的末栏宽度。
  // 物理空间不足时只更新当前显示值，避免临时压缩覆盖正常窗口的末栏偏好。
  if (props.workspacePane && available.value >= props.min + props.minOther) {
    secondSize.value = available.value - next
    writeStoredSize(props.persistKey ? `${props.persistKey}.second` : undefined, secondSize.value)
  }
  if (available.value >= props.min + (props.fitOther ?? props.minOther)) requestedSize.value = next
  publishSize(next, available.value >= props.min + (props.fitOther ?? props.minOther))
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

/** 幂等清理长按计时、全局监听与拖动态，释放/取消/卸载共用。 */
function endSession(id: number): void {
  window.clearTimeout(holdTimer)
  holdTimer = undefined
  holding.value = false
  dragging.value = false
  if (pointerId === id) pointerId = null
  document.body.classList.remove('split-dragging', 'split-dragging-vertical')
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
  const saved = readStoredSize(props.persistKey, props.size)
  requestedSize.value = saved
  readAvailable(saved)
  observer = new ResizeObserver(() => readAvailable())
  if (root.value) observer.observe(root.value)
  // 持久化尺寸可能大于当前窗口，挂载后先夹紧一次再使用。
  if (props.persistKey) {
    if (saved !== props.size) publishSize(clampSize({
      size: saved,
      min: props.min,
      max: props.max,
      minOther: props.minOther,
      available: available.value,
      second: secondSize.value,
      fitOther: props.fitOther,
    }))
  }
})

watch(currentSize, () => {
  // 外部传入的尺寸也可能越界（例如窗口缩小后旧持久化值），同步回父级。
  if (currentSize.value !== props.size) publishSize(currentSize.value)
})

/** 接收真正的外部受控修改；自身发布的显示尺寸与currentSize相等，无须覆盖偏好。 */
watch(() => props.size, value => {
  if (value !== currentSize.value) {
    requestedSize.value = value
    if (props.workspacePane && available.value > 0) {
      secondSize.value = Math.max(props.minOther, available.value - value)
    }
  }
})

// 一次单向报告即可约束外层边界；隐藏面板各自保存偏好，由工作台选择当前模式。
watch([secondSize, () => props.min, () => props.minOther], () => {
  if (props.workspacePane && secondSize.value !== undefined) {
    workspace?.reserve(props.workspacePane, props.min + secondSize.value, props.min + props.minOther)
  }
})

onBeforeUnmount(() => {
  observer?.disconnect()
  if (props.workspacePane) workspace?.reserve(props.workspacePane, null)
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
      :aria-valuemin="Math.min(min, currentSize)"
      :aria-valuemax="Math.max(min, available - minOther)"
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

/* 细线本身：默认透明，避免在白面上出现一条贯穿的灰色带（“有缝感”）。
   悬浮/待命/拖动时才显现，作为“此处可调”的提示。 */
.split-grip {
  display: block;
  background: transparent;
  transition: background 0.14s ease, width 0.14s ease, height 0.14s ease;
}

.split-horizontal .split-grip {
  width: 1px;
  height: 100%;
}

.split-vertical .split-grip {
  width: 100%;
  height: 1px;
}

/* 悬浮：只加粗分隔线本身并转为主题紫，不给命中带铺底色。
   此前额外画过一条 16px 宽的浅紫底，视觉上像一根粗紫条，
   与“细线提示”的意图不符，已移除；命中仍由整条 16px 区域负责。 */
.split-handle:hover .split-grip,
.split-handle:focus-visible .split-grip {
  background: var(--accent);
}

.split-horizontal .split-handle:hover .split-grip,
.split-horizontal .split-handle:focus-visible .split-grip {
  width: 2px;
}

.split-vertical .split-handle:hover .split-grip,
.split-vertical .split-handle:focus-visible .split-grip {
  height: 2px;
}

/* 长按待命与拖动：线条保持紫并加粗（hover 已是同样状态），
   拖动时再叠一圈柔和外发光，作为“正在改变尺寸”的额外确认。
   选择器从根元素出发并带 .split-handle，才能压过上方 hover 规则的特异性
   （.split-handle:hover .split-grip 为 (0,2,1)，裸 .holding 只有 (0,2,0)）。 */
.holding .split-handle .split-grip,
.dragging .split-handle .split-grip {
  background: var(--accent);
}

.split-horizontal.holding .split-grip,
.split-horizontal.dragging .split-grip {
  width: 2px;
}

.split-vertical.holding .split-grip,
.split-vertical.dragging .split-grip {
  height: 2px;
}

.dragging .split-handle .split-grip {
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
