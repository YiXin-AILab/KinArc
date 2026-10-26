import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import EvolutionListPage from '../EvolutionListPage.vue'

const { client } = vi.hoisted(() => ({
  client: {
    listTasks: vi.fn(),
    listTemplates: vi.fn(),
    createTask: vi.fn(),
    taskDetail: vi.fn(),
    startRun: vi.fn(),
    stopRun: vi.fn(),
    getRun: vi.fn(),
    openRunEvents: vi.fn(),
  },
}))

vi.mock('../evolutionClient', () => ({ defaultEvolutionClient: () => client }))

function task(partial: Record<string, unknown>) {
  return {
    task_id: 'tau3_banking_t29v19',
    objective: 'Improve the Banking Agent success rate on fixed Tau3 cases.',
    initial_target: 'application/banking_agent',
    max_iterations: 5,
    candidate_count: 0,
    best_validation_score: null,
    best_candidate_id: null,
    total_tokens: 0,
    valid: true,
    error: null,
    active_run_id: null,
    ...partial,
  }
}

async function mountPage() {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/evolution')
  await router.isReady()
  const wrapper = mount(EvolutionListPage, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

describe('EvolutionListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders task cards with status badges and metrics', async () => {
    client.listTasks.mockResolvedValue([
      task({ active_run_id: 'run-1' }),
      task({
        task_id: 'done_task',
        candidate_count: 4,
        best_validation_score: 0.8,
        best_candidate_id: 'candidate-3',
        total_tokens: 1842300,
      }),
      task({ task_id: 'fresh_task' }),
      task({ task_id: 'broken_task', error: 'config.yaml 解析失败' }),
    ])
    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('任务总数')
    expect(wrapper.text()).toContain('RUNNING')
    expect(wrapper.text()).toContain('COMPLETED')
    expect(wrapper.text()).toContain('无运行')
    expect(wrapper.text()).toContain('80.0%')
    expect(wrapper.text()).toContain('4 / 5')
    expect(wrapper.text()).toContain('config.yaml 解析失败')
    expect(wrapper.text()).toContain('best: candidate-3')
    wrapper.unmount()
  })

  it('shows empty state when no tasks exist', async () => {
    client.listTasks.mockResolvedValue([])
    const wrapper = await mountPage()

    expect(wrapper.text()).toContain('暂无自进化任务')
    wrapper.unmount()
  })

  it('shows error state and retries', async () => {
    client.listTasks.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce([])
    const wrapper = await mountPage()
    expect(wrapper.get('[role="alert"]').text()).toContain('boom')

    await wrapper.get('.platform-state .platform-btn').trigger('click')
    await flushPromises()
    expect(client.listTasks).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('navigates to detail when clicking a task card', async () => {
    client.listTasks.mockResolvedValue([task({ task_id: 'my_task' })])
    const wrapper = await mountPage()

    await wrapper.get('.evo-task-card').trigger('click')
    await flushPromises()
    expect(wrapper.vm.$route.path).toBe('/platform/evolution/my_task')
    wrapper.unmount()
  })
})
