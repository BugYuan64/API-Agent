/** 全局轻量提示队列：依赖 Pinia、Vue 及浏览器定时器，提供新增、定时过期和手动关闭。只承载调用方提供的展示文案，不负责异常重试或业务状态裁决。 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 提示语义色，仅影响图标、样式与过期时间，不作为业务结果状态。 */
export type ToastTone = 'success' | 'danger' | 'warning' | 'info'

/** 一条前端提示；id 用于关闭，detail 可省略，内容由业务调用方生成。 */
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
/** 创建 Pinia 共享提示状态并返回 items、push、dismiss；组件只消费队列，超时关闭在 store 内安排。 */
export const useToastStore = defineStore('toasts', () => {
  const items = ref<Toast[]>([])

  /** 按语义、标题和可选详情追加提示并安排过期；danger 保留 8 秒，其余 4 秒，不返回业务处理结果。 */
  function push(tone: ToastTone, title: string, detail?: string) {
    const id = nextId++
    items.value.push({ id, tone, title, detail })
    window.setTimeout(() => dismiss(id), tone === 'danger' ? 8000 : 4000)
  }

  /** 按提示 ID 从队列移除；ID 已过期或不存在时保持其余提示不变。 */
  function dismiss(id: number) {
    items.value = items.value.filter((item) => item.id !== id)
  }

  return { items, push, dismiss }
})
