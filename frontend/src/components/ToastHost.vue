<script setup lang="ts">
/** 全局提示宿主：依赖共享 toast store 和语义图标，显示队列并允许用户关闭。提示内容和保留时间由 store 与调用方决定，不更改业务结果。 */

import { computed } from 'vue'
import { CheckCircle2, Info, TriangleAlert, X, XCircle } from 'lucide-vue-next'
import { useToastStore } from '@/stores/toasts'

const toastStore = useToastStore()

/** 每种语义色对应一个图标，形状本身也传达含义（不只靠颜色）。 */
const icons = { success: CheckCircle2, danger: XCircle, warning: TriangleAlert, info: Info }

/** 读取 store 的响应式提示队列用于展示；过期和关闭操作仍由 store 维护。 */
const items = computed(() => toastStore.items)
</script>

<template>
  <TransitionGroup name="toast" tag="div" class="toast-stack">
    <div v-for="item in items" :key="item.id" class="toast" :class="`toast-${item.tone}`">
      <component :is="icons[item.tone]" :size="16" class="toast-icon" />
      <div class="toast-body">
        <div class="toast-title">{{ item.title }}</div>
        <div v-if="item.detail" class="toast-detail">{{ item.detail }}</div>
      </div>
      <button class="toast-close" aria-label="关闭提示" @click="toastStore.dismiss(item.id)">
        <X :size="13" />
      </button>
    </div>
  </TransitionGroup>
</template>

<style scoped>
.toast-stack {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 90;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 380px;
}

.toast {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 10px 11px;
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  border: 1px solid var(--border-default);
  box-shadow: var(--shadow-lg);
}

.toast-icon {
  margin-top: 2px;
  flex: none;
}

.toast-success .toast-icon { color: var(--success); }
.toast-danger .toast-icon { color: var(--danger); }
.toast-warning .toast-icon { color: var(--warning); }
.toast-info .toast-icon { color: var(--info); }

.toast-body {
  flex: 1;
  min-width: 0;
}

.toast-title {
  font-weight: 600;
  font-size: 12.5px;
}

.toast-detail {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--text-secondary);
  word-break: break-word;
}

.toast-close {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text-tertiary);
}

.toast-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(6px) scale(0.98);
}
</style>
