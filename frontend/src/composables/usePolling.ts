/** 可取消的单飞轮询：依赖 Vue 生命周期及浏览器定时器，通过世代标记丢弃旧响应。调用方提供拉取、结果处理与终态判断；当前世代失败交给 onError，未停止时继续调度。 */
/**
 * 可取消、带世代保护的轮询循环。
 *
 * 任务列表与任务详情共用此轮询；长连接和 AI 编排保留各自专用流程。
 * 世代标记、单飞和 AbortController 分别约束响应归属、并发与请求取消，
 * 「何时停止」由调用方的 `isTerminal` 判断。
 */
import { onBeforeUnmount, ref } from 'vue'

/** 轮询策略契约：调用方提供可取消拉取、结果消费、可选错误反馈与终态判定，intervalMs 为结算后的等待间隔。 */
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

/** 创建轮询控制器，返回 active、start 和 stop；fetch 的取消由 AbortSignal 传递，结果与错误只交付当前世代。默认卸载停止，调用方应在 onError 中决定是否终止。 */
export function usePolling<T>(options: PollingOptions<T>) {
  const active = ref(false)
  let generation = 0
  let timer: number | undefined
  let controller: AbortController | null = null
  let inFlight = false
  let disposed = false

  /** 判定这次回调是否仍属于“当前这一轮”。 */
  const current = (gen: number) => active.value && gen === generation

  /** 执行指定世代的一次拉取；无效世代或已有请求时跳过。终态停止，异常通知当前 onError，未停止则在请求结算后为最新世代调度下一次。 */
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
      // 主动中止不算失败；当前世代的其他错误交给 onError。
      if (signal.aborted || !current(gen)) return
      options.onError?.(error)
    } finally {
      inFlight = false
      if (controller === requestController) controller = null
      // 请求在途期间可能发生重启；结算后只调度最新世代，所有世代共享单飞约束。
      if (active.value) {
        const next = generation
        timer = window.setTimeout(() => void step(next), options.intervalMs)
      }
    }
  }

  /** 重启轮询并立即尝试拉取；先中止旧请求，旧请求未结算时由其收尾接续最新世代。组件已释放时不再启动。 */
  function start(): void {
    if (disposed) return
    stop()
    active.value = true
    const gen = ++generation
    void step(gen)
  }

  /** 停止调度、中止当前请求并使旧回调失效；不清空调用方已展示的数据，可以重复调用。 */
  function stop(): void {
    active.value = false
    generation += 1
    controller?.abort()
    controller = null
    window.clearTimeout(timer)
    timer = undefined
  }

  if (options.stopOnUnmount !== false) {
    /** 组件卸载时标记不可再启动并中止当前轮询；stopOnUnmount=false 时由调用方负责收尾。 */
    onBeforeUnmount(() => { disposed = true; stop() })
  }

  return { active, start, stop }
}
