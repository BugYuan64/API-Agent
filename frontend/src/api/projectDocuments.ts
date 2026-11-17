/** 项目源码快照与接口文档 API。复用管理端传输，导入路径由运行后端的电脑解释。 */
import { request } from './client'
import type { Project } from './types'

/** 已脱敏源码扫描的摘要，文件与接口仅携带相对路径。 */
export interface SourceSummary {
  id: string
  created_at: string
  sha256: string
  scanned_files: number
  total_files: number
  excluded_files: number
  total_bytes: number
  complete: boolean
  endpoints: { method: string; path: string; file: string; line: number; framework: string; warnings: string[] }[]
  issues: { path: string; reason: string }[]
  warnings: string[]
}

/** 模型提取的接口说明与源码依据，未知项由 uncertainties 保留。 */
export interface DocumentEndpoint {
  method: string
  path: string
  summary: string
  description: string
  parameters: { name: string; location: string; required: boolean; type: string; description: string }[]
  request_body: string
  responses: { status: string; description: string }[]
  authentication: string
  source_files: string[]
  uncertainties: string[]
}

/** 可恢复的生成记录，进度属于冻结的 source_id，状态不表示运行时接口覆盖证明。 */
export interface DocumentRecord {
  id: string
  project_id: string
  source_id: string
  project_name: string
  status: 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled' | 'interrupted'
  created_at: string
  updated_at: string
  processed_files: number
  total_files: number
  processed_batches: number
  total_batches: number
  endpoints: DocumentEndpoint[]
  warnings: string[]
  error: string | null
  markdown: string
}

/** 创建项目并读取指定目录，后端先完成扫描再返回可生成的快照。 */
export const importProject = (body: { name: string; description: string; directory: string }) =>
  request<{ project: Project; source: SourceSummary }>('/api/projects/import', {
    method: 'POST', body, timeoutMs: 120_000,
  })

/** 给已有项目追加源码快照，历史文档继续引用原快照。 */
export const importProjectSource = (projectId: string, directory: string) =>
  request<SourceSummary>(`/api/projects/${encodeURIComponent(projectId)}/source`, {
    method: 'POST', body: { directory }, timeoutMs: 120_000,
  })

/** 读取最新源码摘要，尚未导入由后端返回 404。 */
export const readProjectSource = (projectId: string, signal?: AbortSignal) =>
  request<SourceSummary>(`/api/projects/${encodeURIComponent(projectId)}/source`, { signal })

/** 列出已保存文档，包括刷新前仍在生成的记录。 */
export const listProjectDocuments = (projectId: string, signal?: AbortSignal) =>
  request<{ items: DocumentRecord[] }>(`/api/projects/${encodeURIComponent(projectId)}/documents`, { signal })

/** 按冻结源码快照启动后台生成，前端通过详情接口追踪进度。 */
export const startProjectDocument = (projectId: string, sourceId: string) =>
  request<DocumentRecord>(`/api/projects/${encodeURIComponent(projectId)}/documents`, {
    method: 'POST', body: { source_id: sourceId },
  })

/** 拉取单份文档内容与进度，signal 用于切换记录和卸载时取消等待。 */
export const readProjectDocument = (projectId: string, documentId: string, signal?: AbortSignal) =>
  request<DocumentRecord>(`/api/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`, { signal })

/** 取消正在生成的文档，已完成内容由后端保存。 */
export const cancelProjectDocument = (projectId: string, documentId: string) =>
  request<DocumentRecord>(`/api/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}/cancel`, {
    method: 'POST',
  })

/** 获取可导出的保存版本，仍由共享客户端显示管理错误，避免下载错误响应。 */
export const exportProjectDocument = (projectId: string, documentId: string, format: 'markdown' | 'openapi') =>
  request<string | Record<string, unknown>>(
    `/api/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}/export?format=${format}`,
  )
