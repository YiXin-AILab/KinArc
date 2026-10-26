import { createHttpTransport } from '../api/httpTransport'
import { loadRuntimeConfig } from '../config'
import { SseParser } from '../sse/parser'

/** 自进化任务列表级摘要，对齐 server/schemas.py EvolutionTaskSummary。 */
export interface EvolutionTaskSummary {
  task_id: string
  objective: string
  initial_target: string
  max_iterations: number
  candidate_count: number
  best_validation_score: number | null
  best_candidate_id: string | null
  total_tokens: number
  valid: boolean
  error: string | null
  active_run_id: string | null
}

export interface EvolutionTemplate {
  task_id: string
  objective: string
  initial_target: string
  dataset_case_counts: Record<string, number>
}

export interface EvolutionTaskCreatePayload {
  template_id: string
  task_id: string
  objective?: string
  initial_target?: string
  max_iterations?: number
  improver_timeout_seconds?: number
  max_case_concurrency?: number
  max_retries?: number
  rollback_enabled?: boolean
  exploration_patience?: number
  settings?: Record<string, unknown>
}

export interface EvolutionCaseScore {
  case_id: string
  score: number
  failure_kind: string | null
}

export interface EvolutionRoleResult {
  total_score: number
  correct: number
  case_count: number
  total_tokens: number
  duration_seconds: number
  cases: EvolutionCaseScore[]
}

export interface EvolutionCandidate {
  candidate_id: string
  parent_id: string | null
  is_rollback: boolean
  train: EvolutionRoleResult | null
  validation: EvolutionRoleResult | null
  test: EvolutionRoleResult | null
}

export interface EvolutionTaskDetail {
  task_id: string
  objective: string
  initial_target: string
  max_iterations: number
  settings: Record<string, unknown>
  candidates: EvolutionCandidate[]
  active_run_id: string | null
}

export type EvolutionRunStatus =
  | 'starting'
  | 'running'
  | 'completed'
  | 'failed'
  | 'stopped'

export interface EvolutionRunView {
  run_id: string
  task_id: string
  status: EvolutionRunStatus
  started_at: number
  finished_at: number | null
  stop_reason: string | null
  error: string | null
  best_candidate_id: string | null
  validation_score: number | null
  event_count: number
}

/** SSE progress 帧的 data 负载，对齐 server/evolution.py _encode_progress_frame。 */
export interface EvolutionProgressEvent {
  cursor: number
  ts: number
  event: string
  message: string
  fields: Record<string, string>
}

export interface OpenRunEventsOptions {
  lastEventId?: string
  signal: AbortSignal
  onEvent: (event: EvolutionProgressEvent) => void
  /** 连续重连失败超过上限后回调一次并停止。 */
  onError?: (error: Error) => void
  /** 断线后重连成功时回调，调用方应对齐权威状态。 */
  onReconnect?: () => void
}

export interface EvolutionClient {
  listTasks(signal?: AbortSignal): Promise<EvolutionTaskSummary[]>
  listTemplates(signal?: AbortSignal): Promise<EvolutionTemplate[]>
  createTask(
    payload: EvolutionTaskCreatePayload,
    signal?: AbortSignal,
  ): Promise<EvolutionTaskSummary>
  taskDetail(taskId: string, signal?: AbortSignal): Promise<EvolutionTaskDetail>
  /** 按需拉取单个候选的 changelog 原文（体积大，不进详情响应）。 */
  candidateChangelog(
    taskId: string,
    candidateId: string,
    signal?: AbortSignal,
  ): Promise<string>
  startRun(taskId: string, signal?: AbortSignal): Promise<EvolutionRunView>
  stopRun(runId: string, signal?: AbortSignal): Promise<EvolutionRunView>
  getRun(runId: string, signal?: AbortSignal): Promise<EvolutionRunView>
  /** 打开 Run 事件流并在后台持续读取；断线自动以 Last-Event-ID 指数退避重连。 */
  openRunEvents(runId: string, options: OpenRunEventsOptions): void
}

export interface EvolutionClientConfig {
  apiBaseUrl: string
  tokenProvider?: () => string | null
}

const MAX_RECONNECT_ATTEMPTS = 6

