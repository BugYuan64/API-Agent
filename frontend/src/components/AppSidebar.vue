<script setup lang="ts">
/**
 * 侧边导航统一承载路由入口、主题与折叠偏好。
 * 折叠只收窄外壳并隐藏文字，保留导航分组与每行高度，保证所有图标纵向位置一致。
 */
import { ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Braces,
  Cable,
  FolderKanban,
  History,
  ListTree,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings as SettingsIcon,
  Sun,
  Terminal,
} from 'lucide-vue-next'
import {
  applyTheme, readSidebarCollapsed, readTheme, writeSidebarCollapsed, writeTheme,
} from '@/lib/appearance'

/** 页面分组同时用于展开标签和折叠后的可访问导航。 */
const groups = [
  {
    label: '接口管理',
    items: [
      { to: '/request', label: '接口测试', icon: Braces },
      { to: '/long-connection', label: '长连接测试', icon: Cable },
    ],
  },
  { label: '任务测试', items: [{ to: '/testcases', label: '用例测试', icon: ListTree }] },
  { label: '项目', items: [{ to: '/projects', label: '项目与环境', icon: FolderKanban }] },
  {
    label: '记录',
    items: [
      { to: '/history', label: '请求历史', icon: History },
      { to: '/executions', label: '执行结果', icon: Terminal },
    ],
  },
  { label: '设置', items: [{ to: '/settings', label: '系统设置', icon: SettingsIcon }] },
]

const route = useRoute()
const theme = ref(readTheme())
const collapsed = ref(readSidebarCollapsed())

/** 按路径识别当前入口，导航子路径仍归属于对应主项。 */
const isActive = (to: string) => route.path === to || route.path.startsWith(`${to}/`)

/** 主题仅修改全局设计令牌的选择属性，页面业务与输入状态继续保留。 */
function toggleTheme(): void {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
}

/** 切换侧栏宽度，两个状态都保留相同路由链接与图标占位。 */
function toggleCollapsed(): void {
  collapsed.value = !collapsed.value
}

/** 偏好变动立即同步根节点与本地存储，失败由外观模块安全回退。 */
watch(theme, (value) => {
  applyTheme(value)
  writeTheme(value)
})
watch(collapsed, writeSidebarCollapsed)
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar-collapsed': collapsed }" aria-label="应用侧边栏">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true"><Braces :size="17" :stroke-width="2.2" /></div>
      <div class="brand-text sidebar-label">
        <span class="brand-name">API-Agent</span>
        <span class="brand-sub">本地测试控制台</span>
      </div>
    </div>

    <div class="quick">
      <RouterLink to="/request" class="quick-action" title="新建请求" aria-label="新建请求">
        <span class="sidebar-icon" aria-hidden="true"><Plus :size="17" :stroke-width="2" /></span>
        <span class="sidebar-label">新建请求</span>
      </RouterLink>
    </div>

    <nav class="nav scroll-y" aria-label="主导航">
      <div v-for="group in groups" :key="group.label" class="nav-group">
        <div class="nav-label sidebar-label" :aria-hidden="collapsed">{{ group.label }}</div>
        <RouterLink
          v-for="item in group.items"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ 'nav-item-active': isActive(item.to) }"
          :title="collapsed ? item.label : undefined"
          :aria-label="item.label"
        >
          <span class="sidebar-icon" aria-hidden="true">
            <component :is="item.icon" :size="17" :stroke-width="1.8" />
          </span>
          <span class="sidebar-label nav-text">{{ item.label }}</span>
        </RouterLink>
      </div>
    </nav>

    <div class="sidebar-foot">
      <button
        class="sidebar-control"
        type="button"
        :title="theme === 'dark' ? '切换亮色主题' : '切换暗色主题'"
        :aria-label="theme === 'dark' ? '切换亮色主题' : '切换暗色主题'"
        @click="toggleTheme"
      >
        <span class="sidebar-icon" aria-hidden="true">
          <Sun v-if="theme === 'dark'" :size="17" :stroke-width="1.8" />
          <Moon v-else :size="17" :stroke-width="1.8" />
        </span>
        <span class="sidebar-label">{{ theme === 'dark' ? '亮色主题' : '暗色主题' }}</span>
      </button>
      <button
        class="sidebar-control"
        type="button"
        :title="collapsed ? '展开侧边栏' : '折叠侧边栏'"
        :aria-label="collapsed ? '展开侧边栏' : '折叠侧边栏'"
        :aria-expanded="!collapsed"
        @click="toggleCollapsed"
      >
        <span class="sidebar-icon" aria-hidden="true">
          <PanelLeftOpen v-if="collapsed" :size="17" :stroke-width="1.8" />
          <PanelLeftClose v-else :size="17" :stroke-width="1.8" />
        </span>
        <span class="sidebar-label">{{ collapsed ? '展开侧边栏' : '折叠侧边栏' }}</span>
      </button>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  display: flex;
  flex: none;
  flex-direction: column;
  width: var(--sidebar-width, 248px);
  height: 100%;
  overflow: hidden;
  color: var(--text-primary);
  background: var(--bg-sidebar, var(--bg-surface));
  border-right: 1px solid var(--border-subtle);
  transition: width var(--motion-normal, 220ms) ease;
}

