<script setup lang="ts">
/** Live editor for the active light/dark palette; edits are scoped to that theme. */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, RotateCcw } from 'lucide-vue-next'
import AssistantAvatar from './AssistantAvatar.vue'
import { COLOR_GROUPS, COLOR_LABELS, DEFAULT_PALETTES, getPalette, resetPalette, savePalette, type PaletteTheme } from '@/lib/palette'

const theme = ref<PaletteTheme>('dark')
const palette = ref<Record<string, string>>(getPalette('dark'))
const saved = ref(false)
const title = computed(() => theme.value === 'dark' ? '暗色主题' : '亮色主题')
let themeObserver: MutationObserver | null = null

/** Load the active theme and refresh the editor when the OS-independent theme changes. */
function syncTheme() {
  const active = document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
  theme.value = active
  palette.value = getPalette(active)
}

/** Persist each color change immediately and apply the complete palette consistently. */
function updateColor(token: string, value: string) {
  palette.value[token] = value.toUpperCase()
  savePalette(theme.value, palette.value)
  saved.value = true
}

/** Reset only this theme, preserving custom values for the other palette. */
function resetCurrentTheme() {
  resetPalette(theme.value)
  palette.value = { ...DEFAULT_PALETTES[theme.value] }
  saved.value = false
}

onMounted(() => {
  syncTheme()
  if (typeof MutationObserver !== 'undefined') {
    themeObserver = new MutationObserver(syncTheme)
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
  }
})
onBeforeUnmount(() => { themeObserver?.disconnect(); themeObserver = null })
</script>

<template>
  <section class="palette-settings" aria-labelledby="palette-title">
    <header class="palette-head">
      <div>
        <h2 id="palette-title">主题配色</h2>
        <p>编辑当前的{{ title }}调色板；修改会立即应用并保存在此浏览器。</p>
      </div>
      <div class="palette-actions">
        <span v-if="saved" class="saved-state" role="status"><Check :size="13" />已保存</span>
        <button type="button" class="btn btn-sm" @click="resetCurrentTheme"><RotateCcw :size="13" />重置{{ title }}</button>
      </div>
    </header>
    <label class="theme-select">
      <span>编辑主题</span>
      <select v-model="theme" class="select" aria-label="编辑哪个主题的配色" @change="palette = getPalette(theme)">
        <option value="dark">暗色主题</option>
        <option value="light">亮色主题</option>
      </select>
    </label>
    <div v-for="group in COLOR_GROUPS" :key="group.label" class="color-group">
      <div class="color-group-head">
        <h3>{{ group.label }}</h3>
        <div
          v-if="(group.tokens as readonly string[]).includes('assistant-color')"
          class="assistant-preview"
          :style="{
            '--assistant-color': palette['assistant-color'],
            '--assistant-eye-color': palette['assistant-eye-color'],
          }"
        >
          <AssistantAvatar :size="26" />
          <span>小Y 头像实时预览（点击切换表情）</span>
        </div>
      </div>
      <div class="color-grid">
        <label v-for="token in group.tokens" :key="token" class="color-field">
          <span>{{ COLOR_LABELS[token] }}</span>
          <input :value="palette[token]" type="color" :aria-label="`${title}${COLOR_LABELS[token]}颜色`"
            @input="updateColor(token, ($event.target as HTMLInputElement).value)" />
          <code>{{ palette[token] }}</code>
        </label>
      </div>
    </div>
  </section>
</template>

<style scoped>
.palette-settings { display: grid; gap: 16px; }
.palette-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.palette-head h2 { margin: 0; font-size: 15px; }
.palette-head p { margin: 4px 0 0; color: var(--text-secondary); }
.palette-actions { display: flex; align-items: center; gap: 10px; flex: none; }
.saved-state { display: inline-flex; align-items: center; gap: 4px; color: var(--success); font-size: 11px; }
.theme-select { display: grid; grid-template-columns: 120px minmax(140px, 240px); align-items: center; gap: 8px; color: var(--text-secondary); }
.color-group { display: grid; gap: 7px; }
.color-group-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.color-group h3 { margin: 0; color: var(--text-secondary); font-size: 12px; font-weight: 600; }
.assistant-preview { display: inline-flex; align-items: center; gap: 7px; padding: 3px 9px 3px 6px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-subtle); color: var(--text-secondary); font-size: 11px; }
.color-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 6px; }
.color-field { display: grid; grid-template-columns: minmax(0, 1fr) 32px 60px; align-items: center; gap: 8px; min-width: 0; min-height: 34px; padding: 4px 7px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); }
.color-field span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.color-field input { width: 30px; height: 25px; padding: 1px; border: 1px solid var(--border-default); border-radius: 4px; background: var(--bg-surface); cursor: pointer; }
.color-field code { color: var(--text-tertiary); font-size: 10px; text-align: end; }
@media (max-width: 640px) {
  .palette-head { flex-direction: column; }
  .palette-actions { width: 100%; justify-content: space-between; }
  .theme-select { grid-template-columns: 1fr; }
}
</style>
