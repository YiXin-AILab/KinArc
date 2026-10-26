import { createHttpTransport } from '../api/httpTransport'
import { loadRuntimeConfig } from '../config'

/** 评测任务摘要，对齐 server/schemas.py EvaluationTaskSummary。 */
export interface EvaluationTaskSummary {
  task_id: string
  agent_id: string
  dataset: string
  case_count: number
  run_count: number
  valid: boolean
  error: string | null
}

export type EvaluationRunStatus = 'pending' | 'running' | 'succeeded' | 'failed'

/** 评测 Run 快照，对齐 server/schemas.py EvaluationRunView。 */
export interface EvaluationRunView {
  run_id: string
  task_id: string
  status: EvaluationRunStatus
  created_at: number
  started_at: number | null
  finished_at: number | null
  case_count: number | null
  completed_cases: number
  final_score: number | null
  error: string | null
}

export interface EvaluationTaskDetail {
  task: EvaluationTaskSummary
  runs: EvaluationRunView[]
}

/** 报告模型，对齐 evaluation/models.py。 */
export interface RuleResult {
  rule: string
  passed: boolean
  detail: string
}

export interface DimScore {
  dimension: string
  score: number | null
  weight: number
  rationale: string
  error: string | null
}

export interface CaseScore {
  case_id: string
  rules: RuleResult[]
  dims: DimScore[]
  final_score: number
  failed: boolean
}

export interface EvalReport {
  run_id: string
  task_id: string
  agent_id: string
  dataset: string
  created_at: string
  case_count: number
  failed_count: number
  dim_averages: Record<string, number>
  final_score: number
  cases: CaseScore[]
}

/** 对比报告，对齐 evaluation/report.py ComparisonReport。 */
export interface CaseDelta {
  case_id: string
  baseline_score: number
  current_score: number
  delta: number
  status: 'improved' | 'regressed' | 'unchanged'
}

export interface ComparisonReport {
  baseline_run_id: string
  current_run_id: string
  case_deltas: CaseDelta[]
  dim_deltas: Record<string, number>
  improved_count: number
  regressed_count: number
  unchanged_count: number
  final_score_delta: number
}

export interface EvaluationClient {
  listTasks(signal?: AbortSignal): Promise<EvaluationTaskSummary[]>
  taskDetail(taskId: string, signal?: AbortSignal): Promise<EvaluationTaskDetail>
  startRun(taskId: string, signal?: AbortSignal): Promise<EvaluationRunView>
  getRun(runId: string, signal?: AbortSignal): Promise<EvaluationRunView>
  getReport(runId: string, signal?: AbortSignal): Promise<EvalReport>
  compare(
    runId: string,
    baseline: string,
    signal?: AbortSignal,
  ): Promise<ComparisonReport>
}

export interface EvaluationClientConfig {
  apiBaseUrl: string
  tokenProvider?: () => string | null
}

export function createEvaluationClient(
  config: EvaluationClientConfig,
  fetchImpl: typeof fetch = fetch,
): EvaluationClient {
  const base = `${config.apiBaseUrl}/v1/evaluation`
  const transport = createHttpTransport(config, fetchImpl)

  const request = <T>(
    path: string,
    options: { method?: string; signal?: AbortSignal } = {},
  ): Promise<T> => transport.requestJson<T>(`${base}${path}`, options)

  return {
    async listTasks(signal) {
      const body = await request<{ tasks: EvaluationTaskSummary[] }>('/tasks', {
        signal,
      })
      return body.tasks
    },
    taskDetail(taskId, signal) {
      return request<EvaluationTaskDetail>(`/tasks/${encodeURIComponent(taskId)}`, {
        signal,
      })
    },
    async startRun(taskId, signal) {
      const body = await request<{ run: EvaluationRunView }>(
        `/tasks/${encodeURIComponent(taskId)}/runs`,
        { method: 'POST', signal },
      )
      return body.run
    },
    async getRun(runId, signal) {
      const body = await request<{ run: EvaluationRunView }>(
        `/runs/${encodeURIComponent(runId)}`,
        { signal },
      )
      return body.run
    },
    getReport(runId, signal) {
      return request<EvalReport>(`/runs/${encodeURIComponent(runId)}/report`, {
        signal,
      })
    },
    compare(runId, baseline, signal) {
      const query = `baseline=${encodeURIComponent(baseline)}`
      return request<ComparisonReport>(
        `/runs/${encodeURIComponent(runId)}/compare?${query}`,
        { signal },
      )
    },
  }
}

/** 页面默认使用的评测客户端；测试中 mock 本模块即可替换。 */
export function defaultEvaluationClient(): EvaluationClient {
  const config = loadRuntimeConfig(import.meta.env)
  return createEvaluationClient({
    apiBaseUrl: config.apiBaseUrl,
    tokenProvider: () => sessionStorage.getItem('agentsystem.auth-token'),
  })
}
