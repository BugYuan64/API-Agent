<script setup lang="ts">
/** 空状态展示组件：接收标题、可选说明及 Vue 图标组件，由父级决定何时展示。仅负责呈现，不判断空数据原因或自动重试请求。 */

import type { Component } from 'vue'

withDefaults(
  defineProps<{
    icon?: Component
    title: string
    description?: string
  }>(),
  { icon: undefined, description: undefined },
)
</script>

<template>
  <div class="empty">
    <div v-if="icon" class="empty-icon">
      <component :is="icon" :size="20" />
    </div>
    <div class="empty-title">{{ title }}</div>
    <p v-if="description" class="empty-description">{{ description }}</p>
    <slot />
  </div>
</template>

<style scoped>
.empty-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}

.empty-description {
  margin: 0;
  max-width: 42ch;
  font-size: 12px;
  color: var(--text-tertiary);
  line-height: 1.65;
}
</style>
