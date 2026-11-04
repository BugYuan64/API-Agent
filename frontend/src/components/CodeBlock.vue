<script setup lang="ts">
/**
 * 只读代码块：等宽、可换行、可限高，右上角可选复制按钮。
 *
 * 作用：请求响应体、历史预览、长连接事件正文、设置页接入配置、报告预览
 * 五处各自写了一套 `pre` 样式，背景/圆角/换行规则互有出入。统一后，
 * 「复制」也只在需要时由 `copyable` 打开，不必每处再拼一个 CopyButton。
 */
import { computed } from 'vue'
import CopyButton from './CopyButton.vue'

const props = withDefaults(
  defineProps<{
    text: string | null | undefined
    /** 最大高度，超出滚动。 */
    maxHeight?: string
    /** 是否在右上角显示复制按钮。 */
    copyable?: boolean
    /** 单行省略（用于头值、ID 这类短文本）。 */
    inline?: boolean
  }>(),
  { maxHeight: '', copyable: false, inline: false },
)

const content = computed(() => props.text ?? '')
const style = computed(() => (props.maxHeight ? { maxHeight: props.maxHeight } : undefined))
</script>

<template>
  <div class="code-wrap" :class="{ inline }">
    <div v-if="copyable" class="code-toolbar">
      <CopyButton :text="content" />
    </div>
    <pre class="code-block" :style="style">{{ content }}</pre>
  </div>
</template>

<style scoped>
.code-wrap {
  position: relative;
  min-width: 0;
}

.code-wrap.inline .code-block {
  padding: 2px 6px;
  white-space: nowrap;
  overflow-x: auto;
}

.code-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 4px;
}
</style>
