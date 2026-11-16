/** 请求头行编辑模型：依赖 Vue 响应式状态，为 HTTP 调试与长连接共享新增、删除、重置和折叠规则。只管理前端行数据，不校验后端请求范围；空名忽略，同名按后行覆盖。 */
/**
 * 请求头的「行编辑」模型：视图只维护行，提交时折叠成对象。
 *
 * 作用：接口调试与长连接两个页面各自维护 `{name, value}[]`、各自写
 * `filter(name.trim()).reduce(...)` 折叠逻辑、各自处理“删空后补一行”。
 * 折叠规则一旦不一致（例如一处允许同名覆盖、另一处去重），
 * 两条路径发出去的请求头就会不同。这里统一为一份实现。
 */
import { computed, ref } from 'vue'

/** 请求头编辑行；空名称保留为输入占位，提交折叠时忽略，同名头按后行覆盖。 */
export interface HeaderRow {
  name: string
  value: string
}

/** 复制初始头行并返回响应式行列表、有效头对象、计数和编辑操作；不持有调用方原数组，不验证 HTTP 头合法性。 */
export function useHeaderRows(initial: HeaderRow[] = [{ name: '', value: '' }]) {
  const rows = ref<HeaderRow[]>(initial.map((row) => ({ ...row })))

  /** 折叠成请求头对象：忽略空名行，后出现的同名行覆盖前面的。 */
  const headers = computed<Record<string, string>>(() =>
    rows.value
      .filter((row) => row.name.trim())
      .reduce<Record<string, string>>((acc, row) => {
        acc[row.name.trim()] = row.value
        return acc
      }, {}),
  )

  /** 计算折叠后实际发送的头名称数量；重复名称和空白占位行不会重复计数。 */
  const count = computed(() => Object.keys(headers.value).length)

  /** 追加一条空白可编辑头行；有效头计数仍忽略空名。 */
  function add(): void {
    rows.value.push({ name: '', value: '' })
  }

  /** 按数组索引删除头行；删空后补一条空行以保留输入入口，索引语义沿用 splice。 */
  function remove(index: number): void {
    rows.value.splice(index, 1)
    // 永远保留一行可编辑，避免删空后界面无处输入。
    if (rows.value.length === 0) add()
  }

  /** 用输入行的浅副本替换当前编辑内容；省略参数时恢复一条空行，不修改调用方的原数组。 */
  function reset(next: HeaderRow[] = [{ name: '', value: '' }]): void {
    rows.value = next.map((row) => ({ ...row }))
  }

  return { rows, headers, count, add, remove, reset }
}
