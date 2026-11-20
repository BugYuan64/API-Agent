<script setup lang="ts">
/** Independent assistant transcript and bottom composer using the saved system model. */
import { computed, onMounted, ref, useId, watch } from 'vue'
import { ArrowUp, Bot, Check, ChevronDown } from 'lucide-vue-next'
import AiMarkdown from './AiMarkdown.vue'
import { COPY } from '@/lib/copy'
import { chatTranscriptKey, createChatHistoryId, listChatHistories, saveChatHistory, titleChatHistory, type ChatHistoryRecord } from '@/lib/chatHistory'
import { describeError } from '@/api/client'
import { getLLMConfig, getLLMModels, selectLLMModel } from '@/api/endpoints'
import { chatWithAssistant, generateChatTitle, type TaskChatMessage } from '@/api/tasks'

const props = withDefaults(defineProps<{ taskId?: string; scope?: string; conversationKey?: string }>(), {
  scope: 'assistant',
  conversationKey: 'default',
})
const emit = defineEmits<{
  busy: [value: boolean]
  'history-created': [record: ChatHistoryRecord, draftKey: string]
  'history-titled': [record: ChatHistoryRecord]
}>()
const input = ref('')
const turns = ref<TaskChatMessage[]>([])
const partial = ref('')
const error = ref('')
const sending = ref(false)
const menuId = useId()
const modelMenu = ref<HTMLDivElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const loadingModels = ref(false)
const model = ref('')
const customModel = ref('')
const models = ref<string[]>([])
const modelOptions = computed(() => [...new Set([model.value, ...models.value].filter(Boolean))])
const savingModel = ref(false)
const modelDisabled = computed(() => sending.value || savingModel.value)
const activeConversationKey = ref(props.conversationKey)
let outgoingConversationKey: string | undefined
let pendingConversationKey: string | undefined
let pendingModel: Promise<boolean> | undefined
let modelRevision = 0

/** Keep each test-history conversation in its own browser-local record. */
function storageKey(conversationKey: string) {
  return chatTranscriptKey(props.scope, conversationKey)
}

/** Restore only validated user/assistant turns when the selected test changes. */
function restoreConversation(conversationKey: string) {
  try {
    const stored = window.localStorage.getItem(storageKey(conversationKey))
    const parsed: unknown = stored ? JSON.parse(stored) : []
    turns.value = Array.isArray(parsed) ? parsed.filter((turn): turn is TaskChatMessage =>
      turn && typeof turn === 'object' &&
      ((turn as TaskChatMessage).role === 'user' || (turn as TaskChatMessage).role === 'assistant') &&
      typeof (turn as TaskChatMessage).content === 'string' &&
      (turn as TaskChatMessage).content.length <= 16_000,
    ).slice(-40) : []
  } catch {
    turns.value = []
  }
  partial.value = ''
  error.value = ''
}

/** Persist completed turns only; streamed text remains transient until complete. */
function persistConversation(conversationKey: string, messages: TaskChatMessage[]) {
  try {
    window.localStorage.setItem(storageKey(conversationKey), JSON.stringify(messages.slice(-40)))
  } catch {
    // Conversation display remains usable when browser storage is unavailable.
  }
}

watch(() => props.conversationKey, value => {
  if (activeConversationKey.value === value) return
  if (sending.value) {
    pendingConversationKey = value
    return
  }
  activeConversationKey.value = value
  outgoingConversationKey = undefined
  input.value = ''
  restoreConversation(value)
})
restoreConversation(props.conversationKey)

/** Read the effective setting on entry and before showing provider choices. */
async function readModel() {
  const revision = modelRevision
  try {
    const config = await getLLMConfig()
    if (revision === modelRevision && !savingModel.value) model.value = config.model
  } catch {
    // The server still resolves the effective model when sending.
  }
}
onMounted(() => { void readModel() })

/** Custom names remain available even when the provider does not list models. */
async function loadModels() {
  if (modelDisabled.value || loadingModels.value) return
  customModel.value = ''
  loadingModels.value = true
  try {
    const [, list] = await Promise.allSettled([readModel(), getLLMModels()])
    models.value = list.status === 'fulfilled' && list.value.ok
      ? list.value.models.filter(name => name.length > 0 && name.length <= 120) : []
  } finally {
    loadingModels.value = false
  }
}

/** Commit the shared setting before presenting a new model as active. */
async function selectModel(value: string) {
  const name = value.trim()
  if (modelDisabled.value || !name) return
  if (name === model.value) {
    modelMenu.value?.hidePopover()
    trigger.value?.focus()
    return
  }
  modelRevision++
  savingModel.value = true
  error.value = ''
  pendingModel = selectLLMModel(name).then(config => {
    model.value = config.model
    modelMenu.value?.hidePopover()
    return true
  }).catch(failure => {
    error.value = `模型切换失败：${describeError(failure)}`
    return false
  })
  try {
    if (await pendingModel && !sending.value) trigger.value?.focus()
  } finally {
    pendingModel = undefined
    savingModel.value = false
  }
}

