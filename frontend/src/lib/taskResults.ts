import type { Catalog, TaskCaseResult, TaskRecord, TaskSnapshot, TestCase } from '@/api/tasks'
import type { ConsoleSseEvent, StreamEvent } from '@/api/types'
import { countCaseOutcomes, presentResult, type ResultLike } from './results'

const CASE_ID_RE = /^r(\d+):c(\d+)$/

/** Parse canonical multi-round case IDs shaped like `r1:c0`. */
export function parseTaskCaseId(caseId: string | null | undefined): { round: number; caseIndex: number } | null {
  if (!caseId) return null
  const match = CASE_ID_RE.exec(caseId.trim())
  if (!match) return null
  const round = Number(match[1])
  const caseIndex = Number(match[2])
  if (!Number.isInteger(round) || round < 1 || !Number.isInteger(caseIndex) || caseIndex < 0) return null
  return { round, caseIndex }
}

/** Build the canonical `r{round}:c{caseIndex}` key shared by backend and AI case links. */
export function buildTaskCaseId(round: number, caseIndex: number): string {
  const safeRound = Number.isInteger(round) && round >= 1 ? round : 1
  const safeIndex = Number.isInteger(caseIndex) && caseIndex >= 0 ? caseIndex : 0
  return `r${safeRound}:c${safeIndex}`
}

/** Resolve the 1-based execution round of a recorded case result, defaulting legacy records to 1. */
export function resultRound(result: TaskCaseResult, casesPerRound?: number): number {
  if (typeof result.round === 'number' && Number.isInteger(result.round) && result.round >= 1) {
    return result.round
  }
  const parsed = parseTaskCaseId(result.case_id)
  if (parsed) return parsed.round
  if (
    typeof casesPerRound === 'number' &&
    casesPerRound > 0 &&
    result.case_index == null &&
    typeof result.index === 'number' &&
    result.index >= casesPerRound
  ) {
    return Math.floor(result.index / casesPerRound) + 1
  }
  return 1
}

/** Resolve the 0-based snapshot case index of a recorded result across single- and multi-round runs. */
export function resultCaseIndex(result: TaskCaseResult, casesPerRound?: number): number {
  if (typeof result.case_index === 'number' && Number.isInteger(result.case_index) && result.case_index >= 0) {
    return result.case_index
  }
  const parsed = parseTaskCaseId(result.case_id)
  if (parsed) return parsed.caseIndex
  if (
    typeof casesPerRound === 'number' &&
    casesPerRound > 0 &&
    typeof result.index === 'number' &&
    result.index >= casesPerRound
  ) {
    return result.index % casesPerRound
  }
  return result.index
}

/** Resolve the unique per-round case ID of a result, synthesizing `r{round}:c{caseIndex}` for legacy data. */
export function resultCaseId(result: TaskCaseResult, casesPerRound?: number): string {
  if (typeof result.case_id === 'string' && result.case_id.trim()) return result.case_id.trim()
  return buildTaskCaseId(resultRound(result, casesPerRound), resultCaseIndex(result, casesPerRound))
}

/** Merge incremental NDJSON task results without duplicating or overwriting earlier rounds. */
export function mergeTaskResults(
  existing: readonly TaskCaseResult[],
  incoming: readonly TaskCaseResult[],
  casesPerRound?: number,
): TaskCaseResult[] {
  if (!incoming.length) return existing.slice()
  const merged = existing.slice()
  const positions = new Map<string, number>()
  merged.forEach((item, pos) => {
    positions.set(resultCaseId(item, casesPerRound), pos)
  })
  for (const item of incoming) {
    const key = resultCaseId(item, casesPerRound)
    const found = positions.get(key)
    if (found !== undefined) {
      merged[found] = item
    } else {
      positions.set(key, merged.length)
      merged.push(item)
    }
  }
  return merged
}

/** Locate a recorded result by `case_id` (`r{round}:c{index}`), numeric execution index, or `(round, case_index)`. */
export function findTaskResultByCaseId(
  results: readonly TaskCaseResult[],
  caseId: string | null | undefined,
  casesPerRound?: number,
): TaskCaseResult | undefined {
  if (!caseId) return undefined
  const target = caseId.trim()
  if (!target) return undefined

  const exact = results.find(item => typeof item.case_id === 'string' && item.case_id.trim() === target)
  if (exact) return exact

  const parsed = parseTaskCaseId(target)
  if (parsed) {
    return results.find(
      item => resultRound(item, casesPerRound) === parsed.round && resultCaseIndex(item, casesPerRound) === parsed.caseIndex,
    )
  }

  if (/^\d+$/.test(target)) {
    const numeric = Number(target)
    return results.find(item => item.execution_index === numeric || item.index === numeric)
  }

  return undefined
}

export interface ResolvedTaskCase {
  caseId: string
  round: number
  caseIndex: number
  case_index: number
  executionIndex?: number
  testCase?: TestCase
  result?: TaskCaseResult
}

