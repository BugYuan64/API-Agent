<script setup lang="ts">
/**
 * 统一 URL 输入组件：整合常用协议前缀、浏览器本地自定义前缀管理与可编辑地址后缀。
 * 供接口测试、长连接测试、智能群测、系统设置与大模型测试共用。
 */
import { Check, Link2Off, Pencil, Plus, X } from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref, useId } from 'vue'
import { normalizeUrlPrefix, readUrlPrefixes, URL_PREFIX_STORAGE, urlSuffixPlaceholder } from '@/lib/urlPrefixes'

const props = defineProps<{
  modelValue: string
  address: string
  label: string
  placeholder: string
  websocket?: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [prefix: string]
  'update:address': [address: string]
  submit: []
}>()

const saved = ref<string[]>([])
const menuId = useId()
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLDivElement | null>(null)
const noPrefixButton = ref<HTMLButtonElement | null>(null)
const editor = ref<HTMLDialogElement | null>(null)
const addressInput = ref<HTMLInputElement | null>(null)
const editingPrefix = ref('')
const editorLabel = computed(() => editingPrefix.value ? '修改 URL 前缀' : '自定义 URL 前缀')
const draft = ref('')
const message = ref('')
const validation = ref('')

/** 当前控件可见的自定义前缀；非 WebSocket 场景隐藏 ws(s) 前缀。 */
const visible = computed(() => saved.value.filter(prefix => props.websocket || /^https?:/.test(prefix)))

/** 内置常用协议前缀（未在已保存前缀中重复出现时展示），便于首次使用直接点选。 */
const quickSchemes = computed(() => {
  const base = props.websocket ? ['ws://', 'wss://', 'http://', 'https://'] : ['https://', 'http://']
  return base.filter(scheme => !visible.value.includes(scheme))
})

function reload() {
  try {
    saved.value = readUrlPrefixes(window.localStorage)
  } catch {
    saved.value = []
  }
}
onMounted(reload)

function persist(prefixes: string[]) {
  saved.value = prefixes
  try {
    window.localStorage.setItem(URL_PREFIX_STORAGE, JSON.stringify(prefixes))
    message.value = ''
  } catch {
    message.value = '浏览器存储不可用，前缀变更仅在当前页面生效。'
  }
}

/** 校验并保存新增或修改后的自定义前缀，仅在编辑当前激活前缀时同步更新选中项。 */
function savePrefix() {
  const prefix = normalizeUrlPrefix(draft.value)
  if (!prefix || (!props.websocket && !/^https?:/.test(prefix))) {
    validation.value = props.websocket ? '请输入有效的 HTTP(S) 或 WS(S) 前缀。' : '请输入有效的 HTTP 或 HTTPS 前缀。'
    return
  }
  const original = editingPrefix.value
  const updateSelection = !original || original === props.modelValue
  if (original) {
    if (prefix !== original && saved.value.includes(prefix)) {
      validation.value = '此前缀已存在，请填写其他前缀。'
      return
    }
    if (prefix !== original) persist(saved.value.map(value => value === original ? prefix : value))
  } else {
    persist([prefix, ...saved.value.filter(value => value !== prefix)].slice(0, 30))
  }
  if (updateSelection && prefix !== props.modelValue) emit('update:modelValue', prefix)
  editor.value?.close()
  if (updateSelection) addressInput.value?.focus()
}

/** 选定前缀后关闭弹层并将焦点交回地址输入框。 */
function choosePrefix(prefix: string) {
  if (prefix !== props.modelValue) emit('update:modelValue', prefix)
  menu.value?.hidePopover()
  addressInput.value?.focus()
}

/** 删除自定义前缀时保持弹层打开；若删掉了当前选中前缀则自动切回无前缀。 */
async function removePrefix(prefix: string) {
  persist(saved.value.filter(value => value !== prefix))
  if (prefix === props.modelValue) emit('update:modelValue', '')
  await nextTick()
  noPrefixButton.value?.focus()
}

