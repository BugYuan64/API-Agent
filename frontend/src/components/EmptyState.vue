<script setup lang="ts">
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
