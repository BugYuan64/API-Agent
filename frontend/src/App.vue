<script setup lang="ts">
/** 应用根组件：组合侧栏、顶栏、路由内容与全局提示。依赖路由及共享布局组件；业务请求、页面状态和错误处理由各子组件负责。 */

import AppSidebar from '@/components/AppSidebar.vue'
import AppTopbar from '@/components/AppTopbar.vue'
import ToastHost from '@/components/ToastHost.vue'
</script>

<template>
  <div class="shell">
    <AppSidebar />
    <div class="main">
      <AppTopbar />
      <main class="content scroll-y">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </main>
    </div>
    <ToastHost />
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: calc(100% - 24px);
  margin: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  background: var(--bg-canvas);
  box-shadow: var(--shadow-md);
  overflow: hidden;
}

.main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}

.content {
  flex: 1;
  min-height: 0;
  background: var(--bg-canvas);
}

@media (max-width: 900px) {
  .shell {
    height: 100%;
    margin: 0;
    border: 0;
    border-radius: 0;
  }
}
</style>
