<script setup lang="ts">
/**
 * 侧边导航统一承载路由入口、主题与折叠偏好。
 * 点击项目信号动画 Logo 即可切换侧栏收起/展开，悬停时以气泡显示项目名称。
 */
import { ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import {
  Braces,
  Cable,
  Cpu,
  History,
  ListTree,
  Moon,
  Settings as SettingsIcon,
  Sun,
} from 'lucide-vue-next'
import ProjectLogo from './ProjectLogo.vue'
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
    label: COPY.navigation.modelTesting,
    items: [
      { to: '/model-testing', label: COPY.navigation.modelTesting, icon: Cpu },
    ],
  },
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

/** 点击项目 Logo 切换侧栏宽度，两个状态都保留相同路由链接与图标占位。 */
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
      <button
        class="brand-trigger"
        type="button"
        :aria-label="`${COPY.app.name}，${collapsed ? '展开侧边栏' : '折叠侧边栏'}`"
        :aria-expanded="!collapsed"
        @click="toggleCollapsed"
      >
        <ProjectLogo :size="32" />
        <span class="brand-tooltip" role="tooltip">
          <span class="brand-tooltip-name">API-Agent · {{ COPY.app.name }}</span>
          <span class="brand-tooltip-hint">{{ collapsed ? '点击展开侧边栏' : '点击收起侧边栏' }}</span>
        </span>
      </button>
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
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  position: relative;
  z-index: 30;
  display: flex;
  flex: none;
  flex-direction: column;
  width: var(--sidebar-width);
  height: 100%;
  overflow: visible;
  color: var(--text-primary);
  background: var(--bg-sidebar, var(--bg-surface));
  transition: width 560ms cubic-bezier(.22,.75,.2,1), background 560ms ease, color 560ms ease;
}

.sidebar-collapsed { width: var(--sidebar-collapsed-width); }

.brand {
  position: relative;
  z-index: 40;
  display: flex;
  flex: none;
  align-items: center;
  height: calc(var(--topbar-height) + 14px);
  padding: 0 13px;
  overflow: visible;
}

.brand-trigger {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 10px;
  background: transparent;
  cursor: pointer;
  transform: translateX(0) scale(1);
  transition: transform 240ms cubic-bezier(.22,.75,.2,1);
}

.brand-trigger:hover {
  transform: scale(1.05);
}

.brand-trigger:active {
  transform: scale(0.96);
}

.brand-trigger:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

/* 悬停或聚焦 Logo 时在侧栏右侧弹出项目名称气泡，折叠与展开态均可完整展示。 */
.brand-tooltip {
  position: absolute;
  top: 50%;
  left: calc(100% + 12px);
  z-index: 60;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 7px 11px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-md);
  white-space: nowrap;
  pointer-events: none;
  opacity: 0;
  visibility: hidden;
  transform: translate(-6px, -50%);
  transition: opacity 160ms ease, transform 160ms ease, visibility 160ms ease;
}

.brand-tooltip::before {
  content: '';
  position: absolute;
  top: 50%;
  left: -5px;
  width: 8px;
  height: 8px;
  border-left: 1px solid var(--border-default);
  border-bottom: 1px solid var(--border-default);
  background: var(--bg-surface);
  transform: translateY(-50%) rotate(45deg);
}

.brand-trigger:hover .brand-tooltip,
.brand-trigger:focus-visible .brand-tooltip {
  opacity: 1;
  visibility: visible;
  transform: translate(0, -50%);
}

.brand-tooltip-name {
  font-size: 12.5px;
  font-weight: 650;
  line-height: 1.3;
}

.brand-tooltip-hint {
  color: var(--text-tertiary);
  font-size: 10.5px;
  line-height: 1.2;
}

/* 文本仍参与固定高度的行布局，只渐隐并由子容器裁剪。 */
.sidebar-label {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  transition: opacity 280ms ease, transform 560ms cubic-bezier(.22,.75,.2,1);
}
.sidebar-collapsed .sidebar-label { opacity: 0; pointer-events: none; }
.nav-group,
.sidebar-foot { transform: translateX(0); transition: transform 560ms cubic-bezier(.22,.75,.2,1); }
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
  overflow: hidden;
}
.sidebar-control { border: 0; color: var(--text-secondary); background: transparent; text-align: left; font-size: 12px; transition: color var(--motion-fast, 140ms) ease, background var(--motion-fast, 140ms) ease; }
.sidebar-control:hover { color: var(--text-primary); background: var(--bg-hover); }
.nav-item:focus-visible,
.sidebar-control:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/* 小屏自动收为图标栏，不改写用户的桌面折叠偏好。 */
@media (max-width: 720px) {
  .sidebar { width: var(--sidebar-collapsed-width); }
  .sidebar-label { opacity: 0; pointer-events: none; }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar,
  .sidebar .sidebar-label,
  .sidebar .brand-trigger,
  .sidebar .nav-group,
  .sidebar .sidebar-foot {
    transition-duration: 560ms !important;
    transition-timing-function: cubic-bezier(.22,.75,.2,1) !important;
  }
}
</style>