/** Prevent duplicate sends without blocking testing or the summary stream. */
async function send() {
  const question = input.value.trim()
  if (!question || sending.value) return
  const originalKey = activeConversationKey.value
  const taskId = props.taskId
  const scope = props.scope
  const createsHistory = originalKey.startsWith('draft-')
  const conversationKey = createsHistory ? createChatHistoryId() : originalKey
  outgoingConversationKey = conversationKey
  const previous = turns.value.slice()
  const existingTitle = listChatHistories(scope).some(item => item.id === conversationKey)
  const message: TaskChatMessage = { role: 'user', content: question }
  const firstQuestion = previous.find(turn => turn.role === 'user')?.content ?? question
  const updated = [...previous.slice(-39), message]
  persistConversation(conversationKey, updated)
  if (originalKey === activeConversationKey.value) {
    if (createsHistory) activeConversationKey.value = conversationKey
    turns.value = updated
    if (input.value.trim() === question) input.value = ''
  }
  if (!existingTitle) {
    const record = { id: conversationKey, title: 'AI 对话', created_at: new Date().toISOString() }
    saveChatHistory(scope, record)
    if (createsHistory) emit('history-created', record, originalKey)
    else emit('history-titled', record)
    void generateChatTitle(firstQuestion).then(({ title }) => {
      const titled = titleChatHistory(scope, conversationKey, title)
      if (titled) emit('history-titled', titled)
    }).catch(() => {
      const fallback = titleChatHistory(scope, conversationKey, firstQuestion)
      if (fallback) emit('history-titled', fallback)
    })
  }
  const context: TaskChatMessage[] = [message]
  let size = question.length
  for (let index = previous.length - 2; index >= 0 && context.length < 13; index -= 2) {
    const pair = previous.slice(index, index + 2).map(turn => ({ ...turn, content: turn.content.slice(0, 16000) }))
    const length = pair.reduce((sum, turn) => sum + turn.content.length, 0)
    if (size + length > 32000) break
    size += length
    context.unshift(...pair)
  }
  sending.value = true
  emit('busy', true)
  modelMenu.value?.hidePopover()
  partial.value = ''
  error.value = ''
  try {
    if (pendingModel && !(await pendingModel)) return
    const response = await chatWithAssistant(taskId, context, text => {
      if (conversationKey === activeConversationKey.value || conversationKey === outgoingConversationKey) partial.value = text
    })
    const current = conversationKey === activeConversationKey.value || conversationKey === outgoingConversationKey
    const completed: TaskChatMessage[] = [...previous.slice(-38), message, { role: 'assistant', content: response }]
    persistConversation(conversationKey, completed)
    if (current) {
      turns.value = completed
    }
  } catch (failure) {
    if (conversationKey === activeConversationKey.value || conversationKey === outgoingConversationKey) error.value = describeError(failure)
  } finally {
    outgoingConversationKey = undefined
    partial.value = ''
    sending.value = false
    emit('busy', false)
    if (pendingConversationKey) {
      const nextKey = pendingConversationKey
      pendingConversationKey = undefined
      const executionTranscriptKey = storageKey(nextKey)
      if (executionTranscriptKey !== storageKey(conversationKey)) {
        try {
          window.localStorage.setItem(executionTranscriptKey, window.localStorage.getItem(storageKey(conversationKey)) ?? '[]')
          window.localStorage.removeItem(storageKey(conversationKey))
        } catch {
          // The completed turn stays visible even when transcript migration is unavailable.
        }
      }
      activeConversationKey.value = nextKey
      input.value = ''
      restoreConversation(nextKey)
    }
  }
}
</script>

