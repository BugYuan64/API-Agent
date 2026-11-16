<script setup lang="ts">
/** 侧边导航组件：依赖 Vue Router 和 Lucide icons，根据当前路径推导激活项与标签。 */

import { computed } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Braces,
  Cable,
  FolderKanban,
  History,
  ListTree,
  Plus,
  Settings as SettingsIcon,
  Terminal,
} from 'lucide-vue-next'

/** 侧边栏分组：接口测试、任务测试、项目、记录和设置。 */
const groups = [
  {
    label: '接口管理',
    items: [
      { to: '/request', label: '接口测试', icon: Braces },
      { to: '/long-connection', label: '长连接测试', icon: Cable },
    ],
  },
  {
    label: '任务测试',
    items: [{ to: '/testcases', label: '用例测试', icon: ListTree }],
  },
  {
    label: '项目',
    items: [{ to: '/projects', label: '项目与环境', icon: FolderKanban }],
  },
  {
    label: '记录',
    items: [
      { to: '/history', label: '请求历史', icon: History },
      { to: '/executions', label: '执行结果', icon: Terminal },
    ],
  },
  {
    label: '设置',
    items: [
      { to: '/settings', label: '系统设置', icon: SettingsIcon },
    ],
  },
]

const route = useRoute()
/** 按当前路径判断入口是否激活：完全匹配或入口子路径均返回 true；不执行导航。 */
const isActive = (to: string) => route.path === to || route.path.startsWith(`${to}/`)

/** 根据当前路由的首个匹配入口计算导航标签；未知页面回退“控制台”，不触发跳转。 */
const activeLabel = computed(() => {
  for (const group of groups) {
    const hit = group.items.find((item) => isActive(item.to))
    if (hit) return hit.label
  }
  return '控制台'
})
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-mark">
        <Braces :size="15" :stroke-width="2.4" />
      </div>
      <div class="brand-text">
        <span class="brand-name">API-Agent</span>
        <span class="brand-sub">本地测试控制台</span>
      </div>
    </div>

    <div class="quick">
      <RouterLink to="/request" class="quick-action">
        <Plus :size="14" />
        <span>新建请求</span>
      </RouterLink>
    </div>

    <nav class="nav scroll-y">
      <div v-for="group in groups" :key="group.label" class="nav-group">
        <div class="nav-label">{{ group.label }}</div>
        <RouterLink
          v-for="item in group.items"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ 'nav-item-active': isActive(item.to) }"
        >
          <component :is="item.icon" :size="15" :stroke-width="1.9" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </div>
    </nav>

    <div class="sidebar-foot">
      <span class="foot-dot" />
      <span class="truncate">{{ activeLabel }}</span>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex-direction: column;
  width: var(--sidebar-width);
  flex: none;
  height: 100%;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-subtle);
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  height: var(--topbar-height);
  padding: 0 14px;
  flex: none;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: var(--text-primary);
  color: var(--text-inverse);
}

.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  min-width: 0;
}

.brand-name {
  font-size: 13px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.brand-sub {
  font-size: 10.5px;
  color: var(--text-tertiary);
}

.quick {
  padding: 2px 10px 10px;
  flex: none;
}

.quick-action {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  border-radius: var(--radius-md);
  background: var(--text-primary);
  color: var(--text-inverse);
  font-size: 12.5px;
  font-weight: 550;
}

.quick-action:hover {
  background: #2a2a30;
}

.nav {
  flex: 1;
  padding: 0 10px 12px;
}

.nav-group + .nav-group {
  margin-top: 14px;
}

.nav-label {
  padding: 0 6px 5px;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-tertiary);
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 9px;
  height: 31px;
  padding: 0 8px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 12.5px;
  font-weight: 500;
}

.nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.nav-item-active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}

.nav-item-active:hover {
  background: var(--accent-soft);
  color: var(--accent);
}

.sidebar-foot {
  display: flex;
  align-items: center;
  gap: 7px;
  flex: none;
  height: 34px;
  padding: 0 16px;
  border-top: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
}

.foot-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--success);
  flex: none;
}
</style>
