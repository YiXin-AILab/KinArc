import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../router'
import EvaluationDetailPage from './EvaluationDetailPage.vue'
import EvaluationListPage from './EvaluationListPage.vue'
import type {
  EvalReport,
  EvaluationClient,
  EvaluationRunView,
  EvaluationTaskSummary,
} from './evaluationClient'

const TASK: EvaluationTaskSummary = {
  task_id: 'auto_cs',
  agent_id: 'auto-after-sales',
  dataset: 'main',
  case_count: 44,
  run_count: 1,
  valid: true,
  error: null,
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

const BASELINE: EvaluationRunView = {
  ...RUN,
  run_id: 'run-0',
  final_score: 0.8,
}

const REPORT: EvalReport = {
  run_id: 'run-1',
  task_id: 'auto_cs',
  agent_id: 'auto-after-sales',
  dataset: 'main',
  created_at: '2026-09-30T00:00:00Z',
  case_count: 2,
  failed_count: 1,
  dim_averages: { faithfulness: 0.9, helpfulness: 0.84 },
  final_score: 0.87,
  cases: [
    {
      case_id: 'cs-001',
      rules: [{ rule: 'key_points', passed: true, detail: '覆盖全部要点' }],
      dims: [
        {
          dimension: 'faithfulness',
          score: 0.9,
          weight: 1,
          rationale: '回答与知识库一致',
          error: null,
        },
      ],
      final_score: 0.9,
      failed: false,
    },
    {
      case_id: 'cs-002',
      rules: [{ rule: 'must_refuse', passed: false, detail: '未拒答库外问题' }],
      dims: [
        {
          dimension: 'faithfulness',
          score: 0.4,
          weight: 1,
          rationale: '编造了价格',
          error: null,
        },
      ],
      final_score: 0,
      failed: true,
    },
  ],
}

function makeClient(overrides: Partial<EvaluationClient> = {}): EvaluationClient {
  return {
    listTasks: vi.fn().mockResolvedValue([TASK]),
    taskDetail: vi.fn().mockResolvedValue({ task: TASK, runs: [RUN, BASELINE] }),
    startRun: vi.fn().mockResolvedValue({ ...RUN, run_id: 'run-2', status: 'pending' }),
    getRun: vi.fn().mockResolvedValue(RUN),
    getReport: vi.fn().mockResolvedValue(REPORT),
    compare: vi.fn().mockResolvedValue({
      baseline_run_id: 'run-0',
      current_run_id: 'run-1',
      case_deltas: [
        {
          case_id: 'cs-002',
          baseline_score: 0.5,
          current_score: 0,
          delta: -0.5,
          status: 'regressed',
        },
      ],
      dim_deltas: { faithfulness: 0.1 },
      improved_count: 0,
      regressed_count: 1,
      unchanged_count: 0,
      final_score_delta: 0.07,
    }),
    ...overrides,
  }
}

async function mountList(client: EvaluationClient) {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/evaluation')
  await router.isReady()
  const wrapper = mount(EvaluationListPage, {
    props: { client },
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

async function mountDetail(client: EvaluationClient) {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/evaluation/runs/run-1')
  await router.isReady()
  const wrapper = mount(EvaluationDetailPage, {
    props: { client },
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('EvaluationListPage', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => vi.restoreAllMocks())

  it('renders tasks with run history', async () => {
    const { wrapper } = await mountList(makeClient())

    expect(wrapper.text()).toContain('auto_cs')
    expect(wrapper.text()).toContain('auto-after-sales')
    expect(wrapper.text()).toContain('run-1')
    expect(wrapper.text()).toContain('87.0%')
    expect(wrapper.text()).toContain('已完成')
    wrapper.unmount()
  })

  it('starts a run and navigates to its detail page', async () => {
    const client = makeClient()
    const { wrapper, router } = await mountList(client)

    await wrapper
      .findAll('button')
      .filter((button) => button.text() === '发起评测')[0]!
      .trigger('click')
    await flushPromises()

    expect(client.startRun).toHaveBeenCalledWith('auto_cs')
    expect(router.currentRoute.value.path).toBe('/platform/evaluation/runs/run-2')
    wrapper.unmount()
  })
})

describe('EvaluationDetailPage', () => {
  beforeEach(() => vi.clearAllMocks())
  afterEach(() => vi.restoreAllMocks())

  it('renders the report with dimension averages and failed case highlight', async () => {
    const { wrapper } = await mountDetail(makeClient())

    expect(wrapper.text()).toContain('87.0%')
    expect(wrapper.text()).toContain('faithfulness')
    expect(wrapper.text()).toContain('90.0%')
    expect(wrapper.text()).toContain('cs-002')
    expect(wrapper.find('.run-case-failed').exists()).toBe(true)
    wrapper.unmount()
  })

  it('expands a case to show rule results and judge rationale', async () => {
    const { wrapper } = await mountDetail(makeClient())

    await wrapper.findAll('.run-case-head')[1]!.trigger('click')

    expect(wrapper.text()).toContain('未拒答库外问题')
    expect(wrapper.text()).toContain('编造了价格')
    wrapper.unmount()
  })

  it('polls while the run is active and stops at terminal state', async () => {
    vi.useFakeTimers()
    try {
      const client = makeClient({
        getRun: vi
          .fn()
          .mockResolvedValueOnce({ ...RUN, status: 'running', completed_cases: 10 })
          .mockResolvedValueOnce(RUN),
      })
      const { wrapper } = await mountDetail(client)

      expect(client.getRun).toHaveBeenCalledTimes(1)
      expect(wrapper.text()).toContain('进行中')

      await vi.advanceTimersByTimeAsync(2600)
      await flushPromises()

      expect(client.getRun).toHaveBeenCalledTimes(2)
      expect(client.getReport).toHaveBeenCalledWith('run-1', expect.anything())
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('runs a baseline comparison and renders deltas', async () => {
    const client = makeClient()
    const { wrapper } = await mountDetail(client)

    await wrapper.find('.run-select').setValue('run-0')
    await wrapper
      .findAll('button')
      .filter((button) => button.text() === '对比')[0]!
      .trigger('click')
    await flushPromises()

    expect(client.compare).toHaveBeenCalledWith('run-1', 'run-0')
    expect(wrapper.text()).toContain('回归 1')
    expect(wrapper.text()).toContain('+7.0%')
    expect(wrapper.text()).toContain('cs-002')
    wrapper.unmount()
  })
})