export function createEvolutionClient(
  config: EvolutionClientConfig,
  fetchImpl: typeof fetch = fetch,
): EvolutionClient {
  const base = `${config.apiBaseUrl}/v1/evolution`
  const transport = createHttpTransport(config, fetchImpl)

  const url = (path: string): string => `${base}${path}`

  const request = <T>(
    path: string,
    options: { method?: string; body?: unknown; signal?: AbortSignal } = {},
  ): Promise<T> => transport.requestJson<T>(url(path), options)

  const readEventStream = async (
    response: Response,
    options: OpenRunEventsOptions,
    onCursor: (cursor: string) => void,
  ): Promise<void> => {
    if (!response.body) throw new Error('事件流响应缺少 body')
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    const parser = new SseParser()
    try {
      while (!options.signal.aborted) {
        const { value, done } = await reader.read()
        if (done || options.signal.aborted) break
        for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
          if (frame.id) onCursor(frame.id)
          options.onEvent(parseProgressEvent(frame.data))
        }
      }
    } finally {
      reader.releaseLock()
    }
  }

  const openRunEvents = (runId: string, options: OpenRunEventsOptions): void => {
    const path = `/runs/${encodeURIComponent(runId)}/events`
    let lastEventId = options.lastEventId
    let attempts = 0
    const connect = async (): Promise<void> => {
      while (!options.signal.aborted) {
        const wasReconnect = attempts > 0
        try {
          // 超时仅覆盖连接建立；已建立的流不设总超时，仍受调用方取消信号控制。
          const response = await transport.openStream(url(path), {
            headers: lastEventId ? { 'Last-Event-ID': lastEventId } : undefined,
            signal: options.signal,
          })
          if (wasReconnect) options.onReconnect?.()
          attempts = 0
          await readEventStream(response, options, (cursor) => {
            lastEventId = cursor
          })
          // 服务端在 run 终态后正常关闭流，无需重连。
          return
        } catch (caught) {
          if (options.signal.aborted) return
          attempts += 1
          if (attempts > MAX_RECONNECT_ATTEMPTS) {
            options.onError?.(toError(caught))
            return
          }
          await sleep(Math.min(500 * 2 ** attempts, 8000), options.signal)
        }
      }
    }
    void connect()
  }

  return {
    async listTasks(signal) {
      const body = await request<{ tasks: EvolutionTaskSummary[] }>('/tasks', {
        signal,
      })
      return body.tasks
    },
    async listTemplates(signal) {
      const body = await request<{ templates: EvolutionTemplate[] }>('/templates', {
        signal,
      })
      return body.templates
    },
    async createTask(payload, signal) {
      const body = await request<{ task: EvolutionTaskSummary }>('/tasks', {
        method: 'POST',
        body: payload,
        signal,
      })
      return body.task
    },
    taskDetail(taskId, signal) {
      return request<EvolutionTaskDetail>(`/tasks/${encodeURIComponent(taskId)}`, {
        signal,
      })
    },
    async candidateChangelog(taskId, candidateId, signal) {
      const body = await request<{ candidate_id: string; changelog_markdown: string }>(
        `/tasks/${encodeURIComponent(taskId)}/candidates/${encodeURIComponent(candidateId)}/changelog`,
        { signal },
      )
      return body.changelog_markdown
    },
    async startRun(taskId, signal) {
      const body = await request<{ run: EvolutionRunView }>(
        `/tasks/${encodeURIComponent(taskId)}/runs`,
        { method: 'POST', signal },
      )
      return body.run
    },
    async stopRun(runId, signal) {
      const body = await request<{ run: EvolutionRunView }>(
        `/runs/${encodeURIComponent(runId)}/stop`,
        { method: 'POST', signal },
      )
      return body.run
    },
    async getRun(runId, signal) {
      const body = await request<{ run: EvolutionRunView }>(
        `/runs/${encodeURIComponent(runId)}`,
        { signal },
      )
      return body.run
    },
    openRunEvents,
  }
}

/** 页面默认使用的自进化客户端；测试中 mock 本模块即可替换。 */
export function defaultEvolutionClient(): EvolutionClient {
  const config = loadRuntimeConfig(import.meta.env)
  return createEvolutionClient({
    apiBaseUrl: config.apiBaseUrl,
    tokenProvider: () => sessionStorage.getItem('agentsystem.auth-token'),
  })
}

function parseProgressEvent(data: string): EvolutionProgressEvent {
  const value: unknown = JSON.parse(data)
  if (!isRecord(value)) throw new Error('事件帧格式非法')
  return {
    cursor: numberField(value, 'cursor') ?? 0,
    ts: numberField(value, 'ts') ?? 0,
    event: stringField(value, 'event') ?? 'log',
    message: stringField(value, 'message') ?? '',
    fields: isRecord(value.fields)
      ? stringMap(value.fields as Record<string, unknown>)
      : {},
  }
}

function stringMap(value: Record<string, unknown>): Record<string, string> {
  const result: Record<string, string> = {}
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === 'string') result[key] = entry
  }
  return result
}

function stringField(
  value: Record<string, unknown>,
  field: string,
): string | undefined {
  return typeof value[field] === 'string' ? value[field] : undefined
}

function numberField(
  value: Record<string, unknown>,
  field: string,
): number | undefined {
  return typeof value[field] === 'number' ? value[field] : undefined
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function toError(caught: unknown): Error {
  return caught instanceof Error ? caught : new Error(String(caught))
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms)
    function done(): void {
      clearTimeout(timer)
      signal.removeEventListener('abort', done)
      resolve()
    }
    signal.addEventListener('abort', done, { once: true })
  })
}
