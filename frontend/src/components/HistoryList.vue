<script setup lang="ts">
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
 */
import { ref } from 'vue'

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

withDefaults(
  defineProps<{
    title: string
    items: HistoryEntry[]
    emptyText: string
    activeId?: string | null
  }>(),
  { activeId: null },
)

const emit = defineEmits<{
  (event: 'select', id: string): void
}>()

const listRef = ref<HTMLElement | null>(null)
defineExpose({ listRef })
</script>

<template>
  <aside class="history-column">
    <div class="history-head">
      <span class="history-title">{{ title }}</span>
      <span class="history-count">{{ items.length }}</span>
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
          <!-- 行尾可选操作（如删除）；不传则不占位 -->
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

/* 行内操作（如删除）：默认隐藏，悬浮该行才出现，避免列表噪声。
   通过 actions 插槽传入的内容套用此类即可获得一致行为。 */
.history-item :deep(.history-delete) {
  display: none;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 4px;
  color: var(--text-tertiary);
  cursor: pointer;
  flex: none;
}

.history-item:hover :deep(.history-delete) {
  display: grid;
}

.history-item :deep(.history-delete:hover) {
  background: var(--bg-subtle);
  color: var(--danger);
}
</style>
