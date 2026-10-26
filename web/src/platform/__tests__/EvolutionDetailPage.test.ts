import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import EvolutionDetailPage from '../EvolutionDetailPage.vue'
import type { EvolutionProgressEvent } from '../evolutionClient'

const { client, sse } = vi.hoisted(() => ({
  client: {
    listTasks: vi.fn(),
    listTemplates: vi.fn(),
    createTask: vi.fn(),
    taskDetail: vi.fn(),
    startRun: vi.fn(),
    stopRun: vi.fn(),
    getRun: vi.fn(),
    openRunEvents: vi.fn(),
    candidateChangelog: vi.fn(),
  },
  sse: { options: null as null | Record<string, unknown> },
}))

vi.mock('../evolutionClient', () => ({ defaultEvolutionClient: () => client }))

function progress(
  cursor: number,
  event: string,
  fields: Record<string, string> = {},
): EvolutionProgressEvent {
  return {
    cursor,
    ts: 1760000000 + cursor,
    event,
    message: `${event} ${Object.entries(fields)
      .map(([key, value]) => `${key}=${value}`)
      .join(' ')}`,
    fields,
  }
}

function roleResult(
  score: number,
  correct: number,
  count: number,
  cases: string[] = [],
) {
  return {
    total_score: score,
    correct,
    case_count: count,
    total_tokens: 12000,
    duration_seconds: 95,
    cases: cases.map((case_id, index) => ({
      case_id,
      score: index < correct ? 1 : 0,
      failure_kind: index < correct ? null : 'wrong_result',
    })),
  }
}

function candidate(id: string, partial: Record<string, unknown> = {}) {
  return {
    candidate_id: id,
    parent_id: 'candidate-0',
    is_rollback: false,
    train: roleResult(0.625, 5, 8, ['case_a', 'case_b']),
    validation: roleResult(0.6, 3, 5),
    test: null,
    ...partial,
  }
}

function changelogOf(id: string) {
  return `## 评测版本：${id}\n### 意图\n修复 ${id} 的 dispute_charge 误判\n`
}

function detail(partial: Record<string, unknown> = {}) {
  return {
    task_id: 't1',
    objective: 'Improve success rate.',
    initial_target: 'application/banking_agent',
    max_iterations: 5,
    settings: {},
    candidates: [],
    active_run_id: null,
    ...partial,
  }
}

function runView(status: string, partial: Record<string, unknown> = {}) {
  return {
    run_id: 'run-1',
    task_id: 't1',
    status,
    started_at: 1760000000,
    finished_at: null,
    stop_reason: null,
    error: null,
    best_candidate_id: null,
    validation_score: null,
    event_count: 0,
    ...partial,
  }
}

function emit(...events: EvolutionProgressEvent[]) {
  const onEvent = sse.options?.onEvent as (e: EvolutionProgressEvent) => void
  for (const event of events) onEvent(event)
}