/** Resolve an AI `CaseLinkBlock` target to its exact `(round, case_index, testCase, result)` tuple. */
export function findTaskCaseForResult(
  record: TaskRecord | null | undefined,
  caseId: string | null | undefined,
): ResolvedTaskCase | null {
  if (!record || !caseId) return null
  const target = caseId.trim()
  if (!target) return null

  const cases = record.snapshot?.cases ?? []
  const casesPerRound = cases.length
  const results = record.results ?? []
  const matched = findTaskResultByCaseId(results, target, casesPerRound)
  if (matched) {
    const round = resultRound(matched, casesPerRound)
    const caseIndex = resultCaseIndex(matched, casesPerRound)
    const resolvedId = resultCaseId(matched, casesPerRound)
    const testCase = caseIndex >= 0 && caseIndex < cases.length ? cases[caseIndex] : undefined
    return {
      caseId: resolvedId,
      round,
      caseIndex,
      case_index: caseIndex,
      executionIndex: matched.execution_index ?? matched.index,
      testCase,
      result: matched,
    }
  }

  const parsed = parseTaskCaseId(target)
  if (parsed && parsed.caseIndex >= 0 && parsed.caseIndex < cases.length) {
    return {
      caseId: buildTaskCaseId(parsed.round, parsed.caseIndex),
      round: parsed.round,
      caseIndex: parsed.caseIndex,
      case_index: parsed.caseIndex,
      testCase: cases[parsed.caseIndex],
      result: undefined,
    }
  }

  if (/^\d+$/.test(target)) {
    const numeric = Number(target)
    if (numeric >= 0 && numeric < cases.length) {
      return {
        caseId: buildTaskCaseId(1, numeric),
        round: 1,
        caseIndex: numeric,
        case_index: numeric,
        testCase: cases[numeric],
        result: undefined,
      }
    }
  }

  return null
}

