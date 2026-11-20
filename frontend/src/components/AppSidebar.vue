<script setup lang="ts">
/**
 * 侧边导航统一承载路由入口、主题与折叠偏好。
 * 折叠只收窄外壳并隐藏文字，保留导航分组与每行高度，保证所有图标纵向位置一致。
 */
import { ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Braces,
  Boxes,
  Cable,
  History,
  ListTree,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Settings as SettingsIcon,
  Sun,
} from 'lucide-vue-next'
import {
  readSidebarCollapsed, readTheme, transitionTheme, writeSidebarCollapsed, writeTheme,
} from '@/lib/appearance'
import { COPY } from '@/lib/copy'

/** 页面分组同时用于展开标签和折叠后的可访问导航。 */
const groups = [
  {
    label: COPY.navigation.singleTests,
    items: [
      { to: '/request', label: '接口测试', icon: Braces },
      { to: '/long-connection', label: '长连接测试', icon: Cable },
    ],
  },
  { label: COPY.navigation.intelligentTests, items: [{ to: '/testcases', label: COPY.navigation.intelligentTests, icon: ListTree }] },
  {
    label: COPY.navigation.records,
    items: [
      { to: '/history', label: COPY.navigation.executionResults, icon: History },
    ],
  },
  { label: COPY.navigation.settings, items: [{ to: '/settings', label: COPY.navigation.systemSettings, icon: SettingsIcon }] },
]

const route = useRoute()
const theme = ref(readTheme())
const collapsed = ref(readSidebarCollapsed())

/** 按路径识别当前入口，导航子路径仍归属于对应主项。 */
const isActive = (to: string) => route.path === to || route.path.startsWith(`${to}/`)

/** 主题仅修改全局设计令牌的选择属性，页面业务与输入状态继续保留。 */
function toggleTheme(event?: MouseEvent): void {
  const target = event?.currentTarget as HTMLElement | null
  const bounds = target?.getBoundingClientRect()
  document.documentElement.style?.setProperty('--theme-origin-x', `${bounds ? bounds.left + bounds.width / 2 : 32}px`)
  document.documentElement.style?.setProperty('--theme-origin-y', `${bounds ? bounds.top + bounds.height / 2 : 32}px`)
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
}

/** 切换侧栏宽度，两个状态都保留相同路由链接与图标占位。 */
function toggleCollapsed(): void {
  collapsed.value = !collapsed.value
}

/** 偏好变动立即同步根节点与本地存储，失败由外观模块安全回退。 */
watch(theme, (value) => {
  transitionTheme(value)
  writeTheme(value)
})
watch(collapsed, writeSidebarCollapsed)
</script>

<template>
  <aside class="sidebar" :class="{ 'sidebar-collapsed': collapsed }" aria-label="应用侧边栏">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true"><Boxes :size="18" :stroke-width="1.8" /></div>
      <div class="brand-text sidebar-label">
        <span class="brand-name">{{ COPY.app.name }}</span>
        <span class="brand-sub">{{ COPY.app.subtitle }}</span>
      </div>
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
          :title="item.label"
          :aria-label="item.label"
          :aria-current="isActive(item.to) ? 'page' : undefined"
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
        class="sidebar-control sidebar-toggle"
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
  width: var(--sidebar-width);
  height: 100%;
  overflow: hidden;
  color: var(--text-primary);
  background: var(--bg-sidebar, var(--bg-surface));
  transition: width 560ms cubic-bezier(.22,.75,.2,1), background 560ms ease, color 560ms ease;
}

.sidebar-collapsed { width: var(--sidebar-collapsed-width); }

.brand {
  display: flex;
  flex: none;
  align-items: center;
  gap: 11px;
  height: calc(var(--topbar-height) + 14px);
  padding: 0 16px;
}

.brand-mark {
  display: grid;
  flex: none;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  color: var(--text-on-accent, var(--text-primary));
  background: var(--brand-fill);
  box-shadow: inset 0 0 0 1px var(--border-highlight), var(--shadow-sm);
  transform: translateX(0) scale(1);
  transition: transform 560ms cubic-bezier(.22,.75,.2,1), box-shadow 560ms ease;
}

.brand-text { line-height: 1.4; }
.brand-name { display: block; font-size: 14px; font-weight: 650; }
.brand-sub { display: block; color: var(--text-tertiary); font-size: 10.5px; }

/* 文本仍参与固定高度的行布局，只渐隐并由外壳裁剪。 */
.sidebar-label {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  transition: opacity 280ms ease, transform 560ms cubic-bezier(.22,.75,.2,1);
}
.sidebar-collapsed .sidebar-label { opacity: 0; pointer-events: none; }
.sidebar-collapsed .brand-mark { transform: translateX(-1px) scale(.94); }
.brand-text,
.nav-group,
.sidebar-foot { transform: translateX(0); transition: transform 560ms cubic-bezier(.22,.75,.2,1); }
.sidebar-collapsed .brand-text,
.sidebar-collapsed .nav-group,
.sidebar-collapsed .sidebar-foot { transform: translateX(-7px); }

.nav-item,
.sidebar-control {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 33px;
  padding: 0 10px;
  border-radius: var(--radius-md);
  overflow: hidden;
}

.sidebar-icon {
  display: grid;
  flex: none;
  place-items: center;
  width: 20px;
  height: 20px;
}
.sidebar-icon svg { flex: none; width: 15px; height: 15px; }
.nav { flex: 1; padding: 0 9px 12px; overflow-x: hidden; scrollbar-gutter: auto; }
.nav-group + .nav-group { margin-top: 16px; }
.nav-label { height: 23px; padding: 2px 10px 0; color: var(--text-tertiary); font-size: 11px; font-weight: 550; }
.nav-item { color: var(--text-secondary); font-size: 12.5px; font-weight: 500; transition: color var(--motion-fast, 140ms) ease, background var(--motion-fast, 140ms) ease; }
.nav-item:hover { color: var(--text-primary); background: var(--bg-hover); }
.nav-item + .nav-item { margin-top: 2px; }
.nav-item-active,
.nav-item-active:hover { color: var(--text-primary); font-weight: 600; background: var(--bg-active); }
.nav-item-active .sidebar-icon { color: var(--accent); }

.sidebar-foot {
  display: flex;
  flex: none;
  flex-direction: column;
  gap: 4px;
  padding: 12px 9px;
}
.sidebar-control { border: 0; color: var(--text-secondary); background: transparent; text-align: left; font-size: 12px; transition: color var(--motion-fast, 140ms) ease, background var(--motion-fast, 140ms) ease; }
.sidebar-control:hover { color: var(--text-primary); background: var(--bg-hover); }
.nav-item:focus-visible,
.sidebar-control:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/* 小屏自动收为图标栏，不改写用户的桌面折叠偏好。 */
@media (max-width: 720px) {
  .sidebar { width: var(--sidebar-collapsed-width); }
  .sidebar-label { opacity: 0; pointer-events: none; }
  .sidebar-toggle { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar,
  .sidebar .sidebar-label,
  .sidebar .brand-mark,
  .sidebar .brand-text,
  .sidebar .nav-group,
  .sidebar .sidebar-foot {
    transition-duration: 560ms !important;
    transition-timing-function: cubic-bezier(.22,.75,.2,1) !important;
  }
}
</style>
