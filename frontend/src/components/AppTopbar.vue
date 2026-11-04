<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Activity, RefreshCw, ServerCog, Settings as SettingsIcon } from 'lucide-vue-next'
import { engineStatus, getHealth } from '@/api/endpoints'
import type { EngineStatus } from '@/api/types'
import { engineMeta } from '@/lib/engines'
import { useEngineSelection } from '@/lib/engines'

/** 顶栏展示两个真实状态：管理后端是否存活、执行引擎是否可用。
 *  两者都失败时界面仍可用（页面会各自提示），但用户能立刻知道问题出在哪一层。 */
const healthError = ref(false)
const engine = ref<EngineStatus | null>(null)
const loading = ref(false)

/** 顶栏显示“当前选中的引擎”及其状态，而不是写死某一个引擎名称。 */
const { selected: selectedEngine, refresh: refreshEngineList } = useEngineSelection()

async function refresh() {
  loading.value = true
  try {
    // 首次显示时先确保引擎列表（含服务端默认值）已就绪，再按选中项探活，
    // 否则顶栏会一直停在本地默认值上。
    if (!engine.value) await refreshEngineList()
    const [healthResult, engineResult] = await Promise.allSettled([
      getHealth(),
      engineStatus(selectedEngine.value),
    ])
    healthError.value = healthResult.status === 'rejected'
    engine.value = engineResult.status === 'fulfilled' ? engineResult.value : null
  } finally {
    loading.value = false
  }
}

const engineLabel = computed(() => {
  const name = engineMeta(selectedEngine.value).name
  if (!engine.value) return `${name} 状态未知`
  if (!engine.value.available) return `${name} 不可用`
  return engine.value.version ? `${name} v${engine.value.version}` : name
})

const engineTone = computed(() => {
  if (!engine.value) return 'neutral'
  return engine.value.available ? 'success' : 'warning'
})

onMounted(refresh)
</script>

<template>
  <header class="topbar">
    <div class="crumbs">
      <span class="crumb-root">API-Agent</span>
      <span class="crumb-sep">/</span>
      <span class="crumb-current">{{ $route.meta.title ?? '控制台' }}</span>
    </div>

    <div class="status">
      <div class="pill" :class="healthError ? 'pill-danger' : 'pill-success'">
        <Activity :size="12" />
        <span>{{ healthError ? '后端未连接' : '后端在线' }}</span>
      </div>
      <div class="pill" :class="`pill-${engineTone}`">
        <ServerCog :size="12" />
        <span>{{ engineLabel }}</span>
      </div>
      <button class="btn btn-ghost btn-icon" title="刷新状态" :disabled="loading" @click="refresh">
        <RefreshCw :size="14" :class="{ spinning: loading }" />
      </button>
      <RouterLink to="/settings" class="btn btn-ghost btn-icon" title="系统设置与大模型配置">
        <SettingsIcon :size="14" />
      </RouterLink>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: var(--topbar-height);
  flex: none;
  padding: 0 16px;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  min-width: 0;
}

.crumb-root {
  color: var(--text-tertiary);
}

.crumb-sep {
  color: var(--border-strong);
}

.crumb-current {
  font-weight: 600;
  color: var(--text-primary);
}

.status {
  display: flex;
  align-items: center;
  gap: 7px;
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 25px;
  padding: 0 9px;
  border-radius: 99px;
  font-size: 11.5px;
  font-weight: 500;
  border: 1px solid transparent;
}

.pill-success {
  background: var(--success-soft);
  color: var(--success);
  border-color: #cfeee2;
}

.pill-warning {
  background: var(--warning-soft);
  color: var(--warning);
  border-color: #f3e0bd;
}

.pill-danger {
  background: var(--danger-soft);
  color: var(--danger);
  border-color: #f5d0d0;
}

.pill-neutral {
  background: var(--bg-subtle);
  color: var(--text-secondary);
  border-color: var(--border-default);
}

.spinning {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