.sidebar-collapsed { width: var(--sidebar-collapsed-width, 72px); }

.brand {
  display: flex;
  flex: none;
  align-items: center;
  gap: 11px;
  height: var(--topbar-height);
  padding: 0 22px;
}

.brand-mark {
  display: grid;
  flex: none;
  place-items: center;
  width: 27px;
  height: 27px;
  border: 1px solid var(--border-highlight, var(--border-default));
  border-radius: 8px;
  color: var(--text-on-accent, var(--text-primary));
  background: var(--accent-gradient);
  box-shadow: var(--shadow-sm);
}

.brand-text { line-height: 1.4; }
.brand-name { display: block; font-size: 14px; font-weight: 650; letter-spacing: -0.025em; }
.brand-sub { display: block; color: var(--text-tertiary); font-size: 10px; }

/* 文本仍参与固定高度的行布局，只渐隐并由外壳裁剪。 */
.sidebar-label {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  transition: opacity var(--motion-fast, 140ms) ease;
}
.sidebar-collapsed .sidebar-label { opacity: 0; pointer-events: none; }

.quick { flex: none; padding: 5px 12px 16px; }
.quick-action,
.nav-item,
.sidebar-control {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 36px;
  padding: 0 12px;
  border-radius: var(--radius-md);
  overflow: hidden;
}

.sidebar-icon {
  display: grid;
  flex: none;
  place-items: center;
  width: 22px;
  height: 22px;
}
.sidebar-icon svg { flex: none; }
.quick-action {
  color: var(--text-on-accent, var(--text-primary));
  background: var(--accent-gradient);
  box-shadow: inset 0 0 0 1px var(--border-highlight, var(--border-default)), var(--shadow-sm);
  font-size: 12.5px;
  font-weight: 600;
  transition: background var(--motion-fast, 140ms) ease;
}
.quick-action:hover { background: var(--accent-gradient-hover); }

.nav { flex: 1; padding: 0 12px 12px; overflow-x: hidden; scrollbar-gutter: auto; }
.nav-group + .nav-group { margin-top: 13px; }
.nav-label { height: 25px; padding: 2px 12px 0; color: var(--text-tertiary); font-size: 10px; font-weight: 550; letter-spacing: 0.045em; }
.nav-item { color: var(--text-secondary); font-size: 12.5px; font-weight: 500; transition: color var(--motion-fast, 140ms) ease, background var(--motion-fast, 140ms) ease; }
.nav-item:hover { color: var(--text-primary); background: var(--bg-hover); }
.nav-item-active { color: var(--text-primary); font-weight: 600; background: var(--nav-active-gradient, var(--accent-soft)); box-shadow: inset 0 0 0 1px var(--border-highlight, var(--accent-border)); }
.nav-item-active:hover { color: var(--text-primary); background: var(--nav-active-gradient, var(--accent-soft)); }

.sidebar-foot {
  display: flex;
  flex: none;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  border-top: 1px solid var(--border-subtle);
}
.sidebar-control { border: 0; color: var(--text-secondary); background: transparent; text-align: left; font-size: 12px; transition: color var(--motion-fast, 140ms) ease, background var(--motion-fast, 140ms) ease; }
.sidebar-control:hover { color: var(--text-primary); background: var(--bg-hover); }
.quick-action:focus-visible,
.nav-item:focus-visible,
.sidebar-control:focus-visible { outline: 2px solid var(--focus-ring, var(--accent)); outline-offset: -2px; }

@media (prefers-reduced-motion: reduce) {
  .sidebar,
  .sidebar-label,
  .quick-action,
  .nav-item,
  .sidebar-control { transition: none; }
}
</style>
