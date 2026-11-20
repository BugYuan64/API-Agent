import type { Catalog, TaskCaseResult, TaskRecord, TaskSnapshot, TestCase } from '@/api/tasks'
import type { ConsoleSseEvent, StreamEvent } from '@/api/types'
import { countCaseOutcomes, presentResult, type ResultLike } from './results'

/** Describe transport failures and write gating even for older recorded cases. */
export function taskCaseMessage(result: TaskCaseResult) {
  if (result.status === 'permission_required' || result.reason === 'write_permission_required') {
    return '需要开启「允许写接口和 WebSocket 发送消息」。本条用例未发送请求。'
  }
  const evidence = result.evidence as (ResultLike & { session?: ResultLike }) | undefined
  const transport = evidence?.session ?? evidence
  if (result.status === 'error' && (transport?.result_code || transport?.error_kind)) {
    const detail = presentResult(transport)
    return `${detail.title}。${detail.message}`
  }
  return result.reason ?? ''
}

/** Identify HTTPS runs where every case that reached transport failed TLS. */
export function taskHasTlsFailure(task: TaskRecord) {
  if (!task.snapshot.origin?.startsWith('https://')) return false
  const attempted = task.results.filter(result => result.evidence != null)
  return attempted.length > 0 && attempted.every(result => {
    const evidence = result.evidence as ResultLike & { session?: ResultLike }
    const transport = evidence.session ?? evidence
    return result.status === 'error' && (transport.result_code === 'TLS_ERROR' || transport.error_kind === 'tls_error')
  })
}

interface CaseNode {
  index: number
  input: TestCase
  result?: TaskCaseResult
}

interface OperationNode {
  key: string
  method: string
  protocol: TestCase['protocol']
  path: string
  example?: TestCase
  cases: CaseNode[]
}

/** Recent and legacy records can have an SSE response for a declared HTTP case. */
export function taskCaseProtocol(input: TestCase, result?: TaskCaseResult): TestCase['protocol'] {
  const evidence = result?.evidence as { response?: { sse?: unknown } } | undefined
  return input.protocol === 'http' && evidence?.response?.sse ? 'sse' : input.protocol
}

function routeKey(testCase: TestCase, path: string) {
  return JSON.stringify([testCase.protocol, testCase.method, path])
}

/** Describe the active stream while its verdict is still pending. */
export function activeStreamWait(task: TaskRecord) {
  const index = task.current_index
  if (task.status !== 'running' || task.stage !== 'executing' || index == null ||
    !task.snapshot.cases[index] || task.results.some(result => result.index === index)) return null
  const protocol = task.active_stream === undefined ? task.snapshot.cases[index].protocol : task.active_stream
  if (protocol !== 'sse' && protocol !== 'websocket') return null
  return { index, label: protocol === 'sse' ? '等待 SSE 流结束' : '等待 WebSocket 观察结束' }
}

/** Surface the pending response before any stream has been confirmed. */
export function activeTaskWait(task: TaskRecord) {
  const stream = activeStreamWait(task)
  if (stream) return stream
  const index = task.current_index
  return task.status === 'running' && task.stage === 'executing' && index != null &&
    task.snapshot.cases[index] && !task.results.some(result => result.index === index)
    ? { index, label: '等待响应' } : null
}

/** Show payload data alone. The method and concrete URL belong to the signature. */
export function caseBodyText(testCase: TestCase) {
  return testCase.body == null ? '' : JSON.stringify(testCase.body, null, 2)
}

/** Read only the received payload from saved transport evidence. */
export function taskResponsePayload(result?: TaskCaseResult) {
  const evidence = result?.evidence as {
    response?: {
      body?: string | null
      sse?: { events: ConsoleSseEvent[]; total_events?: number } | null
    } | null
    events?: StreamEvent[]
  } | undefined
  const sse = evidence?.response?.sse
  const messages = evidence?.events?.filter(event => event.direction === 'in' && ['text', 'binary'].includes(event.kind))
  return { sse, messages, body: evidence?.response?.body }
}

/** Preserve event boundaries and use one data line per payload line. */
export function sseDataText(events: readonly ConsoleSseEvent[]) {
  return events.map(event => event.data.split(/\r\n|\n|\r/).map(line => `data: ${line}`).join('\n')).join('\n\n')
}

