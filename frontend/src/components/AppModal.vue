<script setup lang="ts">
/** 受控弹窗组件：接收 open、标题和按钮配置，输出打开状态变更、取消与确认事件。依赖 Vue 生命周期及 DOM 监听，负责 Esc、遮罩关闭和滚动锁定；保存校验由父级负责。 */

/**
 * 通用模态弹窗。
 *
 * 作用：设置页里引擎的“配置”入口需要在不离开页面的前提下展开详情
 * （如 postmanmcp 的服务元信息、工具清单与接入配置）。抽成通用组件后，
 * 遮罩、Esc 关闭、滚动锁定与页脚按钮布局都只有一份实现。
 *
 * 组成：标题栏（可带副标题）+ 内容插槽 + 页脚插槽（默认提供「取消 / 保存」）。
 * 行为：点击遮罩或按 Esc 关闭；打开时锁定页面滚动，关闭后恢复。
 */
import { onBeforeUnmount, watch } from 'vue'
import { X } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    subtitle?: string
    /** 页脚主按钮文案；为空则只显示“取消”。 */
    confirmText?: string
    cancelText?: string
    /** 主按钮是否禁用（如尚未改动或校验未通过）。 */
    confirmDisabled?: boolean
    width?: string
  }>(),
  { confirmText: '保存', cancelText: '取消', confirmDisabled: false, width: '760px' },
)

const emit = defineEmits<{
  (event: 'update:open', value: boolean): void
  (event: 'confirm'): void
  (event: 'cancel'): void
}>()

/** 通知父级关闭弹窗并发送 cancel；不直接修改受控 open，也不执行保存。 */
function close(): void {
  emit('update:open', false)
  emit('cancel')
}

/** 处理键盘事件，仅 Escape 触发取消关闭；其他按键保留默认行为。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

/** 打开时禁止背景滚动，避免滚轮带动下方长表单。 */
function lockScroll(open: boolean): void {
  if (typeof document === 'undefined') return
  document.body.style.overflow = open ? 'hidden' : ''
}

/** 按 open 同步滚动锁与 Esc 监听，立即执行以覆盖初始打开状态；关闭时移除监听。 */
watch(
  () => props.open,
  (open) => {
    lockScroll(open)
    if (open) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
  { immediate: true },
)

/** 组件销毁时解除背景滚动锁和键盘监听，避免路由切换后留下全局副作用。 */
onBeforeUnmount(() => {
  lockScroll(false)
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="open" class="modal-mask" @click.self="close">
        <div class="modal" role="dialog" aria-modal="true" :aria-label="title" :style="{ width }">
          <header class="modal-head">
            <div class="modal-title-group">
              <h2 class="modal-title">{{ title }}</h2>
              <p v-if="subtitle" class="modal-sub">{{ subtitle }}</p>
            </div>
            <button type="button" class="modal-close" aria-label="关闭" @click="close">
              <X :size="15" />
            </button>
          </header>

          <div class="modal-body scroll-y">
            <slot />
          </div>

          <footer class="modal-foot">
            <slot name="footer">
              <button type="button" class="btn btn-ghost" @click="close">{{ cancelText }}</button>
              <button
                v-if="confirmText"
                type="button"
                class="btn btn-accent"
                :disabled="confirmDisabled"
                @click="emit('confirm')"
              >
                {{ confirmText }}
              </button>
            </slot>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  z-index: 120;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgb(23 23 26 / 42%);
}

.modal {
  display: flex;
  flex-direction: column;
  max-width: 100%;
  max-height: calc(100vh - 48px);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-subtle);
  flex: none;
}

.modal-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  color: var(--text-primary);
}

.modal-sub {
  margin: 3px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.modal-close {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  flex: none;
}

.modal-close:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.modal-body {
  flex: 1;
  min-height: 0;
  padding: 18px 20px;
  overflow-y: auto;
}

.modal-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  flex: none;
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.16s ease;
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

.modal-enter-active .modal,
.modal-leave-active .modal {
  transition: transform 0.16s ease;
}

.modal-enter-from .modal,
.modal-leave-to .modal {
  transform: translateY(8px) scale(0.99);
}

@media (max-width: 720px) {
  .modal-mask {
    padding: 12px;
  }
}
</style>
