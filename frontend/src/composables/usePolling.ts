/**
 * 可取消、带世代保护的轮询循环。
 *
 * 作用：项目里有四套「轮询到终态」实现——长连接（AbortController + 60ms）、
 * 任务列表/详情（递归 setTimeout）、AI 编排（setTimeout + 世代 + 单飞）。
 * 后两处缺少世代保护，落地的迟到响应会把已确定的终态覆盖回去。
 * 这里提供一份同时具备「世代标记」「单飞」「可中止」的循环，
 * 并把「何时停止」交给调用方的 `isTerminal` 判断。
 */
import { onBeforeUnmount, ref } from 'vue'

export interface PollingOptions<T> {
  /** 一次拉取。调用方自行处理错误展示。 */
  fetch: (signal: AbortSignal) => Promise<T>
  /** 收到结果后的处理（更新状态等）。 */
  onResult: (value: T) => void
  /** 仅当前世代的失败会触发，避免旧请求显示过期提示。 */
  onError?: (error: unknown) => void
  /** 返回 true 表示已到终态，停止轮询。 */
  isTerminal: (value: T) => boolean
  /** 两次轮询之间的间隔（毫秒）。 */
  intervalMs: number
  /** 组件卸载时是否自动停止。默认开启。 */
  stopOnUnmount?: boolean
}

export function usePolling<T>(options: PollingOptions<T>) {
  const active = ref(false)
  let generation = 0
  let timer: number | undefined
  let controller: AbortController | null = null
  let inFlight = false
  let disposed = false

  /** 判定这次回调是否仍属于“当前这一轮”。 */
  const current = (gen: number) => active.value && gen === generation

  async function step(gen: number): Promise<void> {
    if (!current(gen) || inFlight) return
    inFlight = true
    const requestController = new AbortController()
    controller = requestController
    const signal = requestController.signal
    try {
      const value = await options.fetch(signal)
      // 迟到响应：世代已变（用户重启/切换）或轮询已停止，直接丢弃。
      if (!current(gen)) return
      options.onResult(value)
      if (!current(gen)) return
      if (options.isTerminal(value)) {
        stop()
        return
      }
    } catch (error) {
      // 主动中止不算失败；其他错误由调用方在 fetch 内提示。
      if (signal.aborted || !current(gen)) return
      options.onError?.(error)
    } finally {
      inFlight = false
      if (controller === requestController) controller = null
      // A restart may have arrived while fetch was pending. Hand ownership to
      // the latest generation after settlement, keeping all generations single-flight.
      if (active.value) {
        const next = generation
        timer = window.setTimeout(() => void step(next), options.intervalMs)
      }
    }
  }

  function start(): void {
    if (disposed) return
    stop()
    active.value = true
    const gen = ++generation
    void step(gen)
  }

  function stop(): void {
    active.value = false
    generation += 1
    controller?.abort()
    controller = null
    window.clearTimeout(timer)
    timer = undefined
  }

  if (options.stopOnUnmount !== false) {
    onBeforeUnmount(() => { disposed = true; stop() })
  }

  return { active, start, stop }
}
