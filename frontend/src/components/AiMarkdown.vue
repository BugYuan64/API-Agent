<script setup lang="ts">
/** Render shared AI Markdown as escaped Vue text, including incomplete streamed blocks. */
import { computed } from 'vue'

const props = defineProps<{ text?: string | null }>()
type ReportBlock = { kind: 'heading' | 'paragraph' | 'list' | 'code'; lines: string[] }
const blocks = computed(() => {
  const output: ReportBlock[] = []
  let inCode = false
  for (const raw of (props.text ?? '').split(/\r?\n/)) {
    const line = raw.trim()
    if (line.startsWith('```')) {
      inCode = !inCode
      if (inCode) output.push({ kind: 'code', lines: [] })
    } else if (inCode) {
      output.at(-1)?.lines.push(raw)
    } else if (line.startsWith('# ') || /^#{2,6}\s/.test(line)) {
      output.push({ kind: 'heading', lines: [line.replace(/^#+\s+/, '')] })
    } else if (/^(?:[-*+] |\d+\. )/.test(line)) {
      const item = line.replace(/^(?:[-*+] |\d+\. )/, '')
      const previous = output.at(-1)
      if (previous?.kind === 'list') previous.lines.push(item)
      else output.push({ kind: 'list', lines: [item] })
    } else if (line) {
      output.push({ kind: 'paragraph', lines: [line] })
    } else if (output.at(-1)?.kind === 'list') {
      output.push({ kind: 'paragraph', lines: [] })
    }
  }
  return output.filter(block => block.lines.length)
})

/** Preserve common inline emphasis without injecting model-provided HTML. */
function inline(line: string) {
  return line.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map(part => ({
    kind: part.startsWith('**') && part.endsWith('**') && part.length > 4 ? 'strong'
      : part.startsWith('`') && part.endsWith('`') && part.length > 2 ? 'code' : 'span',
    text: part,
  }))
}
</script>

<template>
  <div class="ai-markdown">
    <template v-for="(block, index) in blocks" :key="index">
      <h3 v-if="block.kind === 'heading'">{{ block.lines[0] }}</h3>
      <ul v-else-if="block.kind === 'list'">
        <li v-for="(line, lineIndex) in block.lines" :key="lineIndex"><component v-for="(part, partIndex) in inline(line)" :key="partIndex" :is="part.kind">{{ part.kind === 'strong' ? part.text.slice(2, -2) : part.kind === 'code' ? part.text.slice(1, -1) : part.text }}</component></li>
      </ul>
      <pre v-else-if="block.kind === 'code'"><code>{{ block.lines.join('\n') }}</code></pre>
      <p v-else><component v-for="(part, partIndex) in inline(block.lines[0])" :key="partIndex" :is="part.kind">{{ part.kind === 'strong' ? part.text.slice(2, -2) : part.kind === 'code' ? part.text.slice(1, -1) : part.text }}</component></p>
    </template>
  </div>
</template>

<style scoped>
.ai-markdown { line-height: 1.65; overflow-wrap: anywhere; }
.ai-markdown h3 { margin: 12px 0 4px; font-size: 14px; color: var(--accent); }
.ai-markdown p, .ai-markdown ul { margin: 5px 0 8px; }
.ai-markdown ul { padding-inline-start: 22px; }
.ai-markdown code { background: var(--bg-subtle); padding: 1px 4px; border-radius: var(--radius-sm); }
.ai-markdown pre { white-space: pre-wrap; padding: 8px; background: var(--bg-subtle); border-radius: var(--radius-sm); }
</style>
