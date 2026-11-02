<script setup lang="ts">
import { Compass } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
</script>

<template>
  <div class="not-found">
    <EmptyState
      :icon="Compass"
      title="页面不存在"
      description="这个地址没有对应的界面。可能是输错了路径，或该功能还没实现。"
    >
      <RouterLink to="/request" class="btn btn-primary btn-sm">回到接口调试</RouterLink>
    </EmptyState>
  </div>
</template>

<style scoped>
.not-found {
  display: grid;
  place-items: center;
  height: 100%;
}
</style>
