import { describe, expect, it, vi } from 'vitest'

import { RuntimeHttpError } from '../api/client'
import {
  createEvaluationClient,
  type EvalReport,
  type EvaluationRunView,
} from './evaluationClient'

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const RUN: EvaluationRunView = {
  run_id: 'run-1',
  task_id: 'auto_cs',
  status: 'succeeded',
  created_at: 1759200000,
  started_at: 1759200001,
  finished_at: 1759200060,
  case_count: 44,
  completed_cases: 44,
  final_score: 0.87,
  error: null,
}

const REPORT: EvalReport = {
  run_id: 'run-1',
  task_id: 'auto_cs',
  agent_id: 'auto-after-sales',
  dataset: 'main',
  created_at: '2026-09-30T00:00:00Z',
  case_count: 1,
  failed_count: 1,
  dim_averages: { faithfulness: 0.9 },
  final_score: 0.9,
  cases: [
    {
      case_id: 'c1',
      rules: [{ rule: 'must_refuse', passed: false, detail: '未拒答' }],
      dims: [
        {
          dimension: 'faithfulness',
          score: 0.9,
          weight: 1,
          rationale: '基本一致',
          error: null,
        },
      ],
      final_score: 0,
      failed: true,
    },
  ],
}

function mockFetch(payload: unknown, status = 200) {
  return vi
    .fn<typeof fetch>()
    .mockImplementation(() => Promise.resolve(jsonResponse(payload, status)))
}

describe('createEvaluationClient', () => {
  it('lists tasks and unwraps the tasks field', async () => {
    const fetchImpl = mockFetch({ tasks: [{ task_id: 'auto_cs' }] })
    const client = createEvaluationClient({ apiBaseUrl: 'http://api' }, fetchImpl)

    const tasks = await client.listTasks()

    expect(fetchImpl).toHaveBeenCalledWith(
      'http://api/v1/evaluation/tasks',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(tasks).toEqual([{ task_id: 'auto_cs' }])
  })

  it('starts a run with POST and unwraps the run field', async () => {
    const fetchImpl = mockFetch({ run: RUN }, 201)
    const client = createEvaluationClient({ apiBaseUrl: '' }, fetchImpl)

    const run = await client.startRun('auto_cs')

    const [url, init] = fetchImpl.mock.calls[0]!
    expect(url).toBe('/v1/evaluation/tasks/auto_cs/runs')
    expect(init?.method).toBe('POST')
    expect(run.run_id).toBe('run-1')
  })

  it('fetches run snapshots and full reports', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ run: RUN }))
      .mockResolvedValueOnce(jsonResponse(REPORT))
    const client = createEvaluationClient({ apiBaseUrl: '' }, fetchImpl)

    const run = await client.getRun('run-1')
    const report = await client.getReport('run-1')

    expect(fetchImpl.mock.calls[0]![0]).toBe('/v1/evaluation/runs/run-1')
    expect(fetchImpl.mock.calls[1]![0]).toBe('/v1/evaluation/runs/run-1/report')
    expect(run.final_score).toBe(0.87)
    expect(report.cases[0]!.rules[0]!.passed).toBe(false)
    expect(report.cases[0]!.dims[0]!.rationale).toBe('基本一致')
  })

  it('encodes the baseline query for compare', async () => {
    const fetchImpl = mockFetch({
      baseline_run_id: 'run-0',
      current_run_id: 'run-1',
      case_deltas: [],
      dim_deltas: {},
      improved_count: 0,
      regressed_count: 1,
      unchanged_count: 0,
      final_score_delta: -0.1,
    })
    const client = createEvaluationClient({ apiBaseUrl: '' }, fetchImpl)

    const comparison = await client.compare('run-1', 'run 0')

    expect(fetchImpl.mock.calls[0]![0]).toBe(
      '/v1/evaluation/runs/run-1/compare?baseline=run%200',
    )
    expect(comparison.regressed_count).toBe(1)
  })

  it('applies a finite deadline when the server stalls', async () => {
    vi.useFakeTimers()
    try {
      const fetchImpl = vi.fn<typeof fetch>().mockImplementation(
        (_url, init) =>
          new Promise<never>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
              once: true,
            })
          }),
      )
      const client = createEvaluationClient({ apiBaseUrl: '' }, fetchImpl)

      const result = client.getRun('run-1').catch((error: unknown) => error)
      await vi.advanceTimersByTimeAsync(30_000)

      expect(fetchImpl.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
      await expect(result).resolves.toMatchObject({ name: 'TimeoutError' })
    } finally {
      vi.useRealTimers()
    }
  })

  it('maps backend error bodies to RuntimeHttpError', async () => {
    const fetchImpl = mockFetch(
      { code: 'evaluation_run_not_found', message: 'Run 不存在：x' },
      404,
    )
    const client = createEvaluationClient({ apiBaseUrl: '' }, fetchImpl)

    const failure = await client.getRun('x').catch((e) => e)

    expect(failure).toBeInstanceOf(RuntimeHttpError)
    expect(failure.status).toBe(404)
    expect(failure.code).toBe('evaluation_run_not_found')
  })
})
