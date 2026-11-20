/** Shared size limits for the three test workspaces. */
export const WORKSPACE_LAYOUT_BOUNDS = {
  history: { min: 200, max: 360 },
  editor: { min: 320, max: 720 },
  results: { min: 320, max: 720 },
  assistantHeight: { min: 200, max: 420 },
  upperMinHeight: 220,
  narrowViewport: 680,
} as const

export type WorkspaceWidths = Record<'history' | 'editor' | 'results', number>
const resizeOrder = ['editor', 'results', 'history'] as const
const bounds = WORKSPACE_LAYOUT_BOUNDS

export const workspaceMaxWidth = bounds.history.max + bounds.editor.max + bounds.results.max

export function workspaceMinWidth(hideHistory: boolean): number {
  return (hideHistory ? 0 : bounds.history.min) + bounds.editor.min + bounds.results.min
}

/** Give each pane as much of the width change as it can absorb, in priority order. */
export function allocateWorkspaceWidth(total: number, previous: WorkspaceWidths, hideHistory = false): WorkspaceWidths {
  const historyMin = hideHistory ? 0 : bounds.history.min
  const historyMax = hideHistory ? 0 : bounds.history.max
  const maximum = historyMax + bounds.editor.max + bounds.results.max
  const available = Math.max(0, Math.min(maximum, total))
  const minimum = workspaceMinWidth(hideHistory)
  if (available < minimum) {
    const history = available * historyMin / minimum
    const editor = available * bounds.editor.min / minimum
    return { history, editor, results: available - history - editor }
  }

  const widths = { ...previous }
  for (const pane of resizeOrder) {
    const { min, max } = pane === 'history' ? { min: historyMin, max: historyMax } : bounds[pane]
    widths[pane] = Math.min(max, Math.max(min, widths[pane]))
  }
  let remaining = available - widths.history - widths.editor - widths.results
  for (const pane of resizeOrder) {
    const { min, max } = pane === 'history' ? { min: historyMin, max: historyMax } : bounds[pane]
    const applied = Math.min(max - widths[pane], Math.max(min - widths[pane], remaining))
    widths[pane] += applied
    remaining -= applied
  }
  return widths
}

/** A divider can only transfer width between its two neighbors. */
export function adjacentWidthRange(widths: WorkspaceWidths, pane: 'history' | 'editor', hideHistory = false) {
  if (pane === 'history' && hideHistory) return { min: 0, max: 0 }
  const neighbor = pane === 'history' ? 'editor' : 'results'
  const total = widths[pane] + widths[neighbor]
  const min = Math.max(bounds[pane].min, total - bounds[neighbor].max)
  const max = Math.min(bounds[pane].max, total - bounds[neighbor].min)
  return min <= max ? { min, max } : { min: widths[pane], max: widths[pane] }
}
