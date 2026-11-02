import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastTone = 'success' | 'danger' | 'warning' | 'info'

export interface Toast {
  id: number
  tone: ToastTone
  title: string
  detail?: string
}

let nextId = 1

/** 轻量的全局提示队列。
 *  为什么不用第三方库：只有“成功/失败”两种主要语义，一个固定位置的队列就够，
 *  省掉一个依赖也省掉样式对齐的麻烦。 */
export const useToastStore = defineStore('toasts', () => {
  const items = ref<Toast[]>([])

  function push(tone: ToastTone, title: string, detail?: string) {
    const id = nextId++
    items.value.push({ id, tone, title, detail })
    window.setTimeout(() => dismiss(id), tone === 'danger' ? 8000 : 4000)
  }

  function dismiss(id: number) {
    items.value = items.value.filter((item) => item.id !== id)
  }

  return { items, push, dismiss }
})