/** Describe transport failures and write gating even for older recorded cases. */
export function taskCaseMessage(result: TaskCaseResult) {
  if (result.status === 'permission_required' || result.reason === 'write_permission_required') {
    return '需要开启「允许写接口和 WebSocket 发送消息」。本条用例未发送请求。'
  }
  if (result.reason?.startsWith('missing_variable:')) {
    return `缺少前置步骤提取的变量（${result.reason.slice('missing_variable:'.length)}），已跳过本次请求。`
  }
  if (result.reason === 'dependency_failed') {
    return '前置依赖用例未通过，已跳过本次请求。'
  }
  if (result.reason === 'variable_extraction_failed') {
    return '响应满足接口传输，但必需的依赖变量提取失败。'
  }
  if (result.reason === 'unsafe_variable_value') {
    return '提取的运行时变量值包含非法路径或请求头字符，已阻止发送。'
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

export interface CaseNode {
  index: number
  caseIndex: number
  round: number
  caseId: string
  executionIndex?: number
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

function hasRecordedRoundCase(task: TaskRecord, round: number, caseIndex: number): boolean {
  const casesPerRound = task.snapshot.cases.length
  const targetId = buildTaskCaseId(round, caseIndex)
  return task.results.some(result => {
    if (result.case_id) return result.case_id === targetId
    return resultRound(result, casesPerRound) === round && resultCaseIndex(result, casesPerRound) === caseIndex
  })
}

/** Describe the active stream while its verdict is still pending. */
export function activeStreamWait(task: TaskRecord): { index: number; label: string; round?: number; caseId?: string } | null {
  const index = task.current_index
  const round = Math.max(1, task.current_round || 1)
  if (task.status !== 'running' || task.stage !== 'executing' || index == null ||
    !task.snapshot.cases[index] || hasRecordedRoundCase(task, round, index)) return null
  const protocol = task.active_stream === undefined ? task.snapshot.cases[index].protocol : task.active_stream
  if (protocol !== 'sse' && protocol !== 'websocket') return null
  const label = protocol === 'sse' ? '等待 SSE 流结束' : '等待 WebSocket 观察结束'
  return (task.total_rounds ?? 1) > 1 || round > 1
    ? { index, round, caseId: buildTaskCaseId(round, index), label }
    : { index, label }
}

/** Surface the pending response before any stream has been confirmed. */
export function activeTaskWait(task: TaskRecord): { index: number; label: string; round?: number; caseId?: string } | null {
  const stream = activeStreamWait(task)
  if (stream) return stream
  const index = task.current_index
  const round = Math.max(1, task.current_round || 1)
  if (task.status !== 'running' || task.stage !== 'executing' || index == null ||
    !task.snapshot.cases[index] || hasRecordedRoundCase(task, round, index)) return null
  return (task.total_rounds ?? 1) > 1 || round > 1
    ? { index, round, caseId: buildTaskCaseId(round, index), label: '等待响应' }
    : { index, label: '等待响应' }
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

export interface BuildTaskResultTreeOptions {
  totalRounds?: number
}

export function buildTaskResultTree(
  snapshot: TaskSnapshot,
  results: readonly TaskCaseResult[],
  options?: BuildTaskResultTreeOptions,
) {
  const casesPerRound = snapshot.cases.length
  const byCaseId = new Map<string, TaskCaseResult>()
  const byRoundAndCase = new Map<string, TaskCaseResult>()
  const extrasByCase = new Map<number, TaskCaseResult[]>()
  let maxResultRound = 1

  for (const result of results) {
    const round = resultRound(result, casesPerRound)
    const caseIndex = resultCaseIndex(result, casesPerRound)
    const caseId = resultCaseId(result, casesPerRound)
    if (round > maxResultRound) maxResultRound = round
    byCaseId.set(caseId, result)
    const roundCaseKey = `${round}:${caseIndex}`
    if (!byRoundAndCase.has(roundCaseKey)) {
      byRoundAndCase.set(roundCaseKey, result)
    } else {
      const bucket = extrasByCase.get(caseIndex) ?? []
      bucket.push(result)
      extrasByCase.set(caseIndex, bucket)
    }
  }

  const explicitRounds = typeof options?.totalRounds === 'number' && Number.isInteger(options.totalRounds) && options.totalRounds >= 1
    ? options.totalRounds
    : 1
  const resolvedRounds = Math.max(1, explicitRounds, maxResultRound)

  const membership = new Map<number, OperationNode>()
  const byRoute = new Map<string, OperationNode | null>()
  const groups: OperationNode[] = []
  for (const operation of snapshot.operations ?? []) {
    const example = snapshot.cases[operation.example_index]
    if (!example) continue
    const group: OperationNode = {
      key: `operation:${operation.example_index}`,
      method: example.method,
      protocol: example.protocol,
      path: operation.path,
      example,
      cases: [],
    }
    groups.push(group)
    for (const index of operation.case_indices) membership.set(index, group)
    for (const path of new Set([operation.path, example.path.split('?')[0]])) {
      const key = routeKey(example, path)
      byRoute.set(key, byRoute.has(key) ? null : group)
    }
  }

  const fallback = new Map<string, OperationNode>()
  const groupForIndex = snapshot.cases.map((input, index) => {
    const path = input.path.split('?')[0]
    const key = routeKey(input, path)
    let group = membership.get(index) ?? byRoute.get(key) ?? fallback.get(key)
    if (!group) {
      group = { key: `route:${key}`, method: input.method, protocol: input.protocol, path, cases: [] }
      groups.push(group)
      fallback.set(key, group)
    }
    return group
  })

  for (let round = 1; round <= resolvedRounds; round++) {
    snapshot.cases.forEach((input, index) => {
      const group = groupForIndex[index]
      const canonicalId = buildTaskCaseId(round, index)
      const matched = byCaseId.get(canonicalId) ?? byRoundAndCase.get(`${round}:${index}`)
      group.cases.push({
        index,
        caseIndex: index,
        round,
        caseId: matched ? resultCaseId(matched, casesPerRound) : canonicalId,
        executionIndex: matched?.execution_index ?? (resolvedRounds === 1 ? matched?.index : (round - 1) * casesPerRound + index),
        input,
        result: matched,
      })
    })
  }

  // Preserve any unexpected duplicate-round results so no recorded evidence is ever silently dropped.
  extrasByCase.forEach((extraResults, caseIndex) => {
    const input = snapshot.cases[caseIndex]
    const group = groupForIndex[caseIndex]
    if (!input || !group) return
    extraResults.forEach((result, offset) => {
      const round = resultRound(result, casesPerRound)
      group.cases.push({
        index: caseIndex,
        caseIndex,
        round,
        caseId: result.case_id?.trim() || `${buildTaskCaseId(round, caseIndex)}#${offset + 2}`,
        executionIndex: result.execution_index ?? result.index,
        input,
        result,
      })
    })
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

/** Copy a history snapshot into a new editable plan only for structural changes, preserving runtime variables. */
export function catalogFromHistory(snapshot: TaskSnapshot, title?: string | null): Catalog {
  const endpoints = buildTaskResultTree(snapshot, []).map(group => {
    const example = group.example ?? group.cases[0].input
    const others = group.cases.map(item => item.input).filter(input => input !== example)
    return { ...example, path_template: group.path,
      cases: others.filter(input => taskCaseLabel(input) !== 'test'),
      generated_cases: others.filter(input => taskCaseLabel(input) === 'test') }
  })
  return {
    format: 'history',
    title,
    server_url: snapshot.origin,
    ...(snapshot.variables ? { variables: { ...snapshot.variables } } : {}),
    endpoints,
    cases: [],
    warnings: snapshot.warnings,
  }
}

export const historyToCatalog = catalogFromHistory
export const buildHistoryCatalog = catalogFromHistory

export function savedRequestData(testCase: TestCase) {
  return {
    path: testCase.path,
    ...(Object.keys(testCase.headers ?? {}).length && { headers: testCase.headers }),
    ...(testCase.body != null && { body: testCase.body }),
    ...(testCase.messages?.length && { messages: testCase.messages }),
    ...(testCase.assertions.length && { assertions: testCase.assertions }),
    ...(testCase.extract?.length && { extract: testCase.extract }),
  }
}
