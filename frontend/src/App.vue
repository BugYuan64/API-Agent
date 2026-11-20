<script setup lang="ts">
/** 应用根组件：侧栏置于工作区之外，顶栏与页面共享一层画布。业务状态和错误处理由各子组件负责。 */

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
  height: 100%;
  background: var(--bg-app);
}

.main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  margin: 7px 7px 7px 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.content {
  flex: 1;
  min-height: 0;
  background: var(--bg-canvas);
}

@media (max-width: 720px) {
  .main {
    margin: 0;
    border: 0;
    border-left: 1px solid var(--border-subtle);
    border-radius: 0;
    box-shadow: none;
  }
}
</style>
