/**
 * 分栏尺寸的纯计算与持久化。
 *
 * 作用：把「阈值夹紧」和「localStorage 读写」从组件里剥离，
 * 这样拖动行为可以用 Node 直接测，不必依赖 DOM 与 Pointer 事件。
 * 组件只负责事件与样式，规则只在下面这一处。
 */
import type { InjectionKey } from 'vue'

/** 单个分栏的像素约束；second 仅用于需要固定末栏的嵌套工作台。 */
export interface ClampOptions {
  size: number
  min: number
  max?: number
  /** 另一侧的最小尺寸；未测量容器时传 0 表示暂不限制。 */
  minOther?: number
  /** 容器可用总尺寸（宽或高）；0 表示尚未测量。 */
  available?: number
  /** 末栏期望宽度；有测量值时，首栏随容器宽度变化以保持末栏。 */
  second?: number
  /** 仅当两侧物理最小值也放不下时启用比例压缩；可小于保留偏好的 minOther。 */
  fitOther?: number
}

/** 工作台只接收布局保留量，不读取面板业务状态；null 表示注销面板。 */
export interface WorkspaceSplitContext {
  reserve(pane: string, width: number | null, minimum?: number): void
}
export const workspaceSplitKey: InjectionKey<WorkspaceSplitContext> = Symbol('workspace-split')

/**
 * 把期望尺寸夹到合法区间。
 *
 * 规则顺序：
 * 1. 不小于 `min`；
 * 2. 不大于 `max`（若给出）；
 * 3. 不大于「容器总尺寸 − 另一侧最小尺寸」，保证对侧不会被挤没；
 * 4. 当容器小到连两个最小值都放不下时，仍返回 `min`（宁可让内容滚动，
 *    也不返回比阈值更小的尺寸）。固定末栏或指定 fitOther 的工作台是例外：
 *    物理最小值也放不下时按最小值比例收缩，避免任何一栏完全消失或越界；
 *    恢复后仍重用末栏偏好，比例降级不改变持久化偏好。
 */
export function clampSize({ size, min, max, minOther = 0, available = 0, second, fitOther }: ClampOptions): number {
  if (second !== undefined && available > 0) {
    size = available - second
  }
  const minimumOther = fitOther ?? (second !== undefined ? minOther : undefined)
  if (minimumOther !== undefined && available > 0 && available < min + minimumOther) {
    min = available * min / (min + minimumOther)
  }
  const otherLimit = available > 0 ? available - minOther : Number.POSITIVE_INFINITY
  const upper = Math.min(max ?? Number.POSITIVE_INFINITY, otherLimit)
  return Math.round(Math.min(Math.max(size, min), Math.max(min, upper)))
}

/** 从 localStorage 读取已保存尺寸；缺失或非法时返回 fallback。 */
export function readStoredSize(key: string | undefined, fallback: number): number {
  if (!key) return fallback
  try {
    const saved = Number(window.localStorage.getItem(key))
    return Number.isFinite(saved) && saved > 0 ? saved : fallback
  } catch {
    return fallback
  }
}

/** 保存尺寸；存储不可用（隐私模式等）时静默失败，不影响拖动。 */
export function writeStoredSize(key: string | undefined, size: number): void {
  if (!key) return
  try {
    window.localStorage.setItem(key, String(Math.round(size)))
  } catch {
    // 忽略：持久化是增强能力，不是拖动的前提。
  }
}
