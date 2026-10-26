import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createAppRouter } from '../../router'
import EvolutionCreatePage from '../EvolutionCreatePage.vue'

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

function template(partial: Record<string, unknown>) {
  return {
    task_id: 'tau3_banking',
    objective: 'Improve the Banking Agent success rate.',
    initial_target: 'application/banking_agent',
    dataset_case_counts: { train: 8, validation: 5, test: 12 },
    ...partial,
  }
}

async function mountPage() {
  const router = createAppRouter(createMemoryHistory())
  await router.push('/platform/evolution/new')
  await router.isReady()
  const wrapper = mount(EvolutionCreatePage, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('EvolutionCreatePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    client.listTemplates.mockResolvedValue([template({})])
  })

  it('loads templates with dataset case counts and prefill placeholders', async () => {
    const { wrapper } = await mountPage()

    expect(wrapper.text()).toContain('tau3_banking')
    expect(wrapper.text()).toContain('train=8')
    expect(wrapper.text()).toContain('validation=5')
    const target = wrapper.get('input[placeholder*="application"]')
    expect((target.element as HTMLInputElement).value).toBe('application/banking_agent')
    wrapper.unmount()
  })

  it('blocks submit with invalid task_id and never calls the API', async () => {
    const { wrapper } = await mountPage()

    await wrapper.get('input[placeholder*="tau3_banking_t"]').setValue('BAD ID')
    const launch = wrapper
      .findAll('button')
      .find((b) => b.text().includes('创建并启动'))
    await launch!.trigger('click')
    await flushPromises()

    expect(client.createTask).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text()).toContain('^[a-z]')
    wrapper.unmount()
  })

  it('creates and starts a run, then navigates to detail', async () => {
    client.createTask.mockResolvedValue(template({ task_id: 'my_task' }))
    client.startRun.mockResolvedValue({ run_id: 'run-1', task_id: 'my_task' })
    const { wrapper, router } = await mountPage()

    await wrapper.get('input[placeholder*="tau3_banking_t"]').setValue('my_task')
    await wrapper.get('input[min="0"][type="number"]').setValue('6')
    // settings 键值对
    const addRow = wrapper
      .findAll('button')
      .find((b) => b.text().includes('添加键值对'))
    await addRow!.trigger('click')
    await wrapper.get('input[placeholder="key"]').setValue('case_timeout_seconds')
    await wrapper.get('input[placeholder="value"]').setValue('1200')

    const launch = wrapper
      .findAll('button')
      .find((b) => b.text().includes('创建并启动'))
    await launch!.trigger('click')
    await flushPromises()

    expect(client.createTask).toHaveBeenCalledTimes(1)
    const payload = client.createTask.mock.calls[0]![0]
    expect(payload.template_id).toBe('tau3_banking')
    expect(payload.task_id).toBe('my_task')
    expect(payload.max_iterations).toBe(6)
    expect(payload.settings).toEqual({ case_timeout_seconds: 1200 })
    expect(client.startRun).toHaveBeenCalledWith('my_task')
    expect(router.currentRoute.value.path).toBe('/platform/evolution/my_task')
    wrapper.unmount()
  })

  it('shows backend error message on conflict', async () => {
    client.createTask.mockRejectedValue(new Error('任务 my_task 已存在'))
    const { wrapper, router } = await mountPage()

    await wrapper.get('input[placeholder*="tau3_banking_t"]').setValue('my_task')
    const create = wrapper.findAll('button').find((b) => b.text() === '创建')
    await create!.trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('任务 my_task 已存在')
    expect(router.currentRoute.value.path).toBe('/platform/evolution/new')
    wrapper.unmount()
  })

  it('renders a live yaml preview of the payload', async () => {
    const { wrapper } = await mountPage()

    await wrapper.get('input[placeholder*="tau3_banking_t"]').setValue('yaml_task')
    const preview = wrapper.get('[data-testid="yaml-preview"]')
    expect(preview.text()).toContain('task_id: yaml_task')
    expect(preview.text()).toContain('template_id: tau3_banking')
    wrapper.unmount()
  })
})
