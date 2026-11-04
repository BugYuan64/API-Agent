/**
 * 请求头的「行编辑」模型：视图只维护行，提交时折叠成对象。
 *
 * 作用：接口调试与长连接两个页面各自维护 `{name, value}[]`、各自写
 * `filter(name.trim()).reduce(...)` 折叠逻辑、各自处理“删空后补一行”。
 * 折叠规则一旦不一致（例如一处允许同名覆盖、另一处去重），
 * 两条路径发出去的请求头就会不同。这里统一为一份实现。
 */
import { computed, ref } from 'vue'

export interface HeaderRow {
  name: string
  value: string
}

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

  const count = computed(() => Object.keys(headers.value).length)

  function add(): void {
    rows.value.push({ name: '', value: '' })
  }

  function remove(index: number): void {
    rows.value.splice(index, 1)
    // 永远保留一行可编辑，避免删空后界面无处输入。
    if (rows.value.length === 0) add()
  }

  function reset(next: HeaderRow[] = [{ name: '', value: '' }]): void {
    rows.value = next.map((row) => ({ ...row }))
  }

  return { rows, headers, count, add, remove, reset }
}