/** 打开共享前缀编辑对话框并保留稳定的焦点回退目标。 */
function openPrefixEditor(prefix = '') {
  menu.value?.hidePopover()
  trigger.value?.focus()
  editingPrefix.value = prefix
  draft.value = prefix
  validation.value = ''
  editor.value?.showModal()
}
</script>

<template>
  <div class="url-control">
    <div class="url-field">
      <slot name="leading" />
      <button
        ref="trigger"
        type="button"
        class="prefix-choice"
        :popovertarget="menuId"
        :disabled="disabled"
        :aria-label="`URL 前缀：${modelValue || '不使用前缀'}`"
        :title="modelValue || '选择 URL 前缀'"
      >
        <span v-if="modelValue">{{ modelValue }}</span>
        <Link2Off v-else :size="15" aria-hidden="true" />
      </button>
      <div :id="menuId" ref="menu" class="prefix-menu" popover="auto" role="group" aria-label="选择 URL 前缀">
        <button
          ref="noPrefixButton"
          type="button"
          class="prefix-option"
          :aria-pressed="!modelValue"
          aria-label="不使用前缀"
          title="不使用前缀"
          @click="choosePrefix('')"
        >
          <Link2Off :size="15" aria-hidden="true" />
          <span>不使用前缀</span>
          <Check v-if="!modelValue" class="prefix-check" :size="14" aria-hidden="true" />
        </button>

        <template v-if="quickSchemes.length">
          <div class="prefix-section-label">常用协议</div>
          <button
            v-for="scheme in quickSchemes"
            :key="scheme"
            type="button"
            class="prefix-option mono"
            :aria-pressed="scheme === modelValue"
            :title="scheme"
            @click="choosePrefix(scheme)"
          >
            <span>{{ scheme }}</span>
            <Check v-if="scheme === modelValue" class="prefix-check" :size="14" aria-hidden="true" />
          </button>
        </template>

        <template v-if="visible.length">
          <div class="prefix-section-label">已保存前缀</div>
          <div v-for="prefix in visible" :key="prefix" class="prefix-row">
            <button
              type="button"
              class="prefix-option mono"
              :aria-pressed="prefix === modelValue"
              :title="prefix"
              @click="choosePrefix(prefix)"
            >
              <span>{{ prefix }}</span>
              <Check v-if="prefix === modelValue" class="prefix-check" :size="14" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="prefix-icon-button prefix-edit"
              :aria-label="`修改前缀 ${prefix}`"
              title="修改前缀"
              @click="openPrefixEditor(prefix)"
            >
              <Pencil :size="15" aria-hidden="true" />
            </button>
            <button
              type="button"
              class="prefix-icon-button prefix-remove"
              :aria-label="`移除前缀 ${prefix}`"
              title="移除前缀"
              @click="removePrefix(prefix)"
            >
              <X :size="15" aria-hidden="true" />
            </button>
          </div>
        </template>

        <button type="button" class="prefix-option prefix-add" @click="openPrefixEditor()">
          <Plus :size="15" aria-hidden="true" /> 自定义前缀…
        </button>
      </div>
      <input
        ref="addressInput"
        :value="address"
        class="url-address mono"
        type="text"
        :aria-label="label"
        :placeholder="modelValue ? urlSuffixPlaceholder(modelValue) : placeholder"
        :disabled="disabled"
        spellcheck="false"
        @input="emit('update:address', ($event.target as HTMLInputElement).value)"
        @keydown.enter.exact.prevent="emit('submit')"
      />
    </div>
    <dialog ref="editor" class="prefix-editor" :aria-label="editorLabel">
      <form @submit.prevent="savePrefix">
        <label class="field">{{ editorLabel }}
          <input
            v-model="draft"
            class="input mono"
            :placeholder="websocket ? 'ws://127.0.0.1:8081 或 https://api.example.com/v1' : 'https://api.example.com/v1 或 http://127.0.0.1'"
            spellcheck="false"
            autofocus
            @input="validation = ''"
          />
        </label>
        <p v-if="validation" class="hint" role="alert">{{ validation }}</p>
        <div class="prefix-actions">
          <button type="button" class="btn btn-sm" @click="editor?.close()">取消</button>
          <button type="submit" class="btn btn-accent btn-sm" :disabled="!draft.trim()">保存前缀</button>
        </div>
      </form>
    </dialog>
    <p v-if="message" class="hint prefix-message" role="status">{{ message }}</p>
  </div>