async function mountPage() {
  client.openRunEvents.mockImplementation((_runId: string, options: unknown) => {
    sse.options = options as Record<string, unknown>
  })
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/evolution/t1')
  await router.isReady()
  const wrapper = mount(EvolutionDetailPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('EvolutionDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sse.options = null
  })

  it('shows config summary and starts a run from idle state', async () => {
    client.taskDetail.mockResolvedValue(detail())
    client.startRun.mockResolvedValue(runView('running'))
    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('任务配置')
    expect(wrapper.text()).toContain('application/banking_agent')

    const launch = wrapper
      .findAll('button')
      .find((b) => b.text().includes('启动优化任务'))
    await launch!.trigger('click')
    await flushPromises()

    expect(client.startRun).toHaveBeenCalledWith('t1')
    expect(client.openRunEvents).toHaveBeenCalledWith('run-1', expect.anything())
    expect(wrapper.text()).toContain('候选版本流水线')
    wrapper.unmount()
  })

  it('projects SSE events into pipeline, stage panel and logs, then final view', async () => {
    client.taskDetail
      .mockResolvedValueOnce(
        detail({
          candidates: [candidate('candidate-0', { parent_id: null })],
          active_run_id: 'run-1',
        }),
      )
      .mockResolvedValue(
        detail({
          candidates: [
            candidate('candidate-0', { parent_id: null }),
            candidate('candidate-1', {
              train: roleResult(0.75, 6, 8, ['case_a', 'case_b']),
              validation: null,
            }),
          ],
        }),
      )
    client.getRun.mockResolvedValueOnce(runView('running')).mockResolvedValue(
      runView('completed', {
        best_candidate_id: 'candidate-0',
        validation_score: 0.6,
      }),
    )

    const wrapper = await mountPage()
    expect(wrapper.text()).toContain('RUNNING')
    expect(client.openRunEvents).toHaveBeenCalledWith('run-1', expect.anything())

    // 迭代开始 → 新增候选卡片
    emit(
      progress(1, 'run_started', { mode: 'improve', max_iterations: '5' }),
      progress(2, 'iteration_started', { iteration: '1/5', candidate: 'candidate-1' }),
    )
    await flushPromises()
    expect(wrapper.findAll('.evo-cand')).toHaveLength(2)
    expect(wrapper.get('[data-testid="stat-iteration"]').text()).toBe('1/5')

    // improver attempt → 改进阶段运行态
    emit(
      progress(3, 'improver_attempt_started', {
        candidate: 'candidate-1',
        output_candidate: 'candidate-1',
        attempt: '1',
        max_attempts: '4',
      }),
    )
    await flushPromises()
    expect(wrapper.get('[data-testid="stage-title"]').text()).toContain('优化改进')
    expect(wrapper.get('[data-testid="stage-right"]').text()).toContain('attempt 1/4')
    const cand1 = wrapper.get('[data-candidate="candidate-1"]')
    expect(cand1.findAll('.evo-stage')[0]!.classes()).toContain('run')

    // 发布 → 前两段完成
    emit(
      progress(4, 'candidate_published', {
        candidate: 'candidate-1',
        parent: 'candidate-0',
      }),
    )
    await flushPromises()
    const stages = wrapper.get('[data-candidate="candidate-1"]').findAll('.evo-stage')
    expect(stages[0]!.classes()).toContain('ok')
    expect(stages[1]!.classes()).toContain('ok')

    // 训练执行 → case 列表实时渲染
    emit(
      progress(5, 'candidate_started', {
        candidate: 'candidate-1',
        stage: 'assessment',
      }),
      progress(6, 'case_dispatched', {
        case: 'case_a',
        index: '1/8',
        completed: '0/8',
        active: '1',
      }),
      progress(7, 'case_completed', {
        case: 'case_a',
        result: 'completed',
        attempts: '1',
        completed: '1/8',
      }),
    )
    await flushPromises()
    expect(wrapper.get('[data-testid="stage-title"]').text()).toContain('训练集执行')
    expect(wrapper.text()).toContain('case_a')
    expect(wrapper.text()).toContain('1 / 1 完成')

    // 训练评分完成 → 趋势与分数更新；验证跳过 → 灰段
    emit(
      progress(8, 'stage_completed', {
        candidate: 'candidate-1',
        role: 'train',
        stage: 'evaluation',
        score: '0.750',
        cases: '8',
      }),
      progress(9, 'validation_skipped', {
        candidate: 'candidate-1',
        reason: 'below_best_train',
      }),
    )
    await flushPromises()
    const cand1After = wrapper.get('[data-candidate="candidate-1"]')
    expect(cand1After.text()).toContain('train 75.0%')
    expect(cand1After.findAll('.evo-stage')[4]!.classes()).toContain('skip')

    // 日志区包含事件消息
    expect(wrapper.get('[data-testid="log-box"]').text()).toContain('iteration_started')
    expect(wrapper.get('[data-testid="log-box"]').text()).toContain(
      'validation_skipped',
    )

    // run_completed → 终态视图
    emit(
      progress(10, 'run_completed', {
        best_candidate: 'candidate-0',
        validation_score: '0.600',
        stop_reason: 'iteration_limit',
      }),
    )
    await flushPromises()

    expect(wrapper.text()).toContain('每轮改进策略')
    expect(wrapper.text()).toContain('候选对比')
    expect(wrapper.text()).toContain('Train Case 通过矩阵')
    expect(wrapper.text()).toContain('BEST')
    // changelog 默认折叠，未拉取
    expect(client.candidateChangelog).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('修复 candidate-0 的 dispute_charge 误判')
    expect(wrapper.text()).toContain('点击展开查看改进策略')
    // 事件流已终止
    const signal = sse.options?.signal as AbortSignal
    expect(signal.aborted).toBe(true)
    wrapper.unmount()
  })

  it('lazy-loads changelog on expand and caches it', async () => {
    client.taskDetail.mockResolvedValue(
      detail({
        candidates: [
          candidate('candidate-0', { parent_id: null }),
          candidate('candidate-1', { validation: null }),
        ],
      }),
    )
    client.candidateChangelog.mockImplementation(
      (_taskId: string, candidateId: string) =>
        Promise.resolve(changelogOf(candidateId)),
    )
    const wrapper = await mountPage()

    // 默认折叠：只显示头部 + 摘要占位
    expect(wrapper.text()).toContain('点击展开查看改进策略')
    expect(client.candidateChangelog).not.toHaveBeenCalled()

    // 点击展开 → 按需拉取并渲染解析结果
    await wrapper.get('[data-candidate="candidate-0"] .cl-head').trigger('click')
    await flushPromises()
    expect(client.candidateChangelog).toHaveBeenCalledTimes(1)
    expect(client.candidateChangelog).toHaveBeenCalledWith('t1', 'candidate-0')
    const card = wrapper.get('[data-candidate="candidate-0"]')
    expect(card.text()).toContain('评测版本：candidate-0')
    expect(card.text()).toContain('修复 candidate-0 的 dispute_charge 误判')
    // 展开后摘要行替换为提取的摘要
    expect(card.text()).toContain('意图 修复 candidate-0 的 dispute_charge 误判')

    // 收起后卸载内容，再展开命中缓存不重复请求
    await wrapper.get('[data-candidate="candidate-0"] .cl-head').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-candidate="candidate-0"]').text()).not.toContain(
      '评测版本：candidate-0',
    )
    await wrapper.get('[data-candidate="candidate-0"] .cl-head').trigger('click')
    await flushPromises()
    expect(client.candidateChangelog).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-candidate="candidate-0"]').text()).toContain(
      '评测版本：candidate-0',
    )
    // 另一个候选独立拉取
    await wrapper.get('[data-candidate="candidate-1"] .cl-head').trigger('click')
    await flushPromises()
    expect(client.candidateChangelog).toHaveBeenCalledTimes(2)
    expect(client.candidateChangelog).toHaveBeenLastCalledWith('t1', 'candidate-1')
    wrapper.unmount()
  })

  it('shows loading state while fetching and expands all at once', async () => {
    client.taskDetail.mockResolvedValue(
      detail({
        candidates: [
          candidate('candidate-0', { parent_id: null }),
          candidate('candidate-1', { validation: null }),
        ],
      }),
    )
    let resolveFetch: (() => void) | null = null
    client.candidateChangelog.mockImplementation(
      (_taskId: string, candidateId: string) =>
        new Promise<string>((resolve) => {
          resolveFetch = () => resolve(changelogOf(candidateId))
        }),
    )
    const wrapper = await mountPage()

    await wrapper.get('[data-candidate="candidate-0"] .cl-head').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-candidate="candidate-0"]').text()).toContain(
      '正在加载 changelog',
    )
    resolveFetch!()
    await flushPromises()
    expect(wrapper.get('[data-candidate="candidate-0"]').text()).toContain(
      '评测版本：candidate-0',
    )

    // 全部展开：candidate-0 已缓存只拉 candidate-1
    const toggleAll = wrapper.findAll('button').find((b) => b.text() === '全部展开')
    await toggleAll!.trigger('click')
    await flushPromises()
    resolveFetch!()
    await flushPromises()
    expect(client.candidateChangelog).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('评测版本：candidate-1')

    // 全部收起
    const collapseAll = wrapper.findAll('button').find((b) => b.text() === '全部收起')
    await collapseAll!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('评测版本：candidate-0')
    expect(wrapper.text()).not.toContain('评测版本：candidate-1')
    wrapper.unmount()
  })

  it('renders final view directly for a finished task', async () => {
    client.taskDetail.mockResolvedValue(
      detail({
        candidates: [
          candidate('candidate-0', { parent_id: null }),
          candidate('candidate-1', { validation: null }),
        ],
      }),
    )
    const wrapper = await mountPage()

    expect(client.openRunEvents).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('每轮改进策略')
    expect(wrapper.text()).toContain('VAL 跳过')
    expect(wrapper.text()).toContain('62.5%')
    // 热力图：case_a 在两列均有数据
    expect(wrapper.findAll('.evo-heat .hc').length).toBeGreaterThan(0)
    wrapper.unmount()
  })
})
