<script setup lang="ts">
/** 只读代码展示组件：接收可空文本、限高和复制选项，依赖 CopyButton 提供复制。空文本显示为空串；格式化由调用方完成，不解析或执行文本内容。 */

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

/** 将可空输入归一化为空串供只读展示；不改变非空文本。 */
const content = computed(() => props.text ?? '')
/** 仅在给定 maxHeight 时产生限高样式；未给定时返回 undefined 由布局自然展开。 */
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
