<script setup lang="ts">
/** 卡片展示组件：接收标题、副标题及内边距选项，通过默认、actions 和 footer 插槽组织内容。依赖父级提供业务内容，不持有请求状态或处理提交错误。 */

/**
 * 统一卡片容器：标题区（图标 + 标题 + 副标题）+ 内容 + 可选页脚。
 *
 * 作用：设置、运行、项目、AI 编排四个页面此前各写一套 card/panel/detail-card
 * 结构与近乎相同的边框圆角样式。收敛为一个组件后，视觉与间距只在一处维护。
 * 用法：默认插槽放内容，`#actions` 放标题右侧操作，`#footer` 放底部操作栏。
 */
defineProps<{
  title?: string
  sub?: string
  /** 是否使用卡片内边距（表单型卡片需要，纯列表型可关掉）。 */
  padded?: boolean
}>()
</script>

<template>
  <section class="app-card">
    <header v-if="title || $slots.actions" class="app-card-head">
      <div class="app-card-title-group">
        <span v-if="$slots.icon" class="app-card-icon"><slot name="icon" /></span>
        <div>
          <h2 v-if="title" class="app-card-title">{{ title }}</h2>
          <p v-if="sub" class="app-card-sub">{{ sub }}</p>
        </div>
      </div>
      <div v-if="$slots.actions" class="app-card-actions"><slot name="actions" /></div>
    </header>
    <div class="app-card-body" :class="{ padded }">
      <slot />
    </div>
    <footer v-if="$slots.footer" class="app-card-foot"><slot name="footer" /></footer>
  </section>
</template>

<style scoped>
.app-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  overflow: hidden;
}

.app-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border-subtle);
}

.app-card-title-group {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.app-card-icon {
  display: grid;
  place-items: center;
  color: var(--accent);
  margin-top: 2px;
  flex: none;
}

.app-card-title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  color: var(--text-primary);
}

.app-card-sub {
  margin: 3px 0 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.app-card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.app-card-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.app-card-body.padded {
  padding: 18px;
}

.app-card-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 18px;
  background: var(--bg-subtle);
  border-top: 1px solid var(--border-subtle);
}
</style>