</template>

<style scoped>
.url-control { flex: 1; min-width: 0; }
.url-field { display: flex; align-items: stretch; min-height: 38px; min-width: 0; border: 1px solid var(--border-default); border-radius: var(--radius-md); background: var(--bg-input); }
.url-field:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--focus-ring); }
.prefix-choice { display: flex; align-items: center; gap: 6px; min-width: 0; max-width: 55%; margin: 3px 0 3px 3px; padding: 0 8px; border: 0; border-radius: var(--radius-sm); background: var(--bg-subtle); color: var(--text-secondary); font-size: 12px; cursor: pointer; }
.prefix-choice:disabled { cursor: default; }
.prefix-choice:not(:disabled):hover { background: var(--bg-rail); color: var(--text-primary); }
.prefix-choice span, .prefix-option span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prefix-choice::after { content: ''; border: solid currentColor; border-width: 0 1px 1px 0; width: 5px; height: 5px; flex: none; transform: translateY(-1px) rotate(45deg); }
.prefix-choice:focus-visible, .prefix-menu button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
/* Anchor the top-layer menu to its trigger, even inside a scrolling workbench. */
.prefix-menu {
  position: fixed;
  inset: auto;
  position-area: bottom span-right;
  position-try-fallbacks: flip-block, flip-inline, flip-block flip-inline;
  justify-self: start;
  align-self: start;
  width: max-content;
  min-width: min(192px, calc(100vw - 24px));
  max-width: min(340px, calc(100vw - 24px));
  max-height: min(320px, 50dvh);
  overflow: auto;
  margin: 4px 0;
  padding: 4px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-md);
}
.prefix-section-label {
  padding: 6px 8px 3px;
  margin-top: 2px;
  border-top: 1px solid var(--border-subtle);
  color: var(--text-tertiary);
  font-size: 11px;
  font-weight: 600;
}
.prefix-row { display: flex; align-items: stretch; gap: 2px; }
.prefix-option, .prefix-icon-button { display: flex; align-items: center; gap: 8px; min-height: 30px; border: 0; border-radius: var(--radius-sm); background: transparent; color: inherit; cursor: pointer; }
.prefix-option { flex: 1; width: 100%; min-width: 0; padding: 0 8px; text-align: left; font-size: 12px; }
.prefix-option:hover { background: var(--bg-rail); }
.prefix-option[aria-pressed='true'] { color: var(--accent); background: var(--bg-subtle); }
.prefix-menu svg { flex: none; }
.prefix-check { margin-left: auto; }
.prefix-icon-button { flex: none; justify-content: center; width: 30px; padding: 0; color: var(--text-secondary); }
.prefix-edit:hover { background: var(--bg-rail); color: var(--accent); }
.prefix-remove:hover { background: var(--danger-soft); color: var(--danger); }
.prefix-add { margin-top: 4px; border-top: 1px solid var(--border-subtle); border-radius: 0 0 var(--radius-sm) var(--radius-sm); padding-top: 4px; }
.url-address { flex: 1; min-width: 0; border: 0; background: transparent; color: inherit; padding: 0 8px; }
.url-address:focus-visible { outline: none; }
.prefix-editor { width: min(420px, calc(100vw - 32px)); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 16px; color: var(--text-primary); background: var(--bg-surface); }
.prefix-editor::backdrop { background: color-mix(in srgb, var(--modal-backdrop) 62%, transparent); }
.prefix-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.prefix-message { margin: 4px 0 0; }
</style>