<template>
  <section class="task-chat" aria-labelledby="task-ai-heading">
    <header class="ai-heading">
      <h2 id="task-ai-heading"><Bot :size="16" aria-hidden="true" /><span>{{ COPY.assistant.heading }}</span></h2>
    </header>
    <div class="ai-output scroll-y" role="region" aria-label="AI 对话输出" tabindex="0">
      <div class="chat-transcript">
        <article v-for="(turn, index) in turns" :key="index" class="chat-turn">
          <strong>{{ turn.role === 'user' ? '你' : COPY.assistant.replyName }}</strong>
          <AiMarkdown v-if="turn.role === 'assistant'" :text="turn.content" />
          <p v-else>{{ turn.content }}</p>
        </article>
        <article v-if="sending" class="chat-turn" role="status">
          <strong>{{ COPY.assistant.replyName }}</strong>
          <AiMarkdown v-if="partial" :text="partial" />
          <p v-else>正在回复…</p>
        </article>
        <p v-if="error" class="chat-error" role="alert">{{ error }}</p>
      </div>
    </div>
    <form class="composer" aria-label="AI 对话" @submit.prevent="send">
      <textarea v-model="input" class="textarea" rows="1" maxlength="4000" :placeholder="COPY.assistant.inputPlaceholder" :aria-label="COPY.assistant.inputPlaceholder" />
      <div class="chat-actions">
        <button ref="trigger" class="btn btn-ghost btn-sm model-choice" type="button" :popovertarget="menuId"
          :aria-label="`对话模型：${model || '默认模型'}`" :disabled="modelDisabled" @click="loadModels">
          <span class="truncate">{{ model || '默认模型' }}</span><ChevronDown :size="13" aria-hidden="true" />
        </button>
        <div :id="menuId" ref="modelMenu" class="model-menu" popover="auto" role="group" aria-label="切换对话模型">
          <button v-for="item in modelOptions" :key="item" class="model-option" type="button" :aria-pressed="model === item"
            :disabled="modelDisabled" :title="item" @click="selectModel(item)">
            <span class="truncate">{{ item }}</span><Check v-if="model === item" :size="14" aria-hidden="true" />
          </button>
          <p v-if="loadingModels" class="hint model-loading" role="status">正在加载模型…</p>
          <div class="custom-model">
            <input v-model.trim="customModel" class="input" maxlength="120" aria-label="模型名称" placeholder="模型名称"
              :disabled="modelDisabled" @keydown.enter.prevent="selectModel(customModel)" />
            <button class="btn btn-sm btn-icon" type="button" aria-label="应用模型" title="应用模型"
              :disabled="modelDisabled || !customModel" @click="selectModel(customModel)"><Check :size="14" aria-hidden="true" /></button>
          </div>
        </div>
        <button class="btn btn-primary btn-icon chat-send" type="submit" aria-label="发送" title="发送"
          :disabled="sending || !input.trim()"><ArrowUp :size="17" aria-hidden="true" /></button>
      </div>
    </form>
  </section>
</template>

<style scoped>
.task-chat {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  height: 100%;
  min-width: 0;
  padding: 9px 18px 10px;
  background: var(--bg-surface);
  container-type: size;
}
.ai-heading { display: flex; align-items: center; gap: 8px; min-height: 22px; }
.ai-heading h2 { font-size: 15px; margin: 0; }
.ai-heading h2 { display: inline-flex; align-items: center; gap: 6px; }
.ai-heading h2 svg { color: var(--accent); }
.ai-output { min-width: 0; padding: 6px clamp(12px, 5cqw, 48px) 10px; overscroll-behavior: contain; scrollbar-gutter: stable both-edges; }
.ai-output:focus-visible { outline: 1px solid var(--accent); outline-offset: -1px; }
.chat-transcript { width: min(100%, 850px); margin-inline: auto; }
.chat-turn { padding: 6px 0; border-bottom: 1px solid var(--border-subtle); }
.chat-turn > p { margin: 3px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.chat-error { color: var(--danger); }
.composer {
  justify-self: center;
  width: min(100%, 850px);
  margin-top: 6px;
  padding: 4px 7px 6px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-xl);
  background: var(--bg-subtle);
}
.composer:focus-within { border-color: var(--accent-border); }
.composer textarea {
  display: block;
  min-height: 28px;
  max-height: clamp(28px, calc(100cqh - 95px), 92px);
  field-sizing: content;
  overflow-y: auto;
  padding: 4px;
  border: 0;
  background: transparent;
  resize: none;
}
.composer textarea:focus { box-shadow: none; }
.chat-actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; min-width: 0; }
.model-choice { min-width: 0; max-width: min(240px, calc(100% - 34px)); }
.model-choice svg, .model-option svg { flex: none; }
.chat-send { flex: none; width: 28px; height: 28px; border-radius: 50%; }
.model-menu {
  position: fixed;
  inset: auto;
  position-area: top span-left;
  position-try-fallbacks: flip-inline;
  justify-self: end;
  align-self: end;
  width: min(260px, calc(100vw - 24px));
  max-height: min(260px, 50dvh);
  margin: 0 0 6px;
  padding: 4px;
  overflow: auto;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-primary);
  box-shadow: var(--shadow-md);
}
.model-option { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; min-height: 30px; padding: 4px 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: inherit; text-align: left; }
.model-option:hover:not(:disabled) { background: var(--bg-hover); }
.model-option[aria-pressed='true'] { color: var(--accent); }
.custom-model { display: flex; align-items: center; gap: 4px; padding: 4px; }
.custom-model .input { min-width: 0; }
.custom-model .btn { flex: none; }
.model-loading { margin: 4px 8px; }
</style>
