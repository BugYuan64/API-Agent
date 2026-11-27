/** 引擎展示信息与共享选择状态：依赖 Vue、管理 API 和 localStorage。模块单例供顶栏、设置及测试页共用；存储失败可降级为内存选择，探测失败由刷新调用方处理。 */
/**
 * 执行引擎的展示元信息与选择状态（单一定义来源）。
 *
 * 作用：引擎名称、说明、传输方式此前在设置页（ENGINE_META）、用例计划页
 * （ENGINE_LABELS）与顶栏（硬编码 "Postman CLI"）各写一份，顶栏那份已经因为
 * 新增 native 引擎而过时。集中到这里后，新增引擎只需补一条映射。
 * 选择持久化也收敛在 `useEngineSelection`，两页共享同一个 localStorage 键。
 */
import { ref, computed } from 'vue'
import { engineList } from '@/api/endpoints'
import type { EngineInfo } from '@/api/types'

/** 引擎的静态展示元信息；实际就绪状态来自管理 API，不由名称或说明推断。 */
export interface EngineMeta {
  name: string
  kind: string
  description: string
  transports: string
}

const ENGINE_META: Record<string, EngineMeta> = {
  native: {
    name: '本地原生引擎',
    kind: '内置',
    description:
      '基于 httpx 的纯 Python 执行器，进程内直接发包并比对断言。零外部依赖、毫秒级响应，适合日常高频调试与 CI。',
    transports: '进程内内存执行',
  },
  postman: {
    name: 'Postman CLI 引擎',
    kind: 'MCP 服务',
    description:
      '编译受控 Postman Collection 后交由原生 CLI 在隔离沙箱中执行，产出 Newman 报告。提供工业级独立裁决与防作弊背书。',
    transports: 'stdio MCP → Postman CLI 子进程',
  },
  'postman-cli': {
    name: 'Postman CLI 引擎',
    kind: 'MCP 服务',
    description:
      '编译受控 Postman Collection 后交由原生 CLI 在隔离沙箱中执行，产出 Newman 报告。提供工业级独立裁决与防作弊背书。',
    transports: 'stdio MCP → Postman CLI 子进程',
  },
}

const STORAGE_KEY = 'api-agent.engine'

/** 未注册的引擎用其 ID 兜底，避免界面出现空白名称。 */
export function engineMeta(id: string): EngineMeta {
  return (
    ENGINE_META[id] ?? {
      name: id,
      kind: '外部导入',
      description: '自定义导入的测试引擎。',
      transports: '—',
    }
  )
}

/** 下拉框/列表用的显示名；与 meta 保持同一来源。 */
export function engineLabel(id: string): string {
  return `${engineMeta(id).name}`
}

/** 读取持久化的引擎 ID；未保存或浏览器拒绝访问存储时返回 null，由刷新逻辑选择服务端默认值。 */
function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY)
  } catch {
    // 隐私模式下 localStorage 可能不可用，选择仅在本页生效。
    return null
  }
}

/** 尝试持久化给定引擎 ID；存储异常静默忽略，当前模块内的选择仍然有效。 */
function writeStored(id: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id)
  } catch {
    /* 同上：持久化失败不影响本次选择 */
  }
}

/**
 * 引擎列表 + 当前选择 + 刷新。
 *
 * 重要：状态是**模块级单例**。若每次调用都新建 ref，设置页改了选择后，
 * 顶栏与用例计划页拿到的仍是各自那份旧值——这正是此前「顶栏永远显示
 * Postman CLI」的原因。共享一份状态后，任一处选择都会即时反映到其他位置。
 */
const engines = ref<EngineInfo[]>([])
const loading = ref(false)
const selected = ref('native')
/** 按共享选中 ID 查找最新探测条目；列表未加载或没有匹配项时返回 null。 */
const activeEngine = computed(
  () => engines.value.find((item) => item.id === selected.value) ?? null,
)

/** 拉取引擎列表，并在已存 ID 仍可用时恢复选择，否则使用服务端默认项。错误向调用方传播，始终解除 loading。 */
async function refresh(): Promise<void> {
  loading.value = true
  try {
    const payload = await engineList()
    engines.value = payload.items
    const stored = readStored()
    selected.value =
      stored && payload.items.some((item) => item.id === stored) ? stored : payload.default
  } finally {
    loading.value = false
  }
}

/** 更新共享选中 ID 并尝试保存；不重新探测引擎或校验 ID，实际可用性由调用方展示。 */
function select(id: string): void {
  selected.value = id
  writeStored(id)
}

/** 多个页面共用同一份引擎状态与「存储值失效时回落默认」规则。 */
export function useEngineSelection() {
  return { engines, loading, selected, activeEngine, refresh, select }
}