export function buildTaskResultTree(snapshot: TaskSnapshot, results: readonly TaskCaseResult[]) {
  const byIndex = new Map(results.map(result => [result.index, result]))
  const membership = new Map<number, OperationNode>()
  const byRoute = new Map<string, OperationNode | null>()
  const groups: OperationNode[] = []
  for (const operation of snapshot.operations ?? []) {
    const example = snapshot.cases[operation.example_index]
    if (!example) continue
    const group = { key: `operation:${operation.example_index}`, method: example.method,
      protocol: example.protocol, path: operation.path, example, cases: [] }
    groups.push(group)
    for (const index of operation.case_indices) membership.set(index, group)
    for (const path of new Set([operation.path, example.path.split('?')[0]])) {
      const key = routeKey(example, path)
      byRoute.set(key, byRoute.has(key) ? null : group)
    }
  }

  const fallback = new Map<string, OperationNode>()
  snapshot.cases.forEach((input, index) => {
    const path = input.path.split('?')[0]
    const key = routeKey(input, path)
    let group = membership.get(index) ?? byRoute.get(key) ?? fallback.get(key)
    if (!group) {
      group = { key: `route:${key}`, method: input.method, protocol: input.protocol, path, cases: [] }
      groups.push(group)
      fallback.set(key, group)
    }
    group.cases.push({ index, input, result: byIndex.get(index) })
  })
  return groups.map(group => ({
    ...group,
    protocol: group.protocol === 'http' && group.cases.some(item => taskCaseProtocol(item.input, item.result) === 'sse')
      ? 'sse' as const : group.protocol,
    outcomes: countCaseOutcomes(group.cases.flatMap(item => item.result ? [item.result] : []))
      .filter(outcome => outcome.count > 0),
  }))
}

export type DraftLocation =
  | { kind: 'history'; index: number }
  | { kind: 'standalone'; index: number }
  | { kind: 'example'; operation: number }
  | { kind: 'cases' | 'generated_cases'; operation: number; index: number }

interface DraftGroup {
  key: string
  path: string
  method?: string
  operation?: number
  testCount: number
  cases: { input: TestCase; location: DraftLocation; label: string }[]
}

/** Keep legacy snapshots readable while preferring an explicit case category. */
export function taskCaseLabel(input: TestCase, example?: TestCase) {
  if (input === example || input.kind === 'contract') return '契约示例'
  if (input.kind === 'test' || (input.kind == null && input.source === 'generated')) return 'test'
  return '文档用例'
}

/** Present imported or saved inputs with stable edit locations, including index-only legacy history. */
export function buildTaskDraftGroups(catalog?: Catalog, snapshot?: TaskSnapshot): DraftGroup[] {
  if (snapshot) {
    return buildTaskResultTree(snapshot, []).map(group => ({
      key: operationDraftKey({ ...(group.example ?? group.cases[0].input), path_template: group.path }),
      path: group.path,
      method: group.method,
      testCount: group.cases.filter(({ input }) => taskCaseLabel(input, group.example) === 'test').length,
      cases: group.cases.map(({ input, index }) => ({
        input,
        location: { kind: 'history', index },
        label: taskCaseLabel(input, group.example),
      })),
    }))
  }
  if (!catalog) return []
  const groups = catalog.endpoints.map((endpoint, operation): DraftGroup => ({
    key: operationDraftKey(endpoint),
    path: endpoint.path_template || endpoint.path.split('?')[0],
    method: endpoint.method,
    operation,
    testCount: endpoint.generated_cases?.filter(input => taskCaseLabel(input) === 'test').length ?? 0,
    cases: [
      { input: { ...endpoint, kind: 'contract' as const }, location: { kind: 'example' as const, operation }, label: '契约示例' },
      ...(endpoint.cases ?? []).map((input, index) => ({ input, location: { kind: 'cases' as const, operation, index }, label: '文档用例' })),
      ...(endpoint.generated_cases ?? []).map((input, index) => ({ input, location: { kind: 'generated_cases' as const, operation, index }, label: taskCaseLabel(input) })),
    ],
  }))
  if (catalog.cases.length) groups.push({
    key: 'standalone', path: '文档独立用例', testCount: 0,
    cases: catalog.cases.map((input, index) => ({ input, location: { kind: 'standalone', index }, label: '文档用例' })),
  })
  return groups
}

/** Keep selection keyed by the interface, not by mutable examples or display order. */
export function operationDraftKey(input: TestCase) {
  return JSON.stringify([input.protocol, input.method, input.path_template || input.path.split('?')[0]])
}

/** Copy a history snapshot into a new editable plan only for structural changes. */
export function catalogFromHistory(snapshot: TaskSnapshot, title?: string | null): Catalog {
  const endpoints = buildTaskResultTree(snapshot, []).map(group => {
    const example = group.example ?? group.cases[0].input
    const others = group.cases.map(item => item.input).filter(input => input !== example)
    return { ...example, path_template: group.path,
      cases: others.filter(input => taskCaseLabel(input) !== 'test'),
      generated_cases: others.filter(input => taskCaseLabel(input) === 'test') }
  })
  return { format: 'history', title, server_url: snapshot.origin, endpoints, cases: [], warnings: snapshot.warnings }
}

export function savedRequestData(testCase: TestCase) {
  return {
    path: testCase.path,
    ...(Object.keys(testCase.headers ?? {}).length && { headers: testCase.headers }),
    ...(testCase.body != null && { body: testCase.body }),
    ...(testCase.messages?.length && { messages: testCase.messages }),
    ...(testCase.assertions.length && { assertions: testCase.assertions }),
  }
}
