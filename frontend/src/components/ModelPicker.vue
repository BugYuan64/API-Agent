<script setup lang="ts">
/** A model field shows one input, or one drop-down when a provider list is available. */
defineProps<{ label: string; models: string[]; disabled?: boolean }>()
const model = defineModel<string>({ required: true })
</script>

<template>
  <select v-if="models.length" v-model="model" class="select mono" :aria-label="label" :disabled="disabled">
    <option v-if="!models.includes(model)" :value="model" disabled>{{ model || '请选择模型' }}</option>
    <option v-for="name in models" :key="name" :value="name">{{ name }}</option>
  </select>
  <input
    v-else v-model="model" type="text" class="input mono" :aria-label="label" :disabled="disabled"
    placeholder="gpt-4o, deepseek-chat, qwen2.5:7b" maxlength="120" spellcheck="false"
  />
</template>

<style scoped>
.input, .select { width: 100%; min-width: 0; }
.input:disabled, .select:disabled { opacity: .72; }
</style>
