<script setup lang="ts">
/** 共享历史列表组件：接收统一条目、标题与当前选中 ID，输出 select/delete/clear 事件并暴露列表元素。业务记录转换与恢复由父级负责；列表不访问后端或裁定执行结果。 */

/**
 * 测试工作台的左侧历史列（手动/自动两种模式共用）。
 *
 * 作用：用例测试的手动与自动模式此前各写一套历史列表——标题文案、数据结构、
 * 甚至行内间距都不同，切换模式时列宽与字号会跳变。这里收敛为一个受控组件：
 * 只接收「标题 + 已是统一形状的条目」，字体、行高、内边距、空状态全部共用，
 * 因此两种模式的历史列在尺寸与排版上完全一致。
 *
 * 条目形状刻意保持扁平（id / label / tone / meta），
 * 让各模式自己决定如何把领域数据映射进来，组件不感知执行或运行的差异。
 *
 * 删除与清空由**可选回调**启用，而不是外抛插槽：
 * 此前删除按钮写在某个父组件的插槽里，导致 ① 组件里那套 `.history-delete`
 * 样式要求父组件必须使用特定类名才有行为配套；② `@click.stop` 防止
 * “点删除顺带选中”的约束完全靠调用方自觉；③ 三个页面能力不一致（只有请求历史能删）。
 * 收进组件后，样式与行为绑定、冒泡由组件保证，调用方只需决定是否传回调。
 */
import { ref } from 'vue'
import { Trash2 } from 'lucide-vue-next'

/** 共享历史展示条目；source 用于父级分派，raw 仅供插槽访问，列表不感知领域数据结构。 */
export interface HistoryEntry {
  id: string
  label: string
  /** 状态色调：success / warning / danger / info / neutral。 */
  tone: string
  /** 状态文字（如「通过」「失败」「运行中」）。 */
  status: string
  /** 右侧次要信息，通常是相对时间。 */
  meta: string
  /** 可选的原始记录，供 prefix/actions 插槽渲染调用方特有字段（如 HTTP 方法）。 */
  raw?: unknown
  /** 可选的时间戳（ISO），仅用于合并多来源时排序，不参与展示。 */
  at?: string
  /** 可选的来源标记，供调用方在点击时分派（如 manual / auto）。 */
  source?: string
}

const props = withDefaults(
  defineProps<{
    title: string
    items: HistoryEntry[]
    emptyText: string
    activeId?: string | null
    /** 传入即显示行内删除按钮；不传则完全不渲染。 */
    onDelete?: ((id: string) => void) | null
    /** 传入即显示「清空全部」按钮；不传则完全不渲染。 */
    onClear?: (() => void) | null
    /** 清空按钮的提示文案，默认「清空全部历史」。 */
    clearLabel?: string
    /** 列表非空但正在删除时置灰清空按钮，避免重复点击。 */
    clearDisabled?: boolean
  }>(),
  { activeId: null, onDelete: null, onClear: null, clearLabel: '清空全部历史', clearDisabled: false },
)

const emit = defineEmits<{
  (event: 'select', id: string): void
  (event: 'delete', id: string): void
  (event: 'clear'): void
}>()

const listRef = ref<HTMLElement | null>(null)
defineExpose({ listRef })

/** 行内删除：阻止冒泡，否则点击会连带触发整行的 select。 */
function requestDelete(id: string): void {
  emit('delete', id)
  props.onDelete?.(id)
}

/** 清空全部：只读回调存在性决定是否渲染，点击统一从这里发出。 */
function requestClear(): void {
  emit('clear')
  props.onClear?.()
}
</script>

<template>
  <aside class="history-column">
    <div class="history-head">
      <span class="history-title">{{ title }}</span>
      <span class="history-count">{{ items.length }}</span>
      <button
        v-if="onClear"
        type="button"
        class="btn btn-ghost btn-sm"
        :disabled="clearDisabled || !items.length"
        :title="clearLabel"
        @click="requestClear"
      >
        <Trash2 :size="13" />
      </button>
      <slot name="head-actions" />
    </div>
    <div ref="listRef" class="history-list scroll-y">
      <button
        v-for="item in items"
        :key="item.id"
        type="button"
        class="history-item"
        :class="{ 'history-item-active': item.id === activeId }"
        @click="emit('select', item.id)"
      >
        <div class="history-line">
          <!-- 行首可选标记（如 HTTP 方法色标）；不传则不占位 -->
          <slot name="prefix" :item="item" />
          <span class="history-label truncate" :title="item.label">{{ item.label }}</span>
          <!-- 行尾删除：由组件渲染，保证 stop 冒泡与样式绑定 -->
          <span
            v-if="onDelete"
            class="history-delete"
            role="button"
            tabindex="0"
            title="删除这条记录"
            @click.stop="requestDelete(item.id)"
            @keydown.stop.prevent.enter="requestDelete(item.id)"
            @keydown.stop.prevent.space="requestDelete(item.id)"
          >
            <Trash2 :size="12" />
          </span>
          <slot name="actions" :item="item" />
        </div>
        <div class="history-line history-sub">
          <span class="tag" :class="`tag-${item.tone}`">{{ item.status }}</span>
          <span class="history-meta">{{ item.meta }}</span>
        </div>
      </button>
      <p v-if="!items.length" class="hint history-empty">{{ emptyText }}</p>
    </div>
  </aside>
</template>

<style scoped>
.history-column {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border-right: 1px solid var(--border-subtle);
  background: var(--bg-surface);
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 12px 8px 14px;
  flex: none;
}

.history-title {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.history-count {
  font-size: 10.5px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.history-list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 8px 12px;
}

.history-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.history-item:hover {
  background: var(--bg-hover);
}

.history-item-active {
  background: var(--accent-soft);
  border-color: var(--accent-border);
}

/* 每条记录两行：首行“方法 + 目标”，次行“状态 + 时间”。
   目标地址占满首行剩余宽度，避免 URL 被压成省略号。 */
.history-line {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.history-sub {
  justify-content: space-between;
}

.history-label {
  flex: 1;
  min-width: 0;
  font-size: 11.5px;
  color: var(--text-primary);
}

.history-meta {
  flex: none;
  font-size: 10.5px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.history-empty {
  padding: 22px 12px;
  line-height: 1.7;
}

/* 行内删除：始终显示。
   刻意不用“悬浮才出现”——那样会让 display: none ↔ grid 来回切换，
   而按钮自身高 18px 高于文字行高（约 15.3px），一旦参与 flex 布局就把首行
   从 15.3px 撑到 18px，整条记录随之跳动 2.7px。常驻后高度恒定、行间对齐。
   默认低对比度以免干扰扫读，悬浮整行或按钮本身时才提亮。 */
.history-delete {
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 4px;
  color: var(--text-tertiary);
  cursor: pointer;
  flex: none;
  /* 常驻但弱化：不抢目标地址的视觉权重。 */
  opacity: 0.45;
  transition: opacity 0.14s ease, background 0.14s ease, color 0.14s ease;
}

.history-item:hover .history-delete,
.history-item:focus-within .history-delete {
  opacity: 1;
}

.history-delete:hover {
  background: var(--bg-subtle);
  color: var(--danger);
  opacity: 1;
}

.history-delete:focus-visible {
  outline: 2px solid var(--accent-border);
  outline-offset: 1px;
}
</style>
